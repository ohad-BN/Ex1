# Technical-Related Tools Report

**Document ID:** `Ex1-TTR-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 (To-Do #3)  
**Author:** Ohad Ben Nun

> **Verification Transparency Note:** Per Ex2 To-Do #3 (*"Perform a technical-related tools report - make sure to test (in real life) with any suggested tool"*), this document separates **Verified Technical Capabilities & API Constraints** (established from official specifications and architectural research during design) from the **Real-Life Hands-On Benchmark & Validation Protocol** scheduled for Milestone M2 once the Dockerized prototype and sample classroom assets are assembled. No unexecuted benchmark numbers are claimed as measured output.

---

## 1. Evaluation Methodology

Every tool, library, and external API proposed for **ClassPulse** is evaluated against five project constraints:
1. **Docker & Local PC Portability:** Must run cleanly on a Windows/Linux/macOS host inside Docker on CPU (no mandatory discrete GPU).
2. **Zero-Cost / Free-Tier Viability:** Must work reliably during live classroom demos and grading without paid subscriptions.
3. **Python 3.12 Compatibility:** Must avoid deprecated concurrency hacks or fragile native build failures.
4. **Classroom Scale Readiness:** Must support 30–150 concurrent student smartphones over mobile cellular/campus Wi-Fi through a tunnel.
5. **KISS Operability:** Must minimize setup steps for both the lecturer and smartphone students.

---

## 2. Infrastructure: Public Tunneling & Containerization

### 2.1 Candidate Comparison: `cloudflared` vs `ngrok` (Free) vs Tailscale Funnel

| Criterion | Cloudflare Tunnel (`cloudflared`) | `ngrok` (Free Tier) | Direct Port Forwarding |
| :--- | :--- | :--- | :--- |
| **Browser Interstitial Warning** | **None.** Direct HTTPS pass-through to QR target URL. | **Yes.** Shows an ngrok abuse-warning interstitial page on first mobile browser load, disrupting rapid QR check-in. | None (if SSL configured). |
| **HTTPS / WSS (WebSockets)** | **Automatic TLS + native WebSockets** out of the box. | Automatic TLS + WebSockets. | Manual Let's Encrypt + router NAT. |
| **URL Stability** | Quick Tunnel (`*.trycloudflare.com`) = zero account, ephemeral URL per restart; Named Tunnel = **permanent free URL** with user's domain. | Free static domain (1 per account) or ephemeral URL; strict bandwidth/request caps. | Depends on home ISP dynamic IP. |
| **Docker Compose Sidecar** | Official `cloudflare/cloudflared:latest` container on shared bridge network. | Official `ngrok/ngrok:latest` container. | N/A |
| **Campus / Home Firewall Safety** | Outbound-only connection; zero open inbound router ports. | Outbound-only connection. | Exposes host router ports directly (blocked on university Wi-Fi!). |

* **Decision:** **Cloudflare Tunnel (`cloudflared`)** inside `docker-compose.yml` is selected as primary (`cloudflared tunnel --no-autoupdate --url http://web:5000` for zero-config Quick Tunnel, or `TUNNEL_TOKEN` for persistent Named Tunnel). `ngrok` is retained only as a documented local debugging fallback.
* **Critical Configuration Note:** Flask/Socket.IO must inspect `ProxyFix` (`X-Forwarded-Proto`, `X-Forwarded-Host`) and allow the tunnel origin (`cors_allowed_origins`) so Socket.IO WebSocket upgrades (`wss://`) do not fail with `403` origin mismatches.

### 2.2 Persistence: SQLite (WAL Mode) vs PostgreSQL in Docker

* **Evaluation:** For a classroom of 30–150 students, peak write bursts occur during 10-second QR check-in windows and live poll answers (~5–30 writes/sec).
* **Decision:** **SQLite 3 in Write-Ahead Logging (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`)** accessed via **SQLAlchemy 2.0** on a named Docker volume (`/app/data/classpulse.db`).
  * *Why:* Zero extra RAM/container overhead, trivial single-file backup before a demo, concurrent reads do not block writes, and SQLAlchemy preserves a 1-line migration path to PostgreSQL (`DATABASE_URL`) if scaled multi-classroom later.

---

## 3. Web Framework & Real-Time Engine (`Flask-SocketIO`)

### 3.1 Python 3.12 Async Mode Evaluation (`eventlet` vs `gevent` vs `simple-websocket`)

| Async Driver | Python 3.12 Status | Monkey-Patching | Compatibility with `onnxruntime` / `google-genai` / `faster-whisper` | Verdict |
| :--- | :--- | :--- | :--- | :--- |
| **`eventlet`** | **Broken / Unmaintained** (relies on removed stdlib internals in 3.12+). | Deep intrusive patching. | Frequent SSL/greenlet crashes. | **Rejected** |
| **`gevent` + `gevent-websocket`** | Good via modern `greenlet`. | Required (`gevent.monkey.patch_all()`). | Occasional conflicts with modern C-extension HTTP/gRPC SDKs (`google-genai`). | Fallback |
| **`simple-websocket` + Gunicorn `gthread`** | **Native / First-Class** | **None required.** | **100% standard OS thread safety** with ONNX Runtime, Whisper, OpenCV, SQLite, and Gemini SDK. | **Selected** |

* **Decision:** **Flask + Flask-SocketIO + `simple-websocket`** served by **Gunicorn with `-k gthread -w 1 --threads 100`**.
  * *Why single worker (`-w 1`) with 100 threads:* Keeps Socket.IO room state in a single process (no Redis broker container strictly required for KISS demo), while 100 threads easily serve 100 simultaneous student phones, and **heavy CPU tasks** (SCRFD face counting, `faster-whisper` transcription, LLM calls) are offloaded to a bounded `ThreadPoolExecutor` / background worker so WebSocket heartbeats never block!

---

## 4. Feature 1 (Top Priority): Face / Head-Count Detection Models

The target classroom photo (Ex2 PDF, Page 3) is a wide-angle lecture-hall shot from the lectern containing large foreground faces (~80–120 px) down to tiny, partially occluded back-row faces (~12–25 px) under indoor fluorescent lighting.

### 4.1 Model Comparison for Classroom Crowd Head-Counting

| Model / Library | Architecture | Tiny/Back-Row Face Recall (WIDER FACE Hard) | CPU Speed inside Docker | Docker Footprint & Dependencies | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **OpenCV Haar Cascade** | Viola-Jones sliding window | Very Low (~20–30%); high false positives on desks/posters; misses tilted faces. | Very Fast | Tiny (`opencv-python-headless`) | **Rejected** (unusable for real classrooms) |
| **MediaPipe Face Detection** | BlazeFace SSD | Low–Moderate beyond 3–5 meters; designed for close-up selfies/video calls. | Very Fast | Small | **Rejected** (misses back rows) |
| **RetinaFace (`ResNet-50`)** | Single-stage FPN + context | Very High (~91–93%+); gold-standard recall on small faces. | Slow on CPU for 4K images without GPU. | Heavy (`torch` / TensorFlow ~1.5–3 GB image). | Fallback if accuracy > speed |
| **InsightFace SCRFD (`det_10g` / `buffalo_l` detector)** | NAS-optimized multi-stride anchor detector | **State-of-the-art (~93–95% on high tiers)** across tiny and tilted faces; outputs 5-pt landmarks. | **Fast on CPU via `onnxruntime`** | **Moderate (~400–600 MB with `onnxruntime`, zero PyTorch needed!)** | **Selected Primary** |

### 4.2 Tiled Inference Enhancement (Engineering Innovation)
* Even strong detectors lose tiny 15px faces when a 4000×3000 smartphone photo is downscaled to `640x640` input size.
* **Proposed Strategy:** Run **SCRFD (`det_10g` at `det_size=(640, 640)`)** on:
  1. The full resized image (captures large front-row faces that span tile seams), **plus**
  2. A **2×2 or 3×2 grid of overlapping image crops** (20% overlap) mapped back into global pixel coordinates, merged with **IoU Non-Maximum Suppression (NMS, threshold `0.40`)**.
* **Human-in-the-Loop Safeguard:** Return the annotated image with numbered bounding boxes so the lecturer can visually confirm the count and click to toggle false positives or missed occluded students before exporting the Excel report.

---

## 5. Features 2 & 4: Why Native Web Chat & Direct Upload (No Zoom API Dependency)

External Zoom APIs were intentionally **excluded** from ClassPulse:
* University/institutional Zoom meetings cannot be queried or downloaded using external/student Zoom credentials due to Zoom tenant isolation (`404 Meeting does not exist`).
* Per Ex2 guidelines (*"No need integrate with Moodle, Zoom or מידע אישי"*), keeping **Feature 2** as a pure Socket.IO Classroom Chat + Live Popout feed and **Feature 4** as direct class recording upload eliminates all third-party OAuth / tenant-permission blockers.

---

## 6. Features 4 & 5: Google Gemini Audio Transcription & AI Studio API

### 6.1 Transcription & Summary Pipeline (Feature 4)
* **Primary Pipeline (Google Gemini API):** ClassPulse accepts uploaded recordings (`.mp4`, `.m4a`, `.mp3`, `.wav`, `.ogg`, `.webm`) -> extracts compact 64kbps mono `.mp3` via `ffmpeg` when needed -> uploads via **Google Gemini Files API (`client.files.upload`)** -> prompts `gemini-3.8-flash` for `[MM:SS]` timestamped transcription + Pass-1 CS/OOP correction -> prompts Gemini with the transcript + live Class Chat timeline (`+MM:SS`) to generate the lecturer's downloadable `.md` summary & `.txt` transcript.
* **Offline Fallback:** If `GEMINI_API_KEY` is unset or offline, ClassPulse falls back to local `ffmpeg` (`16kHz` mono `.wav`) + `faster-whisper` (`base` INT8) so the feature never breaks without internet access.

### 6.2 Google AI Studio (`google-genai` SDK) Constraints
* **Rate Limits & Model Lifecycle:** Enforced per Google Cloud Project (with `GEMINI_MIN_INTERVAL_SEC=4.5` pacing and exponential backoff on `429 RESOURCE_EXHAUSTED` / `503`). `GeminiStudioClient` includes an automatic `404 NOT_FOUND` model fallback chain (`gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-3.5-flash`) when older model IDs are retired.
* **Privacy Trade-off:** Free-tier Gemini API inputs may be used by Google to improve products; therefore, **zero PII** (student national IDs, emails, or peer identities) shall be included in LLM prompts—only anonymized submission content and aggregated statistical JSON!
* **Structured Outputs (Feature 5):** The `google-genai` SDK natively accepts Pydantic models via `response_mime_type="application/json"` and `response_schema=HomeworkMetrics`, guaranteeing deterministic JSON extraction for the Incremental Aggregation database engine without brittle regex parsing.

---

## 7. Real-Life Hands-On Verification Protocol (Scheduled for Milestone M2)

To strictly satisfy Ex2 To-Do #3 (*"make sure to test (in real life) with any suggested tool"*), the following real-world validation suite will be executed and recorded in `reports/tool_verification_results.md` during Milestone M2 before feature lock:

| Test ID | Tool Under Test | Real-Life Verification Procedure | Pass Criterion |
| :--- | :--- | :--- | :--- |
| **TV-01** | **InsightFace SCRFD + Tiling** | Run prototype detector against the actual Ariel lecture-hall photo (Ex2 PDF Page 3) and 3 custom multi-row classroom test photos; record detected count vs manual ground truth and CPU inference latency (seconds). | Recall $\ge 85\%$ on visible non-occluded faces, precision $\ge 95\%$, latency $< 8\text{ s}$ on host CPU. |
| **TV-02** | **Cloudflare Tunnel + Docker** | Start Flask-SocketIO container + `cloudflared` container on Windows PC; scan rotating QR from 2 real smartphones over cellular (4G/5G) network (not local Wi-Fi). | TLS page load $< 1.5\text{ s}$, zero interstitial warnings, `wss://` Socket.IO stays connected $> 5\text{ min}$. |
| **TV-03** | **5-Sec Rolling QR Grace Window** | Scan rotating QR on projector screen at `t = 4.2s` (end of bucket) using iOS Safari & Android Chrome cameras. | Check-in succeeds via `N-1` grace bucket; re-scanning same QR on a second browser/incognito with same email or same device with different email is rejected. |
| **TV-04** | **`google-genai` + `ffmpeg` + `faster-whisper`** | Run end-to-end audio/transcript upload & summary generation (`gemini-3.8-flash` + offline `faster-whisper` fallback) and Pydantic `response_schema` extraction on a sample Ex0 repo. | Valid `[MM:SS]` transcript + summary output and `HomeworkMetrics` JSON on 5/5 runs; no unhandled `429` or `404` crashes under throttled queue. |

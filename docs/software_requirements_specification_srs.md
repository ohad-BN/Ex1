# Software Requirements Specification (SRS) — ClassPulse

**Document ID:** `Ex1-SRS-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 (To-Do #5)  
**Author:** Ohad Ben Nun

---

## 1. Introduction

### 1.1 Purpose
This Software Requirements Specification defines the complete functional, external interface, and non-functional requirements for **ClassPulse**, a Dockerized Python web application for live classroom attendance verification, student-lecturer real-time interaction, ML lecture summarization, and LLM-assisted comparative homework evaluation.

### 1.2 Scope
ClassPulse serves two primary user groups—**Lecturers** (operating a desktop web command center on the host PC or classroom podium) and **Students** (connecting via mobile browsers through an encrypted HTTPS tunnel)—centered on Feature 1 (**5-second rolling QR attendance + computer-vision classroom headcount reconciliation**) as the mission-critical core, supplemented by Features 2–5.

### 1.3 Definitions & Acronyms
* **HMAC-TOTP Token:** Time-stepped cryptographic signature computed via `HMAC-SHA256(secret, session_id + ":" + floor(unix_ts / 5))`.
* **Grace Window:** Acceptance of both current time bucket $t$ and immediate previous bucket $t-1$ (effective validity: $5\text{–}10\text{ s}$).
* **SCRFD:** *Sample and Computation Redistribution for Efficient Face Detection* (InsightFace ONNX detector).
* **SAHI / Tiling:** Overlapping spatial grid cropping applied to high-resolution photos to detect small, distant faces, merged via Non-Maximum Suppression (NMS).
* **Incremental Aggregation:** Architectural pattern computing compact structured JSON metrics per homework submission once and updating cohort statistics mathematically in $O(1)$ without re-evaluating peer submissions.
* **WAL:** SQLite Write-Ahead Logging mode for concurrent read/write stability.

---

## 2. Overall Description

### 2.1 Product Perspective
ClassPulse is a self-contained Docker Compose stack running directly on the lecturer's PC, eliminating third-party SaaS lock-in, university portal/Zoom tenant API dependencies, or cloud server hosting fees while integrating cleanly with Google AI Studio (`GEMINI_API_KEY`) for audio transcription, summarization, and homework evaluation.

### 2.2 User Classes
1. **Lecturer (Administrator / Host):** Creates class sessions, imports optional sanitized rosters (`Name, Email`), projects the 5-second rotating QR code, captures/uploads classroom photos, reviews CV headcounts, exports attendance spreadsheets, manages live/pre-prepared polls, moderates live chat, uploads lecture recordings for ML summarization, and configures homework rubrics.
2. **First-Time Student:** Scans the classroom QR code with a smartphone camera, completes one-time onboarding (`Full Name`, `Email`), receives a signed persistent device cookie, and checks in.
3. **Returning Student:** Scans subsequent QR codes (or rejoins via tunnel link), is automatically recognized by signed device cookie (with quick confirmation), participates in live polls and chat, and submits/reviews homework online.

### 2.3 Operating Environment
* **Server Host:** Windows 10/11, Linux, or macOS running Docker Desktop / Docker Engine with Docker Compose v2+.
* **Application Runtime:** Python 3.12, Flask, Flask-SocketIO (`simple-websocket`), Gunicorn (`gthread`, 1 worker, 100 threads), SQLite 3 (WAL mode), ONNX Runtime (CPU), FFmpeg, `faster-whisper`.
* **Network Ingress:** Cloudflare Tunnel (`cloudflare/cloudflared`) exposing local container port `5000` over HTTPS (`https://*.trycloudflare.com` or custom Cloudflare domain) and WSS.
* **Client Browsers:** Modern iOS Safari (16+), Android Chrome (110+), and Desktop Chrome/Edge/Firefox.

### 2.4 Constraints & Assumptions
* **Constraint 1 (Privacy / Guideline 1):** The system shall never collect, store, or process national ID numbers (`תעודת זהות`) or Moodle/university portal passwords.
* **Constraint 2 (Zero External Zoom/Moodle Dependencies):** Core chat and recording workflows shall work 100% natively without requiring Zoom OAuth/Cloud credentials, Meeting SDK bots, or Moodle integration.
* **Constraint 3 (CPU Execution):** Face counting and offline audio fallback must execute without requiring an NVIDIA CUDA GPU.
* **Assumption:** The host PC maintains outbound internet access during lectures for the Cloudflare tunnel and Google AI Studio API calls (`GEMINI_API_KEY`).

---

## 3. Specific Requirements

### 3.1 External Interfaces

#### 3.1.1 User Interfaces
* **Student Mobile Web UI (`/s/<session_code>`):** Mobile-first responsive interface (touch targets $\ge 44\text{ px}$) with tabs for Check-In Status, Live Chat, Live Polls, Lecture Summaries, and Homework Deep Review.
* **Lecturer Portal (`/teacher` & `/lecturer`):** Desktop-optimized dashboard featuring Live Projector QR View (`Fullscreen` toggle + 5s countdown ring), Classroom Photo Upload & Interactive Bounding-Box Editor, Attendance Audit Table & `.xlsx`/`.csv` Export, Poll Bank Manager & Live Histogram, Chat Moderation / Live Q&A Popout, AI Recording Upload & Summary Queue, and Homework Cohort Dashboard.

#### 3.1.2 REST API & WebSocket Interfaces
* **REST Endpoints:**
  * `POST /api/attendance/checkin` — Validates rotating QR HMAC token, performs first-time onboarding if needed, binds device cookie, records timestamped check-in.
  * `POST /api/sessions/<id>/headcount` — Accepts classroom image upload, triggers tiled SCRFD face counting, returns count + annotated image + bounding-box JSON.
  * `PATCH /api/sessions/<id>/headcount` — Saves lecturer manual bounding-box adjustments and audit notes.
  * `GET /api/sessions/<id>/report?format=xlsx|csv` — Downloads the cross-referenced attendance & discrepancy report.
  * `GET/POST /api/polls`, `POST /api/polls/<id>/launch`, `POST /api/polls/<id>/close` — Pre-class poll bank CRUD and live poll lifecycle transitions.
  * `POST /api/sessions/<id>/lectures/summarize` — Accepts `.mp4`/`.m4a`/`.mp3`/`.wav`/`.vtt`/`.txt` upload (sync or `?async=1`), transcribes via Google Gemini API (`faster-whisper` offline fallback), and runs Gemini summary generation.
  * `GET /api/lectures/<id>/download/summary.md` & `/download/transcript.txt` — Downloads the lecturer summary Markdown and timestamped `[MM:SS]` transcript.
  * `POST /api/assignments/<id>/submit` & `POST /api/submissions/<id>/deep-review` — Handles file/GitHub submission, Stage-1 JSON extraction, cohort math update, and Stage-2 comparative review generation.
* **Socket.IO Real-Time Events (`wss://`):**
  * `join_session`, `qr:rotate` (5s QR payload refresh to projector screen), `attendance:count_update`
  * `chat:send`, `chat:new`, `chat:upvote`, `chat:pin`
  * `poll:sync` (reconnect hydration), `poll:activated`, `poll:vote`, `poll:results_update`, `poll:closed`

#### 3.1.3 External Service Interfaces
* **Cloudflare Tunnel (`cloudflared`):** Terminates HTTPS/WSS edge traffic and forwards `X-Forwarded-Proto` / `X-Forwarded-Host` headers to Gunicorn.
* **Google AI Studio (`google-genai` SDK):** Calls Gemini (`gemini-3.8-flash` with `404` auto-fallback) via HTTPS using `GEMINI_API_KEY` for audio Files API transcription (`files.upload`), lecture summarization, and Pydantic `response_schema` homework analysis.

---

### 3.2 Functional Requirements

#### Platform, Auth & Infrastructure
* **REQ-01 (Dockerized Local Hosting):** The system shall launch via a single `docker compose up` command starting the Python web server and the reverse tunnel container, persisting database and uploaded files on host-mounted volumes.
* **REQ-02 (Dynamic Public Base URL):** The QR generator shall construct check-in URLs dynamically from the active tunnel hostname (`PUBLIC_BASE_URL` or `X-Forwarded-Host`), allowing seamless transitions between Quick Tunnels and Named Tunnels.
* **REQ-03 (Lecturer Access Control):** Lecturer endpoints (`/lecturer/*`, poll launch, headcount upload, spreadsheet export, assignment rubric config) shall require lecturer authentication via configurable admin passphrase/cookie session.

#### Feature 1: Rolling QR Attendance & CV Headcount (Top Priority — P0)
* **REQ-04 (5-Second Rotating QR Code):** The projector view shall generate and display a fresh QR code every $5.0\text{ s}$ embedding `session_code`, `time_bucket` ($\lfloor t / 5 \rfloor$), and `HMAC-SHA256` token signature alongside a live visual countdown indicator.
* **REQ-05 (Grace-Window Token Validation):** The backend shall accept check-in tokens matching either the current bucket $t$ or immediate previous bucket $t-1$ ($5\text{–}10\text{ s}$ window) and reject tokens with invalid signatures or older buckets (`410 Gone` / expired prompt to re-scan).
* **REQ-06 (First-Time Student Onboarding):** On first check-in from an unrecognized browser/email, the system shall prompt for `Full Name` and `Email`, validate email syntax, persist a `Student` record in SQLite, and issue an `HttpOnly`, `SameSite=Lax` signed `device_token` cookie.
* **REQ-07 (Returning Student Fast Check-In & Anti-Proxy Binding):** Returning students presenting a valid `device_token` cookie shall be pre-identified without re-typing registration info. The system shall enforce uniqueness constraints per session for both `(session_id, student_id)` and `(session_id, device_id)` to block checking in multiple students from one phone.
* **REQ-08 (Optional Course Roster Import):** The lecturer shall be able to upload a CSV roster (`name,email`) so the attendance report distinguishes `Present`, `Absent (Registered)`, and `Present (Unlisted Guest)`.
* **REQ-09 (Classroom Photo Face Counting):** Upon uploading/capturing a classroom image (`.jpg`, `.png`, `.webp`), the vision service shall execute InsightFace SCRFD with overlapping multi-scale tiling and NMS to detect and count student faces.
* **REQ-10 (Interactive Visual Verification):** The system shall return an annotated image showing numbered bounding boxes and confidence scores; the lecturer shall be able to click to add missed faces or remove false detections, updating the verified visual headcount in real time.
* **REQ-11 (Cross-Reference Discrepancy Audit):** The system shall compute:
  $$\Delta = N_{\text{QR\_CheckIns}} - N_{\text{Visual\_Faces}}$$
  and classify session integrity status:
  * `VERIFIED` ($|\Delta| \le \tau$, default tolerance $\tau = 2$),
  * `UNDER_SCANNED` ($\Delta < -\tau$: more physical students than QR scans),
  * `DISCREPANCY_ALERT` ($\Delta > +\tau$: more QR scans than physical faces — potential proxy attendance).
* **REQ-12 (Online & Offline Spreadsheet Report):** The system shall display the full audit summary online and export an `.xlsx` workbook (with formatted *Summary* and *Student Roster* sheets) and `.csv` file containing session metadata, student names/emails, check-in timestamps, tardiness flag, visual headcount, delta, and discrepancy classification.

#### Feature 3: Real-Time Polls (`Flask-SocketIO` — P1)
* **REQ-13 (Pre-Class Poll Bank):** Lecturers shall be able to create, edit, delete, and organize polls (`question`, `2–6 options`, optional `time_limit_sec`, optional `correct_option_index`) inside a session poll bank before class begins.
* **REQ-14 (On-the-Spot KISS Polls):** Lecturers shall be able to launch a pre-saved poll or create a 1-click quick poll (`Yes/No`, `A/B/C/D`, or custom prompt) during live class.
* **REQ-15 (Real-Time Voting & Reconnect Hydration):** Active polls shall broadcast immediately via Socket.IO to all connected student clients. Newly joined or reconnected clients shall receive a `poll:sync` payload with current state and remaining timer. Duplicate votes per `(poll_id, student_id)` shall be blocked idempotently.

#### Feature 2: Timestamped Classroom Chat & Live Popout (P1)
* **REQ-16 (Live Timestamped Q&A / Comment Feed):** Students shall be able to post messages tagged as `Question`, `Comment`, or `Suggestion` with server-assigned lecture-relative timestamps (`+MM:SS` from lecture start) and upvote existing questions.
* **REQ-17 (Live Companion Popout & Timeline Export):** The system shall provide a compact floating **Live Q&A Companion Popout** (`/lecturer/chat-popout/<session_code>`) and export structured `+MM:SS` Q&A timelines (`.txt`) for Feature 4 lecture summaries.

#### Feature 4: AI Lecture Transcription & Summary (P2)
* **REQ-18 (Class Recording Upload):** Lecturers shall be able to upload class recordings (`.mp4`, `.m4a`, `.mp3`, `.wav`, `.ogg`, `.webm`) or caption/text files (`.vtt`, `.srt`, `.txt`) directly from the Teacher Portal (`?async=1` background job supported for large recordings).
* **REQ-19 (Google Gemini API Audio Transcription + Fallback):** When `GEMINI_API_KEY` is configured, the backend shall prep compact audio via `ffmpeg` when needed and send the recording to **Google Gemini API** (`client.files.upload`) to produce `[MM:SS]` timestamped segments; when offline, it shall fall back automatically to local `faster-whisper`.
* **REQ-20 (Gemini Lecturer Summary & Export):** Using `GeminiStudioClient`, the system shall refine CS/OOP terminology while preserving timestamps, synthesize a structured Markdown lecturer summary incorporating session chat Q&A highlights, and serve downloadable `.md` summary & `.txt` transcript files.

#### Feature 5: LLM Homework Deep Review & Cohort Comparison (P2)
* **REQ-21 (Submission Ingestion — Files & GitHub Repos):** Students (or batch lecturer import) shall submit homework via `.pdf`, `.py`, `.java`, `.md`, `.zip` archive, or public GitHub repository URL, automatically filtering binary/vendor paths (`.git/`, `node_modules/`, `venv/`).
* **REQ-22 (Stage-1 Single-Submission Feature Extraction):** An LLM call using strict Pydantic `response_schema` JSON output shall analyze **only** the submitted work against the assignment rubric, producing structured metrics (`overall_score`, `topic_scores`, `requirements_coverage`, `strengths`, `weaknesses`, `error_tags`, `style_notes`).
* **REQ-23 (Incremental O(1) Cohort Aggregation & Resubmission Delta):** Upon storing a student's `SubmissionMetrics` JSON, the backend shall mathematically update the assignment's `ClassStatsAggregate` record (running topic means, score histogram, percentile array, cumulative error tag counts) in $O(1)$ without additional LLM calls. On resubmission, prior metric contributions shall be subtracted before applying new metrics.
* **REQ-24 (Stage-2 Anonymized Deep Comparative Review):** On user request ("Generate Deep Review"), a second LLM prompt containing **only** the student's structured JSON and the anonymized `ClassStatsAggregate` JSON shall produce rich comparative feedback (percentile standing, comparative strengths/weaknesses, targeted study guidance) without ever revealing peer identities or peer code.

---

### 3.3 Non-Functional Requirements

* **NFR-01 (QR Check-In Throughput & Latency):** QR validation and attendance DB commit shall complete in $< 250\text{ ms}$ p95 latency at up to $100$ concurrent student check-ins within a 10-second burst.
* **NFR-02 (CV Headcount Latency & Accuracy):** Tiled SCRFD inference on a standard 1080p–4K classroom photo shall complete in $< 10\text{ s}$ on a modern 4+ core CPU without GPU and achieve $\ge 85\%$ recall on visible unoccluded faces.
* **NFR-03 (WebSocket Broadcast Latency):** Live poll state changes and chat messages shall propagate to connected tunnel clients in $< 500\text{ ms}$ p95.
* **NFR-04 (Security & Anti-Tampering):** QR tokens shall be cryptographically signed using HMAC-SHA256 with a server-side `SECRET_KEY`; cookies shall use `HttpOnly` + `Secure` (over HTTPS tunnel) + `SameSite=Lax`.
* **NFR-05 (Student Privacy & Anonymization):** No peer student names, emails, or raw source files shall ever be transmitted to another student's browser or injected into comparative LLM prompts for another student.
* **NFR-06 (LLM Rate-Limit Resilience):** External calls to Google AI Studio (`google-genai`) shall enforce configurable per-minute pacing and exponential backoff retries on HTTP `429` / `503` without corrupting DB state.
* **NFR-07 (Container Portability):** The entire runtime (system libraries, FFmpeg, ONNX models, Python dependencies, and tunnel sidecar) shall build cleanly via `docker compose build` and start via `docker compose up`.
* **NFR-08 (Availability / Non-Blocking Workers):** CPU-heavy tasks (headcount CV, Whisper transcription, LLM pipelines) shall never block the main HTTP/SocketIO event loop from serving live QR rotations, polls, or chat.

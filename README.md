# ClassPulse — Smart Classroom Engagement & Attendance Platform

**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2: *Vibe Coding & Deep Designing: Web-based Teaching Tool*  
**Institution:** Ariel University  
**Instructor:** Prof. Boaz Ben-Moshe  
**Author:** Ohad Ben Nun Einav Shlomov

---

## 1. Problem Statement & Motivation

Current university teaching workflows rely on fragmented tools (Zoom, Moodle, ad-hoc Google Forms, manual Excel sheets, and passive cloud recordings) that fail to provide simple, unified, real-time, and LLM-ready classroom engagement:

1. **Attendance Fraud & Friction:** Static sign-in sheets or static QR links allow students outside the classroom to mark attendance remotely ("proxy attendance"), while manual roll calls waste 10–15 minutes of lecture time. Furthermore, lecturers lack an automated way to verify self-reported check-ins against the actual physical headcount in the lecture hall.
2. **Disconnected Real-Time Interaction:** Students hesitate to interrupt lectures verbally, yet chat tools are fragmented across Zoom, WhatsApp, and live rooms, and launching quick comprehension checks takes too much setup overhead.
3. **Unstructured Post-Lecture Knowledge:** Automated video summaries (such as Zoom's built-in summary) often miss technical terminology, bilingual code/English-Hebrew nuance, and contextual questions asked during class.
4. **Shallow, Non-Comparative Homework Feedback:** Students receive isolated grades without understanding how their design, coverage, and error patterns compare anonymously against the rest of the cohort, while re-evaluating an entire class's raw code on every upload is prohibitively expensive in LLM tokens.

**ClassPulse** solves these gaps with a **KISS (Keep It Simple, Stupid), mobile-first Python web application** hosted directly on the lecturer's PC inside **Docker**, exposed safely over HTTPS to students' smartphones via an encrypted **reverse tunnel**, and engineered around **Feature 1 (Anti-Proxy Attendance & Computer-Vision Headcount Verification)** as the mission-critical core.

---

## 2. Project Scope & Priority Tiers

In alignment with the Ex2 design guidelines (*design what can be implemented, prioritize core attendance, and keep the student UX frictionless*), project scope is structured into three clear priority tiers:

| Priority | Module | Scope Summary | Status Target |
| :--- | :--- | :--- | :--- |
| **P0 (Core MVP)** | **Infrastructure & Tunnel** | Python 3.12 Flask + Flask-SocketIO backend, SQLite (WAL mode) persistence, Docker Compose deployment on host PC, and Cloudflare Tunnel (`cloudflared`) HTTPS/WSS ingress. | **Must-Have (Live Demo Core)** |
| **P0 (Core MVP)** | **Feature 1: Rotating QR Attendance & CV Headcount** | 5-second rolling HMAC-signed QR code, first-time student onboarding (Name + Email stored in DB + device cookie binding), classroom photo face-counting via InsightFace SCRFD + tiled inference, visual bounding-box review, cross-reference audit report, and `.xlsx`/`.csv` spreadsheet export. | **Must-Have (Top Priority)** |
| **P1 (Live Engagement)** | **Feature 3: Real-Time Polls (`Flask-SocketIO`)** | Simplified Kahoot-style live polling with **pre-class poll bank preparation**, instant on-the-spot quick polls, DB-backed state sync on mobile reconnect, and live histogram visualization. | **High-Value Demo Extension** |
| **P1 (Live Engagement)** | **Feature 2: Classroom Chat & Zoom Bridge** | Real-time timestamped Q&A/comment tab (`Flask-SocketIO`) with upvoting, optional Zoom Team Chat channel sync (via OAuth REST), and a compact Live Popout / Transcript-linked Q&A exporter (see [Section 4](#4-architectural-recommendations--better-ideas) for Zoom API constraints). | **High-Value Demo Extension** |
| **P2 (Post-Class AI)** | **Feature 4: ML Lecture Summarizer** | Dual ingestion (local Zoom/Meet `.mp4`/`.m4a`/`.vtt` upload + optional Zoom Cloud Recording connector for Pro/Edu hosts), local `faster-whisper` timestamped transcription, and 2-pass Google AI Studio (`gemini-2.5-flash`) transcript correction + structured lecture summary. | **Bonus AI Module** |
| **P2 (Post-Class AI)** | **Feature 5: LLM Homework Deep Review** | Supports file upload (`.pdf`/`.py`/`.java`/`.zip`) and GitHub repo URLs. Uses **Incremental Aggregation**: Stage-1 LLM extracts structured JSON metrics per submission; backend updates anonymized class-wide statistics in $O(1)$ math; Stage-2 LLM generates a deep comparative review against class averages/percentiles. | **Bonus AI Module** |

### Explicit Boundaries & Constraints
* **No Institutional Credential Scraping:** Per assignment Guideline 1, the system does **not** require scraping Moodle or university private student-ID databases (`מידע אישי`); students onboard with Name + Email on first scan, and an optional sanitized roster CSV (`Name, Email`) can be imported by the lecturer.
* **Host-First Portability:** Everything runs inside containers on the lecturer's Windows/macOS/Linux PC with zero cloud hosting fees and zero router port-forwarding required.

---

## 3. Baseline Technology Stack

| Layer | Selected Technology | Rationale |
| :--- | :--- | :--- |
| **Language & Runtime** | Python 3.12 | Fast prototyping, unified ecosystem for Web, Computer Vision, Audio ASR, and LLM SDKs. |
| **Web & Real-Time Engine** | Flask + Flask-SocketIO (`simple-websocket` + Gunicorn `gthread`) | Lightweight, avoids Python 3.12 `eventlet` deprecation and `gevent` monkey-patching conflicts with AI/CV C-extensions. |
| **Database** | SQLite (WAL mode) via SQLAlchemy 2.0 | Zero-config single-file persistence inside a Docker volume; easily handles 50–150 concurrent classroom users; swappable to PostgreSQL via connection string. |
| **Containerization** | Docker & Docker Compose | Reproducible local PC deployment packaging Python, ONNX Runtime, FFmpeg, and Tunnel sidecar. |
| **Public Tunnel** | Cloudflare Tunnel (`cloudflare/cloudflared`) | Provides free HTTPS/WSS (`*.trycloudflare.com` quick tunnel or fixed custom domain) with **no browser warning interstitial** that breaks QR scanning on mobile phones. |
| **Face / Head Counting (Feature 1)** | InsightFace **SCRFD** (`det_10g` ONNX) + Overlapping Tile Slicing + NMS | Industry-leading small/distant face recall in crowded lecture halls while running on CPU via `onnxruntime`. |
| **Reports & Spreadsheets** | `openpyxl` + Python `csv` | Generates styled offline `.xlsx` workbooks (Attendance Roster + Discrepancy Audit Summary) and `.csv` exports. |
| **ASR Transcription (Feature 4)** | `faster-whisper` (CTranslate2 INT8) | Free local CPU transcription with word/segment timestamps and zero API upload size limits. |
| **LLM Engine (Features 4 & 5)** | Google AI Studio (`google-genai` SDK, Gemini Flash / Pro free tier) | Native Pydantic `response_schema` structured JSON outputs for Feature 5 extraction and high-context summarization for Feature 4. |

---

## 4. Architectural Recommendations & "Better Ideas"

During design validation against real-world APIs and classroom constraints, several critical improvements were incorporated over the initial baseline ideas:

1. **Feature 1 — 5-Second QR Grace Window + Device Binding:**
   * *Problem:* Strict 5-second hard expiry causes false rejections when a student's phone camera takes 2–3 seconds to focus and complete a TLS handshake. Meanwhile, without device binding, one student could scan for three friends.
   * *Better Solution:* Compute HMAC-SHA256 tokens over 5-second discrete time buckets (`floor(ts / 5)`), accepting both bucket `N` and `N-1` (5–10s effective window), paired with an `HttpOnly` signed browser device cookie and unique `(session_id, email)` + `(session_id, device_id)` DB constraints.
2. **Feature 1 — Tiled SCRFD + Human-in-the-Loop Verification Overlay:**
   * *Problem:* Standard OpenCV Haar Cascades or MediaPipe BlazeFace miss tiny, partially occluded faces in rows 4–8 of wide-angle lecture-hall photos.
   * *Better Solution:* Run **InsightFace SCRFD** across overlapping 2x2/3x2 high-resolution tiles merged with Non-Maximum Suppression (NMS), then render numbered bounding boxes back to the lecturer UI so edge cases (e.g., hidden student behind a monitor) can be clicked/adjusted in seconds before exporting the audit spreadsheet.
3. **Feature 2 — Zoom Free API Reality Check (`Live Meeting Chat` vs `Team Chat`):**
   * *Verified Constraint:* Zoom's standard REST API **cannot** inject messages into an active live Zoom meeting's in-meeting chat box (Zoom's Chat REST API only posts to persistent Zoom Team Chat channels; live in-meeting injection requires running a headless Zoom Meeting SDK participant bot).
   * *Better Solution:* Implement a **Native Classroom Chat Tab (`Flask-SocketIO`)** as the reliable single source of truth (accessible to both in-person and remote students via the tunnel) behind a `ZoomBridgeAdapter` that supports: (a) optional Zoom Team Chat channel forwarding via OAuth, (b) a compact "Live Popout" window positioned beside Zoom, and (c) timestamped chat export fed directly into Feature 4's lecture summary.
4. **Feature 3 — Reconnect-Safe `Flask-SocketIO` State Hydration:**
   * *Problem:* Mobile phones lock screens and drop WebSocket connections over cellular/Wi-Fi networks.
   * *Better Solution:* Persist every poll (`DRAFT`, `ACTIVE`, `LOCKED`, `CLOSED`) and vote in SQLite. On WebSocket `connect`/`join`, the server pushes a `poll:sync` snapshot so late joiners and woken phones immediately see the live poll, timer, and existing vote status without duplicate voting.
5. **Feature 4 — Local Recording Upload First + `faster-whisper` + Gemini Correction:**
   * *Verified Constraint:* Zoom's Basic (Free) plan does **not** include Cloud Recording (`GET /meetings/{id}/recordings` returns empty).
   * *Better Solution:* Make **local recording upload (`.mp4`/`.m4a`/`.wav`/`.vtt`)** the primary frictionless path (working with Free Zoom, Google Meet, or classroom audio), with optional Zoom Cloud OAuth pull for University Pro/Edu hosts. Transcribe locally with `faster-whisper`, then use Google AI Studio free tier (`gemini-2.5-flash`) with rate-limit throttling for domain correction and summarization.
6. **Feature 5 — Incremental JSON Aggregation + GitHub Repo Ingestion:**
   * Adopting the two-stage **Incremental Aggregation** architecture avoids $O(N^2)$ token cost by computing compact Pydantic-validated JSON metrics once per submission, updating a single anonymized `ClassStatsAggregate` row mathematically in Python (including clean delta-subtraction on resubmissions), and supporting both file uploads (`.pdf`/`.zip`/code) and GitHub repo URLs (matching Ex2 Q&A #4).

---

## 5. Planned Project Structure

```text
Ex1/
├── README.md                                       # Problem definition, scope, priority tiers, architecture overview
├── Dockerfile                                      # Multi-stage Python 3.12 + FFmpeg + ONNX Runtime image
├── docker-compose.yml                              # Web app + cloudflared tunnel sidecar + persistent volumes
├── requirements.txt                                # Pinned Python dependencies
├── .env.example                                    # Environment template (SECRET_KEY, GEMINI_API_KEY, ZOOM_*, TUNNEL_TOKEN)
│
├── docs/                                           # Software Engineering Documentation Suite (Ex2 To-Do #3–#9)
│   ├── technical_tools_report.md                   # To-Do #3: Tools comparison, API limits & verification protocol
│   ├── scope_of_work_sow.md                        # To-Do #4: Scope of Work, priorities, milestones & risks
│   ├── software_requirements_specification_srs.md  # To-Do #5: Functional & Non-Functional Requirements (REQ/NFR)
│   ├── use_cases_client_stories.md                 # To-Do #6: Personas, Use Cases (UC-01..UC-10) & User Stories
│   ├── software_design_and_architecture.md         # To-Do #7 & #8: Components, Class Diagrams & Sequence Diagrams
│   ├── software_test_plan_stp.md                   # To-Do #9: Testing Strategy mapped to Software Classes
│   └── implementation_plan.md                      # Step-by-step component implementation roadmap (Phases 0–9)
│
├── app/                                            # (Upcoming M3) Modular Flask + Flask-SocketIO application
│   ├── __init__.py                                 # App factory, SQLAlchemy & SocketIO initialization
│   ├── config.py                                   # Environment-driven configuration & secrets
│   ├── models.py                                   # SQLAlchemy ORM models (Student, Session, Attendance, Poll, etc.)
│   ├── routes/                                     # REST & HTML Blueprints (auth, attendance, polls, chat, media, hw)
│   ├── sockets/                                    # Socket.IO event handlers (polls, live chat, attendance counter)
│   ├── services/                                   # Core domain services (QR/HMAC, SCRFD vision, Zoom, Whisper/Gemini, HW)
│   ├── templates/                                  # Mobile-first student & desktop lecturer Jinja2/HTMX/JS views
│   └── static/                                     # Client CSS/JS (Socket.IO client, QR projector, bbox editor)
│
├── tests/                                          # (Upcoming M3) Automated Pytest suites mapped to STP Suites 1–8
└── data/                                           # Persistent Docker volume mount (SQLite DB, uploads, reports)
```

---

## 6. Software Engineering Documentation Index

* **Technical-Related Tools Report (To-Do #3):** [`docs/technical_tools_report.md`](docs/technical_tools_report.md)
* **Scope of Work — SoW (To-Do #4):** [`docs/scope_of_work_sow.md`](docs/scope_of_work_sow.md)
* **Software Requirements Specification — SRS (To-Do #5):** [`docs/software_requirements_specification_srs.md`](docs/software_requirements_specification_srs.md)
* **Main Use Cases & Client Stories (To-Do #6):** [`docs/use_cases_client_stories.md`](docs/use_cases_client_stories.md)
* **Software Components, Low-Level Class & Sequence Diagrams (To-Do #7 & #8):** [`docs/software_design_and_architecture.md`](docs/software_design_and_architecture.md)
* **Software Testing Strategy & Class Traceability Matrix (To-Do #9):** [`docs/software_test_plan_stp.md`](docs/software_test_plan_stp.md)
* **Step-by-Step Implementation Plan (Phases 0–9):** [`docs/implementation_plan.md`](docs/implementation_plan.md)

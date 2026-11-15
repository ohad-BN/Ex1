# Scope of Work (SoW) — ClassPulse Web-Based Teaching Tool

**Document ID:** `Ex1-SOW-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 Assignment  
**Author:** Ohad Ben Nun  
**Target Demo Deadline:** 25/11/2026

---

## 1. Project Objectives

* **Context:** Academic Software Engineering design-and-demo assignment (*Ex2: Vibe Coding & Deep Designing: Web-based Teaching Tool*, Ariel University).
* **Primary Objective:** Design, document, containerize, and demonstrate an end-to-end, KISS-compliant Python web application running on the author's local PC via Docker and accessible over the public internet via an HTTPS tunnel.
* **Core Pedagogical Focus:** Replace fragmented classroom management tools with a unified platform centered on **tamper-resistant QR attendance and automated computer-vision classroom headcount reconciliation** (Feature 1), extended cleanly with real-time interaction (Features 2–3) and cost-efficient AI/LLM workflows (Features 4–5).

---

## 2. Priority Tiers & Feature Scope

To guarantee a rock-solid 10-minute live classroom demonstration while cleanly structuring advanced capabilities, deliverables are divided into strict priority tiers:

### Tier 0 — Mission-Critical Infrastructure & Feature 1 (Top Priority)
1. **Containerized Local Server & Tunnel:**
   * Python 3.12 web server packaged via `Dockerfile` + `docker-compose.yml`.
   * Reverse HTTPS/WSS tunnel (`cloudflared`) exposing the local server cleanly to students' mobile devices without router configuration or static IP requirements.
   * Persistent Docker volume storage for SQLite (WAL mode) database, uploaded classroom photos, and exported reports.
2. **Rotating QR Self-Attendance & Onboarding:**
   * Projector-ready QR code regenerating every **5 seconds** with HMAC-SHA256 signed time-bucket tokens and a 1-step grace window (5–10 seconds) for autofocus/network latency.
   * First-time student sign-in via Name + Email saved to DB, issuing a signed `HttpOnly` device cookie to recognize returning students and prevent single-device multi-student proxy check-ins.
3. **Computer-Vision Headcount & Cross-Reference Audit:**
   * Lecturer captures or uploads a classroom photo (desktop webcam, smartphone camera input, or image file).
   * Tiled **InsightFace SCRFD** detector counts faces across front and back lecture-hall rows.
   * Interactive bounding-box overlay allows the lecturer to verify/adjust counts if a student is occluded.
   * Cross-references `Course Roster`, `QR Check-ins`, and `Visual Face Count`, computing discrepancies and exporting a formatted `.xlsx` / `.csv` spreadsheet report.

### Tier 1 — Live Classroom Engagement (High-Value Extensions)
4. **Feature 3 — Real-Time Polling (`Flask-SocketIO`):**
   * Pre-class poll bank editor (create, reorder, save drafts ahead of the lecture).
   * On-the-spot KISS quick poll launcher (1-click Yes/No, A/B/C/D, or custom question).
   * Reconnect-safe real-time voting and live animated histogram updates over WebSockets.
5. **Feature 2 — Timestamped Classroom Chat & Zoom Bridge:**
   * Real-time student Q&A/comment feed (`Flask-SocketIO`) with timestamps, categories (Question / Comment / Suggestion), and peer upvotes.
   * `ZoomBridgeAdapter` supporting optional Zoom Team Chat channel forwarding via Zoom REST API, a compact "Zoom-Companion Popout" overlay, and structured timestamped Q&A export (explicitly accounting for Zoom REST API's inability to inject into live in-meeting chat).

### Tier 2 — Post-Class AI & Incremental Analytics (Bonus Extensions)
6. **Feature 4 — ML Lecture Summarizer:**
   * Accepts local Zoom/Meet video/audio uploads (`.mp4`/`.m4a`/`.wav`) or `.vtt` transcripts, plus optional Zoom Cloud Recording retrieval for licensed Pro/Edu accounts.
   * Local timestamped ASR transcription via `faster-whisper`, followed by two-pass Google AI Studio (`gemini-2.5-flash` free tier) domain-term correction and structured summary generation.
7. **Feature 5 — LLM Homework Deep Review & Cohort Comparison:**
   * Accepts uploaded submissions (`.pdf`/`.py`/`.java`/`.zip`) or GitHub repository URLs.
   * Implements **Incremental Aggregation**: Stage-1 LLM extracts structured Pydantic JSON metrics per single submission -> Python backend updates anonymized `ClassStatsAggregate` in $O(1)$ time -> Stage-2 LLM synthesizes personalized comparative feedback against cohort averages and common mistakes without ever exposing peer source code or identities.

---

## 3. Out-of-Scope Boundaries

To comply with Ex2 Guidelines (*"No need integrate with Moodle, Zoom or מידע אישי"* and *"You should only design what can be implemented"*), the following are explicitly **excluded**:
* **Moodle / University SSO / Personal ID (`תעודת זהות`):** No scraping or storing national ID numbers or university portal credentials. Students identify via Name and Email only.
* **Heavyweight Headless Zoom Meeting Bots:** Building, certifying, and running a Linux Zoom Meeting SDK participant bot just to inject live chat bubbles into Zoom's meeting overlay is excluded from the core KISS scope due to Zoom API restrictions and container complexity (documented in `docs/technical_tools_report.md`).
* **Biometric Identity Recognition (Face-to-Name Matching):** The CV pipeline performs **anonymous face/head counting** (`count = N`), NOT facial recognition identifying which face belongs to which student, avoiding GDPR/biometric privacy risks and cold-start photo enrollment friction.
* **Cloud Hosting / Kubernetes:** Deployment targets a single lecturer PC running Docker Compose + Tunnel.

---

## 4. Deliverables

| Category | Deliverable | File / Output |
| :--- | :--- | :--- |
| **Documentation** | Repository Problem & Scope Definition | `README.md` |
| **Documentation** | Technical Tools Report & Verification Protocol | `docs/technical_tools_report.md` |
| **Documentation** | Scope of Work (this document) | `docs/scope_of_work_sow.md` |
| **Documentation** | Software Requirements Specification | `docs/software_requirements_specification_srs.md` |
| **Documentation** | Use Cases & Client Stories | `docs/use_cases_client_stories.md` |
| **Documentation** | Component Architecture, Class & Sequence Diagrams | `docs/software_design_and_architecture.md` |
| **Documentation** | Software Test Plan (Class-Mapped) | `docs/software_test_plan_stp.md` |
| **Software** | Dockerized Python Web App + Tunnel Compose Stack | `Dockerfile`, `docker-compose.yml`, `app/` |
| **Verification** | Automated `pytest` Suite & Real-Life Tool Verification Results | `tests/`, `reports/` |
| **Evaluation** | 10-Minute Live Classroom Demo & Final Results Summary | Live classroom execution + closing conclusion |

---

## 5. Project Milestones

| Milestone | Phase | Key Activities | Target Completion |
| :--- | :--- | :--- | :--- |
| **M1** | **Design & Documentation (Current)** | Finalize `README.md`, Tools Report, SoW, SRS, Use Cases, Component/Class/Sequence Diagrams, and Class-Mapped STP. | Nov 12, 2026 |
| **M2** | **Tool Verification & Core MVP** | Execute hands-on tool tests (SCRFD on Ariel lecture photo, `cloudflared` tunnel, `flask-socketio` + `gthread`). Implement Docker stack, DB models, Rotating QR + Device Auth, CV Headcount + Excel Export (Feature 1). | Nov 17, 2026 |
| **M3** | **Engagement & AI Modules** | Implement Pre-prepared & Quick Polls (Feature 3), Realtime Chat + Zoom Bridge (Feature 2), `faster-whisper` + Gemini Summarizer (Feature 4), and Incremental Homework Review (Feature 5). | Nov 22, 2026 |
| **M4** | **Testing, Rehearsal & Demo** | Run full STP automated suite (`pytest`), verify live mobile tunnel stress test, finalize demo script & results report, submit before deadline. | Nov 25, 2026 |

---

## 6. Key Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation Strategy |
| :--- | :--- | :--- | :--- |
| **Distant/occluded faces missed in wide classroom photos** | High | High | Use InsightFace SCRFD (`det_10g`) with overlapping tile slicing + NMS, plus interactive manual +/- click adjustment on the lecturer preview canvas. |
| **5-second QR timeout frustrates students with slow cameras** | Medium | High | Accept both current (`t`) and immediate previous (`t-1`) 5-second HMAC buckets (5–10s effective grace window) while keeping visual rotation every 5s. |
| **Zoom Free API lacks live meeting chat write & cloud recordings** | Certain | Medium | Explicitly decouple core chat & audio ingestion from Zoom Pro/SDK dependencies; use native Socket.IO chat + local `.mp4`/`.m4a` upload as default, with optional Zoom OAuth adapters. |
| **Google AI Studio Free Tier `429 RESOURCE_EXHAUSTED` rate limits** | Medium | Medium | Chunk lecture transcripts cleanly, apply exponential backoff & token budgeting, and use single-call structured JSON extraction for Feature 5. |
| **Tunnel URL changes on container restart mid-demo** | Medium | Medium | Support persistent Named Cloudflare Tunnel via `TUNNEL_TOKEN` for fixed URL, or auto-generate the dynamic QR host URL from request headers (`X-Forwarded-Host`). |

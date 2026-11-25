# Scope of Work (SOW) — ClassPulse

**Course:** Introduction to Software Engineering (OOP / OOD) — Ariel University  
**Instructor:** Prof. Boaz Ben-Moshe  
**Authors:** Ohad Ben Nun & Einav Shlomov  
**Chain Role:** `1. SOW (Agree)` — *Why, what, and boundaries*

---

## 1. Objective

ClassPulse replaces fragmented classroom tools (manual attendance sheets, disconnected chats, cloud-only recordings, isolated homework grades) with a self-hosted, Dockerized Python web platform running on the lecturer's machine and exposed over HTTPS/WSS via Cloudflare Tunnel.

---

## 2. Delivered Feature Scope by Priority Tier

| Priority | Module | Delivered Capabilities |
| :--- | :--- | :--- |
| **P0 (Core)** | **Infrastructure & Multi-Tenant Auth** | Docker Compose (`web` + `cloudflared`), SQLite WAL persistence, Lecturer registration/sign-in (`scrypt`), lockout/reset/audit logs, Course Staff (TA) RBAC, clean-slate startup (`ENABLE_DEMO_SEED=false`). |
| **P0 (Core)** | **Rolling QR Attendance** | Projector QR rotating every 5s (`HMAC-SHA256` + grace window), `Full Name + Student ID` onboarding, `cp_device` cookie anti-proxy binding, late threshold, 2/3-col CSV & `.xlsx` rosters, manual Course Number join + approval. |
| **P0 (Core)** | **CV Headcount & Reports** | Tiled InsightFace SCRFD (`det_10g`) + 90° orientation recovery, webcam/upload capture, Phone Camera Companion QR (`ctok`) + `+1/-1` stepper, manual canvas edit, discrepancy approval, `.xlsx`/`.csv` export, photo retention purge. |
| **P1 (Live)** | **Real-Time Polls & Q&A** | Pre-class poll bank (`DRAFT`/`ACTIVE`/`CLOSED`), 1-click quick polls, single-vote idempotency, reconnect `poll:sync`, live `+MM:SS` Q&A + upvotes/pinning + popout, remote Offline Questions, cross-lecture notifications. |
| **P2 (AI)** | **Lecture Transcriber & Summaries** | Video/audio/transcript upload (`.mp4`/`.mp3`/`.wav`/`.vtt`/`.md`), Gemini Files API transcription + fast daily-quota failover + `faster-whisper`/local fallback, structured cards, Draft -> Publish -> Unpublish -> Cascade-Delete sync. |
| **P2 (AI)** | **Dual-Mode Assignments** | Strict separation between **Comparative Practice** (student-only feedback, $O(1)$ cohort stats, ordinal rank `1st Place`/`2nd Place`, hidden from lecturer) and **Final Submission to Lecturer** (standalone AI score + downloadable file). |

---

## 3. Explicit Out-of-Scope Boundaries

- **No Institutional LMS / Zoom Coupling:** Zero integration with Moodle, university SSO, or Zoom OAuth/Cloud APIs; all attendance, Q&A, and recording workflows run natively.
- **No Biometric Face-to-Name Identification:** SCRFD performs anonymous crowd headcounting (`count = N`), never facial recognition matching students to names.
- **Single-Host Deployment:** Packaged for one lecturer PC (`docker compose up`), not multi-region Kubernetes clustering.

---

## 4. Project Deliverables Chain

| Document / Artifact | Role | Link |
| :--- | :--- | :--- |
| **SOW** *(this doc)* | Agree — Scope, priorities, boundaries, risks | [`docs/scope_of_work_sow.md`](scope_of_work_sow.md) |
| **SRS** | Specify — Requirements & Use Cases (`UC-01`–`UC-10`) | [`docs/software_requirements_specification_srs.md`](software_requirements_specification_srs.md) |
| **SDD** | Architect — System structure, 18-model ERD, stack rationale | [`docs/software_design_and_architecture.md`](software_design_and_architecture.md) |
| **EDD** | Detail — Component algorithms & internal mechanics | [`docs/engineering_design_edd.md`](engineering_design_edd.md) |
| **API** | Interface — REST endpoints, Socket.IO events & auth | [`docs/api_reference.md`](api_reference.md) |
| **STP/STD** | Verify — 10-suite test matrix, E2E, Fault Localization | [`docs/software_test_plan_stp.md`](software_test_plan_stp.md) |
| **README** | Operate — Quick start, API keys, usage & tests | [`README.md`](../README.md) |
| **Application & Benchmarks** | Deliverable code, runner & empirical CV/WS verification | [`app/`](../app/), [`run_all_tests.py`](../run_all_tests.py), [`reports/tool_verification_results.md`](../reports/tool_verification_results.md) |

---

## 5. Milestones & Key Risks

| Milestone | Scope | Status |
| :--- | :--- | :--- |
| **M1 — Architecture & Docs** | Engineering chain, schema & tool selection | Complete |
| **M2 — Core MVP (P0)** | Docker + tunnel, rolling QR, SCRFD headcount, `.xlsx` export | Complete |
| **M3 — Live & AI Modules (P1/P2)** | Polls, Q&A, Video summaries, Comparative vs. Final homework | Complete |
| **M4 — Multi-Tenant Overhaul & QA** | Tenant/comparative isolation, 10-suite `run_all_tests.py`, Chromium E2E | Complete |

| Risk | Mitigation Implemented |
| :--- | :--- |
| Missed distant/occluded faces in wide lecture halls | Overlapping 3x2 Tiled SCRFD + IoU NMS + manual canvas boxes & phone `+1/-1` stepper |
| Slow phone camera handshake misses 5s QR tick | Server-side grace buckets (`QR_GRACE_BUCKETS`) + persistent `cp_device` cookie |
| Gemini Free Tier rate limits or offline lectures | Zero-sleep daily-quota failover (`gemini-2.5-flash-lite` -> `gemini-2.5-flash`) + `faster-whisper`/local summarizer |
| Proxy attendance from shared smartphones | Signed `cp_device` cookie + DB unique `(session_id, student_id)` and `(session_id, device_id)` constraints |

# Software Requirements Specification (SRS) — ClassPulse

**Chain Role:** `2. SRS (Specify)` — *What must the system do? (Requirements & Use Cases)*  
**Upstream:** [`docs/scope_of_work_sow.md`](scope_of_work_sow.md) | **Downstream:** [`docs/software_design_and_architecture.md`](software_design_and_architecture.md)

---

## 1. User Classes & Core Terminology

- **Lecturer:** Owns courses/sessions, manages rosters and TAs, projects QR codes, runs headcounts, publishes summaries, and grades final homework submissions.
- **Course Staff (TA):** Granted course-level access by the course owner to manage lectures/rosters/Q&A (cannot add/remove staff or delete owner artifacts where restricted).
- **Student:** Enrolls via Course Number approval or QR check-in using `Full Name + Student ID`, votes in polls, asks live/offline questions, and submits practice or final assignments.

| Term | Definition |
| :--- | :--- |
| **HMAC Rolling QR** | Token signed with `HMAC-SHA256(secret, session_code:bucket)` where `bucket = floor(ts / 5)`. |
| **Grace Window** | Server accepts recent buckets up to `QR_GRACE_BUCKETS` behind current time. |
| **Tiled SCRFD + NMS** | Full-frame + 3x2 overlapping crop face detection merged via IoU Non-Maximum Suppression (`0.40`). |
| **Comparative vs. Final** | `comparative` submissions stay student-only with $O(1)$ class rank; `final` submissions go exclusively to the lecturer without class ranking. |

---

## 2. Functional Requirements

### Authentication, Courses & Enrollment
- **REQ-01 (Lecturer Auth & Session Security):** Lecturers register/login with `scrypt`-hashed passwords; 5 failed logins trigger a temporary `429` lockout; `/logout-all` and password changes revoke active sessions via `token_version`; CSRF is enforced via `cp_csrf` + `X-CSRF-Token`.
- **REQ-02 (Clean-Slate Default):** Empty databases never auto-seed demo accounts/courses unless `ENABLE_DEMO_SEED=true`.
- **REQ-03 (Multi-Tenant Isolation & Staff RBAC):** Lecturers only access courses they own or belong to via `CourseStaff`; cross-tenant web/API/Socket.IO requests return `403`.
- **REQ-04 (Rosters & Student Access):** Courses support 2-column `(ID, Name)` or 3-column `(ID, Name, Email)` CSV/Excel imports, device-reset, and immediate `403` revocation upon student removal.
- **REQ-05 (Manual Course Join Approval):** Students request entry by case-insensitive Course Number (`pending_courses`); course access remains `403` until approved or rejected in the Lecturer Roster tab.

### Feature 1 — Rolling QR Attendance & CV Headcount (`P0`)
- **REQ-06 (5s Rotating QR & Onboarding):** Projector views rotate QR payloads every 5s; first-time scanners onboard via `Full Name + Student ID` and receive an `HttpOnly` `cp_device` cookie for zero-typing returning scans.
- **REQ-07 (Anti-Proxy & Late Tracking):** DB unique constraints block multiple students per `(session_id, device_id)` (`409`); arrivals past `LATE_AFTER_MINUTES` are flagged `is_late=True`.
- **REQ-08 (Tiled SCRFD + Orientation Recovery):** Classroom photos (file upload, webcam base64, or Phone Camera Companion via scoped `ctok` QR with auto-close) run tiled SCRFD with 90° EXIF/mobile rotation recovery.
- **REQ-09 (Interactive Verification & Discrepancy Audit):** Lecturers can adjust bounding boxes or use the mobile `+1/-1` stepper; `AttendanceReconciler` classifies `VERIFIED`, `UNDER_SCANNED`, or `DISCREPANCY_ALERT` and supports explicit discrepancy approval.
- **REQ-10 (Exports & Photo Retention):** Sessions export `.xlsx` (`Audit Summary` + `Student Attendance` sheets) and BOM UTF-8 `.csv`; `/api/maintenance/purge-expired-photos` deletes aged image files while keeping numeric counts.

### Features 2 & 3 — Live Polls, Q&A Chat & Notifications (`P1`)
- **REQ-11 (Poll Bank & Live Voting):** Lecturers prepare `DRAFT` polls or 1-click quick polls, launch/close via REST + Socket.IO, enforce 1 vote per `(poll_id, student_id)`, and hydrate reconnecting clients via `poll:sync`.
- **REQ-12 (Live Q&A, Offline Questions & Notifications):** Messages record `+MM:SS` lecture offsets (`Question`, `Comment`, `Suggestion`, `Offline Question`), support upvotes, lecturer pinning (`chat:updated`), `.txt` timeline export, live popout window, and cross-lecture `chat:notify` badges.

### Features 4 & 5 — AI Lecture Summaries & Homework Separation (`P2`)
- **REQ-13 (Recording Pipeline):** Uploading `.mp4`/`.m4a`/`.mp3`/`.wav`/`.vtt`/`.md` extracts audio via `ffmpeg`, transcribes via Gemini Files API (with `faster-whisper`/local fallback), parses **Executive Summary**, **Key Topics**, **Action Items**, and collapsible **Full Transcript**, and supports sync `201` or async `202` jobs.
- **REQ-14 (Controlled Draft -> Publish -> Unpublish -> Cascade Delete):** New summaries default to `is_published=False` (hidden from all student endpoints/views). Publishing emits `lecture:summary_ready`; unpublishing immediately sets `is_published=False` and emits `lecture:unpublished`; deleting removes the artifact/file from disk and emits `lecture:deleted`.
- **REQ-15 (Comparative vs. Final Submissions):** Students iterate privately on `comparative` submissions (updating `ClassStatsAggregate` in $O(1)$ with resubmission delta subtraction and ordinal rank `1st Place`, `2nd Place`, etc.) independently of `final` submissions.
- **REQ-16 (Privacy Guarantee):** Lecturers never see `comparative` submissions, cohort rankings, or comparative files (`403`); Stage-2 AI prompts contain zero peer names, emails, or source code.

---

## 3. Non-Functional Requirements

| ID | Category | Requirement |
| :--- | :--- | :--- |
| **NFR-01** | **Concurrency** | SQLite WAL mode handles 50 concurrent check-in writes without `database is locked` errors. |
| **NFR-02** | **CPU Execution** | Face detection (ONNX SCRFD) and local ASR/summarizer fallbacks run on standard CPUs with no GPU required. |
| **NFR-03** | **Security** | Strict `401`/`403` enforcement across lecturer tenants, student enrollments, `ctok` phone cam scopes, and CSRF headers. |
| **NFR-04** | **Frontend Simplicity** | Zero-build HTML5/CSS3 + vanilla ES modules (no Node build step, React, or `node_modules` in production). |
| **NFR-05** | **Test Isolation** | Automated tests isolate SQLite DBs, `UPLOAD_DIR`, and `REPORT_DIR` inside temporary directories (`0` workspace leaks). |

---

## 4. Main Use Cases (`UC-01`–`UC-10`)

| ID | Actor | Trigger & Flow Summary | Outcome |
| :--- | :--- | :--- | :--- |
| **UC-01** | Lecturer | Registers on `/teacher/login`, creates a course + Lecture 1 | Empty personal workspace initialized; course gets unique `join_code` |
| **UC-02** | Lecturer + Student | Lecturer opens `/projector/<code>`; student scans 5s QR, enters Name + ID | Student enrolled in course; `cp_device` cookie bound; live counter updates |
| **UC-03** | Student | Scans second lecture QR using bound phone | Zero-typing auto check-in recorded immediately |
| **UC-04** | Student | Attempts check-in for friend on same bound phone | Rejected with HTTP `409 Conflict` (`Proxy attendance blocked`) |
| **UC-05** | Lecturer | Pairs phone via Headcount QR, snaps classroom photo, taps `+1` stepper | QR auto-closes; SCRFD + manual count reconciles against QR & roster |
| **UC-06** | Lecturer + Student | Lecturer launches pre-saved poll; student wakes phone & votes | `poll:sync` restores active poll; vote tallies update live without duplicates |
| **UC-07** | Student + Lecturer | Student submits `Question` (live) or `Offline Question` (from home) | Lecturer receives cross-lecture `chat:notify` & can jump to message/pin |
| **UC-08** | Student + Lecturer | Student requests course join by Course Number; lecturer approves in Roster | Pending student unlocks course assignments, lectures & summaries |
| **UC-09** | Lecturer -> Student | Lecturer uploads lecture `.mp4`, reviews Draft, clicks **Publish** (or **Unpublish** / **Delete**) | Structured summary syncs to or disappears from student sidebar & views |
| **UC-10** | Student -> Lecturer | Student tests draft in **Comparative Practice** (sees `1st Place` rank), then submits **Final Submission** | Lecturer sees only the final submission, standalone AI score & file download |

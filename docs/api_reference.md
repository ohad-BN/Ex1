# API & WebSocket Reference — ClassPulse

**Chain Role:** `5. API (Interface)` — *How do I call it?*  
**Base URL:** `http://localhost:5000` (or active Cloudflare Tunnel URL) | **Interactive Docs:** `GET /api/docs` (`GET /api/openapi.json`)

---

## 1. Authentication, Cookies & Security Headers

| Mechanism | Name | Usage |
| :--- | :--- | :--- |
| **Lecturer Session Cookie** | `cp_lecturer` | Signed cookie issued by lecturer register/login; carries `token_version` (invalidated by `/logout-all` & password change). |
| **Student Device Cookie** | `cp_device` | Signed `HttpOnly` cookie storing `(device_id, student_id)` after check-in or `/api/student/login`. |
| **CSRF Protection** | `cp_csrf` + `X-CSRF-Token` | Cookie auto-issued on GET responses; state-changing requests (`POST`/`PATCH`/`DELETE`) must send matching `X-CSRF-Token` header (handled automatically by `csrf.js`). |
| **Phone Headcount Scope** | `X-Headcount-Token` or `?ctok=` | Scoped pairing token authorizing mobile companion `/headcount-cam/<code>` photo capture & `+1/-1` stepper. |
| **Projector Token** | `?ptok=` | Signed read-only projector view token for `/projector/<code>`. |

---

## 2. REST API Endpoints

### System, Health & Integrations
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /health`, `GET /healthz` | Public | Liveness probe returning scheme, host, and status. |
| `GET /readyz` | Public | Readiness check verifying DB connectivity and models directory. |
| `GET /api/jobs/<job_id>` | Public | Poll background `AsyncTaskManager` job status (`PENDING`/`RUNNING`/`DONE`/`FAILED`). |
| `GET /api/integrations/status` | Public | Check Gemini configuration & local Whisper/SCRFD availability. |
| `GET /api/openapi.json`, `GET /api/docs` | Public | OpenAPI 3.0.3 schema and Swagger UI. |

### Lecturer Auth, Profile & Staff (`app/routes/courses.py`)
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `POST /api/auth/lecturer/register` | Public | Create lecturer account (rate-limited; activates legacy NULL-pw accounts). |
| `POST /api/auth/lecturer/login` (`POST /api/teacher/login`) | Public | Sign in by username or email (`429` lockout after 5 failures). |
| `POST /api/auth/lecturer/logout`, `POST /api/auth/lecturer/logout-all` | Lecturer | Clear current cookie or increment `token_version` across all sessions. |
| `GET/PATCH /api/auth/lecturer/me` | Lecturer | Read or update `full_name` and `department`. |
| `POST /api/auth/lecturer/change-password` | Lecturer | Rotate password and revoke existing sessions. |
| `POST /api/auth/lecturer/forgot-password`, `POST /api/auth/lecturer/reset-password` | Public | Request/consume single-use password reset token. |
| `GET /api/auth/lecturer/audit-logs` | Lecturer | Fetch recent security audit events for the signed-in lecturer. |

### Courses, Rosters & Student Portal (`app/routes/courses.py`)
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /api/courses`, `POST /api/courses` | Lecturer | List owned/staff courses or create a course + Lecture 1 + `join_code`. |
| `GET /api/courses/<id>/overview`, `POST /api/courses/<id>/sessions` | Staff | Course attendance overview or create sequential lecture session. |
| `GET/POST /api/courses/<id>/staff`, `DELETE /api/courses/<id>/staff/<lid>` | Owner | List, add (`ta`), or remove course staff (`403` for non-owners). |
| `POST /api/courses/<id>/roster`, `DELETE /api/courses/<id>/students/<sid>` | Staff | Import `.csv`/`.xlsx` roster or remove student (revokes access). |
| `POST /api/courses/<id>/students/<sid>/reset-device` | Staff | Clear student's `primary_device_id` binding. |
| `GET /api/courses/<id>/enrollment-requests`, `POST .../approve`, `POST .../reject` | Staff | Manage pending student Course Number join requests. |
| `POST /api/student/login`, `POST /api/student/logout`, `GET /api/student/me` | Public/Student | Remote Name + ID login, logout, and student dashboard payload. |
| `GET /api/student/courses/catalog`, `POST /api/student/enroll`, `POST /api/student/enroll-request` | Student | Enroll via join code/ID or submit manual Course Number join request (`202`). |

### Attendance, QR, Headcount & Phone Camera (`app/routes/attendance.py`)
| Method & Path | Auth | Description |
| :--- | :--- | :--- |
| `GET /s/<code>`, `GET /projector/<code>` | Public / Owner\|`ptok` | Student QR landing/onboarding view & 5s rotating projector QR view. |
| `GET /api/sessions/<code>/qr` | Session Read | Live signed QR payload (`bucket`, `sig`, `url`, `attendance_open`). |
| `POST /api/attendance/checkin` | Public/Student | Validate rolling QR (or `onboarding_grant`), enforce device anti-proxy (`409`), mark `is_late`. |
| `POST/PATCH /api/sessions/<id>/headcount` | Staff | Upload/webcam JPEG -> SCRFD count + boxes; `PATCH` saves manual box edits. |
| `POST /api/sessions/<id>/headcount/approve-discrepancy` | Staff | Explicitly approve `qr_count > visual_count` excess. |
| `GET /api/sessions/<id>/headcount/image/<kind>` (`raw`\|`annotated`) | Staff | Stream stored headcount JPEG. |
| `GET /api/sessions/<id>/status`, `GET /api/sessions/<id>/overview`, `GET /api/sessions/<id>/report?format=xlsx\|csv` | Read / Staff | Live roster + verdict status and styled `.xlsx`/`.csv` download. |
| `POST /api/sessions/<id>/headcount-cam/pair`, `GET .../pair/<nonce>/status` | Staff | Generate/revoke Phone Camera QR (`ctok`) & poll connection status. |
| `GET /headcount-cam/<code>`, `POST/PATCH /api/headcount-cam/<code>`, `GET .../image/<snap_id>` | `ctok` | Mobile companion photo upload, annotated preview & `+1/-1` stepper. |
| `POST /api/maintenance/purge-expired-photos` | Lecturer | Delete headcount image files older than `retention_hours`. |

### Live Polls, Q&A, Summaries & Assignments
| Domain & Endpoints | Auth | Purpose |
| :--- | :--- | :--- |
| **Polls (`polls.py`):** `GET/POST /api/sessions/<id>/polls`, `POST /api/polls/<id>/{launch,close,vote}` | Read / Staff / Student | Poll bank CRUD, launch/close lifecycle, idempotent student vote. |
| **Chat (`chat.py`):** `GET/POST /api/sessions/<id>/chat`, `GET /api/courses/<id>/chat`, `POST /api/chat/<id>/{upvote,pin}`, `GET /api/sessions/<id>/chat/export`, `GET /lecturer/chat-popout/<code>` | Read / Staff | Live Q&A & Offline Questions, upvotes, lecturer pinning, `+MM:SS` `.txt` export, popout. |
| **Lectures (`lectures.py`):** `GET /api/sessions/<id>/lectures`, `GET /api/courses/<id>/lectures`, `POST /api/sessions/<id>/lectures/summarize`, `POST /api/lectures/<id>/publish`, `GET /api/lectures/<id>/download/{summary.md,transcript.txt}`, `DELETE /api/lectures/<id>` | Read / Staff | Upload media (`?async=1`), draft->publish/unpublish toggle (`published_only=1` filtering), structured sections, cascade delete. |
| **Homework (`homework.py`):** `GET/POST /api/courses/<id>/assignments`, `POST .../seed-sample`, `GET /api/assignments/<id>`, `POST /api/assignments/<id>/submissions`, `GET /api/submissions/<id>/download`, `POST /api/submissions/<id>/deep-review` | Staff / Enrolled | `submission_type`: `comparative` (private to student + rank) vs. `final` (lecturer-visible + downloadable; lecturer `403` on comparative). |

---

## 3. Socket.IO Real-Time Events (`app/sockets/`)

| Direction | Event Name | Payload / Purpose |
| :--- | :--- | :--- |
| **Client -> Server** | `join_session`, `leave_session`, `join_notifications` | Join/leave `session_<code>` room (tenant-checked) or course/lecturer notification rooms. |
| **Client -> Server** | `qr:request_refresh`, `poll:request_sync` | Request latest signed 5s QR token or active poll hydration snapshot. |
| **Client -> Server** | `poll:vote`, `chat:send`, `chat:upvote` | Cast live poll vote, send live/offline Q&A message, upvote message. |
| **Server -> Client** | `qr:rotate`, `attendance:count_update`, `headcount:updated` | Push refreshed QR payload, check-in count updates, and CV headcount verdicts. |
| **Server -> Client** | `poll:sync`, `poll:activated`, `poll:opened`, `poll:results_update`, `poll:closed` | Hydrate/open/update/close live classroom polls. |
| **Server -> Client** | `chat:init`, `chat:new`, `chat:updated`, `chat:notify` | Populate chat history, stream messages/pins/upvotes, push cross-lecture badges. |
| **Server -> Client** | `lecture:status`, `lecture:summary_ready`, `lecture:unpublished`, `lecture:deleted` | Stream job progress and immediately show, hide, or remove summaries in student tabs. |
| **Server -> Client** | `assignment:created`, `assignment:review_ready`, `assignment:cohort_updated`, `assignment:final_submitted` | Announce new assignments, async AI reviews, peer cohort shifts, and final submissions. |

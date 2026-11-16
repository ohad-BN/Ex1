# Main Use Cases & Client Stories — ClassPulse

**Document ID:** `Ex1-UC-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 (To-Do #6)  
**Author:** Ohad Ben Nun

---

## 1. Actors & Personas

| Actor | Role | Primary Device | Core Goal |
| :--- | :--- | :--- | :--- |
| **Lecturer** | Course instructor running the class session | Laptop / Podium PC (+ optional phone camera) | Start sessions cleanly, eliminate proxy attendance, check comprehension instantly, and automate post-class summaries & grading insights. |
| **First-Time Student** | Student attending first lecture via ClassPulse | Smartphone (iOS / Android browser) | Scan QR once, register Name + Email in under 15 seconds, and start participating immediately. |
| **Returning Student** | Student with existing signed device cookie | Smartphone (or laptop) | Scan QR in 2 seconds with zero repetitive forms, vote in polls, ask questions, and check comparative homework feedback. |
| **Course Grader / Reviewer** | Lecturer or TA reviewing cohort progress post-class | Desktop browser | Audit attendance discrepancy reports, export Excel sheets, and inspect class-wide homework error distributions. |

---

## 2. Detailed Use Cases (`UC-01` – `UC-10`)

### UC-01: Start Classroom Session & Project 5-Second Rotating QR (P0)
* **Primary Actor:** Lecturer
* **Preconditions:** Docker Compose stack + Cloudflare Tunnel are running; lecturer is authenticated on `/lecturer`.
* **Main Flow:**
  1. Lecturer enters session title (e.g., `"OOP Lecture 4 - Design Patterns"`), optionally uploads/selects a sanitized course roster CSV, and clicks **Start Session**.
  2. System generates a unique `session_code`, enables the attendance window, and opens the **Projector QR Mode** displaying a large scannable QR code plus an animated 5-second countdown ring and live check-in counter (`0 checked in`).
  3. Every 5 seconds, the server generates a new HMAC-SHA256 signed URL (`https://<tunnel-host>/s/<session_code>?t=<bucket>&sig=<hmac>`) and refreshes the QR canvas via `qr:rotate`.
* **Alternative Flows:**
  * *A1 (Pause/Close Attendance):* Lecturer clicks **Lock Attendance** after 10 minutes; projector stops rotating QR and subsequent scans show `"Attendance Window Closed"`.
* **Postconditions:** Active `ClassSession` persisted in DB; projector displays rotating QR.

---

### UC-02: First-Time Student QR Scan & Onboarding Check-In (P0)
* **Primary Actor:** First-Time Student
* **Preconditions:** Session is active; student is physically in class viewing the projected QR code; no valid `device_token` cookie exists on student's phone.
* **Main Flow:**
  1. Student points smartphone camera at projector QR and taps the HTTPS tunnel link.
  2. Server validates `HMAC-SHA256` signature and confirms `t` falls within `[current_bucket - 1, current_bucket]`.
  3. Server detects no `device_token` cookie and renders a clean mobile onboarding card: **Full Name** + **University Email** + **"Register & Mark Attendance"** button.
  4. Student submits Name + Email.
  5. Server validates email format, upserts `Student` in SQLite, creates an `AttendanceRecord` with `checked_in_at` and `device_id`, sets a signed `HttpOnly` `device_token` cookie, and emits `attendance:count_update` to the lecturer dashboard.
  6. Student sees green confirmation badge (`"Checked In at 10:04:18"`) with direct tabs for **Live Chat** and **Live Polls**.
* **Alternative Flows:**
  * *A1 (QR Rotated Mid-Scan):* Student scanned at `4.8s` and HTTP request arrives at `6.1s`. Server accepts via the `t - 1` grace bucket seamlessly.
  * *A2 (Expired Token > 10s, e.g., WhatsApp screenshot forwarded outside class):* Server rejects with HTTP `410` and displays `"QR Expired — Please re-scan the live screen in class"`.
  * *A3 (Same Phone Used for a Second Student):* If a device already marked attendance in this session, server rejects the second email with HTTP `409` (`"This device has already recorded attendance for this session"`).
* **Postconditions:** Student saved permanently in `students` table; `AttendanceRecord` recorded; device bound to browser cookie.

---

### UC-03: Returning Student Frictionless Check-In (P0)
* **Primary Actor:** Returning Student
* **Preconditions:** Student already onboarded in a prior session and holds a valid signed `device_token` cookie.
* **Main Flow:**
  1. Student scans the 5-second QR code on the projector.
  2. Server validates QR token AND verifies signed `device_token` cookie -> automatically resolves `Student` (`"Welcome back, Dana Levi"`).
  3. Server inserts `AttendanceRecord` immediately (1-tap confirmation or auto-check-in on scan) and redirects to the session hub.
* **Alternative Flows:**
  * *A1 (Cleared Cookies / New Phone):* Student enters existing Email; system recognizes existing `Student` record, updates `device_id`, and avoids duplicate student rows.
* **Postconditions:** Check-in completed in $< 3$ seconds with zero repetitive typing.

---

### UC-04: Classroom Photo Headcount & Cross-Reference Audit Report (P0)
* **Primary Actor:** Lecturer
* **Preconditions:** Students have completed QR check-in; lecturer has a desktop webcam, smartphone camera, or image file of the classroom.
* **Main Flow:**
  1. Lecturer navigates to **Headcount & Audit** tab and clicks **Capture / Upload Classroom Photo**.
  2. Server saves image to session storage and dispatches `TiledSCRFDDetector` (Tiled InsightFace SCRFD + NMS) via `AsyncTaskManager` in a background thread so live polls/chat remain responsive.
  3. Server returns the detected face count ($N_{\text{faces}}$) and an annotated image displaying numbered green bounding boxes around every detected face.
  4. *(Optional Verification)* Lecturer notices 1 student hidden behind a laptop screen and clicks on the canvas to add `+1` manual marker (or removes 1 false poster detection), updating `verified_face_count`.
  5. `AttendanceReconciler` cross-references `Course Roster`, `QR Check-Ins` ($N_{\text{QR}}$), and `Verified Visual Headcount` ($N_{\text{faces}}$), computing $\Delta = N_{\text{QR}} - N_{\text{faces}}$ and assigning audit status (`VERIFIED`, `UNDER_SCANNED`, or `DISCREPANCY_ALERT`).
  6. Lecturer reviews the interactive table online and clicks **Export Excel (.xlsx)** or **Export CSV** (`SpreadsheetExporter`) to download the complete offline report.
* **Alternative Flows:**
  * *A1 (Multiple Angle Photos for Wide Auditoriums):* Lecturer can upload 1 wide photo or run a new capture to overwrite/update the session snapshot before locking the report.
* **Postconditions:** `HeadcountSnapshot` persisted; `.xlsx` / `.csv` report generated with roster status, timestamps, visual headcount, and discrepancy audit.

---

### UC-05: Pre-Class Poll Preparation & Live KISS Polling (P1)
* **Primary Actor:** Lecturer (creator) & Students (voters)
* **Preconditions:** Session exists; students are connected via `Flask-SocketIO`.
* **Main Flow:**
  1. **Before Class:** Lecturer opens the **Poll Bank** tab, creates 3 multiple-choice conceptual questions (marking optional correct answers and 30s countdown timers), and saves them as `DRAFT`.
  2. **During Lecture:** Lecturer clicks **Launch** on Poll #1 (or clicks **Quick Poll** -> types 1-line prompt -> **Yes / No / Unclear**).
  3. Server sets poll status to `ACTIVE` in DB and emits `poll:activated` to all students in the room.
  4. Students tap an option on their phone. Client sends `poll:vote`; server enforces unique `(poll_id, student_id)` vote constraint, updates counts, and emits `poll:results_update` to the lecturer's live bar chart.
  5. When timer expires or lecturer clicks **Close Poll**, server emits `poll:closed` (revealing correct answer if quiz mode is enabled).
* **Alternative Flows:**
  * *A1 (Mobile Phone Sleeps & Reconnects):* Student unlocks phone mid-poll -> Socket.IO reconnects -> server emits `poll:sync` with current `ACTIVE` poll, remaining time, and existing vote indicator.
* **Postconditions:** Poll responses persisted for post-lecture engagement analytics.

---

### UC-06: Timestamped Classroom Chat & Live Popout (P1)
* **Primary Actor:** Student & Lecturer
* **Preconditions:** Student is checked into active session.
* **Main Flow:**
  1. Student opens the **Chat / Q&A** tab, selects category (`Question`, `Comment`, or `Suggestion`), types `"Can you re-explain why SOLID Open/Closed applies here?"`, and taps **Send**.
  2. Server tags the message with lecture-relative timestamp (`+24:15`) and UTC time, persists `ChatMessage` in DB, and broadcasts `chat:new` via Socket.IO.
  3. Other students upvote the question (`chat:upvote`), surfacing top confusion points to the lecturer in the **Live Q&A Companion Popout**.
* **Postconditions:** Timestamped Q&A persisted and exported cleanly for Feature 4 lecture summary correlation.

---

### UC-07: Class Recording Upload → Google Gemini Transcription & Summary (P2)
* **Primary Actor:** Lecturer
* **Preconditions:** A class recording (`.mp4`, `.m4a`, `.mp3`, `.wav`, or `.vtt`) is available locally; `GEMINI_API_KEY` is configured (with `faster-whisper` offline fallback if unset).
* **Main Flow:**
  1. Lecturer opens **Tab 5 — AI Lecture Summarizer** and selects/uploads the class recording (`.mp4`/`.m4a`/`.mp3`/`.wav`/`.vtt`).
  2. Background job prepares compact audio via `ffmpeg` if needed and sends it to **Google Gemini API** (`google-genai` Files API + `gemini-3.8-flash`) to transcribe with exact `[MM:SS]` timestamps (falling back to local `faster-whisper` only when offline).
  3. Pass 1 (`LectureSummarizerService`) refines technical CS/OOP terms while preserving `[MM:SS]` offsets.
  4. Pass 2 synthesizes a structured Markdown summary (TL;DR, Timestamped Topics, Key Definitions, Answers to Class Chat Q&A, Action Items) and stores `LectureArtifact` in SQLite.
  5. Lecturer downloads Summary (`.md`) / Transcript (`.txt`), and students view/download published summaries in their **AI Study Hub**.
* **Alternative Flows:**
  * *A1 (Gemini `429` Rate Limit / Retired Model `404`):* `GeminiStudioClient` applies exponential backoff on `429` and automatic `404` model fallback (`gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-3.5-flash`) without crashing.
* **Postconditions:** Corrected transcript and structured lecture summary stored and downloadable.

---

### UC-08: Homework Upload & Stage-1 Structured JSON Extraction (P2)
* **Primary Actor:** Student (or Lecturer batch-importing repos)
* **Preconditions:** Lecturer created an `Assignment` with rubric criteria/topics; student uploads a `.pdf`/`.py`/`.java`/`.zip` file or submits a GitHub repo URL.
* **Main Flow:**
  1. Student uploads solution or pastes GitHub repository URL (`https://github.com/user/repo`).
  2. `SubmissionExtractor` extracts clean source text/code (skipping binaries, `.git/`, `venv/`, `node_modules/`, capping oversized blobs).
  3. `HomeworkReviewService` sends **only this single submission** + rubric to `GeminiStudioClient` (`gemini-2.5-flash` with `response_mime_type="application/json"` and `response_schema=HomeworkMetrics`).
  4. LLM returns deterministic structured JSON (overall score, topic breakdown scores, requirements coverage %, strengths, weaknesses, normalized error tags).
  5. `CohortAggregator` loads `ClassStatsAggregate` in SQLite and updates cohort counts, running topic means, score histogram, and error tag frequencies mathematically in $O(1)$ time (**zero extra LLM calls**).
* **Alternative Flows:**
  * *A1 (Student Resubmits Improved Homework):* Before adding the new `HomeworkMetrics`, `CohortAggregator` subtracts the student's previous `HomeworkMetrics` contribution from `ClassStatsAggregate`, preventing double-counting.
* **Postconditions:** Student dashboard immediately displays visual percentile gauges and topic charts against cohort averages.

---

### UC-09: On-Demand Deep Comparative Homework Review (P2)
* **Primary Actor:** Student or Lecturer
* **Preconditions:** Student submission has `HomeworkMetrics` JSON; `ClassStatsAggregate` contains cohort metrics ($N \ge 1$).
* **Main Flow:**
  1. Student clicks **"Generate Deep Comparative Review"** on their Homework Dashboard.
  2. Server checks if an up-to-date cached review already exists for the current submission version & cohort size; if not, calls Stage-2 LLM via `GeminiStudioClient` with **only two compact JSON payloads**:
     * Student's `HomeworkMetrics` JSON
     * Anonymized `ClassStatsAggregate` JSON (means, percentiles, top cohort error patterns)
  3. LLM generates an insightful comparative review highlighting where the student outperformed the cohort (e.g., *"Top 20% in OOD Modularity; 85 vs class average 71"*), shared cohort pitfalls avoided/encountered, and concrete next steps.
  4. Review is persisted and displayed cleanly on the student's screen.
* **Postconditions:** High-quality personalized comparative review delivered at $O(1)$ prompt token size and zero peer-privacy leakage.

---

### UC-10: Lecturer Cohort Analytics & Roster Management (P0 / P2)
* **Primary Actor:** Course Grader / Reviewer (Lecturer)
* **Preconditions:** Lecturer is authenticated; session or assignment records exist in SQLite.
* **Main Flow:**
  1. Lecturer imports a sanitized CSV roster (`name,email`) via `AttendanceService.import_roster_csv()` so attendance reports flag missing registered students (`Absent`) vs unregistered attendees (`Guest`).
  2. Post-homework deadline, lecturer opens the **Cohort Insights** view displaying `ClassStatsAggregate` topic means, score histogram, and the top 5 recurring `error_tags` across the class to inform next week's lecture review.
* **Postconditions:** Cohort-wide weaknesses identified cleanly without inspecting every submission manually.

---

## 3. Client Stories (Agile User Stories & Acceptance Criteria)

| Story ID | Persona | User Story | Acceptance Criteria |
| :--- | :--- | :--- | :--- |
| **US-01** | Student | *As a student in class, I want to scan a rotating QR code and register once with my name and email so I never waste lecture time filling out forms again.* | First scan creates DB profile + signed cookie in $<15\text{ s}$; second lecture scan checks in with 1 tap/auto-confirm; expired (>10s) or multi-student-per-phone attempts are blocked. |
| **US-02** | Lecturer | *As a lecturer, I want to snap one classroom photo and compare the detected headcount with QR check-ins in an Excel sheet so I can immediately spot proxy attendance.* | Uploaded photo returns annotated face count + boxes; lecturer can click to adjust count; `.xlsx` download contains Roster, Check-in Timestamps, Headcount, Delta, and Audit Flag. |
| **US-03** | Lecturer | *As a lecturer, I want to prepare quiz polls before class and launch quick comprehension checks on the fly so I know whether students understand the material.* | Pre-created drafts appear in 1-click launch list; quick Yes/No poll launches in $<5\text{ s}$; students reconnecting mid-poll still see the active poll and vote exactly once. |
| **US-04** | Student | *As a shy student, I want to ask timestamped questions in a mobile chat tab and upvote others' questions without interrupting the lecture.* | Chat messages deliver across tunnel in $<500\text{ ms}$; timestamps align to lecture start; upvotes reorder top questions cleanly into the Live Q&A Popout. |
| **US-05** | Lecturer | *As a lecturer, I want to upload my class recording and receive a Google Gemini-transcribed, structured summary referencing students' questions so I can download and share clean study notes.* | Uploaded `.mp4`/`.m4a`/`.mp3`/`.wav` is transcribed via Google Gemini API (or `faster-whisper` fallback), summarized cleanly, and downloadable as `.md` and `.txt`. |
| **US-06** | Student | *As a student, I want to see how my homework compares anonymously against the rest of the class without exposing anyone's code so I know what to improve.* | Single submission produces structured JSON; cohort stats update without re-analyzing other files; visual percentiles + on-demand LLM comparative prose contain zero peer names/code. |

# Homework & Assignment Bug Analysis and Implementation Plan

> **Scope:** Root-cause analysis and implementation plan for two reported issues on the Student UI (`/student`) and Homework Pipeline:
> 1. **New assignments do not appear automatically** on the Student UI (requires manual page refresh).
> 2. **Students cannot reliably submit assignments** on the Student UI (silent crashes, blocking LLM timeouts/failures, confusing UX & form traps).
>
> **Status:** Planning document only — no application source files modified yet.

---

## 1. Root-Cause Diagnosis

### Bug 1: New Assignments Require a Manual Refresh on `/student`

| Layer | Current Behavior | Exact Gap |
|---|---|---|
| **Backend Routes** (`app/routes/homework.py`) | `create_course_assignment` (`POST /api/courses/<course_id>/assignments`) and `seed_sample_assignment` (`POST /api/courses/<course_id>/assignments/seed-sample`) commit the `Assignment` row and return HTTP `201` JSON. | Neither endpoint emits any Socket.IO event (`assignment:created` / `assignment:updated`) to connected students. |
| **Socket Rooms** (`app/sockets/session_events.py`) | Connected students automatically join `course_notify_<course_id>` (for every enrolled course on `connect`, and for the active session's course in `join_session`) as well as `session_<session_code>`. | The real-time rooms already exist (`course_notify_<course_id>`), but homework creation never publishes to them. |
| **Student Frontend** (`app/templates/student_app.html`) | `loadStudyHub()` is only called on `selectSession(...)`, clicking the **AI Study Hub** tab (`switchView('view-study')`), pressing **Refresh**, or receiving `lecture:summary_ready` (line `1070`). The `2.5s` timer (`setInterval(fetchLatestSessionPolls, 2500)`) only syncs polls. | No `socket.on("assignment:created")` listener, no background assignment fallback sync, and no visual badge/toast on `#tab-btn-study` when a student is on **Live Class** (`view-live`) or **Courses & Logs** (`view-classes`). |
| **DOM Re-render Side Effect** (`app/templates/student_app.html`) | `renderStudyAssignments(assignments)` (line `997`) unconditionally executes `el.innerHTML = assignments.map(...)`. | If an auto-refresh runs while a student is actively typing into `#hw-code-${a.id}`, `#hw-git-${a.id}`, or selecting a file in `#hw-file-${a.id}`, replacing `innerHTML` will wipe their unsaved draft unless drafts and `<details open>` state are preserved. |

---

### Bug 2: Student UI Assignment Submission Failures & Hangs

Live inspection of the application code and running container logs (`docker compose logs web`) identified **five compounding causes** making submissions appear broken or fail:

1. **Stage-1 Pydantic Schema Rejected by Gemini Developer API (`additionalProperties` error)**
   - In `app/services/homework_pipeline.py` (line `75`), `HomeworkMetrics` defines `topic_scores: dict[str, float]`.
   - `app/services/llm_client.py` (`generate_structured`, lines `203–206`) passes `"response_schema": HomeworkMetrics` directly to `client.models.generate_content(...)`.
   - Standard JSON Schema for `dict[str, float]` emits `"additionalProperties": {"type": "number"}`, which Gemini Developer API rejects on every submission (verified in live `web` container logs: `Gemini generate_structured failed; using fallback=True: additionalProperties is only supported in Gemini Enterprise Agent Platform mode, , not in Gemini Developer API mode.`).
   - This wastes an upstream LLM call (and quota) on every single submission before falling back to `_local_structured_fallback`.

2. **Synchronous Blocking LLM Calls + Free-Tier `429`/`503` Backoff Inside HTTP `POST`**
   - `submit_assignment_homework` (`app/routes/homework.py:139–151`) executes **Stage 1** (`generate_structured`) AND **Stage 2** (`generate_deep_review` -> `generate_text`) **synchronously** inside the HTTP request handler.
   - Live container logs confirm Gemini repeatedly triggers `503 UNAVAILABLE` and `429 RESOURCE_EXHAUSTED` (5 RPM limit) with retry sleeps (`Sleeping 2.0s`, `4.0s`, `8.0s`), stalling the `POST /api/assignments/<id>/submissions` request for **15–30+ seconds**.
   - Behind proxies/tunnels or under Gunicorn/browser request timeouts, long stalls cause connection resets or HTML `502`/`504` error responses.

3. **Uncaught `res.json()` Crash & Missing Loading / Error Feedback in `submitStudentHomework()`**
   - In `app/templates/student_app.html` (`1042–1068`), `submitStudentHomework(assignId)`:
     - Immediately calls `const data = await res.json();` (line `1065`) without checking `Content-Type` or wrapping in `try/catch`. Any HTML error page (`500`, `502`, `504`, `413` Request Entity Too Large) or network abort throws an uncaught `SyntaxError` / `TypeError`, leaving the UI dead with zero feedback.
     - Does **not** disable the **Submit & Review** button or render a progress indicator while the request runs, encouraging repeated clicks (concurrent duplicate submissions and `429` storms) and making the button look unresponsive.
     - Only surfaces errors via blocking `alert(...)` rather than an inline status/error box near the assignment card.

4. **Guest / Unauthenticated UX Trap & Empty Input Handling**
   - On `/student`, the Quick One-Time Sign-In card (`#login-card`, line `364`) sits at the top of the page above `.mode-tabs`. When an unauthenticated visitor opens **AI Study Hub** (`#view-study`), the assignment submission form renders completely open (`<details open>`) with an active **Submit & Review** button. Clicking it only pops an `alert("Sign in with Name & Email above first.")` without focusing/scrolling to sign-in or telling them clearly inside the Study Hub card.
   - Similarly, clicking **Submit & Review** with empty code, empty GitHub URL, and no file fires an unnecessary network round-trip returning HTTP `400` instead of validating inline first.

5. **Mixed File + Notes Submission Dropped on Frontend & Missing `generate_review` Flag**
   - In `submitStudentHomework(assignId)` (`lines 1048–1053`), when `fileEl.files.length > 0`, `FormData` appends only `file` and `student_id`, silently discarding any text typed in `#hw-code-${assignId}` or URL in `#hw-git-${assignId}`.

*(Side observation in `loadStudyHub()` line `950`: `/api/courses/${cid}/lectures` uses `require_course_access`, so unauthenticated/student calls return `401`/`403` and silently leave `"Loading course lecture summaries..."` unreplaced when there is no lecturer cookie; worth noting/cleaning separately if desired.)*

---

## 2. Implementation Plan

### Part A — Real-Time Auto-Sync for New Assignments (Bug 1)

#### A1. Backend Broadcast on Assignment Creation (`app/routes/homework.py`)
1. Import `socketio` from `app` and `ClassSession` from `app.models`.
2. Add a small shared helper `_emit_assignment_created(svc: HomeworkReviewService, assignment: Assignment) -> None` in `app/routes/homework.py`:
   - Serialize a public course-wide payload (`svc.serialize_assignment(assignment, include_submissions=False)` — never include `student_id`-specific or other students' submissions in a broadcast).
   - Emit `"assignment:created"` to `to=f"course_notify_{assignment.course_id}"`.
   - Also emit `"assignment:created"` to `to=f"session_{sess.session_code}"` for every `ClassSession` belonging to `assignment.course_id` (covers guest or session-joined tabs before enrollment cookies are set).
3. Call `_emit_assignment_created(svc, assignment)` from **both**:
   - `create_course_assignment` (`POST /api/courses/<course_id>/assignments`) after `svc.create_assignment(...)`.
   - `seed_sample_assignment` (`POST /api/courses/<course_id>/assignments/seed-sample`) only when a new assignment was actually created (avoid duplicate alerts if an existing seeded assignment is returned idempotently).

#### A2. Student UI Live Listener, Tab Badge, & Draft-Safe Render (`app/templates/student_app.html`)
1. **Live Socket.IO Listeners:**
   - Add `socket.on("assignment:created", (payload) => { ... })`:
     - Verify `payload` matches `ACTIVE_SESSION.course_id` (or belongs to one of `STATE.my_courses` / `STATE.all_courses`; if the student is currently viewing a session of that course, refresh assignments immediately).
     - Show a non-blocking banner/toast notification (or badge on `#tab-btn-study`, e.g., `"New Homework: <title>"` with one-click jump to **AI Study Hub**) when `activeViewId !== "view-study"`, and clear the unread dot when switching into `view-study`.
   - Add `socket.on("assignment:review_ready", (payload) => { ... })` (paired with Part B2 async grading) to automatically reveal the completed deep review card as soon as background grading finishes.
2. **Resilient Background Fallback Sync:**
   - Extend the periodic sync (e.g., every `10s–15s` or throttled inside existing polling cycle when `ACTIVE_SESSION?.course_id` is set) to check `/api/courses/${cid}/assignments` so even after temporary WebSocket disconnects or mobile sleep, new assignments appear automatically without refreshing.
   - Compare serialized signature / assignment IDs + submission timestamps before re-rendering so idle DOM isn't needlessly churned.
3. **Preserve Unsaved Drafts Across Re-renders:**
   - Before replacing `#student-assignments-list` in `renderStudyAssignments(assignments)`, snapshot per-assignment draft state keyed by `assignment.id`:
     - `codeText` (`#hw-code-${id}` value)
     - `gitUrl` (`#hw-git-${id}` value)
     - `isOpen` (`<details>` open state)
     - Active submission/status message if a submission is currently in flight (`PENDING` / `SUBMITTING`)
   - After rendering updated cards, restore those draft values so incoming `assignment:created` or `lecture:summary_ready` events never erase code the student is writing.

---

### Part B — Reliable, Non-Blocking Student Homework Submission (Bug 2)

#### B1. Fix Gemini Developer API `HomeworkMetrics` Schema Compatibility (`app/services/homework_pipeline.py` & `app/services/llm_client.py`)
1. **Replace `dict[str, float]` in `HomeworkMetrics`:**
   - Define an explicit Pydantic sub-model, e.g.:
     ```python
     class TopicScoreItem(BaseModel):
         topic: str
         score: float = Field(ge=0.0, le=100.0)
     ```
   - Update `HomeworkMetrics` (or wire Gemini structured output schema cleanly with backward compatibility for existing serialized `metrics_json` rows in SQLite):
     - **Option 1 (Cleanest backward-compatible approach):** Keep `HomeworkMetrics.topic_scores: dict[str, float]` as the normalized domain model stored in `metrics_json`, and introduce a Gemini-safe wire schema `HomeworkMetricsResponse` with `topic_scores: list[TopicScoreItem]` that converts cleanly into `HomeworkMetrics` (`{item.topic: item.score for item in resp.topic_scores}`).
     - Result: Gemini Developer API accepts the schema without `additionalProperties` errors, Stage-1 structured grading actually succeeds via Gemini instead of failing on every attempt, and zero existing DB rows break!
2. **Optional Defensive Sanitizer in `GeminiStudioClient.generate_structured` (`app/services/llm_client.py`):**
   - If any caller passes a Pydantic model whose JSON schema still triggers Developer API constraints, log clearly and optionally strip unsupported keys cleanly — though fixing `HomeworkMetricsResponse` resolves the root cause directly.

#### B2. Non-Blocking Async Review Pipeline (`app/routes/homework.py` & `app/services/homework_pipeline.py`)
1. Reuse the repository's existing `AsyncTaskManager` (`app/services/task_runner.py`, already mounted at `current_app.extensions["task_manager"]`, same pattern as `app/routes/lectures.py:164–186`):
   - Split or parameterize `submit_homework` so:
     - **Immediate Phase (runs quickly in HTTP request, `< 200ms`):**
       - Validate assignment, student, uploaded file / code / GitHub URL via `SubmissionExtractor.extract(...)` (`HomeworkPipelineError` caught immediately -> clean `400`/`404` JSON!).
       - Save/update `HomeworkSubmission` row cleanly (or run fast local metrics initially / provisional `"ANALYZING"` state so the student gets immediate confirmation that their submission was **saved**).
     - **Background Phase (`?async=1` or default on Student UI):**
       - Enqueue Stage-1 Gemini grading + `CohortAggregator` update + Stage-2 `generate_deep_review` in `task_manager.submit(...)` using a fresh `sessionmaker(bind=engine)` session.
       - On completion, emit `socketio.emit("assignment:review_ready", serialized_submission, to=room_for_student_and_course)` (including `student_id` & `assignment_id`) and also update any job status endpoint (`GET /api/jobs/<job_id>` if added, or fallback polling in `loadStudyHub()`).
       - On failure inside background worker, fall back gracefully to local metrics/review (never leave submission stuck in `"ANALYZING"` forever).
   - Keep existing synchronous `POST /api/assignments/<id>/submissions` behavior (when `async` is omitted/false) for 100% compatibility with existing `pytest` tests and API callers, while opting the browser UI in with `?async=1` (or `async: true`).

#### B3. Harden `submitStudentHomework()` & Study Hub Submission UX (`app/templates/student_app.html`)
1. **Auth Awareness Inside `#view-study`:**
   - When `!STATE.student` (Guest mode):
     - Display a clear inline callout inside `#student-assignments-list` / assignment cards: *"Sign in above with your Name & Email to submit homework and save your AI review"*, with a quick `"Go to Sign-In"` button that scrolls to `#login-card` and focuses `#login-name`.
2. **Client-Side Validation & Combined Payload:**
   - Before sending `fetch`, check that at least one of `code` (`#hw-code-${assignId}`), `git` (`#hw-git-${assignId}`), or `file` (`#hw-file-${assignId}`) is non-empty. If all three are empty, show an inline validation error message under the card (never fire an empty request).
   - When uploading a file via `FormData`, also append `content_text` (if non-empty), `github_url` (if non-empty), and `generate_review="true"` + `async="true"`.
3. **Loading Lock, In-Flight Indicator, & Safe Response Parsing:**
   - Add per-assignment inline status element `#hw-status-${assignId}` and explicit button ID `#hw-btn-${assignId}`.
   - Track `SUBMITTING_ASSIGNMENTS` set so double-clicks cannot fire concurrent requests:
     - Disable `#hw-btn-${assignId}`, update label to `"Submitting..."` / `"AI Reviewing..."`.
   - Parse response safely:
     ```javascript
     const rawText = await res.text();
     let data = {};
     try {
       data = rawText ? JSON.parse(rawText) : {};
     } catch {
       data = { error: `Server returned HTTP ${res.status}` };
     }
     ```
   - Handle `201` (immediate review ready), `202` (saved & AI review running in background — show `"Submitted! Generating AI review..."` badge and wait for `assignment:review_ready` / fallback poll), and non-2xx errors (`400`/`401`/`413`/`500`) clearly inside `#hw-status-${assignId}` without uncaught exceptions.
   - Reset button enabled state cleanly inside `finally { ... }`.

---

## 3. Target Files Summary

| File | Planned Changes |
|---|---|
| `app/services/homework_pipeline.py` | Introduce Gemini-compatible `TopicScoreItem` + `HomeworkMetricsResponse` wire schema (converting cleanly to `HomeworkMetrics` with `dict[str, float]`); support provisional/async two-step grading & error-safe fallback so submissions never get stuck. |
| `app/routes/homework.py` | Emit Socket.IO `assignment:created` on `create_course_assignment` and newly seeded `seed_sample_assignment` (to `course_notify_<course_id>` and course `session_<code>` rooms); support `?async=1` / `async=true` on `POST /api/assignments/<id>/submissions` via `AsyncTaskManager`, emitting `assignment:review_ready` when done. |
| `app/templates/student_app.html` | Listen for `assignment:created` & `assignment:review_ready`; add unread indicator/toast on `#tab-btn-study`; preserve unsaved homework form drafts in `renderStudyAssignments`; harden `submitStudentHomework` with guest CTA, empty-check, `FormData` + text support, button disable/spinner, safe text-then-JSON response parsing, and inline `#hw-status-<id>` feedback. |
| `tests/test_homework_pipeline.py` (or new test module) | Add regression tests for Socket.IO `assignment:created`, `HomeworkMetricsResponse` -> `HomeworkMetrics` conversion, async submission (`202` + completion), and error paths (`400`/`401`). |

---

## 4. Verification Plan

1. **Automated Unit & Integration Tests (`pytest`):**
   - **Schema compatibility:** Verify `HomeworkMetricsResponse` JSON schema contains **no** `additionalProperties` and maps cleanly to `HomeworkMetrics` (`topic_scores: dict[str, float]`).
   - **Socket.IO `assignment:created`:** Connect a Flask-SocketIO test client joined to `course_notify_<course_id>` / `session_<code>`, call `POST /api/courses/<id>/assignments` and `POST /api/courses/<id>/assignments/seed-sample`, and assert `"assignment:created"` is received with assignment metadata (and no leaked private submissions).
   - **Homework Submission Flows:**
     - Test unauthenticated submission (`401` JSON error).
     - Test empty submission (`400` JSON error).
     - Test code text submission, file upload (`.py`, `.zip`, `.pdf`), and `?async=1` background submission returning `202` immediately and updating DB + emitting `assignment:review_ready`.
   - Run full existing suite (`pytest`) after edits to ensure zero regressions.

2. **Live Docker End-to-End Verification (`http://127.0.0.1:5055`):**
   - Open `/teacher` (Tab 6 Homework) in Browser Window A and `/student` (signed in, sitting on **Live Class** tab) in Browser Window B (no refresh after initial load).
   - Create a new assignment (or click **Seed Sample OOP Assignment**) in Window A -> verify Window B receives the new assignment immediately (tab badge/toast + card visible in **AI Study Hub** without manual refresh, and any text already typed in another assignment textarea remains intact).
   - In Window B, test empty submit (inline warning), code paste submit, and `.py` / `.zip` file submit -> verify button enters loading state, submission succeeds cleanly without JS console errors or proxy timeouts, AI comparative review renders automatically when ready, and `docker compose logs web` shows zero `additionalProperties` Gemini schema errors.

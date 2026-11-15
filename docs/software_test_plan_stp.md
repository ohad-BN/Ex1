# Software Test Plan (STP) — ClassPulse

**Document ID:** `Ex1-STP-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 (To-Do #9)  
**Author:** Ohad Ben Nun

---

## 1. Overview & Testing Strategy

Per Ex2 To-Do #9 (*"Define a testing strategy; make sure to address the classes (as in 7)"*), this Software Test Plan defines an automated verification strategy (`pytest`) and live end-to-end verification protocol mapped directly to every software class and service defined in [`docs/software_design_and_architecture.md`](software_design_and_architecture.md).

### Testing Pyramid
1. **Unit Tests (Fast, Deterministic, Offline):** Test pure Python math, cryptography, aggregation logic, geometry/NMS, and SQLite state transitions with external APIs (`GeminiStudioClient`, `ZoomOAuthRestBridge`, GitHub HTTP) mocked behind their interfaces (`LLMClient`, `ZoomBridgeAdapter`, `FaceDetector`).
2. **Integration Tests (Real Components inside Docker/Local Python):** Exercise Flask test client + Flask-SocketIO `test_client()`, real SQLite WAL concurrent access, `SpreadsheetExporter` `.xlsx` parsing, and real **InsightFace SCRFD** inference on the Ariel classroom photo (`Ex2` Page 3) and synthetic multi-face images.
3. **End-to-End Tunnel & Mobile Smoke Tests:** Verify Docker Compose + `cloudflared` tunnel from real mobile phones over cellular networks, testing rotating QR scans, reconnect hydration, and live photo upload.

---

## 2. Class-to-Test Traceability Matrix (Addressing Classes from To-Do #7 & #8)

| Architecture Component / Class | Test Suite | Test File (Planned) | Key Behaviors Verified |
| :--- | :--- | :--- | :--- |
| `RollingQRService` | Suite 1 | `tests/test_qr_security.py` | 5s time-bucket computation, HMAC-SHA256 verification, 2-bucket grace window (`t`, `t-1`), rejection of `t-2`/tampered tokens. |
| `DeviceBindingManager` | Suite 1 | `tests/test_qr_security.py` | Signed cookie serialization, tampering rejection, expiration/malformed payload safety. |
| `AttendanceService`, `Student`, `AttendanceRecord`, `RosterEntry` | Suite 2 | `tests/test_attendance_service.py` | First-time onboarding, returning student lookup, email validation, `(session, student)` & `(session, device)` anti-proxy unique constraints, CSV roster import. |
| `TiledSCRFDDetector`, `BoundingBox`, `FaceDetector` | Suite 3 | `tests/test_face_counter.py` | Tile grid coordinate mapping, IoU Non-Maximum Suppression deduplication across tile borders, annotated PNG rendering, classroom photo recall/count sanity. |
| `AttendanceReconciler`, `AuditVerdict` | Suite 3 | `tests/test_reconciler.py` | Delta computation ($\Delta = N_{\text{QR}} - N_{\text{visual}}$), tolerance boundaries (`VERIFIED`, `UNDER_SCANNED`, `DISCREPANCY_ALERT`), manual override recomputation. |
| `SpreadsheetExporter` | Suite 3 | `tests/test_report_exporter.py` | Valid `.xlsx` binary generation readable by `openpyxl`, exact sheet names/headers, formula/data integrity, `.csv` UTF-8 BOM Excel Hebrew/English compatibility. |
| `PollService`, `Poll`, `PollVote` | Suite 4 | `tests/test_polls_socketio.py` | Pre-class `DRAFT` creation, `ACTIVE`/`CLOSED` transitions, single-vote DB uniqueness under burst concurrency, Socket.IO `poll:sync` reconnect hydration. |
| `ChatService`, `ChatMessage`, `ZoomBridgeAdapter`, `NoOpZoomBridge`, `ZoomOAuthRestBridge` | Suite 5 | `tests/test_chat_zoom.py` | Lecture-relative `+MM:SS` timestamping, upvote idempotency/ordering, non-blocking Zoom Team Chat mock dispatch, graceful degradation when Zoom API returns HTTP 401/500. |
| `LectureSummarizerService`, `LectureArtifact` | Suite 6 | `tests/test_lecture_pipeline.py` | FFmpeg audio extraction guard, Whisper segment timestamp schema, two-pass Gemini correction/summary orchestration with chat Q&A injection, backoff retry on `429`. |
| `SubmissionExtractor`, `CohortAggregator`, `HomeworkReviewService`, `ClassStatsAggregate`, `HomeworkMetrics` | Suite 7 | `tests/test_homework_incremental.py` | Binary/`.git`/`venv` path exclusion, Pydantic JSON schema validation, $O(1)$ incremental mean/error-tag updates, **resubmission delta subtraction**, strict zero-PII anonymization in Stage-2 prompts. |
| `GeminiStudioClient`, `AsyncTaskManager` | Suite 6 & 7 | `tests/test_llm_and_tasks.py` | Exponential backoff on mocked `429 RESOURCE_EXHAUSTED`, pacing interval enforcement, background job state transitions (`PENDING` -> `RUNNING` -> `DONE`/`FAILED`). |
| Docker Stack + `cloudflared` Tunnel | Suite 8 | `tests/test_e2e_tunnel.py` / Manual Checklist | Container healthcheck, SQLite WAL volume persistence across restart, HTTPS `ProxyFix` headers, WSS Socket.IO handshake via tunnel. |

---

## 3. Detailed Test Suites (Suites 1–8)

### Suite 1: Rolling QR Cryptography & Device Cookie Security
* **Target Classes:** `RollingQRService`, `DeviceBindingManager`
* **Test Cases:**
  1. `test_bucket_boundary_acceptance`: Simulate clock at `t = 1004.9s` (bucket `200`) and verify token at `t = 1006.0s` (bucket `201`) succeeds via the `N-1` grace bucket.
  2. `test_expired_bucket_rejected`: Verify token from bucket `199` when current bucket is `201` (> 10s elapsed) fails strictly with expired status (`410`).
  3. `test_hmac_tamper_rejected`: Flip 1 character in `sig` or swap `session_code` and assert rejection (`403`).
  4. `test_device_cookie_tampering`: Modify serialized `device_token` cookie string and assert `parse_device_cookie()` returns `None` rather than raising unhandled exceptions.

### Suite 2: Attendance Onboarding, Anti-Proxy & SQLite WAL Concurrency
* **Target Classes:** `AttendanceService`, `Student`, `AttendanceRecord`, `RosterEntry`
* **Test Cases:**
  1. `test_first_time_onboarding_and_return`: First scan registers `Student` and creates `AttendanceRecord`; second session scan with valid cookie auto-identifies the same `Student` without creating duplicate student rows.
  2. `test_proxy_same_device_blocked`: Two different emails attempting check-in with the same `device_id` in one session must succeed for Student A and return HTTP `409 Conflict` for Student B.
  3. `test_duplicate_student_checkin_idempotent`: Same student scanning twice in one session returns friendly "Already Checked In" without duplicating `AttendanceRecord`.
  4. `test_concurrent_50_student_burst`: Spawn 50 threads calling `process_checkin` simultaneously against SQLite WAL DB; assert 50/50 succeed with zero `sqlite3.OperationalError: database is locked`.

### Suite 3: Computer-Vision Headcount, Reconciliation & Spreadsheet Export
* **Target Classes:** `TiledSCRFDDetector`, `BoundingBox`, `AttendanceReconciler`, `SpreadsheetExporter`
* **Test Cases:**
  1. `test_nms_merges_tile_border_duplicates`: Feed overlapping synthetic `BoundingBox` pairs spanning tile edges (IoU $= 0.65 > 0.40$) and assert `_apply_nms` collapses them to 1 higher-confidence box.
  2. `test_scrfd_on_reference_classroom_photo`: Run `TiledSCRFDDetector` on a bundled test fixture (and during M2 verification against the Ariel lecture-hall photo from Ex2 PDF Page 3) and assert valid bounded coordinates ($0 \le x_1 < x_2 \le W$) and non-zero detection matching target recall bounds.
  3. `test_reconciler_verdicts`: Verify `VERIFIED` when $|\Delta| \le 2$, `UNDER_SCANNED` when $\Delta < -2$, and `DISCREPANCY_ALERT` when $\Delta > +2$; verify manual count edit recalculates verdict immediately.
  4. `test_xlsx_export_roundtrip`: Generate `.xlsx` bytes via `SpreadsheetExporter`, reopen with `openpyxl.load_workbook()`, and assert both `Audit Summary` and `Student Attendance` sheets match expected counts, Hebrew/English names, and discrepancy flags.

### Suite 4: Real-Time Polling & Reconnect Hydration (`Flask-SocketIO`)
* **Target Classes:** `PollService`, `Poll`, `PollVote`
* **Test Cases:**
  1. `test_preclass_poll_bank_lifecycle`: Create `DRAFT` polls -> launch -> verify `ACTIVE` -> cast votes -> close -> verify `CLOSED` rejects late votes.
  2. `test_single_vote_per_student`: Emit two rapid `poll:vote` events from the same student; verify DB retains exactly 1 vote and option tally increments once.
  3. `test_reconnect_sync_hydration`: Disconnect a Socket.IO test client during an `ACTIVE` poll, reconnect and emit `join_session`, and assert `poll:sync` payload includes active poll ID, options, live tallies, and `already_voted` boolean.

### Suite 5: Timestamped Classroom Chat & Zoom Bridge Isolation
* **Target Classes:** `ChatService`, `ChatMessage`, `ZoomBridgeAdapter`, `NoOpZoomBridge`, `ZoomOAuthRestBridge`
* **Test Cases:**
  1. `test_chat_lecture_relative_timestamp`: Session started 15m30s ago -> posted message records `offset_seconds == 930` (`+15:30`) and emits `chat:new`.
  2. `test_zoom_api_failure_does_not_break_chat`: Inject a mocked `ZoomOAuthRestBridge` raising `TimeoutError` / HTTP `500`; assert `ChatService.post_message` still saves message to DB (`synced_to_zoom=False`) and broadcasts to all Socket.IO clients without error.
  3. `test_chat_timeline_export`: Export session Q&A ordered by upvotes & timestamp for consumption by Feature 4.

### Suite 6: ML Lecture Transcription & Gemini Correction/Summary Pipeline
* **Target Classes:** `LectureSummarizerService`, `GeminiStudioClient`, `AsyncTaskManager`, `LectureArtifact`
* **Test Cases:**
  1. `test_two_pass_pipeline_with_mock_llm`: Supply sample Whisper segment list + session chat Q&A list to mocked `LLMClient`; assert Pass 1 preserves segment timestamps and Pass 2 produces non-empty Markdown summary stored in `LectureArtifact`.
  2. `test_gemini_429_backoff_recovery`: Mock `google-genai` raising `429 RESOURCE_EXHAUSTED` twice before returning valid response; assert `GeminiStudioClient._call_with_backoff` succeeds on attempt 3.
  3. `test_async_task_error_isolation`: Force an exception inside a background job and assert `AsyncTaskManager` records `FAILED` status cleanly without crashing the Flask server process.

### Suite 7: Homework Incremental Aggregation & Privacy Isolation
* **Target Classes:** `SubmissionExtractor`, `CohortAggregator`, `HomeworkReviewService`, `ClassStatsAggregate`, `HomeworkMetrics`
* **Test Cases:**
  1. `test_repo_extractor_ignores_bloat`: Extract a mock zip/repo containing `.git/`, `node_modules/`, `__pycache__/`, binary `.png`, and `.py` source; assert only relevant source/doc files are retained within `max_chars`.
  2. `test_incremental_o1_aggregation_exactness`: Submit 5 distinct `HomeworkMetrics` JSON objects sequentially; assert `ClassStatsAggregate.mean_overall_score`, `topic_sums_json`, and `error_tag_counts_json` equal exact full-batch ground-truth math with **only 1 Stage-1 LLM call per student** (5 total calls, never $1+2+3+4+5=15$).
  3. `test_resubmission_subtracts_prior_contribution`: Student #2 resubmits improving score from `60` to `90`; assert `submission_count` stays `5` and cohort mean shifts strictly by $(90 - 60) / 5 = +6.0$.
  4. `test_stage2_prompt_contains_zero_peer_pii`: Inspect the exact prompt string passed to mocked `LLMClient` during `generate_deep_review(student_A)` and assert **zero occurrences** of Student B's name, email, or raw code appear anywhere in the prompt.

### Suite 8: Docker Containerization & Cloudflare Tunnel Live Smoke Check
* **Scope:** End-to-end containerized verification on host Windows PC.
* **Procedures:**
  1. Build and boot full stack via `docker compose up --build`.
  2. Confirm `/health` returns HTTP `200` and SQLite WAL volume persists across `docker compose restart web`.
  3. Scan live 5-second rotating QR from an external smartphone on cellular data (bypassing local LAN) via `cloudflared` HTTPS URL; verify check-in, Socket.IO live poll vote, chat message, and photo headcount Excel export end-to-end.

---

## 4. Entry & Exit Criteria

* **Entry Criteria (to begin Milestone M4 final verification):**
  * All domain models and service classes implemented in `app/`.
  * Bundled sample test fixtures (sample roster CSV, lecture hall classroom photo, 2-minute sample audio clip, sample Ex0 repo submissions).
* **Exit Criteria (ready for live 10-minute classroom demo):**
  * **100% pass rate** on automated Unit & Integration Suites 1–7 (`pytest -v`).
  * **0 blocking errors** during 50-thread SQLite WAL concurrency burst test.
  * Successful completion of Suite 8 live tunnel smoke test from real student smartphones over cellular network.
  * Empirical tool results recorded in `reports/tool_verification_results.md`.

# Software Test Plan & Test Documentation (STP/STD) — ClassPulse

**Chain Role:** `6. STP/STD (Verify)` — *How do we prove it works?*  
**Upstream:** [`docs/api_reference.md`](api_reference.md) | **Downstream:** [`README.md`](../README.md)

---

## 1. Three-Layer Verification Architecture

1. **Backend Integration & Unit Suites (`pytest`):** Runs **91 fast tests** (`-m "not slow"`) plus **5 real SCRFD/ffmpeg `@pytest.mark.slow` tests** using isolated SQLite WAL DBs, Flask test clients, and Socket.IO `test_client()` (`tests/conftest.py`).
2. **Zero-Dependency Frontend JS Unit Tests (`node --test`):** Executes ES-module DOM/fetch/canvas specs in [`tests/test_frontend_js.mjs`](../tests/test_frontend_js.mjs) via [`tests/test_frontend_js.py`](../tests/test_frontend_js.py) covering `csrf.js`, `notifications.js`, `image_compress.js`, and `headcount_canvas.js`.
3. **Live Headless Chromium CDP E2E (`scripts/e2e_ui_verify.py`):** Verifies live routing, all Teacher tabs & modals, Student card navigation, 7-viewport geometry, RBAC isolation, and zero R2/R3 stack violations across Tiers 1–4.

---

## 2. 10-Suite Traceability Matrix (`run_all_tests.py`)

| Suite | Files Executed | Components & Behaviors Verified |
| :--- | :--- | :--- |
| **Suite 1** | `test_foundation_models.py` | ProxyFix HTTPS headers, `AsyncTaskManager`, SQLite WAL 50-thread concurrency, 18-model uniqueness constraints |
| **Suite 2** | `test_auth_and_security.py`, `test_gaps_auth_and_courses.py` | Register/login, lockout, reset/change password, `logout-all`, CSRF, `CourseStaff` RBAC, `identity_helpers`, rate limiter, roster removal |
| **Suite 3** | `test_lecturer_isolation.py`, `test_multi_course_portals.py`, `test_mobile_student_fixes.py` | Multi-lecturer `403`/`409` isolation, sequential lecture numbering, Excel/CSV rosters, course/lecture date sorting, Student SPA HTML |
| **Suite 4** | `test_qr_and_attendance.py`, `test_headcount_and_reports.py`, `test_phone_headcount_cam.py` | 5s HMAC QR + grace window, `cp_device` anti-proxy, Tiled SCRFD + 90° recovery (`@slow`), Phone Cam pairing/stepper, `.xlsx`/`.csv` |
| **Suite 5** | `test_polls_socketio.py`, `test_chat_core.py`, `test_chat_notifications_tabs.py`, `test_gaps_attendance_headcount_polls_chat.py` | Poll lifecycle & `poll:sync`, `+MM:SS` chat, pinning RBAC, cross-lecture notifications, `is_late`, retention purge, reconciler edges |
| **Suite 6** | `test_homework_incremental.py`, `test_gaps_homework_ranking_security.py` | Stage-1 schema, $O(1)$ aggregation + resubmission rollback, zero peer PII, comparative/final independence, ordinal ties, corrupt `.zip` `400` |
| **Suite 7** | `test_lecture_pipeline.py`, `test_gaps_lecture_pipeline_security.py` | Gemini Files API mock, daily-quota failover, WebVTT 80-seg chunking, corrupt `.mp4` `FAILED` isolation, Draft/Publish/Unpublish/Delete RBAC |
| **Suite 8** | `test_overhaul_isolation_and_ux.py` | Full end-to-end multi-tenant isolation, manual Course Number join approval, discrepancy approval, video `.mp4` (`@slow`), clean-slate boot |
| **Suite 9** | `test_frontend_js.py`, `test_frontend_js.mjs` | Node built-in unit tests for `csrf.js`, `notifications.js`, `image_compress.js`, and `headcount_canvas.js` |
| **Suite 10** | `test_diagnostics.py` | Self-verification of `FaultLocalizationEngine`, traceback source pinpointing, HTTP trace context, and RCA classification |
| **E2E** | `scripts/e2e_ui_verify.py` | Live Chromium CDP Tiers 1–4 + R3 zero-Node-build stack check |

---

## 3. Isolation & Fault Localization Guarantees

- **Zero Workspace Leaks:** Every fixture in `tests/conftest.py` and existing test modules isolates `DATABASE_URL`, `UPLOAD_DIR`, and `REPORT_DIR` inside pytest's `tmp_path`, shutting down `task_manager` and disposing SQLAlchemy engines on teardown (`0` files written to workspace `data/`).
- **Automated Fault Localization ([`tests/diagnostics.py`](../tests/diagnostics.py)):** Captures recent `FlaskClient` HTTP calls, filters framework trace frames to pinpoint exact `app/` or `tests/` lines, prints compact failure cards (`Where`, `Why`, `Diff`, `HTTP`, `Code`, `Hint`), and writes refreshed JSON/text reports into gitignored `tests/logs/`.
- **Non-Destructive Self-Test:** `python run_all_tests.py --self-test` injects 3 controlled in-memory bugs (`.zip` crash, `200 != 500` HTTP mismatch, `KeyError`) to prove detectors work cleanly without altering repo files.

---

## 4. Verification Commands & Exit Criteria

| Command | Scope | Pass Criteria |
| :--- | :--- | :--- |
| `python run_all_tests.py` | Suites 1–10 (fast mode, skips `@slow`) | `0` failed, concise table `TOTAL: X run \| X passed \| 0 failed` |
| `python run_all_tests.py --all --e2e` | All 96+ tests (incl. SCRFD/ffmpeg) + Chrome E2E | `100%` suite pass + `E2E failures=0` |
| `python run_all_tests.py --self-test` | Fault Localization self-check | All 3 simulated failures caught & pinpointed cleanly |
| `python scripts/e2e_ui_verify.py --tier all --allow-theme-fail` | Full Tiers 1–4 Chromium audit & screenshots | Zero 5xx errors, zero R2/R3 violations, `failures=0` |

Empirical SCRFD face detection, ProxyFix/Socket.IO, and HMAC verification measurements are recorded in [`reports/tool_verification_results.md`](../reports/tool_verification_results.md).

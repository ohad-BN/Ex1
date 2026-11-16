# Software Design & Architecture — ClassPulse

**Document ID:** `Ex1-SDD-1.0`  
**Course:** Introduction to Software Engineering (OOP / OOD) — Ex2 (To-Do #7 & #8)  
**Author:** Ohad Ben Nun

---

## 1. Main Software Components (To-Do #7)

ClassPulse follows a **Layered Modular Monolith** packaged inside Docker Compose on the lecturer's PC. Heavy CPU/API workloads (SCRFD face counting, `faster-whisper` transcription, Gemini LLM pipelines) run asynchronously on a bounded `ThreadPoolExecutor` so the Flask + Flask-SocketIO (`simple-websocket` + `gthread`) server remains non-blocking for 5-second QR rotation, live polls, and chat.

```mermaid
flowchart TD
    Students["Students (Mobile Browsers via Cellular / WiFi)"] -->|HTTPS / WSS| Tunnel["Cloudflare Tunnel Container (cloudflared)"]
    Lecturer["Lecturer Browser (Projector / Podium PC)"] -->|HTTPS / HTTP| Tunnel
    Lecturer -->|Localhost :5000| Gunicorn

    subgraph DockerHost["Lecturer PC — Docker Compose Stack"]
        Tunnel -->|HTTP + WS Upgrade| Gunicorn["Gunicorn + Flask + Flask-SocketIO (gthread)"]

        subgraph WebLayer["Presentation & Transport Layer"]
            Routes["REST Blueprints (auth, attendance, polls, media, hw)"]
            Sockets["Socket.IO Hub (qr:rotate, poll:*, chat:*)"]
        end

        subgraph DomainServices["Core Business & OOD Service Layer"]
            QR["RollingQRService & DeviceBindingManager"]
            Attend["AttendanceService & AttendanceReconciler"]
            Vision["TiledSCRFDDetector (InsightFace ONNX)"]
            Report["SpreadsheetExporter (.xlsx / .csv)"]
            Live["PollService & ChatService"]
            Whisper["LectureSummarizerService (FFmpeg + Gemini / faster-whisper)"]
            HW["HomeworkReviewService & CohortAggregator"]
            LLM["GeminiStudioClient (google-genai SDK)"]
            Tasks["AsyncTaskManager (ThreadPoolExecutor)"]
        end

        subgraph Storage["Persistent Docker Volumes (/app/data)"]
            DB["SQLite 3 (WAL Mode via SQLAlchemy 2.0)"]
            Files["Media & Report Store (photos, recordings, .xlsx)"]
        end

        Gunicorn --> Routes
        Gunicorn --> Sockets
        Routes --> QR
        Routes --> Attend
        Routes --> Report
        Routes --> Tasks
        Sockets --> QR
        Sockets --> Live
        Attend --> Vision
        Tasks --> Vision
        Tasks --> Whisper
        Tasks --> HW
        Whisper --> LLM
        HW --> LLM
        QR --> DB
        Attend --> DB
        Live --> DB
        HW --> DB
        Whisper --> DB
        Vision --> Files
        Report --> Files
    end

    LLM -->|HTTPS Audio & Text| GeminiAPI["Google AI Studio (Gemini Flash Free Tier)"]
    HW -->|Optional Public Clone| GitHub["GitHub Repositories"]
```

### Component Responsibility Matrix

| Component | Package / Module | Core Responsibility |
| :--- | :--- | :--- |
| **Ingress Tunnel** | `cloudflared` container | Terminates public HTTPS/WSS (`*.trycloudflare.com` or custom domain) with zero interstitial pages and forwards `X-Forwarded-*` headers to Flask. |
| **Web & Socket.IO Hub** | `app/routes/`, `app/sockets/` | Serves mobile-first student views and lecturer dashboard; manages WebSocket rooms (`session_<id>`) and reconnect state hydration (`poll:sync`). |
| **QR & Anti-Proxy Security** | `RollingQRService`, `DeviceBindingManager` | Generates 5-second HMAC-SHA256 rolling tokens, validates 2-bucket grace windows (`t`, `t-1`), and signs/verifies `HttpOnly` device cookies. |
| **Attendance & Reconciliation** | `AttendanceService`, `AttendanceReconciler`, `SpreadsheetExporter` | Onboards first-time students (`Name + Email`), imports optional roster CSVs, computes QR vs Visual Headcount delta $\Delta$, and exports styled `.xlsx`/`.csv`. |
| **CV Face Counter** | `FaceDetector` -> `TiledSCRFDDetector` | Runs InsightFace SCRFD (`det_10g` ONNX) across full image + overlapping tiles + IoU NMS; draws numbered bounding boxes for interactive lecturer review. |
| **Live Polls & Chat** | `PollService`, `ChatService` | Manages pre-class poll banks, quick polls, idempotent votes, timestamped chat feeds (`+MM:SS`), upvotes, and the Live Q&A Companion Popout. |
| **AI Lecture Summarizer** | `LectureSummarizerService` | Preps uploaded recordings via `ffmpeg`, transcribes via **Google Gemini API** (`files.upload`, offline `faster-whisper` fallback), and generates lecturer summaries + `.md`/`.txt` exports. |
| **Homework Incremental AI** | `SubmissionExtractor`, `CohortAggregator`, `HomeworkReviewService` | Extracts files/GitHub repos, gets Stage-1 Pydantic JSON metrics, updates anonymized `ClassStatsAggregate` in $O(1)$ math (with resubmission subtraction), and runs Stage-2 comparative review. |
| **Async Task Runner** | `AsyncTaskManager` | Bounded `ThreadPoolExecutor` tracking long-running CV/Whisper/LLM jobs (`PENDING`, `RUNNING`, `DONE`, `FAILED`) without blocking live Socket.IO traffic. |

---

## 2. Low-Level Class Diagrams (To-Do #8)

To preserve clarity and readability, the low-level Object-Oriented Design is partitioned into three cohesive class diagrams matching the system layers.

### 2.1 Domain Models & Feature 1 Core (Attendance, CV Headcount & Reporting)

```mermaid
classDiagram
    class ClassSession {
        +int id
        +str session_code
        +str title
        +datetime started_at
        +bool attendance_open
        +int headcount_tolerance
        +bool is_active()
    }

    class Student {
        +int id
        +str full_name
        +str email
        +str primary_device_id
        +datetime created_at
    }

    class RosterEntry {
        +int id
        +int session_id
        +str full_name
        +str email
    }

    class AttendanceRecord {
        +int id
        +int session_id
        +int student_id
        +str device_id
        +datetime checked_in_at
        +bool is_late
        +str token_bucket
    }

    class HeadcountSnapshot {
        +int id
        +int session_id
        +str raw_image_path
        +str annotated_image_path
        +int auto_face_count
        +int verified_face_count
        +str boxes_json
        +datetime captured_at
    }

    class BoundingBox {
        +int label_id
        +float x1
        +float y1
        +float x2
        +float y2
        +float confidence
        +str source
        +float iou(BoundingBox other)
    }

    class AuditVerdict {
        +int roster_total
        +int qr_count
        +int visual_count
        +int delta
        +str status
        +str summary_note
    }

    class RollingQRService {
        -bytes secret_key
        -int step_seconds
        -int grace_buckets
        +int compute_bucket(float timestamp)
        +str sign_token(str session_code, int bucket)
        +bool verify_token(str session_code, str token, int bucket)
        +str build_checkin_url(str base_url, str session_code)
    }

    class DeviceBindingManager {
        -str signer_salt
        +str create_device_cookie(str device_id, int student_id)
        +Optional_tuple parse_device_cookie(str cookie_val)
    }

    class FaceDetector {
        <<interface>>
        +List~BoundingBox~ detect(ndarray image)*
    }

    class TiledSCRFDDetector {
        -str model_path
        -float conf_threshold
        -float nms_iou_threshold
        -tuple grid_shape
        +List~BoundingBox~ detect(ndarray image)
        -List~tuple~ _generate_tiles(int width, int height)
        -List~BoundingBox~ _apply_nms(List~BoundingBox~ boxes)
        +str render_annotated_image(str image_path, List~BoundingBox~ boxes, str out_path)
    }

    class AttendanceService {
        -Session db
        -RollingQRService qr_service
        -DeviceBindingManager device_mgr
        +AttendanceRecord process_checkin(str session_code, int bucket, str token, str name, str email, str device_id)
        +int import_roster_csv(int session_id, str csv_content)
        +AuditVerdict get_audit_summary(int session_id)
    }

    class AttendanceReconciler {
        +AuditVerdict reconcile(int roster_total, int qr_count, int visual_count, int tolerance)
    }

    class SpreadsheetExporter {
        +bytes export_xlsx(ClassSession session, AuditVerdict verdict, List~dict~ rows)
        +str export_csv(ClassSession session, AuditVerdict verdict, List~dict~ rows)
    }

    ClassSession "1" --> "*" AttendanceRecord
    ClassSession "1" --> "*" RosterEntry
    ClassSession "1" --> "*" HeadcountSnapshot
    Student "1" --> "*" AttendanceRecord
    FaceDetector <|.. TiledSCRFDDetector
    TiledSCRFDDetector ..> BoundingBox : produces
    AttendanceService --> RollingQRService
    AttendanceService --> DeviceBindingManager
    AttendanceService --> AttendanceReconciler
    AttendanceReconciler ..> AuditVerdict : returns
    SpreadsheetExporter ..> AuditVerdict : formats
```

### 2.2 Features 2 & 3 (Real-Time Polls & Timestamped Classroom Chat)

```mermaid
classDiagram
    class Poll {
        +int id
        +int session_id
        +str question
        +str options_json
        +Optional_int correct_index
        +int time_limit_sec
        +str status
        +int sort_order
        +datetime activated_at
    }

    class PollVote {
        +int id
        +int poll_id
        +int student_id
        +int selected_index
        +datetime voted_at
    }

    class ChatMessage {
        +int id
        +int session_id
        +int student_id
        +str author_name
        +str category
        +str body
        +int offset_seconds
        +int upvotes
        +bool is_pinned
        +datetime created_at
    }

    class PollService {
        -Session db
        +Poll create_poll(int session_id, str question, List~str~ options, int time_limit, Optional_int correct_idx)
        +Poll launch_poll(int poll_id)
        +dict cast_vote(int poll_id, int student_id, int option_index)
        +Poll close_poll(int poll_id)
        +dict build_sync_payload(int session_id, int student_id)
    }

    class ChatService {
        -Session db
        +ChatMessage post_message(int session_id, int student_id, str category, str body)
        +ChatMessage upvote_message(int message_id, int student_id)
        +List~ChatMessage~ list_messages(int session_id)
        +str export_timeline_text(int session_id)
    }

    Poll "1" --> "*" PollVote
    PollService --> Poll
    PollService --> PollVote
    ChatService --> ChatMessage
```

### 2.3 Features 4 & 5 (Google Gemini Lecture Transcriber/Summarizer & Incremental Homework Review)

```mermaid
classDiagram
    class GeminiStudioClient {
        -str api_key
        -str fast_model
        -str pro_model
        -float min_interval_sec
        +str generate_text(str prompt, str system_instruction)
        +T generate_structured(str prompt, type schema_cls, str system_instruction)
        +str transcribe_audio_file(Path audio_path, str prompt, str system_instruction)
        -Any _generate_with_model_fallback(str requested_model, callable fn)
    }

    class LectureArtifact {
        +int id
        +int session_id
        +str source_type
        +str media_path
        +str raw_transcript_json
        +str corrected_transcript_json
        +str summary_markdown
        +str status
        +Optional_str error_message
    }

    class LectureSummarizerService {
        -Session db
        -GeminiStudioClient llm_client
        +List~dict~ transcribe_media_file(Path media_path)
        +LectureArtifact summarize_session_recording(int session_id, Path media_path)
        +tuple build_summary_download(int artifact_id)
        +tuple build_transcript_download(int artifact_id)
    }

    LectureSummarizerService --> GeminiStudioClient
    LectureSummarizerService --> LectureArtifact

    class Assignment {
        +int id
        +str title
        +str rubric_text
        +str topics_json
        +datetime created_at
    }

    class HomeworkSubmission {
        +int id
        +int assignment_id
        +int student_id
        +int version
        +str source_type
        +str source_ref
        +str metrics_json
        +str deep_review_md
        +datetime submitted_at
    }

    class ClassStatsAggregate {
        +int assignment_id
        +int submission_count
        +float mean_overall_score
        +str topic_sums_json
        +str scores_list_json
        +str error_tag_counts_json
        +datetime updated_at
    }

    class HomeworkMetrics {
        +float overall_score
        +Dict~str, float~ topic_scores
        +float requirements_coverage_pct
        +List~str~ strengths
        +List~str~ weaknesses
        +List~str~ error_tags
        +str style_summary
    }

    class SubmissionExtractor {
        -int max_chars
        +str extract_from_file(str file_path)
        +str extract_from_github_url(str repo_url)
    }

    class CohortAggregator {
        +ClassStatsAggregate apply_metrics(ClassStatsAggregate stats, HomeworkMetrics metrics)
        +ClassStatsAggregate revert_metrics(ClassStatsAggregate stats, HomeworkMetrics old_metrics)
        +dict build_anonymized_snapshot(ClassStatsAggregate stats, float student_score)
    }

    class HomeworkReviewService {
        -Session db
        -LLMClient llm
        -SubmissionExtractor extractor
        -CohortAggregator aggregator
        +HomeworkSubmission process_submission(int assignment_id, int student_id, str source_type, str source_ref)
        +str generate_deep_review(int submission_id)
    }

    class AsyncTaskManager {
        -ThreadPoolExecutor pool
        -Dict~str, dict~ jobs
        +str submit(str job_name, callable fn, tuple args)
        +dict get_status(str job_id)
    }

    LLMClient <|.. GeminiStudioClient
    LectureSummarizerService --> LLMClient
    LectureSummarizerService --> LectureArtifact
    Assignment "1" --> "*" HomeworkSubmission
    Assignment "1" --> "1" ClassStatsAggregate
    HomeworkReviewService --> LLMClient
    HomeworkReviewService --> SubmissionExtractor
    HomeworkReviewService --> CohortAggregator
    HomeworkReviewService ..> HomeworkMetrics : validates via Pydantic
```

---

## 3. Sequence Diagrams (To-Do #8)

### 3.1 Feature 1A — 5-Second Rolling QR Attendance & First-Time Student Sign-In

```mermaid
sequenceDiagram
    autonumber
    actor L as Lecturer Projector
    actor S as Student Phone
    participant T as Cloudflare Tunnel
    participant App as Flask + SocketIO
    participant QR as RollingQRService
    participant DB as SQLite WAL

    loop Every 5 Seconds (Projector Timer)
        L->>App: Socket.IO request / timer tick
        App->>QR: build_checkin_url(tunnel_host, session_code)
        QR-->>App: url?t=bucket_N&sig=hmac
        App-->>L: Emit qr:rotate (QR SVG + 5s countdown)
    end

    S->>T: Scan QR -> GET /s/CS101?t=N&sig=hmac
    T->>App: Forward HTTPS + X-Forwarded-Host
    App->>QR: verify_token(session_code, sig, bucket_N)
    Note right of QR: Accepts current bucket N or grace bucket N-1 (5-10s)
    alt Invalid or Expired Token (> 10s)
        App-->>S: HTTP 410 "QR Expired — Please re-scan projector"
    else Valid Token
        App->>App: Parse signed device_token cookie
        alt Returning Student Cookie Present
            App->>DB: Lookup Student & check duplicate device/student
            App-->>S: Auto check-in confirmation + Live Tabs
        else First-Time Student (No Cookie)
            App-->>S: Render Onboarding Card (Name + Email)
            S->>App: POST /api/attendance/checkin (name, email, t, sig)
            App->>QR: Re-verify token + enforce unique (session, device_id)
            App->>DB: Upsert Student + Insert AttendanceRecord
            App-->>S: Set-Cookie device_token (HttpOnly) + HTTP 200 Checked In
            App-->>L: Emit attendance:count_update
        end
    end
```

### 3.2 Feature 1B — Classroom Photo Headcount & Cross-Reference Audit Report

```mermaid
sequenceDiagram
    autonumber
    actor L as Lecturer
    participant App as Flask REST API
    participant Jobs as AsyncTaskManager
    participant CV as TiledSCRFDDetector
    participant Rec as AttendanceReconciler
    participant Exp as SpreadsheetExporter
    participant DB as SQLite WAL

    L->>App: POST /api/sessions/1/headcount (classroom_photo.jpg)
    App->>Jobs: submit(detect_faces, photo_path)
    App-->>L: HTTP 202 Accepted (job_id)

    Jobs->>CV: detect(image_ndarray)
    CV->>CV: Full image + 2x2/3x2 overlapping tiles
    CV->>CV: SCRFD ONNX inference + coordinate map + IoU NMS
    CV->>CV: render_annotated_image(numbered bounding boxes)
    CV->>DB: Save HeadcountSnapshot (auto_count, boxes_json)
    CV->>Rec: reconcile(roster_total, qr_count, auto_count, tolerance)
    Rec-->>DB: Store AuditVerdict (VERIFIED / DISCREPANCY_ALERT)

    L->>App: GET /api/jobs/<job_id> (polls or SocketIO notify)
    App-->>L: Annotated photo + numbered boxes + AuditVerdict
    opt Lecturer adjusts occluded face (+1 / -1 click)
        L->>App: PATCH /api/sessions/1/headcount (verified_count, edited_boxes)
        App->>Rec: Recompute delta & AuditVerdict
        App->>DB: Update HeadcountSnapshot
    end

    L->>App: GET /api/sessions/1/report?format=xlsx
    App->>DB: Load Roster + AttendanceRecords + HeadcountSnapshot
    App->>Exp: export_xlsx(session, verdict, joined_rows)
    Exp-->>L: Download ClassPulse_CS101_Audit.xlsx
```

### 3.3 Feature 3 — Reconnect-Safe Real-Time Polls (`Flask-SocketIO`)

```mermaid
sequenceDiagram
    autonumber
    actor L as Lecturer
    participant App as Flask + SocketIO
    participant PollSvc as PollService
    participant DB as SQLite WAL
    actor S as Student Phone

    L->>App: POST /api/polls (Pre-class poll bank draft)
    App->>PollSvc: create_poll(DRAFT) -> DB
    L->>App: POST /api/polls/7/launch
    App->>PollSvc: launch_poll(7) -> status=ACTIVE
    App->>DB: Commit poll state
    App-->>S: Emit poll:activated (poll_id=7, options, timer)

    S->>App: Emit poll:vote (poll_id=7, option=2)
    App->>PollSvc: cast_vote(7, student_id, 2)
    PollSvc->>DB: Insert PollVote (UNIQUE poll_id, student_id)
    App-->>L: Emit poll:results_update (live histogram counts)

    Note over S,App: Student phone locks screen and reconnects 20s later
    S->>App: Socket.IO reconnect -> emit join_session
    App->>PollSvc: build_sync_payload(session_id, student_id)
    App-->>S: Emit poll:sync (active poll, remaining_sec, already_voted=true)

    L->>App: POST /api/polls/7/close
    App->>PollSvc: close_poll(7) -> status=CLOSED
    App-->>S: Emit poll:closed (final counts + correct answer)
```

### 3.4 Feature 2 & Feature 4 — Classroom Chat & Google Gemini Lecture Summarizer

```mermaid
sequenceDiagram
    autonumber
    actor S as Student
    actor L as Lecturer
    participant Chat as ChatService
    participant Sum as LectureSummarizerService
    participant LLM as GeminiStudioClient (Google API)
    participant DB as SQLite WAL

    S->>Chat: chat:send (category=Question, body)
    Chat->>DB: Save ChatMessage with lecture offset (+24:15)
    Chat-->>S: Broadcast chat:new to session room

    Note over L,DB: After Lecture Ends
    L->>Sum: Upload class recording (.mp4 / .m4a / .mp3 / .wav)
    Sum->>Sum: Extract compact .mp3 via FFmpeg (if video/large file)
    Sum->>LLM: transcribe_audio_file(audio) via Google Files API (offline fallback: faster-whisper)
    LLM-->>Sum: Timestamped [MM:SS] Transcript
    Sum->>DB: Load Session Chat Timeline (Q&A timestamps)
    Sum->>LLM: Pass 1 - Refine CS terminology preserving [MM:SS]
    LLM-->>Sum: Corrected Transcript JSON
    Sum->>LLM: Pass 2 - Generate Structured Lecturer Summary + Q&A highlights
    LLM-->>Sum: Structured Summary Markdown
    Sum->>DB: Save LectureArtifact (status=COMPLETED)
    Sum-->>L: Download Summary (.md) & Transcript (.txt) + Publish to Study Hub
```

### 3.5 Feature 5 — Homework Incremental JSON Aggregation & Deep Comparative Review

```mermaid
sequenceDiagram
    autonumber
    actor S as Student
    participant HW as HomeworkReviewService
    participant Ext as SubmissionExtractor
    participant LLM as GeminiStudioClient
    participant Agg as CohortAggregator
    participant DB as SQLite WAL

    S->>HW: Submit file (.zip/.py/.pdf) or GitHub Repo URL
    HW->>Ext: extract_from_file / extract_from_github_url
    Ext-->>HW: Filtered source text (ignores .git, venv, binaries)

    Note over HW,LLM: Stage 1 - Single-Submission Feature Extraction (Fast JSON)
    HW->>LLM: generate_structured(single_submission + rubric, schema=HomeworkMetrics)
    LLM-->>HW: Structured JSON (scores, coverage, strengths, error_tags)

    HW->>DB: Load Assignment ClassStatsAggregate & check prior submission version
    opt Resubmission Exists
        HW->>Agg: revert_metrics(stats, old_metrics)
    end
    HW->>Agg: apply_metrics(stats, new_metrics)
    Note right of Agg: Pure O(1) Python math — zero extra LLM calls
    HW->>DB: Save HomeworkSubmission + updated ClassStatsAggregate
    HW-->>S: Return visual percentile & topic gap dashboard

    S->>HW: Click "Generate Deep Comparative Review"
    Note over HW,LLM: Stage 2 - Compact Comparative Insights (Anonymized)
    HW->>Agg: build_anonymized_snapshot(stats, student_score)
    HW->>LLM: generate_text(student_metrics_json + anonymized_cohort_json)
    LLM-->>HW: Personalized comparative pedagogical review (Markdown)
    HW->>DB: Cache deep_review_md on HomeworkSubmission
    HW-->>S: Display Comparative Review (zero peer code/names exposed)
```

# ClassPulse — Comprehensive Production & Professionalization Plan

**Purpose:** Transform ClassPulse from its current working course-project / prototype state into a production-grade, secure, multi-tenant university classroom platform.  
**Core Identity Requirement:**  
- **Professors / Lecturers:** Full professional authentication (**Sign In**, **Register**, **Username**, **Email**, **Password**, session invalidation, password reset, and RBAC).  
- **Students:** Keep frictionless classroom onboarding using **Student ID + Email** (plus Full Name & cryptographic device binding), avoiding password friction during 5-second rolling QR check-ins while preventing impersonation.

---

## 1. Current State vs. Target Production State (Gap Analysis)

| Area | Current Prototype State (`main` / Ex1) | Production Target State |
| :--- | :--- | :--- |
| **Professor Auth** | Passwordless `get_or_create_lecturer()` in `app/services/course_service.py` issuing a 365-day `cp_lecturer` cookie via `email` only. Anyone knowing a professor's email can sign in as them. | Separate **Register** & **Sign In** with unique `username`, unique `email`, salted `scrypt`/`Argon2id` password hashes, account lockout, email verification, and password reset tokens. |
| **Student Identity** | `Student` (`full_name`, `email`, `primary_device_id`) + `cp_device` cookie. No university Student ID column; roster CSV stores `Name, Email` only. | **Student ID (`student_id_number`) + Email** dual-key identity, strict `(student_id_number, email)` consistency validation, ID-aware CSV/Excel roster audit, and lecturer-approved device reset. |
| **Session Security** | `itsdangerous.URLSafeSerializer` (`cp_lecturer`, `cp_device`) has no expiration (`max_age` not enforced server-side), no revocation mechanism, and no CSRF tokens. | `URLSafeTimedSerializer` with configurable TTL, server-side `token_version` / session revocation on logout & password change, and CSRF protection on all state-changing forms/APIs. |
| **Authorization & Sockets** | HTTP route ownership guards exist in `app/routes/guards.py`, but unauthenticated fallbacks remain (`POST /api/sessions`, `list_polls`) and Socket.IO `join_session` does not verify tenant membership. | Unified RBAC decorators (`@lecturer_required`, `@course_owner_required`, `@student_required`), authenticated Socket.IO handshakes/rooms, and strict PII isolation. |
| **Database & Migrations** | SQLite WAL + manual `_migrate_sqlite_columns()` in `app/models.py` (cannot alter constraints, drop columns, or scale across multiple app workers). | Versioned **Alembic** migrations + dual support for SQLite (local dev) and **PostgreSQL** (`DATABASE_URL`) in production. |
| **Background Jobs** | In-process `AsyncTaskManager` thread pool (`task_runner.py`); jobs are lost if the container restarts mid-transcription. | Durable queue (**Redis + Celery/RQ**) with job retries, progress persistence, and startup recovery. |
| **Frontend Architecture** | Single-file HTML/CSS/JS Jinja templates (`teacher_portal.html` is ~1,590 lines; `student_app.html` is monolithic). | Modular Jinja partials + ES modules (or React + Vite + TypeScript frontend) backed by versioned Pydantic-validated `/api/v1/*` JSON endpoints. |
| **Ops, Privacy & CI** | Ephemeral `trycloudflare.com` quick tunnel, no rate limiting on auth/AI routes, plain logs, manual `pytest` runs. | Fixed domain / Cloudflare Named Tunnel, `Flask-Limiter`, structured audit logs, auto-purge media retention policy, and GitHub Actions CI (`pytest`, `ruff`, `mypy`). |

---

## 2. Phase 1 — Professional Authentication & Identity Architecture

### 2.1 Professor Accounts: Register, Sign In, Username, Email & Password

#### A. Database Model Upgrades (`app/models.py`)
Expand the `Lecturer` model (`lecturers` table):
- `id` (PK)
- `username`: `String(40)`, `unique=True`, `index=True`, `nullable=False` — lowercase alphanumeric + `._-` (`^[a-z0-9][a-z0-9._-]{2,31}$`)
- `email`: `String(160)`, `unique=True`, `index=True`, `nullable=False` — normalized lowercase
- `password_hash`: `String(255)`, `nullable=False` — generated via `werkzeug.security.generate_password_hash(password, method="scrypt")` (or `argon2-cffi`)
- `full_name`: `String(120)`, `nullable=False`
- `department`: `String(120)`, `nullable=False`
- `role`: `String(24)`, `default="lecturer"` (`lecturer` | `ta` | `admin`)
- `is_active`: `Boolean`, `default=True`
- `email_verified`: `Boolean`, `default=False` (toggleable via `REQUIRE_EMAIL_VERIFICATION` env flag for campus vs. dev environments)
- `token_version`: `Integer`, `default=1` — incrementing this invalidates all existing session cookies immediately ("Sign Out All Devices" / password change)
- `failed_login_attempts`: `Integer`, `default=0`
- `locked_until`: `DateTime(timezone=True)`, `nullable=True`
- `last_login_at`: `DateTime(timezone=True)`, `nullable=True`

Add a `PasswordResetToken` (and optional `EmailVerificationToken`) table storing **SHA-256 hashed tokens**, `lecturer_id`, `expires_at` (15 minutes), and `used_at` (single-use enforcement).

#### B. Separate Endpoints (Replace Passwordless `get_or_create_lecturer`)
Replace `POST /api/teacher/login` auto-registration with explicit auth endpoints:

1. **`POST /api/auth/lecturer/register`**
   - **Inputs:** `full_name`, `username`, `email`, `password`, `confirm_password`, `department` (+ optional `invite_code` if restricted to faculty).
   - **Validation Rules:**
     - `username`: 3–32 chars, lowercase `[a-z0-9._-]`, reserved words blocked (`admin`, `root`, `support`, `teacher`).
     - `email`: valid RFC syntax, normalized lowercase.
     - `password`: minimum 8 characters, at least 1 letter + 1 digit/symbol, rejected if equal to username/email.
   - **Conflict Checks:** Return **`409 Conflict`** with field-specific error if `email` or `username` is already registered (never overwrite or silently log in!).
   - **Result:** Creates `Lecturer`, hashes password, issues signed `cp_lecturer` session cookie (or triggers email verification step), returns `201 Created` + lecturer profile + `courses: []`.

2. **`POST /api/auth/lecturer/login`**
   - **Inputs:** `identifier` (accepts **either `email` or `username`**), `password`, `remember_me` (bool).
   - **Security Checks:**
     - Check `locked_until` before verifying password; return `429 Too Many Requests` with remaining cooldown if locked.
     - Verify `check_password_hash(lecturer.password_hash, password)`. On mismatch, increment `failed_login_attempts`; after 5 failures, lock for 15 minutes. Always return generic `"Invalid username/email or password."` (`401 Unauthorized`) to prevent account enumeration.
     - On success: reset `failed_login_attempts = 0`, set `last_login_at = utcnow()`, emit `AuditLog` event, issue signed `cp_lecturer` cookie containing `{ "lecturer_id": id, "token_version": v }`.

3. **`POST /api/auth/lecturer/logout` & `POST /api/auth/lecturer/logout-all`**
   - `/logout` clears cookies.
   - `/logout-all` increments `lecturer.token_version += 1` so every browser/tablet holding an old cookie is logged out instantly.

4. **`POST /api/auth/lecturer/forgot-password` & `/reset-password`**
   - Generates timed cryptographically random token (`secrets.token_urlsafe(32)`), stores `sha256(token)` in `PasswordResetToken`, sends SMTP/transactional email link (with console dev fallback when `MAIL_SERVER` is unset). Resetting the password increments `token_version`.

5. **Profile & Account Management (`GET/PATCH /api/auth/lecturer/me`, `POST /api/auth/lecturer/change-password`)**
   - Lets professors update display name, department, or change password (requiring current password).

---

### 2.2 Student Identity: Keeping Frictionless Student ID + Email

In a live lecture hall, 80+ students scanning a **5-second rolling QR code** on their phones cannot complete multi-step password + CAPTCHA registration inside class time. The target design preserves speed while tightening identity integrity:

#### A. `Student` & `RosterEntry` Schema Updates (`app/models.py`)
- Add to `Student`:
  - `student_id_number`: `String(32)`, `unique=True`, `index=True`, `nullable=False` (or configurable for legacy rows during migration; normalized digits/alphanumeric, e.g., 9-digit university ID)
  - Retain `email` (`unique=True`), `full_name`, `primary_device_id`, `device_bound_at`
- Expand `RosterEntry`:
  - Add `student_id_number` (`String(32)`, optional index) so lecturers can import university CSVs in `(Student ID, Full Name, Email)` or `(Full Name, Email)` formats, and export audit spreadsheets (`attendance.xlsx` / `.csv`) with **Student ID** as an official column.

#### B. Student Sign-In & First-Time QR Onboarding Flow
1. **Inputs:** `Student ID` + `Email` (+ `Full Name` on first registration or first scan).
2. **Consistency & Anti-Impersonation Guard:**
   - Look up by `email` and by `student_id_number`:
     - If both match the same `Student` record -> check device binding (`primary_device_id`), issue/refresh `cp_device` cookie, and check in.
     - If `email` exists with a *different* `student_id_number`, OR `student_id_number` exists with a *different* `email` -> reject with **`409 Conflict`** (`"Student ID and Email do not match an existing student record. Contact your lecturer if you entered a typo."`).
     - This guarantees one student cannot type a classmate's email with their own ID (or vice versa) to steal credit.
3. **Lecturer-Controlled Device Re-Binding:**
   - Keep the anti-proxy lock (`409` if two different students check in on the same browser/device).
   - Add a **"Reset Bound Device"** action per student row inside Tab 1 of `teacher_portal.html` so a professor can clear `primary_device_id` (logged in `AuditLog`) when a student genuinely replaces or loses their phone.
4. **Optional Verification Upgrade (Post-MVP):**
   - Optional 6-digit **email OTP code** if a student logs into `/student` from a *brand-new* unrecognized browser outside the lecture hall (`POST /api/student/login`), while keeping live projector QR scans zero-friction when the `cp_device` cookie or valid 90s onboarding grant is present.

---

### 2.3 Cookie, Session & CSRF Hardening (`app/services/qr_service.py`)

Upgrade `DeviceBindingManager`:
- Switch both serializers from `URLSafeSerializer` to **`URLSafeTimedSerializer`**:
  - Lecturer cookie (`cp_lecturer`): embeds `{"lecturer_id": id, "tv": lecturer.token_version}`; verified with `max_age=86400 * 7` (or `43200` when `remember_me=False`). Every request validates `payload["tv"] == lecturer.token_version`.
  - Student cookie (`cp_device`): embeds `{"device_id": dev, "student_id": sid}`; verified with `max_age=86400 * 180` + checked against `student.primary_device_id` on sensitive actions.
- **Cookie Flags:** Always `HttpOnly=True`, `SameSite="Lax"`, and `Secure=True` whenever served over HTTPS / Cloudflare Tunnel (`X-Forwarded-Proto: https` via `ProxyFix`).
- **CSRF Protection:** Issue a per-session CSRF token cookie (`cp_csrf`, readable by same-origin JS) and validate `X-CSRF-Token` on all `POST`/`PATCH`/`DELETE` requests coming from browser sessions.
- **Secret Key Enforcement:** Fail fast on startup in production (`FLASK_ENV=production`) if `SECRET_KEY` is still the default fallback or shorter than 32 characters.

---

## 3. Phase 2 — Authorization, Multi-Tenancy & Socket.IO Hardening

Even with `app/routes/guards.py` in place, a professional audit must close remaining edges:

1. **Eliminate Unauthenticated Teacher Defaults:**
   - Remove the unauthenticated fallback in `GET /api/courses` so it requires `@lecturer_required` (`401` otherwise). Expose a separate, minimal `GET /api/student/courses/catalog` (course code, title, lecturer name only — zero rosters/analytics) if students need course discovery.
   - Protect `POST /api/sessions`, `GET /api/sessions/<id>/status`, `GET /api/sessions/<id>/overview`, and `GET /api/sessions/<id>/headcount/image/<kind>` with `@session_owner_required` so student PII, attendance rows, and classroom photos are never publicly downloadable by guessing numeric IDs (`1`, `2`, `3`).
   - Keep `GET /projector/<session_code>` authorized by lecturer session OR a signed projector share token (`?ptok=...`) so a room projector display never needs the professor's full account cookie if opened on a separate classroom PC.
2. **Socket.IO Authentication & Room Authorization (`app/sockets/`):**
   - Inspect cookies during `@socketio.on("connect")` and attach verified `lecturer_id` / `student_id` to `flask.session` / connection context.
   - Inside `join_session`, verify:
     - Lecturers can only join rooms for sessions belonging to their own courses.
     - Students can only join rooms for courses they are enrolled in / actively checked into (or valid QR grant).
   - Prevent forged `poll:vote` or `chat:send` Socket.IO payloads from spoofing `student_id` or `author_name` — always resolve actor identity from the verified connection cookie, never trust client-supplied `student_id`!
3. **Co-Instructors & Teaching Assistants (`CourseStaff`):**
   - Replace the single `Course.lecturer_id` limitation with a primary owner + `CourseStaff(course_id, lecturer_id, role)` model (`owner`, `co_lecturer`, `ta`), allowing a professor to invite a TA by `@username` or email with configurable permissions (e.g., TA can manage polls/headcount/homework without deleting the course).
4. **Course Code vs. Unique Join Code:**
   - Allow two different professors to teach their own `CS101` (`UniqueConstraint("lecturer_id", "course_code")`) **while** generating an unambiguous 7-character `join_code` (e.g. `CS101-K9M` or auto-generated token) for student self-enrollment when course codes collide across instructors.
5. **Rate Limiting & Upload Validation:**
   - Add `Flask-Limiter` backed by Redis (in-memory in dev):
     - Auth endpoints (`/login`, `/register`, `/forgot-password`): `5–10 requests / minute` per IP & identifier.
     - AI endpoints (`/lectures/summarize`, `/assignments/*/review`): per-lecturer daily/minute quota caps so one user cannot exhaust the shared Gemini API budget.
   - Enforce `MAX_CONTENT_LENGTH` (e.g. `100 MB` for recordings, `10 MB` for classroom photos/ZIPs), sanitize filenames (`uuid` storage keys on disk instead of user filenames), and verify file headers/magic bytes rather than trusting file extensions alone.

---

## 4. Phase 3 — Database, Migrations & Reliable Infrastructure

1. **Alembic Migrations (`alembic/`):**
   - Replace `_migrate_sqlite_columns()` in `app/models.py` with Alembic revision scripts so constraint additions (`NOT NULL`, `UNIQUE`, foreign keys, index changes) work reproducibly across SQLite and PostgreSQL without manual DB recreation.
2. **PostgreSQL Support for Production:**
   - Keep SQLite + WAL as zero-config local/demo default (`start.bat`), and add PostgreSQL (`psycopg[binary]`) service support in `docker-compose.prod.yml` so multiple Gunicorn/WebSocket workers can handle hundreds of concurrent check-ins without SQLite write-lock contention.
3. **Durable Background Task Queue (Redis + Celery/RQ):**
   - Replace `AsyncTaskManager` thread pool (`task_runner.py`) with a persistent worker container (`worker`) connected to Redis. Store job state (`PENDING`, `TRANSCRIBING`, `SUMMARIZING`, `READY`, `FAILED`) in DB + Redis pub/sub -> Socket.IO so container restarts don't orphan in-flight Gemini transcriptions.
4. **Configurable Demo Seeding Guard:**
   - Gate `scripts/seed_demo.py` and default demo credentials behind an explicit `--demo` / `ENABLE_DEMO_SEED=true` flag so demo accounts (`Prof. Boaz Ben-Moshe`) are never accidentally seeded onto a live production database.

---

## 5. Phase 4 — UI / UX & Codebase Modularization

1. **Reorganize Frontend & Auth Experience:**
   - **Unified Auth Portal (`/login` & `/register`):**
     - **Professor Tab:** Toggle cleanly between **Sign In** (`Username or Email` + `Password` + `"Forgot password?"`) and **Create Lecturer Account** (`Full Name`, `Username`, `University Email`, `Department`, `Password`, `Confirm Password` + live password strength meter & inline availability checks).
     - **Student Tab:** Clean **Student Sign In** (`Student ID` + `University Email` + `Full Name`), showing clearly why passwordless ID + Email works with device binding.
   - **Account Profile Page (`/teacher/account`):** View username/email, edit name/department, change password, `"Sign out all sessions"`, and manage active courses.
2. **Split Monolithic Templates (`teacher_portal.html`, `student_app.html`):**
   - Break `app/templates/teacher_portal.html` (~1,590 lines) into maintainable Jinja2 partials (`templates/teacher/_sidebar.html`, `_tab_attendance.html`, `_tab_polls.html`, `_tab_chat.html`, `_tab_roster.html`, `_tab_ai_hub.html`) and modular static ES scripts (`app/static/js/`), OR migrate the UI to **React + TypeScript + Vite** consuming versioned APIs.
3. **Typed Request/Response Schemas & Uniform Errors:**
   - Use **Pydantic v2** schemas (already in `requirements.txt`) for all `/api/v1/*` request validation, returning consistent structured error payloads (`code`, `message`, `field_errors`) and auto-generated OpenAPI documentation (`/api/docs`).

---

## 6. Phase 5 — Observability, Privacy, Compliance & Deployment

1. **Security & Activity Audit Log (`AuditLog` Model):**
   - Record security-sensitive events (`lecturer.login_success`, `lecturer.login_failed`, `lecturer.register`, `lecturer.password_reset`, `course.access_denied_403`, `attendance.device_reset`, `attendance.manual_override`) with `actor_id`, `ip_address`, `user_agent`, and timestamp.
2. **Privacy & Media Retention (FERPA / GDPR Alignment):**
   - **Classroom Headcount Photos:** Add configurable auto-deletion (e.g., automatically purge raw/annotated classroom images after 24–72 hours once the lecturer locks the attendance audit, retaining only numeric counts).
   - **Uploaded Recordings & Homework:** Retain the existing zero-PII prompt guarantee on homework; display explicit consent/retention notices before sending audio to Gemini API; allow lecturers to delete lecture artifacts and uploaded media cleanly (`DELETE /api/lectures/<id>`).
3. **Production Deployment & CI/CD:**
   - Replace ephemeral `trycloudflare.com` quick tunnels with a persistent **Cloudflare Named Tunnel** (`cloudflared` token) or Nginx/Caddy reverse proxy + custom domain (`classpulse.ariel.ac.il`) with enforced HTTPS.
   - Add `/healthz` (liveness) and `/readyz` (DB + Redis + disk check) endpoints.
   - Set up **GitHub Actions CI**: run `pytest`, `ruff` (lint/format), `mypy` (strict type checking), and `pip-audit` on every push/PR to `main`.

---

## 7. Recommended Implementation Roadmap & Test Matrix

| Sprint | Scope | Key Deliverables | Verification & Tests |
| :--- | :--- | :--- | :--- |
| **Sprint 1: Prof Auth & Student ID** | Identity & DB schema | Alembic init; `Lecturer` (`username`, `password_hash`, `token_version`); `/register`, `/login` (email or username), `/logout-all`, `/forgot-password`; `Student.student_id_number` + ID/Email check-in validation; timed cookies + CSRF. | Unit & integration tests: register duplicate email/username (`409`), weak password (`400`), wrong password lockout (`429`), login by username vs email (`200`), `token_version` invalidation after logout-all, student `(id, email)` mismatch (`409`). |
| **Sprint 2: Security & Socket Hardening** | RBAC & WebSockets | Remove public `/api/courses` fallback; enforce `@session_owner_required` on status/headcount images; authenticate Socket.IO `connect` & `join_session`; remove trusted `student_id` from client JSON/WS payloads; add `Flask-Limiter`. | Cross-tenant attack test suite: Boas vs Daniel HTTP + WebSocket spoofing attempts (`401`/`403`), projector signed token access, student cannot join another course's live socket room. |
| **Sprint 3: Frontend Refactor & UX** | UI/UX polish | Split `teacher_portal.html` partials + modular JS; build dedicated tabbed Auth UI + Lecturer Account Settings + Student Device Reset button; Pydantic request schemas. | Playwright / Selenium E2E flow tests: Register Lecturer A & B -> isolated dashboards -> Student ID+Email QR check-in -> bound device block + lecturer device reset. |
| **Sprint 4: Production Ops & Scale** | Infra & CI | PostgreSQL profile + Redis/Celery worker; `AuditLog`; classroom photo auto-purge cron; Named Tunnel / TLS config; GitHub Actions CI pipeline. | Load/concurrency test (`50–200` simultaneous check-ins + votes against Postgres), worker restart recovery test, full CI pipeline green on `main`. |

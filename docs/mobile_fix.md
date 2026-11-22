# Mobile Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Polish the mobile Student App (`/student`) with an in-app mobile Back button that never navigates back to sign-in, a unified date-grouped lecture list (newest to oldest), recent-lecture-first course sorting, and removal of the `+ Join` bar and `"Loading ..."` placeholder text.

**Architecture:** Sorting of courses (by most recent lecture) and lectures (by `started_at DESC, id DESC`) is enforced in `CourseService.build_student_dashboard_payload()` and mirrored defensively in `student_app.html`. All UI changes live strictly in the mobile Student App (`student_app.html`, `icons.html`, and mobile `.cp-m-*` rules in `classpulse.css`) using vanilla JS History API (`pushState`/`popstate`) for tab-aware back navigation and single-pass date-grouped DOM rendering, leaving all desktop lecturer/projector views untouched.

**Tech Stack:** Python 3.12, Flask 3.1.3, SQLAlchemy, Jinja2, HTML5, CSS3, Vanilla JS, pytest, Playwright/CDP (`scripts/e2e_ui_verify.py`).

**Spec:** User mobile fix specification (5 requirements + 3 reference screenshots):
1. Mobile-only Back button that steps back to the previous in-app tab and never returns to sign-in; on the Home screen (`view-live`), Back stays in the app and never returns to sign-in.
2. Replace the cluttered duplicated `"All Lectures & Attendance"` chip cloud + log rows with one clean, user-friendly date-organized list ordered newest → oldest.
3. Remove the `+ Join` course-code bar from `Courses & Logs` (`view-classes`).
4. Sort the courses list so the course with the most recent lecture appears at the top.
5. Remove `"Loading ..."` placeholder text (`"Loading course assignments..."` and `"Loading lecture summaries..."`) from `AI Study Hub` (`view-study`).

## Global Constraints

- Scope strictly to the **mobile Student App** (`/student`, `app/templates/student_app.html`, `CourseService.build_student_dashboard_payload`, mobile `.cp-m-*` styles). Do NOT alter desktop Lecturer Portal (`teacher_portal.html`), Projector, or Lecturer Dashboard views.
- Zero external frontend frameworks, bundlers, or CDN scripts — strict HTML5 / CSS3 / Vanilla JS.
- Preserve existing API payload keys (`started_at`, `my_attendance_logs`, `my_courses`, `all_courses`) and keep `POST /api/student/enroll` intact so existing automated tests (`tests/test_multi_course_portals.py`, `tests/test_lecturer_isolation.py`) remain backward-compatible.
- Rebuild Docker (`docker compose up -d --build web`) after completing implementation so the running container reflects all changes.

## Review Focus

1. **Course with zero lectures alongside active courses:** `build_student_dashboard_payload()` must not raise on missing sessions; courses with lectures sort newest → oldest at the top, and zero-lecture courses appear deterministically at the bottom (pinned in Task 1).
2. **Phone/browser Back button on Home (`view-live`) immediately after QR check-in:** pressing browser Back or tapping hardware Back on `view-live` must NEVER navigate back to `/join/<code>` or sign-in; it stays on `view-live` via a History API root guard (pinned in Task 2 & Task 5).
3. **Repeated tab switches and open drawer when pressing Back:** tapping the same active tab twice must not create duplicate history entries, and pressing Back while `#student-nav-drawer` is open must close the drawer before popping a tab (pinned in Task 2 & Task 5).
4. **Multiple lectures on the same date, identical timestamps, and `ABSENT` rows:** lectures sharing a calendar date must render under one shared date header (no duplicate headings), break timestamp ties by `id DESC`, display lecture time even when `ABSENT`, and never show raw `"YYYY-MM-DD HH:MM:SS UTC"` strings (pinned in Task 1 & Task 4).
5. **Empty, guest, or failed-fetch Study Hub state:** if no active course exists or `/api/courses/<id>/assignments` or `/lectures` errors out, `#student-lecture-summaries` and `#student-assignments-list` must never show `"Loading ..."` text and must show clean empty-state cards (pinned in Task 3 & Task 5).

---

## File Structure

| File | Action | Responsibility |
| --- | --- | --- |
| `app/services/course_service.py` | Modify | Sort sessions by `started_at DESC, id DESC`; add `started_at_iso` and `latest_lecture_at`; sort `my_courses` and `all_courses` by latest lecture descending |
| `app/templates/macros/icons.html` | Modify | Add `arrow-left` SVG icon for `#student-back-btn` |
| `app/templates/student_app.html` | Modify | Add `#student-back-btn` + History API stack/guard; remove `#join-code-input` / `+ Join` bar; remove `"Loading ..."` text + hydrate cleanly; replace duplicate lecture chips/logs with unified date-grouped lecture list |
| `app/static/css/classpulse.css` | Modify | Add mobile-only `.cp-m-back-btn` rules (`max-width: 768px`) and `.cp-m-date-group` / `.cp-m-lecture-row` styling |
| `tests/test_mobile_student_fixes.py` | Create | Unit & integration tests covering recency sorting, edge cases, and rendered mobile HTML invariants |
| `scripts/e2e_ui_verify.py` | Modify | Add `390×844` mobile E2E assertions for Back navigation, Home sign-in guard, removed `+ Join`, zero `"Loading ..."` text, course sorting, and unified date-grouped lecture list |

---

### Task 1: Backend Recency Sorting for Courses & Lectures

**Files:**
- Modify: `app/services/course_service.py:980-1117`
- Create: `tests/test_mobile_student_fixes.py`

**Interfaces:**
- Consumes: `ClassSession.started_at`, `ClassSession.id`, `Course.id` from `app/models.py`
- Produces: `CourseService.build_student_dashboard_payload(student_id: Optional[int]) -> dict[str, Any]` where:
  - Each item in `my_courses` and `all_courses` includes `"latest_lecture_at": str` (ISO-8601 string of newest session `started_at`, or `""` when no sessions exist).
  - `my_courses` and `all_courses` are sorted descending by `(has_session, latest_started_at, latest_session_id, course_id)`.
  - Each item in `course["sessions"]` includes `"started_at_iso": str` (`s.started_at.isoformat()`) and is ordered by `(started_at DESC, id DESC)`.
  - `course["my_attendance_logs"]` preserves its exact schema and matches the same `(started_at DESC, id DESC)` order.

- [ ] **Step 1: Write failing backend sorting tests in `tests/test_mobile_student_fixes.py`**

Create `tests/test_mobile_student_fixes.py` with tests:
- `test_courses_sorted_by_most_recent_lecture_first(school_app)`:
  - Create 3 courses (`OLD101`, `MID102`, `NEW103`) enrolled by one student.
  - Set `ClassSession.started_at` explicitly: `OLD101` has a session at `2026-11-10 09:00`, `MID102` has a session at `2026-11-22 14:00` (newest), `NEW103` has a session at `2026-11-18 11:00`.
  - Call `/api/student/me` and assert `[c["course_code"] for c in data["my_courses"][:3]] == ["MID102", "NEW103", "OLD101"]` and `data["my_courses"][0]["latest_lecture_at"].startswith("2026-11-22")`.
- `test_courses_with_no_lectures_sort_after_courses_with_lectures(school_app)`:
  - Create an enrolled course `EMPTY104` with 0 sessions alongside `HAS105` with 1 session (`2026-11-01 08:00`).
  - Assert `HAS105` appears before `EMPTY104` in both `my_courses` and `all_courses`, and `EMPTY104["latest_lecture_at"] == ""`.
- `test_sessions_sorted_newest_first_with_tie_breaker(school_app)`:
  - Inside one course, create 3 sessions: `S_OLD` (`2026-11-12 10:00`), `S_TIE_A` (`2026-11-20 10:00`), `S_TIE_B` (`2026-11-20 10:00`, higher `id`).
  - Assert `[s["session_code"] for s in course["sessions"]] == ["S_TIE_B", "S_TIE_A", "S_OLD"]`, each session has non-empty `started_at_iso`, and `my_attendance_logs` matches the exact same order.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_mobile_student_fixes.py -v`
Expected: FAIL (`AssertionError` on course order — currently ordered by `Course.id.asc()` — and missing `latest_lecture_at` / `started_at_iso`).

- [ ] **Step 3: Implement recency sorting in `CourseService.build_student_dashboard_payload()` (`app/services/course_service.py:980-1117`)**

- Change the session query at line 1004-1009 to `.order_by(ClassSession.started_at.desc(), ClassSession.id.desc())`.
- Add `"started_at_iso": s.started_at.isoformat() if s.started_at else ""` to `s_card`.
- Compute `latest_session = sessions[0] if sessions else None` and add `"latest_lecture_at": latest_session.started_at.isoformat() if (latest_session and latest_session.started_at) else ""` to `course_card`.
- Sort both `my_courses` and `catalog_courses` using a helper key based on `(1 if latest_dt else 0, latest_dt or datetime.min, latest_session_id, course_id)` with `reverse=True` (do not leak private sort keys into the JSON dict).

- [ ] **Step 4: Run backend tests to verify they pass**

Run: `pytest tests/test_mobile_student_fixes.py tests/test_multi_course_portals.py tests/test_lecturer_isolation.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/course_service.py tests/test_mobile_student_fixes.py
git commit -m "feat(student): sort courses and lectures by most recent date"
```

---

### Task 2: Mobile-Only In-App Back Button & Home Sign-In Back Guard

**Files:**
- Modify: `app/templates/macros/icons.html:13-16`
- Modify: `app/templates/student_app.html:137-154`, `app/templates/student_app.html:303-438`, `app/templates/student_app.html:1402-1438`
- Modify: `app/static/css/classpulse.css:2028-2065`, `app/static/css/classpulse.css:2415-2445`
- Test: `tests/test_mobile_student_fixes.py`

**Interfaces:**
- Consumes: `cp_icon` macro (`app/templates/macros/icons.html`), `switchView(viewId, btnEl, opts)` and `closeStudentDrawer()` in `app/templates/student_app.html`
- Produces:
  - `cp_icon('arrow-left', 19)` in `app/templates/macros/icons.html`.
  - `#student-back-btn` button inside `.cp-m-appbar` (hidden by default on `view-live`, shown only on mobile viewports `<= 768px` when `activeViewId !== "view-live"`).
  - JS functions in `student_app.html`:
    - `updateBackButtonVisibility() -> void`
    - `handleStudentBack() -> void` (pops `STUDENT_NAV_STACK` or falls back to `"view-live"`; never navigates to sign-in)
    - `switchView(viewId: string, btnEl?: HTMLElement, opts?: { fromHistory?: boolean }) -> void`
    - `initStudentHistoryGuard(cleanUrl: string) -> void` (installs root + guard `history.pushState` entries and `popstate` handler that closes an open drawer first, steps back one tab on secondary views, and re-pushes the guard on `"view-live"` so browser/phone Back never returns to sign-in).

- [ ] **Step 1: Add failing HTML/template assertions for `#student-back-btn` and Back guard in `tests/test_mobile_student_fixes.py`**

Add `test_student_app_renders_mobile_back_button_and_history_guard(school_app)`:
- `GET /student` returns `200`.
- HTML contains `id="student-back-btn"`, `onclick="handleStudentBack()"`, `hidden` initial attribute on `#student-back-btn`, and `addEventListener("popstate"` guard handler.
- CSS (`app/static/css/classpulse.css`) contains `.cp-m-back-btn` and hides it on desktop viewports (`@media (min-width: 769px)`).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mobile_student_fixes.py::test_student_app_renders_mobile_back_button_and_history_guard -v`
Expected: FAIL (`AssertionError: id="student-back-btn" not found`).

- [ ] **Step 3: Add `arrow-left` icon, `#student-back-btn`, mobile-only CSS, and History API tab-stack + sign-in guard**

- In `app/templates/macros/icons.html`, add `{%- elif name == 'arrow-left' -%}<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>`.
- In `app/templates/student_app.html`:
  - Place `#student-back-btn` (`class="cp-m-menu-btn cp-m-back-btn"`, `aria-label="Go back to previous tab"`, `onclick="handleStudentBack()"`, `hidden`) right after `#student-drawer-toggle` inside `.cp-m-appbar`.
  - Maintain `const STUDENT_NAV_STACK = ["view-live"];` and `let STUDENT_CLEAN_URL = "/student";`.
  - In `switchView(viewId, btnEl, opts = {})`:
    - When `!opts.fromHistory && viewId !== prevViewId`: push `viewId` onto `STUDENT_NAV_STACK` (avoiding duplicate consecutive entries; if `viewId === "view-live"`, reset stack to `["view-live"]` so Home has no stale back trail) and call `window.history.pushState({ cpStudentView: viewId }, "", STUDENT_CLEAN_URL)`.
    - Call `updateBackButtonVisibility()` (shows `#student-back-btn` iff `activeViewId !== "view-live"`).
  - In `handleStudentBack()`:
    - If drawer is open, call `closeStudentDrawer()` and return.
    - If `activeViewId === "view-live"`, do nothing (never navigate to sign-in).
    - Pop current view from `STUDENT_NAV_STACK`, pick `prev = STUDENT_NAV_STACK[STUDENT_NAV_STACK.length - 1] || "view-live"`, and call `switchView(prev, null, { fromHistory: true })`.
  - In `initStudentHistoryGuard(cleanUrl)`:
    - Store `STUDENT_CLEAN_URL = cleanUrl`.
    - Call `window.history.replaceState({ cpStudentView: "view-live", cpRoot: true }, "", cleanUrl)` and immediately `window.history.pushState({ cpStudentView: "view-live", cpGuard: true }, "", cleanUrl)`.
    - Register `window.addEventListener("popstate", (evt) => ...)`:
      - If `#student-nav-drawer` has class `open`, close drawer and re-push current state (`{ cpStudentView: activeViewId }`), returning early.
      - If `activeViewId !== "view-live"`, pop `STUDENT_NAV_STACK` (or use `evt.state?.cpStudentView` if valid and different, else `"view-live"`), call `switchView(target, null, { fromHistory: true })`, and if landing on `"view-live"`, ensure the guard entry stays ahead of root so the next Back stays in-app.
      - If `activeViewId === "view-live"`, immediately call `window.history.pushState({ cpStudentView: "view-live", cpGuard: true }, "", STUDENT_CLEAN_URL)` so pressing phone Back on Home never exits to sign-in.
  - Call `initStudentHistoryGuard(nextUrl)` from `initStudentApp()` (replacing the bare `replaceState` block at lines 1405-1408 so both QR check-in and direct `/student` visits install the guard).
- In `app/static/css/classpulse.css`:
  - Add `.cp-m-back-btn[hidden] { display: none; }` and `@media (min-width: 769px) { .cp-m-back-btn { display: none !important; } }` so the Back button renders exclusively on mobile viewports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mobile_student_fixes.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/macros/icons.html app/templates/student_app.html app/static/css/classpulse.css tests/test_mobile_student_fixes.py
git commit -m "feat(student-mobile): add in-app back button and home sign-in back guard"
```

---

### Task 3: Remove `+ Join` Course Bar and All `"Loading ..."` Text

**Files:**
- Modify: `app/templates/student_app.html:227-276`, `app/templates/student_app.html:454-526`, `app/templates/student_app.html:896-958`
- Test: `tests/test_mobile_student_fixes.py`

**Interfaces:**
- Consumes: `STATE.my_courses`, `STATE.all_courses`, `renderStudyLectures(artifacts)`, `renderStudyAssignments(assignments)` in `app/templates/student_app.html`
- Produces:
  - `#view-classes` containing only `#courses-list` (no `#join-code-input`, no `+ Join` button, no `joinCourseByCode()`).
  - `#student-lecture-summaries` and `#student-assignments-list` with empty initial HTML (zero `"Loading ..."` copy).
  - Graceful fallback rendering in `loadStudyHub()` and `loadAssignmentsOnly()` so empty/guest/error states render clean empty-state cards instead of staying blank or stuck.

- [ ] **Step 1: Add failing assertions in `tests/test_mobile_student_fixes.py`**

Add `test_student_app_removes_join_button_and_loading_text(school_app)`:
- `GET /student` returns `200`.
- Assert `b'id="join-code-input"' not in html`, `b'joinCourseByCode' not in html`, and `b'Join course by code' not in html`.
- Assert `b'Loading course assignments...' not in html` and `b'Loading lecture summaries...' not in html`.
- Confirm `POST /api/student/enroll` still returns `200` for an authenticated student (API backward compatibility preserved).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mobile_student_fixes.py::test_student_app_removes_join_button_and_loading_text -v`
Expected: FAIL (`join-code-input` and `Loading course assignments...` currently present in `student_app.html`).

- [ ] **Step 3: Remove `+ Join` UI and `"Loading ..."` text in `app/templates/student_app.html`**

- In `#view-classes` (`lines 228-238`), remove the `.cp-input-action` block (`#join-code-input` + `+ Join` button) and remove `async function joinCourseByCode()` (`lines 454-470`).
- Update the empty-courses copy in `renderCourses()` (`line 524`) to `"No courses yet. Scan a classroom QR code to get started!"`.
- In `#view-study` (`lines 264-274`), remove `"Loading lecture summaries..."` and `"Loading course assignments..."` so both containers start empty (`""`).
- In `selectSession(sessionObj, courseObj)`: look up the full course object from `STATE.my_courses` / `STATE.all_courses` by `courseObj.id` and immediately render its preloaded `lecture_summaries || []` and `assignments || []` before firing background `loadStudyHub()`, so Study Hub renders instantly with zero loading flash.
- In `loadAssignmentsOnly()` and `loadStudyHub()`:
  - If `!ACTIVE_SESSION || !ACTIVE_SESSION.course_id`, call `renderStudyLectures([])` / `renderStudyAssignments([])` instead of returning silently.
  - On non-OK response or network error, render empty-state card only if the container currently has no children (never leave blank or stale loading text).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mobile_student_fixes.py tests/test_multi_course_portals.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/student_app.html tests/test_mobile_student_fixes.py
git commit -m "fix(student-mobile): remove Join bar and Loading placeholder text"
```

---

### Task 4: Unified Date-Organized Lecture List (Newest → Oldest) & Defensive Course Sorting

**Files:**
- Modify: `app/templates/student_app.html:519-595`
- Modify: `app/static/css/classpulse.css:2340-2415`
- Test: `tests/test_mobile_student_fixes.py`

**Interfaces:**
- Consumes: `course.sessions[]` (with `started_at`, `started_at_iso`, `attendance_status`, `checked_in`, `title`, `session_code`, `id`) and `course.latest_lecture_at` from Task 1.
- Produces:
  - Pure JS helpers in `app/templates/student_app.html`:
    - `sortCoursesByRecentLecture(courses: Array) -> Array`
    - `sortSessionsNewestFirst(sessions: Array) -> Array`
    - `formatLectureDateHeading(isoOrStr: string) -> { key: string, label: string, time: string }`
    - `formatAttendanceBadge(session: Object) -> { label: string, badgeClass: string }`
  - Unified DOM structure inside `<details class="cp-m-accordion">` per course:
    - `.cp-m-date-group[data-date-key="YYYY-MM-DD"]` → `.cp-m-date-heading` (e.g., `"Today · Nov 22, 2026"` or `"Sun, Nov 15, 2026"`) → `.cp-m-lecture-list` → `<button type="button" class="cp-m-lecture-row session-chip" data-session-code="...">`.
    - Each lecture appears **once** (no separate chip cloud + separate `my_attendance_logs` list).

- [ ] **Step 1: Add failing template/structure test in `tests/test_mobile_student_fixes.py`**

Add `test_student_app_uses_unified_date_grouped_lecture_list(school_app)`:
- `GET /student` returns `200`.
- Assert `b"sortCoursesByRecentLecture"` and `b"cp-m-date-group"` and `b"cp-m-lecture-row"` are present in `student_app.html`.
- Assert the old duplicate rendering pattern `c.my_attendance_logs` is no longer rendered as a second list inside `renderCourses()` (while `my_attendance_logs` remains present in `/api/student/me` JSON).

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mobile_student_fixes.py::test_student_app_uses_unified_date_grouped_lecture_list -v`
Expected: FAIL (`cp-m-date-group` not found).

- [ ] **Step 3: Implement unified date-grouped lecture list in `app/templates/student_app.html` and styles in `app/static/css/classpulse.css`**

- In `app/templates/student_app.html`:
  - Implement `sortCoursesByRecentLecture(courses)` sorting descending by newest session timestamp (`latest_lecture_at || sessions[0]?.started_at_iso || sessions[0]?.started_at || ""`), placing zero-session courses last.
  - Implement `sortSessionsNewestFirst(sessions)` sorting descending by `started_at_iso || started_at || ""`, tie-breaking by `Number(b.id || 0) - Number(a.id || 0)`.
  - Implement `formatLectureDateHeading(rawDate)`:
    - Parse `YYYY-MM-DD` and `HH:MM` cleanly (handling both ISO `2026-11-15T13:20:00` and `"2026-11-15 13:20"` without timezone shifting the calendar day).
    - Compute `"Today · Mon DD, YYYY"`, `"Yesterday · Mon DD, YYYY"`, or `"Weekday, Mon DD, YYYY"` (fallback `{ key: "undated", label: "Other Dates", time: "" }`).
  - Implement `formatAttendanceBadge(session)`:
    - `PRESENT` → `{ label: "Present", badgeClass: "cp-badge--green" }`
    - `LATE` → `{ label: "Late", badgeClass: "cp-badge--amber" }`
    - `ABSENT` (or default) → `{ label: "Absent", badgeClass: "cp-badge--red" }`
    - (If the student is not enrolled in the course, omit the absent pill and show `"Open"` neutral badge only when needed, or keep `Active` indicator.)
  - Refactor `renderCourses()` (`lines 519-594`):
    - Use `sortCoursesByRecentLecture(rawCourses)` and `sortSessionsNewestFirst(c.sessions || [])`.
    - Inside `<details class="cp-m-accordion">` (`All Lectures & Attendance (${sessions.length})`):
      - Group sorted `sessions` into ordered date buckets (preserving newest-to-oldest order so identical dates share one `.cp-m-date-group` header).
      - Render each lecture once as a full-width tappable `<button type="button" class="cp-m-lecture-row session-chip ${isSelected ? 'active' : ''}" data-session-code="...">` containing:
        - Left: lecture title + compact meta row (`HH:MM` + `<code>session_code</code>` + optional `"Active"` dot).
        - Right: compact status badge (`Present` / `Late` / `Absent`) + `›` chevron.
      - Clicking a row calls `selectSession(...)` and `switchView("view-live")`.
    - Remove the separate `sessionsHtml` pill cloud and separate `logsHtml` hairline rows.
- In `app/static/css/classpulse.css`:
  - Style `.cp-m-date-group` (clean vertical spacing), `.cp-m-date-heading` (sticky-feel uppercase muted eyebrow with hairline), `.cp-m-lecture-list` (vertical flex stack, `gap: 6px`), `.cp-m-lecture-row` (full-width tappable surface, `min-height: 52px`, `border-radius: 13px`, crisp hover/active border, zero horizontal overflow), `.cp-m-lecture-meta`, and `.cp-m-lecture-right`.

- [ ] **Step 4: Run backend + template tests to verify they pass**

Run: `pytest tests/test_mobile_student_fixes.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add app/templates/student_app.html app/static/css/classpulse.css tests/test_mobile_student_fixes.py
git commit -m "feat(student-mobile): replace duplicate lecture logs with date-grouped timeline"
```

---

### Task 5: Mobile E2E Verification (`390×844`), Full Test Suite & Docker Rebuild

**Files:**
- Modify: `scripts/e2e_ui_verify.py` (Tier 3 mobile Student App section)

**Interfaces:**
- Consumes: All DOM IDs and behaviors from Tasks 1–4 (`#student-back-btn`, `#student-nav-drawer`, `#view-live`, `#view-classes`, `#view-study`, `.cp-m-date-group`, `.cp-m-lecture-row`).
- Produces: Automated mobile E2E checks in `scripts/e2e_ui_verify.py` + verified screenshots + rebuilt Docker `web` container.

- [ ] **Step 1: Add mobile `390×844` E2E assertions to `scripts/e2e_ui_verify.py`**

In the Student App verification section at `390×844`, verify:
1. `#student-back-btn` is hidden on Home (`view-live`), visible after switching to `view-classes` and `view-study`, clicking it pops `view-study` → `view-classes` → `view-live`, and triggering `window.history.back()` on `view-live` keeps `window.location.pathname === '/student'` and `view-live` active (never navigates back to sign-in).
2. Opening `#student-nav-drawer` on `view-classes` and triggering Back closes the drawer without leaving `view-classes`.
3. `#join-code-input` does not exist in DOM, and neither `"Loading course assignments..."` nor `"Loading lecture summaries..."` appears in `document.body.innerText` across all 3 views.
4. Inside `view-classes`, courses are ordered with the course owning the newest lecture first, and expanding `<details class="cp-m-accordion">` renders `.cp-m-date-group` headers in descending date order with each `session_code` appearing exactly once (no duplicate rows).

- [ ] **Step 2: Run full unit & integration test suite**

Run: `python run_all_tests.py`
Expected: 100% pass (0 failures).

- [ ] **Step 3: Run full E2E verification suite and inspect mobile screenshots**

Run: `python scripts/e2e_ui_verify.py --tier all`
Expected: `0 warnings, 0 failures`, all mobile `390×844` checks pass; visually inspect Student App screenshots (`view-live`, `view-classes` with expanded date-grouped list, `view-study`).

- [ ] **Step 4: Rebuild and restart Docker `web` container**

Run: `docker compose up -d --build web`
Expected: Exit code `0`, `classpulse_web` running cleanly with all mobile fixes live.

- [ ] **Step 5: Commit**

```bash
git add scripts/e2e_ui_verify.py
git commit -m "test(e2e): verify mobile student back navigation, date-grouped list, and sorting"
```

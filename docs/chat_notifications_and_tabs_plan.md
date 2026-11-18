# ClassPulse — Action Plan: Chat Bug Fixes, Live Notification Pop-Ups (with Chrome Tab Red Badge) & Tab 5 Split

**Scope:** Detailed diagnosis and step-by-step implementation & testing plan for:
1. **Fixing broken Classroom Chat (unable to send / receive messages)**
2. **Adding real-time clickable chat notification pop-ups (Student & Lecturer deep-linking to the exact lecture & message) + Red Circle Chrome Tab Badge**
3. **Splitting Tab 5 (*AI Lecture Studio & Homework*) into two separate full-width workspace tabs**

---

## 1. Issue #1 — Chat Is Broken (Cannot Send / Receive Messages)

### 1.1 Exact Root Causes Observed in Live Logs & Code
1. **Legacy SQLite Schema Mismatch (`500 Internal Server Error` on every send):**
   - Live container logs (`classpulse_web`) show every `POST /api/sessions/<id>/chat` crashes with:
     `sqlite3.IntegrityError: NOT NULL constraint failed: chat_messages.synced_to_zoom`
   - When Zoom integration was removed from [app/models.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/models.py), ORM inserts omitted `synced_to_zoom`, while the existing SQLite `data/classpulse.db` table on disk still enforces `synced_to_zoom BOOLEAN NOT NULL` without a default value.
2. **Silent Error Handling & Missing HTTP Fallback in UI ([teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html), [student_app.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_app.html)):**
   - `sendLecturerChat()` and `sendStudentChat()` clear the message input box immediately without checking `if (!res.ok)` and never merge the returned JSON message into the local `chatMessages` / `CHAT_MESSAGES` array. If the server errors or Socket.IO hasn't joined the room yet, messages vanish silently.
   - `student_app.html` never fetches `GET /api/sessions/<id>/chat` on session selection — it depends 100% on Socket.IO `chat:init`.
3. **Socket.IO `join_session` Blocks Guest / Pre-Enrollment Students ([app/sockets/session_events.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/sockets/session_events.py)):**
   - `_is_socket_authorized_for_session()` rejects students opening `/student` (or clicking *"Preview Student App"*) if they haven't scanned a QR code or enrolled in that course yet, preventing them from joining `session_<CODE>` and receiving `chat:init` or `chat:new`.
   - Also, `student_app.html` never leaves the old Socket.IO room when switching lectures, causing chat feeds from two lectures to interleave.
4. **Additional Chat Polish Bugs Found:**
   - Export link in [teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html#L662) points to `/api/sessions/{{ session.id }}/chat/export.txt` (`404`) instead of `/api/sessions/{{ session.id }}/chat/export`.
   - Pressing `Enter` inside `#lecturer-chat-body` does not submit the message.
   - In [app/routes/chat.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/routes/chat.py), `post_chat_message()` checks `_resolve_student_from_cookie()` even when the signed-in professor submits from `/teacher`, instead of resolving `get_current_lecturer()` cleanly when posting from the lecturer portal.

### 1.2 Fix Plan for Chat
1. **Database Legacy Column Cleanup ([app/models.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/models.py)):**
   - Extend `_migrate_sqlite_columns()` to detect legacy leftover columns (`chat_messages.synced_to_zoom`, `class_sessions.zoom_meeting_id`, `class_sessions.zoom_auto_sync`, etc.) via `PRAGMA table_info(...)` and drop them cleanly using `ALTER TABLE ... DROP COLUMN ...` (with a safe table-rebuild fallback if older SQLite fails on constraints).
2. **Backend Chat Route Hardening ([app/routes/chat.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/routes/chat.py) & [app/services/chat_service.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/services/chat_service.py)):**
   - Resolve author identity properly in `post_chat_message()`: if `get_current_lecturer()` owns the session's course and `payload.get("as_lecturer")` (or no student cookie), attribute `lecturer.full_name` (`"sender_role": "lecturer"`); otherwise attribute the verified student (`"sender_role": "student"`).
   - Ensure `session.id` / `session_code` / `course_id` / `course_code` / `lecture_title` are included in `serialize_message()` + broadcast payloads.
3. **Socket.IO Room Membership & Leave/Switch ([app/sockets/session_events.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/sockets/session_events.py)):**
   - Keep blocking cross-lecturer snooping (`another_lecturer` -> `session:error`), while allowing students opening `/student` on active open sessions (`session.attendance_open`) or enrolled courses to join the classroom room cleanly.
   - Add `@socketio.on("leave_session")` so `student_app.html` leaves the old room before joining a newly clicked lecture chip.
4. **Frontend Chat Resilience & Fixes ([teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html), [student_app.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_app.html), [student_checkin.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_checkin.html)):**
   - Update `sendLecturerChat()` & `sendStudentChat()` to:
     - Check `if (!res.ok)` and show a clear error alert while restoring the user's unsent text.
     - Immediately merge the created JSON message into local `chatMessages` / `CHAT_MESSAGES` and re-render (deduped by `m.id` when `chat:new` arrives via Socket.IO).
   - Add HTTP `fetchLatestSessionChat()` alongside `fetchLatestSessionPolls()` in `student_app.html` and `teacher_portal.html` as a zero-drop safety net.
   - Fix export link (`/api/sessions/{{ session.id }}/chat/export`) and add `Enter`-to-send on `#lecturer-chat-body`.

---

## 2. Issue #2 — Real-Time Clickable Chat Notifications + Red Circle on Chrome Tab

### 2.1 Requirements Breakdown
- **Pop-Up Toast Every Time a Chat Message Is Sent:** Visible to both **Students** and **Lecturers** (suppressed only for the user who just sent that exact message).
- **Click-to-Navigate Directly to the Respective Lecture & Message:**
  - Works whether the user is on the *same* lecture (switches tab/view & scrolls to message), a *different tab* (e.g., Tab 1 Attendance -> jumps to Tab 3 Q&A), or a *different lecture / course* (navigates to that lecture & message).
- **Chrome Tab Visual Change (Red Circle Badge):**
  - If the lecturer (or student) is on a different Chrome tab or another workspace tab when a message arrives, the **Chrome browser tab icon displays a crisp red circle notification dot** (plus unread counter in tab title `● (N) ...`).

### 2.2 Technical Design
1. **Cross-Session Notification Delivery ([app/sockets/session_events.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/sockets/session_events.py), [app/routes/chat.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/routes/chat.py)):**
   - Currently Socket.IO emits `chat:new` only to `session_<session_code>`. If a professor teaches 3 lectures/courses, they won't hear messages from Lecture 2 while looking at Lecture 1 unless subscribed to a personal notification room!
   - **Solution:**
     - On `connect` / `join_notifications` in `session_events.py`:
       - Verified lecturers automatically join `lecturer_notify_<lecturer.id>`.
       - Verified students automatically join `course_notify_<course_id>` for each enrolled course.
     - Whenever a chat message is created (via REST `POST /api/sessions/<id>/chat` or Socket `chat:send`):
       - Emit `chat:new` to `session_<session_code>` (updates open chat feed).
       - Emit `chat:notify` with rich payload:
         ```json
         {
           "message_id": 42,
           "course_id": 1,
           "course_code": "CS204",
           "session_id": 3,
           "session_code": "ARIEL1",
           "lecture_title": "Lecture 1 — OOD",
           "author_name": "Alice Cohen",
           "sender_role": "student",
           "sender_student_id": 5,
           "sender_lecturer_id": null,
           "category": "Question",
           "body": "How does Bridge pattern help?",
           "offset_mmss": "+04:12"
         }
         ```
         to `lecturer_notify_<owner_id>` (plus any `CourseStaff` IDs) and `course_notify_<course_id>`.
2. **Shared Notification & Chrome Tab Red-Circle Module (`app/static/js/notifications.js`):**
   - **Floating Clickable Toast Stack:**
     - Fixed container (`bottom: 20px; right: 20px; z-index: 9999`), sleek dark card with accent border, displaying `[COURSE_CODE • SESSION_CODE] Author (Category)` + message snippet + `"Click to open lecture & message →"`.
     - Auto-dismisses after `8s` (stays visible on hover), close (`×`) button stops propagation.
   - **Click Handler (Deep Link to Lecture + Highlight Message):**
     - **On Lecturer Portal (`/teacher`):**
       - Same session (`payload.session_code === SESSION_CODE`): call `switchTab('tab-chat')`, ensure message exists in `chatMessages`, scroll `[data-msg-id="<id>"]` into view (`behavior: "smooth", block: "center"`), add pulsing `.msg-highlight` glow for 3 seconds, and clear unread badge.
       - Different course/lecture: navigate to `/teacher?course_id=${payload.course_id}&session=${payload.session_code}&tab=tab-chat&msg_id=${payload.message_id}`.
     - **On Student App (`/student`):**
       - If target course/session is in `STATE`: call `selectSession(targetSess, targetCourse)`, switch to `view-live`, scroll to `[data-msg-id="<id>"]`, highlight it, and clear unread badge.
       - Otherwise navigate to `/student?session=${payload.session_code}&view=view-live&msg_id=${payload.message_id}`.
     - Add URL startup deep-link handler in both `teacher_portal.html` and `student_app.html` reading `?tab=` / `?view=` / `?msg_id=` on page load so cross-lecture redirects land directly on the highlighted message!
   - **Dynamic Chrome Tab Red Circle Favicon + Title (`setTabUnreadBadge(count)`):**
     - Draws a `32×32` PNG favicon on an offscreen `<canvas>`:
       - Base icon: clean ClassPulse rounded blue square (`#0b1120` + `#38bdf8` pulse glyph).
       - **When `unreadCount > 0`:** draws a bright **solid red circle (`#ef4444`)** with white border at top-right (`radius 10px`, displaying count `1..9+` inside!) and updates `<link id="app-favicon" rel="icon" href="canvas.toDataURL('image/png')">` + prepends `(N) 🔴 ` to `document.title`!
       - Notice Chrome immediately displays that red circle directly on the browser tab even when the lecturer is on Gmail, slides, or another Chrome window!
     - **In-Page Red Indicators:**
       - Red circular dot + count on the `3. Live Classroom Q&A` tab button whenever viewing another tab (e.g., Tab 1 Attendance or Tab 5 AI).
       - Optional small red dot next to any sidebar lecture (`session-item`) or course (`course-item`) that received unread chat messages.
     - **Auto-Reset Rules:**
       - When the Chrome tab regains visibility (`visibilitychange` -> visible) AND the user is actively viewing Tab 3 / `view-live` for that session (or clicks the toast / opens Tab 3), `unreadCount` resets to `0`, restoring the clean default favicon and original `document.title`.

---

## 3. Issue #3 — Split Tab 5 Into 2 Separate Full-Screen Tabs

### 3.1 Why Split Tab 5
Currently `5. AI Lecture Studio & Homework` packs both **Lecture Recording Transcription & Summarizer** and **Homework Cohort Analytics** into a 50/50 `.grid-2` split inside one tab, cramping transcript reading, assignment rubrics, and submission review lists.

### 3.2 Target 6-Tab Workspace Layout ([teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html) & Partials)
Update `.tab-bar` in [teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html) to 6 clear tabs (wrapping cleanly on narrower screens with `flex-wrap: wrap`):
1. `1. Live Attendance & CV Headcount` (`#tab-attendance`)
2. `2. Live Lecture Polls` (`#tab-polls`)
3. `3. Live Classroom Q&A` (`#tab-chat` — includes `#chat-unread-badge` red circle dot)
4. `4. Course Overview & Roster` (`#tab-course-roster` — [teacher/_tab_roster.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher/_tab_roster.html))
5. **`5. AI Lecture Summarizer` (`#tab-ai-lectures` -> new partial `app/templates/teacher/_tab_lecture_ai.html`):**
   - Full-width layout dedicated exclusively to Feature 4:
     - Top bar: Google Gemini API status badge, recording file uploader (`.mp4/.m4a/.mp3/.wav/.vtt`), *"Upload, Transcribe & Summarize"*, *"Run Ariel Lecture Sample"*, live stage progress banner (`TRANSCRIBING` -> `SUMMARIZING` -> `COMPLETED`), and optional transcript paste box.
     - Wide workspace below (`2-column` or spacious stacked cards): full readable Lecturer Summary Markdown + `.md`/`.txt`/Delete buttons on the left, and expandable `[MM:SS]` corrected transcript timeline clearly visible with generous height (`max-height: 540px`) on the right.
6. **`6. Homework Cohort Analytics` (`#tab-homework` -> new partial `app/templates/teacher/_tab_homework.html`):**
   - Full-width layout dedicated exclusively to Feature 5:
     - Top assignment management bar (selector dropdown, `Seed Sample Cohort`, new assignment title + rubric creator).
     - Spacious cohort analytics dashboard (`mean score`, `submissions count`, `top error tags`, topic average bars).
     - Full-width student submission & comparative AI review table with large modal inspection.

### 3.3 Tab Navigation & Deep-Link Polish
- Update `switchTab(tabId, btnEl)` so passing `btnEl` is optional (finds matching `[data-tab-target="${tabId}"]` automatically — crucial when toast clicks or `?tab=tab-chat` programmatically switch tabs!).
- Support `?tab=...` query parameter initialization on load (replaces hardcoded Tab 1 active state when deep-linking).

---

## 4. Implementation & Verification Test Plan

### 4.1 Step-by-Step Build Order
1. **Fix DB Migration + Chat Backend:** Update `_migrate_sqlite_columns()` in [app/models.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/models.py) to drop legacy `synced_to_zoom` / `zoom_*` columns, enhance [app/routes/chat.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/routes/chat.py), [app/services/chat_service.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/services/chat_service.py), and [app/sockets/session_events.py](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/sockets/session_events.py) (`chat:notify` + notification rooms + `leave_session`).
2. **Create `app/static/js/notifications.js`:** Toast UI + click-to-jump deep link + HTML5 `<canvas>` Chrome tab red-circle favicon & title badge.
3. **Split Tab 5 & Fix Chat JS:** Create `app/templates/teacher/_tab_lecture_ai.html` and `app/templates/teacher/_tab_homework.html`; update [teacher_portal.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/teacher_portal.html), [student_app.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_app.html), and [student_checkin.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_checkin.html).

### 4.2 Automated & Manual Test Checklist
- **Automated `pytest` Tests:**
  1. **Legacy DB Migration Test:** Create a SQLite DB with legacy `chat_messages.synced_to_zoom BOOLEAN NOT NULL`, initialize `create_app()`, and verify `POST /api/sessions/<id>/chat` succeeds (`201`) instead of throwing `IntegrityError`.
  2. **Chat Attribution & Export Test:** Verify professor posts carry `Lecturer`'s real name, `/api/sessions/<id>/chat/export` returns `200`, and cross-lecturer `join_session` stays blocked while students browsing open sessions can receive chat.
  3. **Notification Payload Test:** Verify sending a chat message in Lecture 2 emits `chat:notify` to `lecturer_notify_<id>` and `course_notify_<cid>` containing `course_id`, `session_code`, `message_id`, and `lecture_title`.
  4. **Tab Split Rendering Test:** Verify `GET /teacher` renders both `#tab-ai-lectures` and `#tab-homework` as separate top-level tab panes and loads `/static/js/notifications.js`.
- **Live Browser Verification (Chrome + Docker):**
  1. Send message from Student App -> confirm toast pop-up appears on Lecturer dashboard + **red circle dot appears on Chrome tab** when on Tab 1 or switched to another Chrome tab!
  2. Switch Lecturer to Lecture 2 -> have Student post in Lecture 1 -> click toast on Lecturer portal -> confirm browser opens Lecture 1, switches to Tab 3, and highlights that exact message!
  3. Repeat in reverse (Lecturer sends announcement -> Student on Courses tab clicks toast -> opens Live Class & highlights message).
  4. Click Tab 5 (`AI Lecture Summarizer`) and Tab 6 (`Homework Cohort Analytics`) to confirm spacious full-width views.

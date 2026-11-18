# ClassPulse Complete UI Overhaul — Collaborative Planning & Design Document (M0)

**Status:** Completed & Verified (Milestones M0–M5 Complete)  
**Target Codebase:** `c:\Users\ohadb\Desktop\ALL\School\קורסים\מונחה עצמים\Ex1`  
**Stack Constraint (R3):** Python 3.12 · Flask 3.1.3 · Flask-SocketIO 5.7.2 · Jinja2 · Pure CSS3 · Vanilla JS  
**Baseline Regression Gate:** `python run_all_tests.py` — **46 / 46 tests passing** across 11 suites  

---

## 1. Executive Summary & Role Alignment

This document is the authoritative pre-implementation blueprint for the ClassPulse UI Overhaul (Requirement **R1**). It translates the visual design grammar of the two uploaded inspiration images (**R2**) onto every ClassPulse lecturer, student, and classroom display view while preserving 100% of existing Python/Flask/Jinja2/JS functionality, test contracts, and stack boundaries (**R3**).

No `.py`, `.html`, `.css`, or `.js` files are modified in Milestone M0. All five collaborating roles reviewed the codebase, inspiration assets, skills, and automated test suite, raised cross-role concerns, and signed off on the consensus below.

### 1.1 Role Contributions & Sign-Off Summary

| Role | Core Contribution | Primary Sign-Off Focus |
|---|---|---|
| **Head Staff / Coordinator** | Synthesized `ORIGINAL_REQUEST.md` (R1–R3) with all three codebase/design/QA surveys; partitioned M1–M4 into strictly non-overlapping file write ownership sets; resolved cross-role tradeoffs (`lecturer_dashboard.html` parity, broken report URL fix, CDN Socket.IO guard). | Zero file-ownership collisions across parallel milestones; full 8-view coverage; strict M0 -> M1 -> (M2 \|\| M3 \|\| M4) -> M5 execution gating. |
| **UI/UX Designer** | Extracted the visual system from Inspiration Images 1 & 2 into concrete CSS tokens (`#F4F2F0` alabaster-peach-periwinkle mesh, frosted glass shells, pure-white bento cards, 3D ClassPulse coral-lavender orb, dot-matrix card headers, pill navigation, WCAG AA ink) and defined the strict R2 exclusion list. | 1:1 cohesive visual language between Lecturer Desktop Web and Student Mobile Web; zero leftover wellness/meditation copy; high-contrast media islands for QR & CV canvas. |
| **Planner** | Flagged the direct R3 conflict in `SKILLS/python-only-web-ui.md` (lines 13–18), mapped `SKILLS/master-ui-designer.md` to Jinja2 `base.html` + shared CSS design system, and sequenced shared foundation (`M1`) ahead of view migrations (`M2–M4`). | R3 stack constraint preserved without introducing React/Vue/TS/Tailwind or Streamlit/Gradio; every feature mapped to a milestone. |
| **Implementer** | Audited all 12 templates (`~5,765` lines), 3 JS helpers (`csrf.js`, `notifications.js`, `headcount_canvas.js`), and inline JS HTML generators (`teacher_portal.html`, `student_app.html`, `student_checkin.html`); identified 4 pre-existing bugs/gaps and responsive risks. | Refactor JS template literals from hardcoded dark hexes (`#0f172a`, `#1e293b`) to semantic CSS classes; guard `io` when offline; fix report URLs and `#kpi-visual` sync. |
| **Tester** | Verified 46/46 baseline `pytest` pass (`run_all_tests.py`), cataloged all test-asserted DOM IDs/strings/tab orders/routes, and verified zero-install headless Chromium CDP (`chrome-headless-shell.exe` + `websockets 17.2` + `pillow 12.1.1`) for E2E & visual rubric inspection. | Immutable contract preservation across 11 `pytest` suites; zero console/JS errors; zero horizontal overflow across Desktop (`1440×900`, `1024×768`) and Mobile (`390×844`, `360×740`). |

### 1.2 Cross-Role Dialogue: Questions, Tradeoffs & Resolved Consensus

| # | Question Raised Between Roles | Tradeoff Considered | Resolved Team Consensus |
|---|---|---|---|
| **D1** | **Planner -> Coordinator/Tester:** `SKILLS/python-only-web-ui.md:13-18` forbids Flask + HTML/CSS/JS templates and mandates Streamlit/Gradio/Reflex/NiceGUI/Flet/Dash. Should we migrate? | Migrating to Streamlit/NiceGUI violates R3 (*"strictly with the resources and languages already present in the project"*), breaks Socket.IO rooms, breaks HTML5 canvas face-box clicking, and breaks all 46 tests. | **Override framework-swap rule in `python-only-web-ui.md`.** Retain Python + Flask/Jinja2 + HTML5 + CSS3 + Vanilla JS; keep zero Node/npm/React/Vue/TS/Tailwind CLI; apply `SKILLS/master-ui-designer.md` via Jinja2 `base.html` + `app/static/css/classpulse.css`. |
| **D2** | **Implementer -> Designer/Tester:** `lecturer_dashboard.html` is orphaned because `GET /` and `GET /lecturer` in `app/routes/attendance.py:58-67` redirect 302 to `/teacher`. Deprecate or restyle? | Deleting `lecturer_dashboard.html` risks failing R2's explicit list of 8 views; changing `/` redirect breaks navigation tests. | **Keep `/` and `/lecturer` 302 redirects intact** for route compatibility, **AND overhaul `lecturer_dashboard.html`** in M2 using `classpulse.css`, `csrf.js`, and `notifications.js` so all 8 named templates share identical visual parity. |
| **D3** | **Implementer -> Designer:** Moving to a light frosted-glass theme could wash out QR codes or `#00e676`/`#fbbf24` CV bounding boxes on `#headcount-canvas`. | Full-light QR cards without quiet zones or light frames behind bright green/amber canvas annotations reduce scannability and box legibility. | **Enforce High-Contrast Media Islands:** QR SVG containers remain strict `#FFFFFF` with `>=10px` quiet-zone padding; `#headcount-canvas` keeps a dark `#111827` media frame with crisp border/caption bar. |
| **D4** | **Tester -> Implementer:** `teacher_portal.html:521-522` links to `/api/sessions/{{ session.id }}/reports/attendance.xlsx` & `.csv`, which 404 against `attendance.py:643` (`/api/sessions/<id>/report?format=xlsx|csv`). Fix now? | Leaving broken download links undermines the live showcase; fixing the `href` requires no backend change and unlocks real report verification. | **Fix in M3:** Update both `<a>` links in `teacher_portal.html` to `/api/sessions/{{ session.id }}/report?format=xlsx` and `?format=csv`. |
| **D5** | **Implementer -> Tester:** `headcount_canvas.js:37-38` updates `#kpi-visual-count` (`lecturer_dashboard.html`), whereas `teacher_portal.html:444` uses `#kpi-visual`. | Renaming `#kpi-visual` in `teacher_portal.html` risks breaking existing hooks; ignoring it leaves KPI #3 stale during canvas edits until polling catches up. | **Update `headcount_canvas.js` in M1** to safely update both `#kpi-visual-count` and `#kpi-visual` (if present) while preserving the `onUpdate` callback. |
| **D6** | **Implementer -> Tester:** 6 templates load `https://cdn.socket.io/4.7.5/socket.io.min.js`. What if a headless or classroom network blocks CDN access? | Unconditional `const socket = io();` throws `ReferenceError: io is not defined`, halting subsequent JS initialization (`loadPolls`, chat, QR rotation). | **Guard all `io()` initialization in M1–M4** (`const socket = (typeof window.io === "function") ? window.io(...) : _createNoopSocket();`) so offline environments degrade cleanly to existing `2.5s`/`3.5s` polling with zero JS console errors. |
| **D7** | **Designer -> Implementer:** Desktop `teacher_portal.html` uses a 260–280px left sidebar. How does it behave at `<=980px` tablet/mobile without overlapping or huge vertical scrolling? | Always-visible full-height stacked sidebar pushes the 6 tabs below the fold; hiding it permanently traps lecturers on narrow viewports. | **Collapsible Mobile/Tablet Course Drawer (`<=980px`):** Convert the sidebar into a compact top course/session summary bar with a `"Courses & Lectures"` toggle pill button (`aria-expanded`) that smoothly opens/closes the drawer above the workspace. |

---

## 2. R3 Stack Constraint & Skill Conflict Analysis (CRITICAL)

### 2.1 Exact Conflict Evidence
Inspection of `ORIGINAL_REQUEST.md` (**R3**) and `SKILLS/python-only-web-ui.md` reveals an irreconcilable directive conflict if `python-only-web-ui.md` is applied literally:

- **`ORIGINAL_REQUEST.md` (R3):**
  > *"Use the skills in `SKILLS/` (`SKILLS/master-ui-designer.md` and `SKILLS/python-only-web-ui.md`) as tools/guidance, while working strictly with the resources and languages already present in the project (Python, Flask/Jinja2, HTML/CSS/JS). Do NOT introduce unfamiliar programming languages or external frontend frameworks that the user does not know and would not be able to code review. If any aspect of `SKILLS/python-only-web-ui.md` conflicts with preserving the existing project stack or makes a requirement unachievable within these parameters, flag it clearly."*
- **`SKILLS/python-only-web-ui.md` (lines 7, 13–18):**
  - Line 13: *"Strictly Python: Use pure Python libraries designed for web and UI development (e.g., Streamlit, Gradio, Reflex, NiceGUI, Flet, or Dash)."*
  - Line 14: *"No External Frontend Code: Do not write or suggest using JavaScript, TypeScript, HTML/CSS (unless strictly required within a Python wrapper), React, Angular, Vue, or Java."*
  - Line 18 (`Gotchas`): *"Do not accidentally drop into Flask/Django + HTML/JS templates. Stick strictly to Python-native UI libraries where the UI is written entirely in Python."*

### 2.2 Technical Impact if Followed Literally
Replacing Flask + Jinja2 + HTML/CSS/JS with Streamlit, Gradio, Reflex, NiceGUI, Flet, or Dash would:
1. Violate **R3** directly by introducing a new UI framework not present in the project (`requirements.txt` / `app/`).
2. Break **Flask-SocketIO** realtime session rooms (`qr:rotate`, `poll:activated`, `chat:notify`, `lecture:summary_ready`, `assignment:review_ready`).
3. Break custom **HTML5 `<canvas>`** coordinate click-to-add/remove face bounding-box editing (`app/static/js/headcount_canvas.js`) and dynamic browser tab favicon notification badges (`app/static/js/notifications.js`).
4. Break the global `cp_csrf` double-submit cookie/header enforcement (`app/static/js/csrf.js`) and HMAC `cp_device` / `cp_lecturer` cookie workflows.
5. Invalidate **all 11 existing `pytest` suites (46 tests)**, which inspect Flask test-client HTTP status codes, redirects, and Jinja2 HTML DOM IDs/strings.

### 2.3 Team Resolution
1. **Explicit Override of `SKILLS/python-only-web-ui.md` Framework Swap:** Retain ClassPulse's native **Python 3.12 + Flask 3.1.3 + Flask-SocketIO 5.7.2 + Jinja2 + HTML5 + CSS3 + Vanilla JS** stack.
2. **Spirit Preserved:** Honor `python-only-web-ui.md`'s anti-bloat intent by introducing **zero** Node.js build pipelines, **zero** npm packages, **zero** TypeScript, and **zero** external frontend frameworks (no React, Vue, Angular, Tailwind CLI, Bootstrap, or icon/font CDNs).
3. **Apply `SKILLS/master-ui-designer.md` (lines 13–19):** Follow its visual hierarchy, accessible hex palette, font pairing, UX justification, wireframe-to-polish workflow, and framework-adaptation rule (*"Tailor the 'master UI' advice to the capabilities of the specific tools they are using"*) through a Jinja2 layout hierarchy (`app/templates/layouts/base.html`) and a shared pure-CSS design system (`app/static/css/classpulse.css`).

---

## 3. Visual Design System Specification (R2 Aesthetic Transfer + Exclusions)

### 3.1 Aesthetic DNA Extracted from Inspiration Images 1 & 2
From direct visual inspection of `media_1794983697089_0b3a02ae.jpg` (Image 1: Modular Component Kit) and `media_1794983697134_81c56c90.jpg` (Image 2: Assembled 3-Column Desktop Shell):
1. **Ambient Warm Mesh Canvas:** Full-bleed soft warm alabaster-to-peach-to-periwinkle gradient backdrop (`#F4F2F0` base with soft radial peach glow at bottom-left and periwinkle-lavender glow at bottom-right).
2. **Frosted Glass Outer App Frame:** Translucent white container (`backdrop-filter: blur(22px)` with high-opacity fallback) framed by crisp `1.5px` semi-opaque white edges and `26px` rounded corners.
3. **Crisp Elevated White Bento Cards:** Clean `#FFFFFF` / `rgba(255,255,255,0.92)` cards (`20px` radius, hairline border, soft ambient elevation shadow) creating clear modular grouping.
4. **White-Pill Active Navigation & Segmented Switchers:** Left sidebar active rows and horizontal tab switchers render as raised pure-white pills (`border-radius: 999px`) with subtle drop shadow, coral-tinted active icon, and crisp charcoal ink.
5. **Signature 3D ClassPulse Orb & Radial Dot-Matrix Motif:** Pure-CSS glowing peach-coral-lavender sphere emblem (`32px–44px`) paired with a subtle `12px` radial halftone dot-matrix texture behind AI/Q&A header regions.
6. **Accessible Typography & Tabular Numerals:** Crisp charcoal ink (`#181A1F`), tracked uppercase micro-labels (`10px–11px`, `700`, `0.08em`) for sidebar groups/eyebrows, and `font-variant-numeric: tabular-nums` on every KPI, countdown timer, `[MM:SS]` badge, poll percentage, and rubric score.

### 3.2 Authoritative CSS Custom Property Tokens (`app/static/css/classpulse.css`)

```css
:root {
  /* Ambient Backdrop & Glass Surfaces */
  --cp-bg-base: #F4F2F0;
  --cp-bg-mesh:
    radial-gradient(62% 58% at 10% 92%, rgba(245, 208, 195, 0.72) 0%, rgba(245, 208, 195, 0) 68%),
    radial-gradient(60% 62% at 92% 90%, rgba(215, 206, 242, 0.76) 0%, rgba(215, 206, 242, 0) 70%),
    radial-gradient(50% 45% at 50% 10%, rgba(255, 255, 255, 0.85) 0%, rgba(244, 242, 240, 0) 72%),
    #F4F2F0;
  --cp-shell-bg: rgba(255, 255, 255, 0.70);
  --cp-shell-bg-fallback: rgba(255, 255, 255, 0.92);
  --cp-shell-border: 1.5px solid rgba(255, 255, 255, 0.88);
  --cp-shell-shadow: 0 24px 60px rgba(31, 34, 51, 0.08), 0 4px 16px rgba(31, 34, 51, 0.04);

  /* Bento Cards, Wells & Borders */
  --cp-surface: rgba(255, 255, 255, 0.92);
  --cp-surface-solid: #FFFFFF;
  --cp-well: #F7F6F9;
  --cp-well-hover: #EFEFF5;
  --cp-border: rgba(24, 26, 31, 0.09);
  --cp-border-strong: rgba(24, 26, 31, 0.16);
  --cp-card-shadow: 0 10px 28px rgba(31, 34, 51, 0.055), 0 2px 6px rgba(31, 34, 51, 0.03);

  /* Typography Ink (WCAG AA >= 4.5:1 on white/frosted surfaces) */
  --cp-ink: #181A1F;
  --cp-ink-secondary: #434957;
  --cp-ink-muted: #555D6E;

  /* Brand & Academic Accents */
  --cp-coral: #E87148;
  --cp-coral-hover: #D45D34;
  --cp-coral-soft: rgba(232, 113, 72, 0.13);
  --cp-indigo: #5B5FEF;
  --cp-indigo-hover: #474BDB;
  --cp-indigo-soft: rgba(91, 95, 239, 0.12);
  --cp-lavender-tint: rgba(215, 206, 242, 0.45);
  --cp-peach-tint: rgba(245, 208, 195, 0.48);

  /* Semantic Status Tokens */
  --cp-green: #16A34A;
  --cp-green-ink: #15803D;
  --cp-green-soft: rgba(22, 163, 74, 0.12);
  --cp-amber: #D97706;
  --cp-amber-ink: #B45309;
  --cp-amber-soft: rgba(217, 119, 6, 0.13);
  --cp-red: #DC2626;
  --cp-red-ink: #B91C1C;
  --cp-red-soft: rgba(220, 38, 38, 0.11);

  /* High-Contrast Media Islands */
  --cp-qr-bg: #FFFFFF;
  --cp-cv-frame-bg: #111827;

  /* Radii & Spacing Scale */
  --cp-radius-shell: 26px;
  --cp-radius-card: 20px;
  --cp-radius-well: 14px;
  --cp-radius-pill: 999px;

  /* Font Stacks */
  --cp-font-sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --cp-font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
```

#### Signature Pure-CSS Motifs (Zero External Images/Fonts)
- **ClassPulse 3D Orb (`.cp-orb`, `.cp-orb--sm`, `.cp-orb--lg`):**
  ```css
  .cp-orb {
    border-radius: 50%;
    background: radial-gradient(circle at 32% 28%,
      #FFF4ED 0%,
      #FFBE9E 26%,
      #E87148 60%,
      #B9A6EB 95%);
    box-shadow:
      inset -4px -6px 12px rgba(91, 95, 239, 0.25),
      inset 3px 3px 8px rgba(255, 255, 255, 0.75),
      0 8px 20px rgba(232, 113, 72, 0.28);
  }
  ```
- **Radial Halftone Dot-Matrix Header (`.cp-dot-header`):**
  ```css
  .cp-dot-header {
    background-image: radial-gradient(rgba(24, 26, 31, 0.08) 1px, transparent 1px);
    background-size: 12px 12px;
    mask-image: radial-gradient(ellipse at 50% 35%, black 25%, transparent 80%);
  }
  ```
- **Backdrop-Filter Fallback:** Use `@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))` to apply `--cp-shell-bg-fallback: rgba(255, 255, 255, 0.92)` so all text remains crisp on low-GPU or older browsers.
- **Inline SVG Icon Macros (`app/templates/macros/icons.html`):** Thin `1.75px` stroke Lucide-inspired inline SVGs (zero icon CDN requests) replacing raw Unicode emojis in navigation, tabs, buttons, and KPI labels.

### 3.3 Strict R2 Exclusion List (FORBIDDEN Content & Widgets)
The following copy, personas, categories, and widgets from Inspiration Images 1 & 2 **MUST NOT** appear anywhere in ClassPulse HTML, CSS, JS, or copy:
- **Brand & Personas:** `"Sense"`, `"Andrew"`, `"Sara Lee"`, `"Alex Morgan"`, `"Daniel Cru"`.
- **Wellness / Meditation Copy:** `"Mindfulness"`, `"Focus"`, `"Relaxation"`, `"Meditate"`, `"Sleep"`, `"Move"`, `"Music"`, `"Ease into Sleep"`, `"Mindful Moments"`, `"Breath of Balance"`, `"Calm Flow Journey"`, `"5 Min"`, `"Good Morning Andrew, What's on your mind?"`.
- **Unrelated Navigation / Widgets:** `"My Sessions"`, `"Popular Sessions"`, `"Community"`, `"Download"`, `"Premium Access"`, `"Shared Sessions"`, `"Saved Practices"`, `"Recommended"`, audio play buttons, bookmark ribbon buttons, thumbs-up/down AI rating icons on fake replies, microphone voice-dictation icons, and non-functional `"Search..."` inputs (no search bar added unless wired to real functionality; ClassPulse has direct course/lecture/tab navigation).

### 3.4 High-Contrast Media Islands
1. **QR Code Cards (`teacher_portal.html` `#dash-qr-img`, `lecturer_dashboard.html` `#qr-img`, `projector.html` `#proj-qr`):** Strict `#FFFFFF` background with `padding >= 12px` (`>= 24px` on projector) quiet zone and `1px solid var(--cp-border)` frame so smartphone cameras decode instantly.
2. **Computer Vision Face-Counter Canvas (`#headcount-canvas`):** Enclosed in a dark `#111827` media frame (`.cp-cv-stage`, `border-radius: 16px`, subtle shadow) so `#00e676` (auto-detected) and `#fbbf24` (manual-added) bounding boxes remain high-contrast over classroom photography.

---

## 4. Complete 8-View + Tabs + Modals Mapping

### 4.1 View-by-View Mapping Table (Desktop Lecturer Web & Matching Student Mobile)

| # | Template / Module | Route(s) | Target Viewport(s) | Structural & Visual Overhaul Mapping |
|---|---|---|---|---|
| **1** | `portal_home.html` | `GET /portal` | Desktop `1440×900`, Mobile `390×844` | Centered frosted-glass hero card on ambient mesh backdrop with ClassPulse 3D orb (`44px`), pill eyebrow (`Ariel University • School-Wide Classroom Platform`), crisp H1 (`ClassPulse Multi-Course Hub`), and 2 elevated white bento portal cards (`Teacher & Lecturer Portal` -> `/teacher` with coral CTA pill; `Student Classroom App` -> `/student` with indigo/green CTA pill). Stacks to 1 column at `<720px`. |
| **2** | `teacher_login.html` | `GET /teacher/login`, `?setup=1` | Desktop `1440×900`, Mobile `390×844` | Centered frosted shell (`max-width: 520px`) with 3D orb brand lockup, 3-step pill stepper (`#pill-step-1`, `#pill-step-2`, `#pill-step-3`), segmented white-pill auth switcher (`#tab-btn-signin`, `#tab-btn-register`), light `#F7F6F9` inputs, forgot/reset password accordions, Step 2 Class Setup (`#card-step-2`), and existing courses quick-launch cards (`#my-courses-box`). High-contrast tinted alert banners for `#login-error` / `#login-ok` / `#course-error`. |
| **3** | `teacher_portal.html` + 4 `teacher/_*.html` partials | `GET /teacher` | Desktop `1440×900` (primary), `1024×768`, Mobile `390×844` | **Assembled 2-Zone Frosted Desktop Shell (`>= 980px`: `268px` glass sidebar + `1fr` workspace; `<= 980px`: collapsible mobile drawer toggle bar):**<br>• **Sidebar**: ClassPulse 3D orb + wordmark, `MY COURSES` micro-label + `+ Course` pill, white-pill active course card, `LECTURES` micro-label + `+ Lecture` pill, white-pill active lecture row, bottom lecturer card (`Security`, `Switch Lecturer`, `Sign Out`).<br>• **Topbar**: Course code pill + Lecture H1 + `LIVE`/`CLOSED` semantic pill + `#top-enrolled-count` + `#top-tunnel-url` + `Launch Projector QR`, `Q&A Pop-Out`, `Preview Student App` pill actions.<br>• **Segmented Tab Bar**: White-pill active tab across all 6 tabs (preserving exact labels & ordering). |
| **3a** | `teacher_portal.html` **Tab 1 (`#tab-attendance`)** | Tab 1 | Desktop & Tablet | 4 bento KPI tiles (`#kpi-roster`, `#kpi-qr`, `#kpi-visual`, `#kpi-delta` with `tabular-nums`) -> 2-column bento grid: Left `#FFFFFF` 5s QR card (`#dash-qr-img`, `#dash-qr-progress`, `#dash-bucket`, `#qr-countdown-badge`, `#dash-scan-url` in light pill well) + Right CV Headcounter (`#audit-badge`, `#audit-explanation`, `#photo-input`, `#cv-status`, `#headcount-canvas` inside `#111827` frame, `#snap-auto-count`, `#snap-verified-count`) -> Attendance Log card with `overflow-x: auto` table wrapper and **fixed** `.xlsx`/`.csv` download links. |
| **3b** | `teacher_portal.html` **Tab 2 (`#tab-polls`)** | Tab 2 | Desktop & Tablet | Frosted 1-Click Instant Polls action bar -> 2-column grid: Left Custom Poll Builder (`#poll-question`, `#poll-options`, `#poll-correct-idx`) + Lecture Poll Bank (`#poll-bank-list`) + Right Mentimeter-style Live Poll Stage (`#live-poll-status-pill`, `#live-poll-title`, `#live-poll-vote-num`, `#btn-close-poll`, `#live-poll-bars`, `#poll-sync-indicator`) on crisp white bento card with colorful coral/indigo/green/amber bars. |
| **3c** | `teacher_portal.html` **Tab 3 (`#tab-chat`)** | Tab 3 | Desktop & Tablet | Dot-matrix header accent card (`#tab-chat-badge` preserved) + pill composer (`#lecturer-chat-cat`, `#lecturer-chat-body`) + elevated white message cards (`[data-msg-id]`) with coral `[MM:SS]` badges, upvote/pin pills, and warm-amber pinned highlight. |
| **3d** | `teacher/_tab_lecture_ai.html` **Tab 4 (`#tab-ai-lectures`)** | Tab 4 | Desktop & Tablet | Gemini API status pill (`VERIFIED` green / `UNDER_SCANNED` amber) + `"Run Ariel Lecture Sample"` pill button + 2-col `#F7F6F9` wells for media upload (`#lecture-media-input`) & manual transcript (`#lecture-transcript-input`) + Published Study Guides list (`#lecture-artifacts-list`). |
| **3e** | `teacher/_tab_homework.html` **Tab 5 (`#tab-homework`)** | Tab 5 | Desktop & Tablet | Assignment Creator & Selector well (`#hw-new-title`, `#hw-new-rubric`, `#hw-assignment-select`) + 3 Cohort KPI bento tiles (`#hw-cohort-kpis`) + error/category tag pills (`#hw-top-tags`) + responsive submissions table (`#hw-submissions-list`). |
| **3f** | `teacher/_tab_roster.html` **Tab 6 (`#tab-course-roster`)** | Tab 6 | Desktop & Tablet | Left column: CSV/Excel dashed light dropzone (`#roster-file-input`, `#roster-import-msg`) + Staff/TA access card (`#staff-identifier`, `#staff-role`, `#course-staff-list`). Right column: Course Attendance Monitor table (`#course-students-tbody`, `overflow-x: auto`, `"Remove Student"` action pills). |
| **3g** | **4 Modals** (`#modal-new-course`, `#modal-new-lecture`, `#modal-hw-review`, `#modal-account-security`) | Overlays | Desktop & Mobile | Frosted backdrop (`rgba(24, 26, 31, 0.35)` + `blur(8px)`) + crisp `#FFFFFF` modal cards (`22px` radius, soft elevation shadow), clean light `#F7F6F9` inputs, and pill close/submit buttons. `_modal_security.html` preserves `#sec-cur-pw`, `#sec-new-pw`, `#sec-confirm-pw`, `#security-audit-list`. |
| **4** | `lecturer_dashboard.html` | `GET /`, `GET /lecturer` (redirect to `/teacher`; template retained for R2 parity) | Desktop `1440×900` | Restyled with `classpulse.css`, `csrf.js`, and `notifications.js` (frosted header, 4 bento KPIs including `#kpi-visual-count`, `#FFFFFF` QR card, `#111827`-framed `#headcount-canvas[data-session-id]` + `#canvas-empty-hint`, live polls, Q&A, attendance table). Preserves `/` and `/lecturer` redirects intact. |
| **5** | `chat_popout.html` | `GET /lecturer/chat-popout/<session_code>` | Desktop `1440×900`, Compact `480×800` | Compact `100vh` frosted-glass companion window with dot-matrix header (`"Live Q&amp;A Companion Feed"` preserved, session pill, `Export .txt` pill) + scrollable `#feed` of white elevated `.msg` cards (`[data-msg-id]`, coral `[MM:SS]` `.ts`, `.cat` pill, green `+N` upvote pill, warm-amber `.msg.pinned`). |
| **6** | `projector.html` | `GET /projector/<session_code>` | Projector `1920×1080`, `1280×720` | Ambient warm-alabaster-to-pearl studio canvas (`>= 7:1` contrast dark charcoal `#181A1F` title `>= 36px`), centered `#FFFFFF` quiet-zone QR card (`420px`, `24px` padding, elevated shadow), coral-to-indigo 5s countdown bar (`#proj-timer`), and oversized tabular counter pill (`#proj-count` `"Students Checked In"`). |
| **7** | `student_app.html` | `GET /student` | Mobile `390×844`, `360×740`, Desktop `1440×900` | **Mobile-First Frosted Shell (`max-width: 600px`):**<br>• **Sticky Glass Topbar**: 3D mini orb + `"ClassPulse Student App"` + `#student-identity-line` + `Lecturer` pill link.<br>• **Optional Sign-In Card (`#login-card`)**: Responsive grid stacking cleanly to 1 column at `<480px`.<br>• **Session Strip**: `#active-session-pill`, `#active-session-label`, `#live-attendance-box`.<br>• **3-Segment White-Pill Mode Switcher**: `#tab-btn-live` (`#poll-live-dot`), `#tab-btn-classes`, `#tab-btn-study` (`#study-new-dot`).<br>• **Mode 1 (`#view-live`)**: Auto-expanding Live Poll Stage (`#poll-stage-card`, `48px`-tall tap-friendly `.poll-opt` pills with smooth fill bars) + Q&A composer (stacks cleanly at `<480px`) & `#student-chat-feed`.<br>• **Mode 2 (`#view-classes`)**: Join-by-code pill card (`#join-code-input`) + white enrolled course cards (`#courses-list`) with clickable `.session-chip` pills & attendance logs.<br>• **Mode 3 (`#view-study`)**: `"AI Study Hub"` header + `#student-lecture-summaries` + `#student-assignments-list` with 2-col `AI Rubric Grade /100` vs `Class Standing /10` tiles, category comparison bars, telemetry pills, Markdown review, and resubmission drawer. |
| **8** | `student_checkin.html` | `GET /s/<session_code>?t=&sig=` | Mobile `390×844`, `360×740` | **Mobile Frosted Shell (`max-width: 560px`) covering all 4 states:**<br>1. **Error States (403/404/410/409)**: Soft-red `#DC2626`-tinted high-contrast error card.<br>2. **First-Time Onboarding (`checked_in=False`)**: 3D orb + `"First-Time Student Sign-In"` header + session pill + clean light `#F7F6F9` inputs (`full_name`, `student_id_number`, `email`) + coral/green primary submit CTA.<br>3. **Checked-In / Auto-Verified (`checked_in=True`)**: Green-tinted verified header (`"Attendance Verified!"` + `student.full_name`) + `Full Student App ->` pill + `#poll-stage-card` + Q&A stream + Enrolled Classes & Logs stack. |

### 4.2 Refactoring JS-Generated Inline HTML Strings & Shared JS Modules
Survey 1 & Survey 2 identified that dynamic JS renderers currently embed hardcoded dark hex colors (`#0f172a`, `#1e293b`, `#f8fafc`, `#cbd5e1`, `#38bdf8`) inside template literals. All four files below **MUST** replace inline dark styles with semantic `classpulse.css` classes:

1. **`app/static/js/notifications.js` (`M1`):**
   - Replace `#cp-notify-styles` (`:17-68`) with light frosted `#FFFFFF` toast card, `4px` coral/red left accent (`#E87148`/`#DC2626`), dark charcoal ink (`#181A1F`), subtle shadow (`0 14px 32px rgba(31,34,51,0.14)`), and light-compatible `.cp-msg-flash` highlight (`rgba(232, 113, 72, 0.14)` + `2px solid #E87148`).
   - Update `toast.innerHTML` (`:210-223`) to use semantic classes / light-theme contrast (`#E87148` header, `#555D6E` close/category, `#15803D` CTA link).
   - Update `_drawFavicon` (`:96-110`) to a clean coral-on-white or crisp slate-on-white rounded icon that matches the ClassPulse orb/pulse palette while keeping the `#dc2626` unread count circle.
2. **`app/static/js/headcount_canvas.js` (`M1`):**
   - Preserve `#00e676` / `#fbbf24` canvas strokes (`:26-34`) for contrast against classroom photos.
   - Update both `#kpi-visual-count` and `#kpi-visual` (`:37-38`) cleanly when present.
3. **`app/templates/teacher_portal.html` (`M3`, lines `1044–1668`):**
   - Refactor `renderOverview` / `refreshOverview`, `renderPollBank`, `renderLivePoll`, `renderChatFeed`, `renderLectureArtifacts`, `renderSelectedAssignment`, `renderTeacherReviewHtml`, `openReviewModal`, and `loadAuditLogs` to emit `.cp-well`, `.cp-poll-item`, `.cp-poll-bar`, `.cp-chat-msg`, `.cp-chat-msg--pinned`, `.cp-artifact-card`, `.cp-review-md`, `.cp-badge-*` instead of `#0f172a`/`#1e293b`/`#f8fafc`.
4. **`app/templates/student_app.html` (`M4`, lines `647–1287`) & `app/templates/student_checkin.html` (`M4`, lines `247–464`):**
   - Refactor `renderCourses`, `renderStudentPoll`, `renderStudentChat`, `renderStudyLectures`, `renderTelemetryPills`, `renderCategoryBreakdownHtml`, `renderSimpleReviewHtml`, `buildAssignmentCardElement`, `setHomeworkStatus`, and `notifyNewAssignment` to emit `.cp-course-card`, `.cp-log-row`, `.cp-poll-opt`, `.cp-hw-card`, `.cp-metric-tile`, `.cp-cat-bar`, `.cp-review-box`, `.cp-chat-msg`, and semantic status classes.

---

## 5. Pre-Existing Gaps, Feasibility Notes & Technical Decisions

| # | Gap / Observation | Verified Location | Approved Resolution & Target Milestone |
|---|---|---|---|
| **1** | **Broken Excel/CSV Report Links (404)** | `app/templates/teacher_portal.html:521-522` links to `/api/sessions/{{ session.id }}/reports/attendance.xlsx` & `.csv`, whereas `app/routes/attendance.py:643` serves `/api/sessions/<int:session_id>/report?format=xlsx|csv`. | Fix both `href` URLs in `teacher_portal.html` (M3) to `/api/sessions/{{ session.id }}/report?format=xlsx` and `?format=csv`. |
| **2** | **Orphaned `lecturer_dashboard.html`** | `app/routes/attendance.py:58-67` redirects `GET /` and `GET /lecturer` (302) to `courses.teacher_portal`. | Keep `/` and `/lecturer` redirects untouched (backwards compatibility) AND restyle `lecturer_dashboard.html` in M2 with `classpulse.css`, `csrf.js`, and `notifications.js` for 100% R2 parity. |
| **3** | **`headcount_canvas.js` KPI ID Mismatch** | `app/static/js/headcount_canvas.js:37-38` queries `#kpi-visual-count` (`lecturer_dashboard.html`), missing `#kpi-visual` (`teacher_portal.html:444`). | Update `headcount_canvas.js` in M1 to update both `#kpi-visual-count` and `#kpi-visual` when present, keeping `onUpdate` intact. |
| **4** | **External `socket.io.min.js` CDN Risk** | 6 templates load `https://cdn.socket.io/4.7.5/socket.io.min.js`; offline/restricted environments would throw `ReferenceError: io is not defined`. | Add a lightweight `typeof window.io === "function"` guard + noop socket shim across all 6 templates (M2–M4) so REST polling (`2.5s`/`3.5s`) continues seamlessly with zero JS console errors. |
| **5** | **Desktop & Mobile Responsive Overflow Risks** | `teacher_portal.html` 280px sidebar stacks awkwardly at `<=980px`; wide tables lack `overflow-x: auto`; `student_app.html` `.chat-composer` (`105px 1fr auto`) and `#login-card` (`1fr 1fr auto`) squeeze on `360px` screens. | In M1/M3/M4: add collapsible mobile sidebar drawer (`<=980px`), wrap all data tables in `.cp-table-scroll { overflow-x: auto; }`, and stack `.cp-composer` / `#login-card` cleanly at `<=480px`. |
| **6** | **Inline HTML at `GET /api/docs`** | `app/__init__.py:166-181` serves a small dark Swagger-UI-like inline HTML string. | **Explicitly out of scope:** `GET /api/docs` is developer OpenAPI documentation, not one of the 8 user-facing views in R2, and `app/__init__.py` remains untouched. |

---

## 6. Immutable Contracts & Regression Preservation Matrix

Implementers in **M1–M4** **MUST** preserve every item in this matrix verbatim. Changing any item below without an explicit, justified update approved by Coordinator + Tester will break existing automated tests or JS integrations.

### 6.1 Flask HTTP Routes, Redirects & Status Codes (`app/routes/*.py` — Read-Only)
- `GET /portal` -> `200` (`portal_home.html`)
- `GET /` & `GET /lecturer` -> `302` redirect to `/teacher`
- `GET /teacher/login` (`?setup=1`) -> `200` (`teacher_login.html`)
- `GET /teacher` -> `302` `/teacher/login` (unauth); `302` `/teacher/login?setup=1` (0 courses); `403` (cross-tenant `?course_id`/`?session`); `200` (`teacher_portal.html`)
- `GET /lecturer/chat-popout/<session_code>` -> `200` (`chat_popout.html`), `302` (unauth), `403` (cross-tenant)
- `GET /projector/<session_code>` -> `200` (owner cookie or `?ptok=`), `403` (cross-tenant)
- `GET /student` -> `200` (`student_app.html`)
- `GET /s/<session_code>` -> `200` (onboarding or checked-in), `403`/`404`/`410` error states (`student_checkin.html`)

### 6.2 Immutable DOM IDs, Attributes, Scripts & Test-Asserted Strings

| Template | Required DOM IDs / Selectors | Required Exact Strings & Ordering | Forbidden Strings / IDs | Required `<script>` Assets |
|---|---|---|---|---|
| **`teacher_portal.html` & `teacher/_*.html`** | `#tab-attendance`, `#tab-polls`, `#tab-chat`, `#tab-ai-lectures`, `#tab-homework`, `#tab-course-roster`, `#tab-chat-badge`, `#tab-btn-course-monitor`, `[data-tab-target]`, `#roster-file-input`, `#headcount-canvas`, `#dash-qr-img`, `#dash-qr-progress`, `#dash-bucket`, `#qr-countdown-badge`, `#dash-scan-url`, `#kpi-roster`, `#kpi-qr`, `#kpi-visual`, `#kpi-delta`, `#audit-badge`, `#audit-explanation`, `#photo-input`, `#cv-status`, `#snap-auto-count`, `#snap-verified-count`, `#attendance-tbody`, `#top-enrolled-count`, `#top-tunnel-url`, `#poll-question`, `#poll-options`, `#poll-correct-idx`, `#poll-bank-list`, `#live-poll-status-pill`, `#live-poll-title`, `#live-poll-vote-num`, `#btn-close-poll`, `#live-poll-bars`, `#poll-sync-indicator`, `#lecturer-chat-cat`, `#lecturer-chat-body`, `#lecturer-chat-feed`, `[data-msg-id]`, `#lecture-media-input`, `#lecture-ai-status`, `#lecture-transcript-input`, `#lecture-artifacts-list`, `#hw-new-title`, `#hw-new-rubric`, `#hw-assignment-select`, `#hw-cohort-kpis`, `#hw-top-tags`, `#hw-submissions-list`, `#roster-import-msg`, `#staff-identifier`, `#staff-role`, `#course-staff-list`, `#course-students-tbody`, `#modal-new-course`, `#modal-new-lecture`, `#modal-hw-review` (`#modal-hw-review-title`, `#modal-hw-review-body`), `#modal-account-security` (`#sec-cur-pw`, `#sec-new-pw`, `#sec-confirm-pw`, `#security-audit-list`) | Strict tab order:<br>`"4. AI Lecture Summarizer"` <br>**before**<br>`"5. Homework Cohort Analytics"` <br>**before**<br>`"6. Course Attendance Monitor"`<br>Plus: `"Remove Student"`, active `course_code`, active `session.title` | `id="tab-ai-hub"`, `"Course Overview &amp; Roster"`, `"Reset Device"`, `"/chat/export.txt"` | `/static/js/csrf.js`<br>`/static/js/notifications.js`<br>`/static/js/headcount_canvas.js`<br>Socket.IO client (guarded) |
| **`teacher_login.html`** | `#pill-step-1`, `#pill-step-2`, `#pill-step-3`, `#card-step-1`, `#card-step-2`, `#tab-btn-signin`, `#tab-btn-register`, `#panel-signin`, `#panel-register`, `#forgot-box`, `#reset-confirm-box`, `#login-identifier`, `#login-password`, `#login-remember`, `#forgot-identifier`, `#reset-token`, `#reset-new-pw`, `#reg-name`, `#reg-username`, `#reg-email`, `#reg-dept`, `#reg-password`, `#reg-confirm`, `#setup-course-code`, `#setup-course-name`, `#setup-lecture-title`, `#login-error`, `#login-ok`, `#course-error`, `#my-courses-box`, `#my-courses-list`, `#btn-open-existing` | Auth/setup labels and status messages | — | `/static/js/csrf.js` |
| **`lecturer_dashboard.html`** | `#kpi-roster-count`, `#kpi-qr-count`, `#kpi-visual-count`, `#kpi-delta`, `#qr-img`, `#qr-progress`, `#qr-url-link`, `#qr-bucket-badge`, `#toggle-open-btn`, `#headcount-canvas[data-session-id]`, `#canvas-empty-hint`, `#live-poll-monitor`, `#poll-bank-list`, `#lecturer-chat-input`, `#lecturer-chat-feed`, `#attendance-tbody` | Existing dashboard headings & controls | — | `/static/js/csrf.js`<br>`/static/js/notifications.js`<br>`/static/js/headcount_canvas.js`<br>Socket.IO client (guarded) |
| **`chat_popout.html`** | `#feed`, `[data-msg-id]`, `.msg`, `.msg.pinned` | `"Live Q&amp;A Companion Feed"` | — | Socket.IO client (guarded) |
| **`projector.html`** | `#proj-qr`, `#proj-timer`, `#proj-count` | `session.title`, `"Students Checked In"` | — | Socket.IO client (guarded) |
| **`student_app.html`** | `#student-identity-line`, `#login-card` (`#login-name`, `#login-sid`, `#login-email`, `#login-course`), `#active-session-pill`, `#active-session-label`, `#live-attendance-box`, `#tab-btn-live`, `#tab-btn-classes`, `#tab-btn-study`, `#poll-live-dot`, `#study-new-dot`, `#view-live`, `#view-classes`, `#view-study`, `#poll-stage-card`, `#poll-header-title`, `#student-poll-badge`, `#student-poll-box`, `#chat-room-code`, `#student-chat-cat`, `#student-chat-body`, `#student-chat-feed`, `[data-msg-id]`, `#join-code-input`, `#courses-list`, `#study-course-code`, `#student-lecture-summaries`, `#student-assignments-list`, `[data-hw-card]`, `[data-hw-sig]`, `#hw-code-<id>`, `#hw-git-<id>`, `#hw-file-<id>`, `#hw-btn-<id>`, `#hw-status-<id>` | `"ClassPulse Student App"`, logged-in student `full_name`, `"AI Study Hub"`, 4 category labels (`"Materials & Details"`, `"Execution Style"`, `"Technical Logic & Architecture"`, `"Testing & Verification"`) | — | `/static/js/csrf.js`<br>`/static/js/notifications.js`<br>Socket.IO client (guarded) |
| **`student_checkin.html`** | `#poll-stage-card`, `#student-poll-badge`, `#student-poll-box`, `#chat-category`, `#chat-body`, `#student-chat-feed`, `[data-msg-id]`, onboarding `<form method="post" action="/api/attendance/checkin">` (`session_code`, `onboarding_grant`, `t`, `sig`, `device_id`, `full_name`, `student_id_number`, `email`) | `"First-Time Student Sign-In"` (onboarding form state), `"Attendance Verified!"` + student `full_name` (checked-in state) | — | `/static/js/csrf.js`<br>`/static/js/notifications.js`<br>Socket.IO client (guarded) |
| **`portal_home.html`** | Links `/teacher`, `/student` | `"ClassPulse Multi-Course Hub"` | — | None required |

---

## 7. Verification & Agent-as-Judge Visual Rubric Plan

### 7.1 Automated Regression Gate
- Run `python run_all_tests.py` (`pytest -v` across all 11 suites in `tests/`) after M1, M2, M3, M4, and as the final gate in M5.
- **Required Result:** **46/46 existing tests PASS** (`exit code 0`, zero skips, zero unjustified test edits) PLUS new E2E UI verification tests passing at 100%.

### 7.2 Zero-Install Headless Chromium CDP & Opaque-Box E2E Suite (Tiers 1–5)
Verified available on the host machine without installing new Node/Python packages:
- **Browser Binary:** `%LOCALAPPDATA%\ms-playwright\chromium_headless_shell-1217\chrome-headless-shell-win64\chrome-headless-shell.exe`
- **CDP Driver & Image Check:** Python `websockets 17.2` + `pillow 12.1.1`
- **Test Fixture Setup:** Isolated temporary SQLite `create_app(...)` server on `127.0.0.1:<free_port>` seeded with a lecturer, 2 courses, 2 lecture sessions, enrolled students, active poll, timestamped chat messages (including pinned), headcount snapshot, AI lecture summary artifact, and homework assignment/submission.

#### E2E Verification Tiers
- **Tier 1 (Route & Server Cleanliness):** HTTP `200`/`302` checks across all 8 views + error states (`403`, `404`, `410`) with zero `500`s and zero `python-only` R3 violations (zero `.tsx`, `.jsx`, `package.json`, `node_modules`).
- **Tier 2 (Runtime & Network Cleanliness via CDP):** `Runtime.enable`, `Log.enable`, `Network.enable` — assert **0** `Runtime.exceptionThrown`, **0** `console.error`, **0** failed local HTTP requests (`status >= 400` outside intentional error-state tests), and all visible `<img>` elements loaded (`complete === true && naturalWidth > 0`).
- **Tier 3 (Interactive Flows Across All Views, Tabs & Modals):**
  - `teacher_login`: toggle Sign In / Register / Forgot Password, complete login.
  - `teacher_portal`: click through all **6 tabs** (`#tab-attendance`, `#tab-polls`, `#tab-chat`, `#tab-ai-lectures`, `#tab-homework`, `#tab-course-roster`), verify fixed `.xlsx`/`.csv` report URLs return `200`, open & close all **4 modals** (`#modal-new-course`, `#modal-new-lecture`, `#modal-hw-review`, `#modal-account-security`), verify sidebar drawer toggle at `<=980px`.
  - `student_app`: switch all **3 modes** (`#view-live`, `#view-classes`, `#view-study`), cast poll vote, post chat message, expand homework review.
  - `student_checkin`: verify error state, onboarding form state (`"First-Time Student Sign-In"`), and checked-in state (`"Attendance Verified!"`).
  - `portal_home`, `chat_popout`, `projector`, `lecturer_dashboard`: verify render & live element presence.
- **Tier 4 (DOM Geometry & Cross-Viewport Audit):**
  - Viewports tested: Desktop `1440×900`, Narrow Desktop `1024×768`, Projector `1920×1080` & `1280×720`, Compact Popout `480×800`, Mobile `390×844`, Small Mobile `360×740`.
  - Assert `document.documentElement.scrollWidth <= window.innerWidth` (zero horizontal overflow), all visible interactive controls inside viewport (`rect.right <= window.innerWidth + 1`), non-zero bounding boxes, and zero overlapping sibling nav/tab/KPI bounding boxes.
- **Tier 5 (Adversarial Hardening & Negative Tests):**
  - Scan all `app/templates/` and `app/static/` files for any leftover R2 excluded wellness terms (`Sense`, `Meditate`, `Mindfulness`, `Relaxation`, `Breath of Balance`, `Ease into Sleep`, `Andrew`, `Sara Lee`) -> must be **0** hits.
  - Scan all JS renderers in `teacher_portal.html`, `student_app.html`, `student_checkin.html`, `notifications.js` for orphaned dark-slate card hexes (`#0f172a`, `#1e293b`, `#151f32`, `#0b1120`) -> must be **0** hits (except `#111827` exclusively on `.cp-cv-stage`).
  - Simulate blocked CDN (`socket.io.min.js`) and assert zero uncaught `ReferenceError`s.

### 7.3 Agent-as-Judge Rendered Visual Rubric (PNG Inspection)
Capture full-page and viewport PNG screenshots via CDP `Page.captureScreenshot` across all 8 views, 6 teacher tabs, modals, and desktop/mobile viewports, and inspect via `read_file` against:
1. **Inspiration Fidelity (Aesthetic Only):** Warm alabaster-peach-periwinkle mesh backdrop, frosted glass outer shell, crisp white bento cards, 3D ClassPulse orb, radial dot-matrix header accent, white-pill nav/tab switchers, and coral/indigo palette clearly evoke Inspiration Images 1 & 2 with zero wellness copy.
2. **Cross-Role Visual Cohesion:** Lecturer Web (`portal_home`, `teacher_login`, `teacher_portal`, `lecturer_dashboard`, `chat_popout`, `projector`) and Student Mobile (`student_app`, `student_checkin`) unmistakably belong to one unified ClassPulse product family.
3. **Layout & Contrast Integrity:** WCAG AA legible charcoal ink, pure `#FFFFFF` QR cards, `#111827`-framed `#headcount-canvas`, crisp tabular numerals, clear active states, zero clipped controls, zero overlapping text, and zero horizontal overflow on every tested viewport.

---

## 8. Execution Roadmap & Exclusive File Ownership (M1–M5 + E2E Track)

| Milestone | Scope | Exclusive File Write Ownership | Dependencies | Parallelism |
|---|---|---|---|---|
| **M0** | Collaborative Planning & Design Document | `docs/UI_OVERHAUL_DESIGN_PLAN.md` | Step 0 Surveys | Complete |
| **M1** | Shared Design System & Base Foundation (`classpulse.css`, `base.html`, `icons.html`, `notifications.js` light toast/favicon refactor, `headcount_canvas.js` `#kpi-visual` + `#kpi-visual-count` sync) | `app/static/css/classpulse.css`<br>`app/templates/layouts/base.html`<br>`app/templates/macros/icons.html`<br>`app/static/js/notifications.js`<br>`app/static/js/headcount_canvas.js` | M0 | Blocks M2–M4 |
| **M2** | Shared, Auth & Ambient Display Views (including `lecturer_dashboard.html` parity & Socket.IO CDN guards) | `app/templates/portal_home.html`<br>`app/templates/teacher_login.html`<br>`app/templates/chat_popout.html`<br>`app/templates/projector.html`<br>`app/templates/lecturer_dashboard.html` | M1 | Parallel with M3, M4, E2E |
| **M3** | Lecturer Command Center (Desktop 2-zone shell, collapsible `<=980px` drawer, 6 tabs, 4 modals, JS renderers refactored, report URL fix, Socket.IO CDN guard) | `app/templates/teacher_portal.html`<br>`app/templates/teacher/_tab_lecture_ai.html`<br>`app/templates/teacher/_tab_homework.html`<br>`app/templates/teacher/_tab_roster.html`<br>`app/templates/teacher/_modal_security.html` | M1 | Parallel with M2, M4, E2E |
| **M4** | Student Mobile & Check-In Views (`student_app.html` 3 modes + JS renderers, `student_checkin.html` 4 states, mobile `<480px` stacking, Socket.IO CDN guard) | `app/templates/student_app.html`<br>`app/templates/student_checkin.html` | M1 | Parallel with M2, M3, E2E |
| **E2E Track** | Opaque-box E2E + headless Chromium CDP test suite (Tiers 1–4 + visual screenshot harness) | `tests/test_ui_overhaul_e2e.py` (or dedicated E2E test module) | M0 | Parallel authoring with M1–M4; full run in M5 |
| **M5** | Final Verification, Tier 5 Adversarial Hardening & Agent-as-Judge Visual Inspection | Final regression & visual audit fixes (if any) | M2, M3, M4, E2E | Final Gate |

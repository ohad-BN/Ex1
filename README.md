# ClassPulse

Dockerized smart-classroom platform for **5s anti-proxy QR attendance**, **computer-vision headcounting**, **live polls & Q&A**, **AI lecture summaries**, and **comparative vs. final homework grading**.  
See the [Product Demo Video](docs/demo/ClassPulse_Product_Demo.mp4) and [Live API Reference](docs/api_reference.md).

---

## 1. Quick Start

### Option A — Docker Compose *(Recommended)*
```bash
cp .env.example .env        # Configure SECRET_KEY & optional GEMINI_API_KEY
docker compose up --build   # Starts web (:5000) + Cloudflare public HTTPS tunnel
```
- **Lecturer Portal:** `http://localhost:5000` (or the `https://*.trycloudflare.com` URL printed by `classpulse_tunnel`).

### Option B — Local Python 3.12
```bash
pip install -r requirements.txt
cp .env.example .env
python run.py
```

---

## 2. Environment Keys & Where to Get Them

Edit `.env` (copied from [`.env.example`](.env.example)):

| Variable | Required? | Purpose | Where to Get It |
| :--- | :---: | :--- | :--- |
| `SECRET_KEY` | **Yes** | Signs QR tokens & session cookies (`>=32` chars in prod) | Run: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `GEMINI_API_KEY` | Optional | AI lecture audio/video summaries & deep homework review | Free key at [Google AI Studio](https://aistudio.google.com/apikey) *(falls back locally if empty)* |
| `TUNNEL_TOKEN` | Optional | Fixed custom domain for Cloudflare Tunnel | [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) -> *Networks -> Tunnels* *(empty = free Quick Tunnel)* |
| `ENABLE_DEMO_SEED` | Optional | Set `true` only if you want sample demo courses on empty DB | Local flag (default `false` for clean slate) |

---

## 3. Features & How to Use

| Feature | Lecturer Flow (`/teacher/login` -> `/teacher`) | Student Flow (`/student` or Scan QR) |
| :--- | :--- | :--- |
| **Courses & Rosters** | Create course + lectures, import `.csv`/`.xlsx` rosters, approve pending students, assign TAs. | Sign in with **Full Name + ID Number**; join via **Course Number** or QR scan. |
| **QR Attendance & CV Headcount** | Open **Projector QR**; snap photo via **Upload / Webcam / Phone Cam QR** (`+1/-1` stepper); export `.xlsx`/`.csv`. | Scan 5s projector QR (`cp_device` cookie auto-checks in on future lectures). |
| **Polls & Classroom Q&A** | Launch Poll Bank or 1-click Quick Polls; answer/pin Q&A & Offline Questions; popout Q&A view. | Vote live on phone (`poll:sync` survives sleep); post live or **Offline Questions**. |
| **AI Lecture Summaries** | Upload `.mp4`/`.mp3`/`.vtt`/`.md` -> review structured **Draft** -> click **Publish** (or **Unpublish** / **Delete**). | View published **Executive Summary / Topics / Actions / Transcript** in sidebar. |
| **Assignments** | Create assignment + rubric; grade **Final Submissions** (standalone AI score + file download). | Test drafts in **Comparative Practice** (private rank `1st Place`) before **Final Submit**. |

---

## 4. Run Tests

```bash
python run_all_tests.py             # Fast suite (Suites 1–10: backend, JS unit tests & diagnostics)
python run_all_tests.py --self-test # Non-destructive in-memory fault-localization demo
python run_all_tests.py --all --e2e # Full suite incl. @slow SCRFD/ffmpeg + Headless Chrome E2E
```

---

## 5. Engineering Documentation Chain

| Stage | Document | Answers |
| :--- | :--- | :--- |
| **1. SOW** | [Scope of Work](docs/scope_of_work_sow.md) | Why, what, priority tiers & boundaries |
| **2. SRS** | [Requirements & Use Cases](docs/software_requirements_specification_srs.md) | Functional/NFR specs (`REQ-01`–`16`, `UC-01`–`10`) |
| **3. SDD** | [Design & Architecture](docs/software_design_and_architecture.md) | System structure, 18-model ERD, security & tech stack |
| **4. EDD** | [Engineering Design](docs/engineering_design_edd.md) | Component internals, algorithms & AI pipelines |
| **5. API** | [API & WebSocket Reference](docs/api_reference.md) | Auth headers, REST endpoints & Socket.IO events |
| **6. STP/STD** | [Test Plan & Diagnostics](docs/software_test_plan_stp.md) | 10-suite test matrix, E2E & Fault Localization |

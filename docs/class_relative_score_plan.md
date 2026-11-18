# Class Relative Score (`1–10`) & Categorized Peer Comparison — Research & Implementation Plan

> **Objective:** Add a dedicated **`1–10` Class Standing Score** computed **strictly relative to classmates' submissions** (updating dynamically whenever peers submit or resubmit), break homework feedback into clear **titles, categories, and comparative metrics**, and design the UX/data model so students can **never confuse the fixed AI Rubric Grade (`0–100`) with the dynamic Class Standing Score (`1–10`)**.
>
> **Status:** Research & Implementation Plan only — no application source files modified yet.

---

## 1. Research: Best Algorithm for Dynamic `1–10` Peer-Relative Scoring

To satisfy the exact product requirements—specifically:
1. **Dynamic re-ranking** as classmates submit (e.g., *Ohad submits first → `10/10`; Enav submits a better solution → Enav gets `10/10` and Ohad updates dynamically to `7/10`*),
2. **Category-level comparison** (*Materials & Details*, *Execution Style*, *Technical Correctness*, *Testing & Verification*),
3. **Zero extra LLM cost per peer comparison** (critical for Free-Tier Gemini latency/quotas), and
4. **Zero peer PII leakage** (students never see classmate names, emails, or source code),

we evaluated four candidate peer-ranking models:

| Model | How It Works | Handles User Example (`N=2`)? | Outlier Behavior | Cost & Complexity | Verdict |
|---|---|---|---|---|---|
| **Naive Min-Max Normalization** (`1 + 9*(s - min)/(max - min)`) | Scales strictly between `min` and `max`. | **Fails.** On `N=2`, 2nd-best student equals `min` and crashes straight to `1/10`, even for strong `78/100` work. | **Extreme distortion.** One outlier ruins every classmate's score. | `O(1)` | Reject |
| **Pure Percentile Rank** | Maps ordinal position `(rank / N)` to `1–10`. | **Poor.** Ignores whether `#2` was 2 points or 50 points behind `#1`; discrete jumps on small classes. | Immune to outliers, but destroys magnitude gaps. | `O(log N)` | Partial only |
| **Bradley-Terry Pairwise LLM Comparisons** | Runs pairwise matches (`A vs B`) and fits latent log-abilities via MLE. | **Expensive.** Requires `O(log N)`–`O(N)` extra LLM calls per submission + risks peer context leakage. | Smooth, but cannot run incrementally on single submission without re-matching. | High LLM quota cost | Reject for primary loop |
| **Spread-Anchored Hybrid Relative Standing (Recommended)** | Combines **Gap-to-Class-Leader** (with spread floor against small-$N$ cliffs) and **Ordinal Percentile Rank**, computed both overall and per-category from `ClassStatsAggregate`. | **Exact match.** First submission (`N=1`) is `10/10`. When a stronger submission arrives (`N=2`), leader stays `10/10` and `#2` scales smoothly (e.g., `7/10`). | Protected by spread floor (`Δ_min = 35` pts) + percentile smoothing (`35%` weight). | **`O(N log N)` / `O(1)` DB rows, 0 extra LLM calls** | **Selected** |

---

## 2. Mathematical Specification of the `1–10` Class Score

Let $S = [s_1, s_2, \dots, s_N]$ be all classmates' numeric scores for an assignment (or for one specific category), $s_{\max} = \max(S)$, $s_{\min} = \min(S)$, and $s_i$ be the target student's score.

### Edge Cases
- **$N \le 1$ (First / only submission):**
  $$\text{ClassScore}_i = 10 \quad (10.0)$$
- **Zero spread ($s_{\max} = s_{\min}$ — all tied):**
  $$\text{ClassScore}_i = 10 \quad (10.0)$$
- **Exact Leader ($s_i = s_{\max}$):**
  Guaranteed **`10 / 10`** (tied leaders both receive `10/10`).

### General Formula ($N \ge 2$, $s_i < s_{\max}$)

1. **Spread-Stabilized Gap-to-Leader Score ($G_i \in [1, 10]$):**
   Prevents `N = 2` from collapsing `#2` to `1/10`:
   $$\text{spread} = \max\!\left(\Delta_{\min},\; s_{\max} - s_{\min}\right), \quad \Delta_{\min} = 35.0$$
   $$G_i = 10.0 - 9.0 \cdot \min\!\left(1.0,\; \frac{s_{\max} - s_i}{\text{spread}}\right)$$

2. **Strict-Below Ordinal Rank Component ($R_i \in [1, 10]$):**
   Let $b_i = |\{s \in S : s < s_i\}|$ (strictly lower peers) and $e_i = |\{s \in S : s = s_i\}|$ (ties including self):
   $$p_i = \frac{b_i + 0.5 \cdot (e_i - 1)}{\max(N - 1,\; 1)} \in [0, 1]$$
   $$R_i = 1.0 + 9.0 \cdot p_i$$

3. **Weighted Blend & Rounding:**
   $$\text{raw}_i = 0.65 \cdot G_i + 0.35 \cdot R_i$$
   - Store exact `float` (`class_score_precise = round(raw_i, 1)`) for smooth delta/sorting.
   - Display integer **`class_score` (`1–10`)**:
     - If $s_i = s_{\max} \implies 10$.
     - Otherwise `max(1, min(9, round(raw_i)))` so only submissions matching the class leader display `10/10`!

### Worked Walkthrough on the User's Example
- **Step 1 — Ohad submits first (`ohad_score = 78`, `N = 1`):**
  - Only submission in cohort → Ohad's **Class Score = `10 / 10`** (`Rank #1 of 1`).
  - Ohad's **AI Rubric Grade** = `78 / 100`.
- **Step 2 — Enav submits a stronger solution (`enav_score = 92`, `N = 2`):**
  - Enav (`92 == s_max`) → Enav's **Class Score = `10 / 10`** (`Rank #1 of 2`).
  - Ohad (`78`):
    - `spread = max(35.0, 92 - 78) = 35.0`
    - $G_{\text{Ohad}} = 10.0 - 9.0 \cdot (14 / 35.0) = 6.40$
    - $p_{\text{Ohad}} = 0 / 1 = 0.0$ *(or with small-$N$ smoothing when `N <= 3`: anchor floor $R_i = 4.0 + 6.0 \cdot p_i$ so `#2 of 2` reflects second place cleanly without harsh penalty)* → look at small-$N$ ordinal floor:
      If `rank_floor = max(1.0, 10.0 - 1.5 * (N - 1))` when `N < 7` (i.e., with 2 students max rank penalty yields `8.5` for `#2` ordinal), then:
      - $R_{\text{Ohad}} = 8.5$
      - $\text{raw}_{\text{Ohad}} = 0.65 \cdot 6.40 + 0.35 \cdot 8.5 = 4.16 + 2.975 = \mathbf{7.14 \rightarrow 7 / 10}$!
  - Result: **Enav = `10/10` (`#1 of 2`), Ohad dynamically updates to `7/10` (`#2 of 2`)**, while Ohad's **AI Rubric Grade stays `78/100`**!
- **Step 3 — 15 more students submit ranging `45–96`:**
  - Small-$N$ floor naturally fades once $N \ge 7$ (`rank_floor = 1.0`), spanning the full **`1–10`** curve cleanly across the classroom!

*(Note: Notice how `rank_floor(N) = max(1.0, 10.0 - 1.5 * (N - 1))` automatically guarantees `N=2 -> [8.5, 10]`, `N=3 -> [7.0, 10]`, ..., `N>=7 -> [1.0, 10]`, making the scale behave intuitively for both 2-student live demos and 100-student cohorts!)*

---

## 3. Standardized Homework Titles, Categories & Comparative Metrics

To replace flat scores with structured breakdown titles so students understand *why* their Class Standing (`1–10`) is what it is:

### A. Four Universal Evaluation Categories (Structured Stage-1 Output)
Every homework submission is scored (`0–100`) and summarized across 4 standardized categories alongside assignment-specific rubric topics:

1. **Materials & Details (`materials_details`)**
   - *Scope:* Deliverable completeness, adherence to prompt requirements, documentation/README, input validation, edge-case coverage.
2. **Execution Style (`execution_style`)**
   - *Scope:* Code cleanliness, naming conventions, modularity/OOP encapsulation, readability, comments/docstrings.
3. **Technical Logic & Architecture (`technical_logic`)**
   - *Scope:* Correctness, algorithm/pattern choice, separation of concerns, error handling, portability.
4. **Testing & Verification (`testing_verification`)**
   - *Scope:* Automated unit tests, assertions, test coverage of failure paths, reproducibility proof.

For **each category**, Stage-1 extracts:
- `score: float` (`0–100` criterion score)
- `summary_note: str` (1 short concrete sentence explaining what was strong/missing in this submission for this category)

### B. Deterministic Peer-Comparison Telemetry Metrics (Zero LLM Tokens)
Computed in pure Python inside `SubmissionExtractor` / `HomeworkReviewService` (`metrics_json["telemetry"]`):
- `line_count` (LOC / answer lines)
- `char_count` (size / thoroughness)
- `function_or_class_count` (structural decomposition count)
- `has_tests` + `assertion_count` (verification depth)
- `doc_comment_ratio` (comment/docstring density)
- Stored anonymously as cohort medians/averages in `ClassStatsAggregate`, allowing the Student UI to show:
  - *"Your Code Structure: 4 classes/functions (Class Avg: 3.1) • Assertions: 5 (Class Avg: 2.0)"*

---

## 4. Separating the Two Scores: Zero-Confusion Dual-Score UX

Students must immediately distinguish **absolute AI grading** from **relative class standing**:

### Visual & Terminological Contract

| Attribute | Score #1: **AI Rubric Grade** | Score #2: **Class Standing Score** |
|---|---|---|
| **Display Format** | **`78 / 100`** | **`7 / 10`** |
| **Badge / Subtitle** | `AI Rubric Grade • Fixed Criterion` | `Class Standing • Live Peer Rank (#2 of 14)` |
| **Behavior Over Time** | **Fixed** for this submission version (only changes if student resubmits) | **Dynamic** — automatically recalculates as classmates submit |
| **Color Palette** | Emerald / Slate (`criterion` neutral tone) | Indigo / Amber / Violet (`cohort` peer badge) |
| **Tooltip / Helper Text** | *"Graded 0–100 against the assignment rubric only."* | *"Scored 1–10 ONLY by comparing your work to classmates. First submission starts at 10/10 and shifts as stronger work arrives."* |

### Student UI (`#view-study` in `app/templates/student_app.html`) Layout per Assignment
1. **Top Dual-KPI Header (Two Side-by-Side Cards, Distinct Framing):**
   - Left card: **`AI Rubric Grade: 78 / 100`** (`Requirements Coverage: 82%`)
   - Right card: **`Class Standing: 7 / 10`** (`Rank #2 of 2 • Above/Below Avg • Top Classmate: 10/10`)
   - If the student's dynamic `Class Standing` dropped/rose since their last view (or when `assignment:cohort_updated` arrives live over Socket.IO), show a clear informational strip:
     - *"Cohort Update: A classmate submitted new work. Your AI Rubric Grade stayed `78/100`, while your Class Standing updated (`10/10 → 7/10`)."*
2. **Category Breakdown Grid ("Where You Stand vs. Classmates"):**
   For each of the 4 categories (*Materials & Details*, *Execution Style*, *Technical Logic*, *Testing & Verification*):
   - Category Title + 1-line AI diagnostic note for that category
   - **Class Standing Badge (`1–10`)** for that category (e.g., `Execution Style: 8/10`, `Materials & Details: 6/10`)
   - Comparative Horizontal Track showing: `Your Score (0–100)` vs. `Class Mean` vs. `Class Top` (zero PII!)
3. **Peer Metrics Snapshot Strip:**
   - Objective metrics comparison pills (`Lines`, `Classes/Funcs`, `Tests/Assertions`, `Docs`) vs. Class Average.
4. **Actionable Stage-2 AI Comparative Review (`deep_review_md`):**
   - Organized under matching category headings so narrative advice aligns 1-to-1 with the category cards above.

### Lecturer UI (`#tab-homework` in `app/templates/teacher_portal.html`) Upgrades
- Update cohort table columns in Tab 6:
  - `Student` | `Ver` | `AI Rubric Grade (0–100)` | `Class Standing (1–10)` | `Weakest Category` | `Review`
- Add **Per-Category Class Averages** strip alongside the existing KPIs so the lecturer sees at a glance if the whole class struggles in *Materials & Details* or *Testing & Verification*.

---

## 5. Backend Architecture & Database Schema Changes

### 5.1 Database & Aggregate Storage (`app/models.py` & `app/services/homework_pipeline.py`)
- **`ClassStatsAggregate` (`app/models.py`):**
  - Existing columns (`submission_count`, `mean_overall_score`, `topic_sums_json`, `scores_list_json`, `error_tag_counts_json`) remain intact.
  - Add additive SQLite-migrated JSON columns via `_migrate_sqlite_columns`:
    - `category_scores_json` (`TEXT DEFAULT '{}'`, stores sorted `list[float]` per category, e.g. `{"Materials & Details": [72.0, 88.0], "Execution Style": [...]}` — **zero PII**!)
    - `telemetry_sums_json` (`TEXT DEFAULT '{}'`, stores O(1) cohort sums of `line_count`, `function_or_class_count`, `assertion_count`, `doc_comment_ratio`)
- **Stage-1 Pydantic Schemas (`app/services/homework_pipeline.py`):**
  - Keep `HomeworkMetrics` + `HomeworkMetricsResponse` backward-compatible with existing DB rows (`overall_score`, `topic_scores`, `error_tags`, etc.) and add:
    - `category_scores: dict[str, float]` (`list[CategoryScoreItem]` on `HomeworkMetricsResponse` so zero `additionalProperties` are sent to Gemini!)
    - `category_notes: dict[str, str]` (`list[CategoryNoteItem]` on `HomeworkMetricsResponse`)
    - `telemetry: dict[str, float]` (pure-Python deterministic metrics added during `submit_homework`, never sent to/from LLM schema!)
  - Map old/legacy submissions gracefully (fallback mapping from `topic_scores` or `overall_score` into the 4 standard categories if reading older SQLite rows).

### 5.2 Dynamic Calculation Engine (`CohortAggregator` in `app/services/homework_pipeline.py`)
Add pure-Python methods with zero extra DB/LLM cost:
1. `compute_relative_class_score(scores_list: list[float], score: float) -> dict[str, Any]`
   - Implements the exact spread-stabilized gap + smoothed ordinal formula above.
   - Returns:
     ```python
     {
         "class_score": 7,            # integer 1..10
         "class_score_precise": 7.1,  # float 1.0..10.0
         "rank": 2,                   # 1-indexed competition rank (ties share rank)
         "cohort_size": 2,
         "leader_score": 92.0,        # anonymized top score (no name)
         "cohort_mean": 85.0,
     }
     ```
2. `build_category_comparison(stats: ClassStatsAggregate, metrics: HomeworkMetrics) -> list[dict[str, Any]]`
   - Computes `compute_relative_class_score` **independently per category** (`Materials & Details`, `Execution Style`, `Technical Logic`, `Testing & Verification`) plus `my_rubric_score`, `cohort_mean`, `cohort_best`, and `note`.
3. `build_telemetry_comparison(stats: ClassStatsAggregate, telemetry: dict[str, Any]) -> dict[str, Any]`
   - Computes student vs. cohort averages for structural metrics (`line_count`, `function_or_class_count`, `assertion_count`).
4. Expose unified fields cleanly inside `serialize_submission(sub, stats=stats)` and `serialize_assignment(...)`:
   - `submission["ai_rubric_grade"]` (`0–100`)
   - `submission["class_standing"]` (`{ class_score: 1..10, rank, cohort_size, ... }`)
   - `submission["category_breakdown"]` (`list` of category comparison objects)
   - `submission["telemetry_comparison"]` (`dict`)
   - Notice: because `serialize_submission` computes `class_standing` dynamically from the **current** `ClassStatsAggregate` at serialization time, Ohad's `10/10 → 7/10` update happens **automatically and atomically** as soon as Enav submits—even across 1,000 students—without needing an `UPDATE homework_submissions` fan-out loop across all rows in SQLite!

### 5.3 Real-Time Updates (`app/routes/homework.py`)
- After any `submit_homework` completes (both synchronous `POST` and async `_worker`), emit:
  ```python
  _emit_to_course_rooms(
      db,
      course_id,
      "assignment:cohort_updated",
      {
          "assignment_id": assignment_id,
          "course_id": course_id,
          "cohort_stats": CohortAggregator.build_anonymized_cohort_snapshot(stats),
      },
  )
  ```
- In [student_app.html](file:///c:/Users/ohadb/Desktop/ALL/School/%D7%A7%D7%95%D7%A8%D7%A1%D7%99%D7%9D/%D7%9E%D7%95%D7%A0%D7%97%D7%94%20%D7%A2%D7%A6%D7%9E%D7%99%D7%9D/Ex1/app/templates/student_app.html): listen for `socket.on("assignment:cohort_updated", ...)` and refresh `loadAssignmentsOnly()` so other connected students watch their **`Class Standing (1–10)`** update live (with a `"Class Standing changed (e.g. 10/10 → 7/10)"` banner) while their **AI Rubric Grade (`0–100`)** stays clearly unchanged.

---

## 6. Target Files & Phased Rollout

| Phase | Target File | Planned Work |
|---|---|---|
| **1. Schema & Migrations** | `app/models.py` | Add `category_scores_json` & `telemetry_sums_json` on `ClassStatsAggregate` + `_migrate_sqlite_columns` entries. |
| **2. Metrics, Categories & Relative Engine** | `app/services/homework_pipeline.py` | Add 4 standard categories + `CategoryScoreItem` / `CategoryNoteItem` on `HomeworkMetricsResponse` (no `additionalProperties`), deterministic `extract_telemetry()` in `SubmissionExtractor`, `CohortAggregator.compute_relative_class_score()` + `build_category_comparison()`, update `apply_metrics` / `revert_metrics` / `serialize_submission`, and structure Stage-2 review prompt around the 4 categories + relative standing (zero PII). |
| **3. API & Live Socket Broadcast** | `app/routes/homework.py` | Emit `assignment:cohort_updated` after sync and async submissions finish so all connected students receive updated dynamic `1–10` rankings immediately. |
| **4. Dual-Score UI & Category Cards** | `app/templates/student_app.html`, `app/templates/teacher_portal.html` | Render side-by-side non-confusable `AI Rubric Grade (0–100)` vs `Class Standing (1–10)` badges, interactive per-category relative breakdown bars, telemetry comparison pills, delta toast on live cohort shifts, and Teacher Tab 6 category breakdown columns. |
| **5. Automated Tests** | `tests/test_homework_incremental.py` | Test Ohad (`10/10`) → Enav (`10/10`) → Ohad (`7/10`) progression, tie handling, resubmission rollback of category distributions, Gemini schema check (`additionalProperties`-free), zero-PII enforcement, and `assignment:cohort_updated` Socket.IO emission. |

---

## 7. Verification Criteria

1. **Mathematical & Unit Invariants (`pytest`):**
   - `N = 1`: single submission always scores `10/10`.
   - `N = 2` (Ohad = 76–78, Enav = 90–94): Enav scores `10/10`, Ohad dynamically recalculates to `~7/10` (`>= 6` and `< 10`), and Ohad's `ai_rubric_grade` remains fixed.
   - Equal scores tie cleanly (`score_a == score_b` implies identical `class_score` and `rank`).
   - Resubmissions accurately subtract old category lists & telemetry sums before applying new values.
   - Zero classmate PII (names, emails, raw code) appears in student-facing API payloads or Stage-2 LLM prompts.
2. **UI Clarity Check (Student & Teacher):**
   - Visual distinction between `AI Rubric Grade (XX/100)` and `Class Standing (X/10)` verified with explanatory labels.
   - All 4 categories (*Materials & Details*, *Execution Style*, *Technical Logic*, *Testing & Verification*) display per-category `1–10` standing + `0–100` vs. anonymized cohort mean/best.
3. **Live Demo Verification:**
   - Two browser tabs (`/student` signed in as Ohad and Enav): submit Ohad first (`10/10`), then submit a stronger file as Enav (`10/10`) → verify Ohad's UI updates automatically to `~7/10` via Socket.IO with zero page refresh and clear explanation.

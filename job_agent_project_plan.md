# Job Agent — Upgrade Project Plan

## Overview

Upgrade `study-bot/job_agent.py` from a URL-driven single-job tool into an
interactive job-board browsing agent that:

1. Asks which job board to use
2. Opens a visible browser and logs in
3. Browses listings and pauses to ask "apply to this one?"
4. Detects Easy Apply — if present, flags it in the console, generates documents anyway, displays the link, and asks if you applied
5. Runs the full resume/cover letter pipeline for standard applications
6. Pauses for document review and edits
7. Fills the application (visible browser, not headless)
8. Pauses before clicking Next on every page
9. Pauses before final Submit
10. Logs every confirmed application to a SQLite database
11. Separate `view_applications.js` script displays the database as a chalk-formatted table with weekly/monthly counts

---

## Folder Structure (after changes)

```
study-bot/
├── job_agent.py                         ← main orchestrator (refactored)
├── data/
│   └── job-apps/
│       ├── components.yaml
│       ├── output/
│       └── applications.db              ← NEW SQLite database
├── logs/
│   └── actions_log.json
└── job-agent-support/
    ├── credentials.yaml                 ← NEW (gitignored) login info per board
    ├── boards/
    │   ├── builtin.py                   ← NEW login + browse + apply for builtin.com
    │   ├── governmentjobs.py            ← NEW login + browse + apply for governmentjobs.com
    │   └── indeed.py                   ← NEW login + browse + apply for indeed.com
    ├── ats/
    │   └── greenhouse.py               ← MOVED/extracted from job_agent.py
    └── db.py                            ← NEW database helper module
├── view_applications.js                 ← NEW chalk.js database viewer
└── package.json                         ← NEW (for chalk dependency)
```

---

## Phase 1 — Job Board Selection & Login

### 1.1  Interactive board picker (in `job_agent.py`)
- Remove `--url` as a required arg; make it optional
- Add a `--board` arg: `builtin | governmentjobs | indeed`
- If neither `--url` nor `--board` is supplied, prompt interactively:

```
Which job board would you like to use?
  1. builtin.com
  2. governmentjobs.com
  3. indeed.com
  4. Enter a direct job URL

Choice [1-4]:
```

### 1.2  `credentials.yaml` (gitignored)
```yaml
builtin:
  email: ""
  password: ""

governmentjobs:
  email: ""
  password: ""

indeed:
  email: ""
  password: ""
```

### 1.3  Per-board login in `job-agent-support/boards/<board>.py`
Each board module must export:
```python
def login(page, credentials: dict) -> None
def search_jobs(page, query: str | None) -> None  # navigate to listings
def get_job_listings(page) -> list[dict]           # return [{title, company, url}, ...]
def navigate_to_next_listing(page) -> bool         # return False if no more pages
```

Login strategies:
- **builtin.com** — opens login page and pauses; user completes Google sign-in manually in the visible browser
- **governmentjobs.com** — NeoGov SSO; email + password filled automatically
- **indeed.com** — opens login page and pauses; user completes Google sign-in manually in the visible browser

---

## Phase 2 — Browse, Easy Apply Detection & Per-Job Approval

### 2.1  Browse loop (in `job_agent.py`)
```
for each job listing on current page:
    display: title, company, brief snippet
    ask_approval("Apply to this job?")
    if yes:
        detect_easy_apply()
        if easy apply found → Easy Apply path (see 2.3)
        else               → Full pipeline path (Steps 1–6)
ask: "Move to next page of results?"
```

### 2.2  Job detail extraction
- Navigate to individual job URL
- Use existing `read_job_description()` (already scrapes + LLM-extracts)
- Keep browser open for user to see

### 2.3  Easy Apply detection

Add `detect_easy_apply(page) -> dict | None` in `job_agent.py`. Returns a dict
with the Easy Apply URL if found, otherwise `None`.

**builtin.com** Easy Apply selector:
```python
# builtin surfaces an "Easy Apply" badge and button on job cards and detail pages
el = page.query_selector('a:has-text("Easy Apply"), button:has-text("Easy Apply"), [data-testid="easy-apply"]')
```

**indeed.com** Easy Apply selector:
```python
# Indeed uses "Easily apply" label and an "Apply now" button that stays on-site
el = page.query_selector('.ia-IndeedApplyButton, button:has-text("Easily apply"), [data-testid="indeedApplyButton"]')
```

### 2.4  Easy Apply console flow

When Easy Apply is detected, the agent does NOT open the ATS form. Instead:

```
============================================================
⚡ EASY APPLY DETECTED
   Job Title : Software Engineer
   Company   : Acme Corp
   Board     : builtin.com

   Easy Apply Link:
   👉  https://builtin.com/job/acme/software-engineer/123

   Generating tailored documents for your reference...
============================================================
```

1. Still runs Steps 1–3 (read description → select components → generate documents)
   so you have a fresh resume/cover letter on hand if the Easy Apply form asks for uploads.
2. Auto-opens the documents so you can review them.
3. Prints the Easy Apply URL prominently so you can click it in the terminal or browser.
4. Asks: `"Did you apply to this job? [y/n]"`
5. If yes → `log_application()` with `easy_apply = 1` flag in the database record.
6. If no  → skip, continue to next listing.

### 2.5  `easy_apply` flag in schema

Add a column to the `applications` table (see Phase 6):
```sql
easy_apply  INTEGER DEFAULT 0   -- 1 if applied via Easy Apply, 0 if full application
```

This lets you filter in `view_applications.js` to see which applications were Easy Apply vs. full.

---

## Phase 3 — Document Review with Edit Prompt

After `generate_documents()` (current Step 3), add an explicit review pause:

```
📄 Documents generated:
   Resume      : data/job-apps/output/resume_<company>.docx
   Cover Letter: data/job-apps/output/cover_<company>.docx

Opening files now…
Press ENTER when done reviewing.
Do you want to make any edits before continuing? [y/n]
  → if y: "Please edit the files and press ENTER when ready."
```

Optionally auto-open files with `os.startfile()` (Windows) / `subprocess.call(['open', path])` (macOS) / `xdg-open` (Linux).

---

## Phase 4 — Application Form (Visible Browser, Human-in-the-Loop)

### 4.1  ATS detection
After determining `apply_url`, detect which ATS is in use:
```python
def detect_ats(url: str) -> str:
    if "greenhouse.io" in url:  return "greenhouse"
    if "lever.co"      in url:  return "lever"
    if "workday"       in url:  return "workday"
    return "generic"
```

### 4.2  Per-page Next approval
Current code fills form and waits at the end. Replace with page-by-page loop:

```python
while not on_final_page:
    fill_current_page(page, ...)
    ask_approval("Ready to click Next?")
    click_next(page)

ask_approval("⚠️  FINAL SUBMIT — this cannot be undone. Submit?")
click_submit(page)
```

### 4.3  `job-agent-support/ats/greenhouse.py`
Extract the existing Greenhouse-specific selectors and logic out of
`fill_application_form()` in `job_agent.py` into this module:
```python
def fill_page(page, job_info, docs, components, fields) -> dict
def click_next(page) -> bool        # returns True if another page exists
def click_submit(page) -> None
```

---

## Phase 5 — Credentials & Security

- `credentials.yaml` added to `.gitignore`
- On first run, if `credentials.yaml` is missing or empty, prompt user to fill it
- Never log credentials to `actions_log.json`

---

## Phase 6 — Application Tracking Database

### 6.1  Database file
SQLite database stored at `data/job-apps/applications.db`. Created automatically
on first run — no setup required.

### 6.2  Schema

```sql
CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_title    TEXT    NOT NULL,
    company      TEXT    NOT NULL,
    job_board    TEXT,               -- 'builtin', 'governmentjobs', 'indeed', or 'direct'
    date_applied TEXT    NOT NULL,   -- ISO format: YYYY-MM-DD
    pay          TEXT,               -- e.g. "$80,000 – $100,000" or "Not listed"
    address      TEXT,               -- optional; city/state, "Remote", or "Hybrid – [city]"
    apply_url    TEXT,
    resume_path  TEXT,
    easy_apply   INTEGER DEFAULT 0,  -- 1 if applied via Easy Apply button, 0 if full form
    status       TEXT DEFAULT 'applied'  -- 'applied', 'interviewing', 'rejected', 'offer'
);
```

### 6.3  `job-agent-support/db.py`

New helper module with two functions:

```python
def init_db(db_path: Path) -> None:
    """Create the applications table if it doesn't exist."""

def log_application(db_path: Path, record: dict) -> int:
    """
    Insert a new application record. Returns the new row id.
    record keys: job_title, company, job_board, date_applied,
                 pay, address, apply_url, resume_path, easy_apply
    """
```

### 6.4  Pay and address extraction

Pay and address are extracted from `job_info` during Step 1 (`read_job_description`).
Add them to the LLM extraction prompt:

```
Return ONLY a JSON object with keys:
  job_title, company, summary, requirements, responsibilities,
  pay, address
```

- `pay`: salary range or hourly rate as a plain string; `"Not listed"` if absent
- `address`: office city/state or "Remote" or "Hybrid – [city]"; `null` if absent

### 6.5  When the record is written

The database insert happens in `run_application_pipeline()` immediately after a
successful submit (after Step 6 final approval). If the user declines to submit,
no record is written.

```python
# After submit_application() succeeds (full apply) or user confirms Easy Apply:
from job_agent_support.db import log_application

log_application(DB_PATH, {
    "job_title":    job_info.get("job_title"),
    "company":      job_info.get("company"),
    "job_board":    job_info.get("source_board", "direct"),
    "date_applied": datetime.now().strftime("%Y-%m-%d"),
    "pay":          job_info.get("pay", "Not listed"),
    "address":      job_info.get("address"),
    "apply_url":    job_info.get("apply_url"),
    "resume_path":  docs.get("resume_path"),
    "easy_apply":   1 if job_info.get("is_easy_apply") else 0,
})
```

`source_board` is set on `job_info` at the start of `run_application_pipeline()`
when called from the board browse loop (e.g. `"builtin"`), or `"direct"` when
called via `--url`.

### 6.6  `view_applications.js` — Chalk dashboard

Standalone Node.js script at `study-bot/view_applications.js`. Run with:
```bash
node view_applications.js
```

**Dependencies** (`study-bot/package.json`):
```json
{
  "dependencies": {
    "chalk": "^5.0.0",
    "better-sqlite3": "^9.0.0"
  }
}
```

**Column layout** (one row per application, newest first):

| # | Date | Job Title | Company | Board | Pay | Location | Type | Status |
|---|------|-----------|---------|-------|-----|----------|------|--------|

- **#** — row index, dim gray
- **Date** — yellow
- **Job Title** — bold white
- **Company** — cyan
- **Board** — blue (`builtin` / `indeed` / `governmentjobs` / `direct`)
- **Pay** — green if a value is present, dim gray `"—"` if not listed
- **Location** — white; dim gray `"—"` if null
- **Type** — magenta `⚡ Easy Apply` or white `Full App`
- **Status** — color-coded:
  - `applied`      → white
  - `interviewing` → bold yellow
  - `offer`        → bold green
  - `rejected`     → dim red

**Footer** (printed after the table, separated by a divider line):

```
──────────────────────────────────────────────────────────
  Applied this week  :  3
  Applied this month :  11
──────────────────────────────────────────────────────────
```

"This week" = Mon–Sun of the current calendar week.
"This month" = current calendar month.
Both counts are bold white numbers on a dim label.

---

## Refactored `main()` flow

```
1.  Ask / parse board choice
2.  Load credentials
3.  Open browser (headless=False always)
4.  Log in to board  [board module]
5.  Browse listings loop:
      a. Display job card
      b. ask_approval("Apply to this job?")
      c. If yes:
           — detect_easy_apply()
           IF Easy Apply detected:
             i.   read_job_description()          [Step 1]
             ii.  select_components()             [Step 2]
             iii. generate_documents()            [Step 3]
             iv.  Auto-open documents
             v.   Print ⚡ EASY APPLY banner + clickable link
             vi.  ask "Did you apply? [y/n]"
             vii. If yes → log_application(easy_apply=1) → applications.db
           IF standard application:
             i.   read_job_description()          [existing Step 1]
             ii.  select_components()             [existing Step 2]
             iii. generate_documents()            [existing Step 3]
             iv.  Review + edit pause             [NEW]
             v.   inspect_application_form()      [existing Step 4]
             vi.  ask_approval("Fill form?")
             vii. fill_application_form() — page by page w/ Next approvals
             viii.ask_approval("SUBMIT?")
             ix.  submit_application()
             x.   log_application(easy_apply=0) → applications.db
             xi.  log_action(...)
      d. Continue to next listing
6.  ask_approval("Next page of results?")
```

---

## File Change Summary

| File | Action |
|---|---|
| `job_agent.py` | Refactor `main()`, make `--url` optional, add board picker, Easy Apply detection, per-page Next/Submit prompts |
| `job-agent-support/credentials.yaml` | New — gitignored |
| `job-agent-support/boards/builtin.py` | New — login pauses for Google sign-in; adds `detect_easy_apply()` |
| `job-agent-support/boards/governmentjobs.py` | New — auto email/password login |
| `job-agent-support/boards/indeed.py` | New — login pauses for Google sign-in; adds `detect_easy_apply()` |
| `job-agent-support/ats/greenhouse.py` | Extract from `job_agent.py` |
| `job-agent-support/db.py` | New — SQLite helper (init + insert), includes `easy_apply` field |
| `data/job-apps/applications.db` | New — auto-created on first run |
| `view_applications.js` | New — chalk.js terminal dashboard with weekly/monthly counts |
| `package.json` | New — chalk + better-sqlite3 dependencies |
| `.gitignore` | Add `credentials.yaml`, `node_modules/` |

---

## Out of Scope (Future)

- Lever, Workday, iCIMS ATS support
- Updating application `status` field interactively (interviewing, rejected, offer)
- Auto-scheduler / batch run
- Email confirmation parser
- Filtering / sorting options in `view_applications.js`

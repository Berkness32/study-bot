"""
job_agent.py — Job Application Agent
Pauses at each step for user approval before proceeding.

Usage:
    python job_agent.py                          # interactive board picker
    python job_agent.py --board builtin          # go straight to builtin.com
    python job_agent.py --board governmentjobs   # go straight to governmentjobs.com
    python job_agent.py --board indeed           # go straight to indeed.com
    python job_agent.py --url "https://..."      # skip board picker, use direct URL

Requirements:
    pip install playwright python-docx pyyaml ollama
    playwright install chromium
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import ollama
import yaml
from playwright.sync_api import sync_playwright
from job_agent_support.db import (already_applied, log_application,
                                   is_dead_listing, log_dead_listing,
                                   log_apply_later)
from job_agent_support.doc_builder import (
    ALLOWED_TAGS, validate_tag, generate_documents as _build_documents,
)

# ── Config ────────────────────────────────────────────────────────────────────
_ROOT            = Path(__file__).parent
COMPONENTS_PATH  = _ROOT / "data/job-apps/components.yaml"
OUTPUT_DIR       = _ROOT / "data/job-apps/output"
ACTIONS_LOG      = _ROOT / "logs/actions_log.json"
CREDENTIALS_PATH = _ROOT / "job_agent_support/credentials.yaml"
DB_PATH          = _ROOT / "data/job-apps/applications.db"
SCAN_LOG_DIR     = _ROOT / "logs/job_agents_logs"
CHAT_MODEL       = "qwen3:8b"
LLAVA_MODEL      = "llava:13b"
LLM_TIMEOUT      = 600                          # 10-min logical timeout (managed in _ollama_call)
_OLLAMA_CLIENT   = ollama.Client(timeout=660)   # 11-min httpx backstop
_MEMORY_LOG      = _ROOT / "logs/memory_log.json"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)


# ── Ollama / memory helpers ───────────────────────────────────────────────────

def _get_loaded_models() -> list[str]:
    try:
        return [m.model for m in _OLLAMA_CLIENT.ps().models]
    except Exception:
        return []


def _stop_model(model_name: str) -> bool:
    """Unload model_name from Ollama memory. Returns True if it was running."""
    loaded = _get_loaded_models()
    base   = model_name.split(":")[0].lower()
    match  = next((m for m in loaded if base in m.lower()), None)
    if not match:
        return False
    try:
        _OLLAMA_CLIENT.generate(model=match, prompt="", keep_alive=0)
        print(f"  🛑 Stopped {match} to free memory.")
        return True
    except Exception as e:
        print(f"  ⚠️  Could not stop {match}: {e}")
        return False


def _memory_snapshot() -> dict:
    snap = {
        "timestamp":     datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "ollama_loaded": _get_loaded_models(),
    }
    try:
        raw   = subprocess.check_output(["vm_stat"], text=True)
        psize = 4096
        mb    = {}
        for line in raw.splitlines():
            for k in ("Pages free", "Pages active", "Pages inactive",
                      "Pages wired down", "Pages occupied by compressor"):
                if line.startswith(k):
                    v = int(re.sub(r"[^\d]", "", line.split(":")[-1]))
                    mb[k] = round(v * psize / 1_048_576)
        snap["memory_mb"] = mb
    except Exception as e:
        snap["vm_stat_error"] = str(e)
    try:
        out = subprocess.check_output(
            "ps aux | sort -k4 -rn | head -11",
            shell=True, text=True,
        )
        snap["top_processes"] = out.strip().splitlines()
    except Exception as e:
        snap["ps_error"] = str(e)
    return snap


def _print_and_log_snapshot(label: str) -> None:
    snap = _memory_snapshot()
    mb   = snap.get("memory_mb", {})
    print(f"\n  📊 Memory [{label}] {snap['timestamp']}")
    if mb:
        print(f"     Free     : {mb.get('Pages free', '?')} MB")
        print(f"     Active   : {mb.get('Pages active', '?')} MB")
        print(f"     Wired    : {mb.get('Pages wired down', '?')} MB")
        print(f"     Swapped  : {mb.get('Pages occupied by compressor', '?')} MB")
    print(f"     Ollama   : {snap['ollama_loaded'] or 'none'}")
    procs = snap.get("top_processes", [])
    if procs:
        print("     Top procs by %MEM:")
        print(f"       {procs[0]}")          # header row
        for line in procs[1:6]:             # top 5 processes
            print(f"\n       {line}")

    snap["label"] = label
    entries = []
    if _MEMORY_LOG.exists():
        try:
            with open(_MEMORY_LOG, encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            entries = []
    entries.append(snap)
    with open(_MEMORY_LOG, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _ollama_call(messages: list, options: dict) -> dict:
    """
    Run _OLLAMA_CLIENT.chat in a background thread.
    - Stops llava before starting if it is loaded.
    - Prints memory snapshots at 2-min and 5-min marks.
    - On timeout: prints memory, re-checks/stops llava, raises TimeoutError.
    """
    for m in list(_get_loaded_models()):
        if "llava" in m.lower():
            print(f"  ℹ️  {m} is loaded — stopping it to free memory...")
            _stop_model(m)
            time.sleep(1)

    result: list = [None]
    exc:    list = [None]

    def _run():
        try:
            result[0] = _OLLAMA_CLIENT.chat(
                model=CHAT_MODEL, messages=messages, options=options,
            )
        except Exception as e:
            exc[0] = e

    t     = threading.Thread(target=_run, daemon=True)
    t.start()

    start = time.time()
    marks = [(120, "2-minute mark"), (300, "5-minute mark")]
    seen  = set()

    while t.is_alive():
        elapsed = time.time() - start
        if elapsed >= LLM_TIMEOUT:
            print(f"\n  ⚠️  LLM call timed out after {LLM_TIMEOUT // 60} minutes.")
            _print_and_log_snapshot("timeout")
            for m in _get_loaded_models():
                if "llava" in m.lower():
                    _stop_model(m)
            raise TimeoutError(
                f"Ollama did not respond in {LLM_TIMEOUT // 60} min — "
                "check memory pressure and retry."
            )
        for secs, lbl in marks:
            if secs not in seen and elapsed >= secs:
                seen.add(secs)
                _print_and_log_snapshot(lbl)
        t.join(timeout=1.0)

    if exc[0] is not None:
        raise exc[0]
    return result[0]


# ── Ollama preflight check ────────────────────────────────────────────────────

def _check_ollama() -> None:
    """Abort early if Ollama is unreachable, model is missing, or warmup times out."""
    try:
        names = [m.model for m in _OLLAMA_CLIENT.list().models]
        if CHAT_MODEL not in names:
            print(f"\n  ⚠️  Model '{CHAT_MODEL}' is not installed.")
            print(f"  Run: ollama pull {CHAT_MODEL}")
            sys.exit(1)
    except Exception as e:
        print(f"\n  ⚠️  Cannot reach Ollama: {e}")
        print("  Make sure Ollama is running:  ollama serve")
        sys.exit(1)

    # Stop llava before warmup so the cold-load ping has full RAM available.
    for m in list(_get_loaded_models()):
        if "llava" in m.lower():
            print(f"  ℹ️  Stopping {m} before warmup to free memory...")
            _stop_model(m)

    print("  🔍 Warming up Ollama (loading model into memory)...")
    try:
        _ollama_call(
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            options={"temperature": 0, "num_predict": 5},
        )
        print("  ✅ Ollama is ready.")
    except TimeoutError as e:
        print(f"\n  ⚠️  {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ⚠️  Ollama warmup failed: {e}")
        print("  The model may be swapping to disk — close other apps and retry.")
        sys.exit(1)


# ── Logging ───────────────────────────────────────────────────────────────────

def _strip_llm_raw(text: str) -> str:
    text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*',     '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$',     '', text)
    return text.strip()


def log_action(action: str, job_title: str, company: str,
               outcome: str, notes: str = ""):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "action":    action,
        "job_title": job_title,
        "company":   company,
        "outcome":   outcome,
        "notes":     notes,
    }
    entries = []
    if ACTIONS_LOG.exists():
        with open(ACTIONS_LOG, encoding="utf-8") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                entries = []
    entries.append(entry)
    with open(ACTIONS_LOG, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    print(f"  📋 Logged: {action} → {outcome}")


# ── User approval ─────────────────────────────────────────────────────────────

def ask_approval(prompt: str, details: str = "") -> bool:
    print()
    print("=" * 60)
    print(f"⏸  APPROVAL REQUIRED: {prompt}")
    if details:
        print()
        print(details)
    print("=" * 60)
    while True:
        answer = input("  Proceed? [y/n/q to quit]: ").strip().lower()
        if answer == "y":
            return True
        elif answer == "n":
            return False
        elif answer == "q":
            print("Exiting.")
            sys.exit(0)
        else:
            print("  Please enter y, n, or q.")


def ask_choice(prompt: str, options: dict, details: str = "") -> str:
    """Present a labeled menu of choices and return the selected key."""
    print()
    print("=" * 60)
    print(f"⏸  {prompt}")
    if details:
        print()
        print(details)
    print()
    for key, label in options.items():
        print(f"  [{key}] {label}")
    print(f"  [q] Quit")
    print("=" * 60)
    valid = set(options.keys())
    while True:
        answer = input("  Choice: ").strip().lower()
        if answer == "q":
            print("Exiting.")
            sys.exit(0)
        if answer in valid:
            return answer
        print(f"  Please enter one of: {', '.join(sorted(valid))}, q")


# ── File opener (cross-platform) ──────────────────────────────────────────────

def _open_file(path):
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(path))
        elif system == "Darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    except Exception as e:
        print(f"  Note: could not auto-open file ({e}) — open it manually.")


# ── Credentials loader ────────────────────────────────────────────────────────

def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        print(f"\n  ⚠️  credentials.yaml not found at {CREDENTIALS_PATH}")
        print("  Creating a blank one — fill it in before re-running.\n")
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CREDENTIALS_PATH, "w") as f:
            f.write("builtin:\n  email: \"\"\n  password: \"\"\n\n"
                    "governmentjobs:\n  email: \"\"\n  password: \"\"\n\n"
                    "indeed:\n  email: \"\"\n  password: \"\"\n")
        sys.exit(0)
    with open(CREDENTIALS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Board picker ──────────────────────────────────────────────────────────────

def pick_board() -> str:
    print("\nWhich job board would you like to use?")
    print("  1. builtin.com")
    print("  2. governmentjobs.com")
    print("  3. indeed.com")
    print("  4. Enter a direct job URL")
    while True:
        choice = input("Choice [1-4]: ").strip()
        if choice == "1": return "builtin"
        if choice == "2": return "governmentjobs"
        if choice == "3": return "indeed"
        if choice == "4": return "url"
        print("  Please enter 1, 2, 3, or 4.")


def load_board_module(board: str):
    if board == "builtin":
        from job_agent_support.boards.builtin import login, browse_jobs, get_job_listings, go_to_next_page
    elif board == "governmentjobs":
        from job_agent_support.boards.governmentjobs import login, browse_jobs, get_job_listings, go_to_next_page
    elif board == "indeed":
        from job_agent_support.boards.indeed import login, browse_jobs, get_job_listings, go_to_next_page
    else:
        raise ValueError(f"Unknown board: {board}")
    return {"login": login, "browse_jobs": browse_jobs,
            "get_job_listings": get_job_listings, "go_to_next_page": go_to_next_page}


# ── ATS detection ─────────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    if "greenhouse.io" in url or "boards.greenhouse.io" in url:
        return "greenhouse"
    if "lever.co" in url:
        return "lever"
    if "myworkdayjobs" in url or "workday.com" in url:
        return "workday"
    if "icims.com" in url:
        return "icims"
    if "ashbyhq.com" in url:
        return "ashbyhq"
    return "generic"


def load_ats_module(ats: str):
    if ats == "greenhouse":
        from job_agent_support.ats.greenhouse import fill_page, has_next_page, click_next, click_submit
    elif ats == "lever":
        from job_agent_support.ats.lever import fill_page, has_next_page, click_next, click_submit
    elif ats == "workday":
        from job_agent_support.ats.workday import fill_page, has_next_page, click_next, click_submit
    elif ats == "icims":
        from job_agent_support.ats.icims import fill_page, has_next_page, click_next, click_submit
    elif ats == "ashbyhq":
        from job_agent_support.ats.ashbyhq import fill_page, has_next_page, click_next, click_submit
    else:
        return None
    return {"fill_page": fill_page, "has_next_page": has_next_page,
            "click_next": click_next, "click_submit": click_submit}


# ── Easy Apply detection ───────────────────────────────────────────────────────

def detect_easy_apply(page) -> dict | None:
    """Return {url} if an Easy Apply button is visible on the current page, else None."""
    selectors = [
        'a:has-text("Easy Apply")',
        'button:has-text("Easy Apply")',
        '[data-testid="easy-apply"]',
        '.ia-IndeedApplyButton',
        'button:has-text("Easily apply")',
        '[data-testid="indeedApplyButton"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return {"url": page.url}
        except Exception:
            pass
    return None


# ── Job-not-found detection ───────────────────────────────────────────────────

_NOT_FOUND_PHRASES = [
    "job not found",
    "job listing not found",
    "this job is no longer available",
    "this position is no longer available",
    "position has been filled",
    "this posting has expired",
    "posting has been removed",
    "no longer accepting applications",
    "this job has been closed",
    "listing is no longer active",
]


def detect_job_not_found(page) -> bool:
    """Return True if the current page indicates the job posting no longer exists."""
    try:
        # Check title first (cheap)
        title = page.title().lower()
        if any(p in title for p in _NOT_FOUND_PHRASES):
            return True

        # Check Workday-style error message element
        error_el = page.query_selector('[data-automation-id="errorMessage"]')
        if error_el and error_el.is_visible():
            error_text = error_el.inner_text().lower()
            if any(p in error_text for p in _NOT_FOUND_PHRASES):
                return True

        # Check #mainContent for not-found phrases (scoped, cheaper than full body)
        main = page.query_selector("#mainContent")
        if main:
            main_text = main.inner_text().lower()
            if any(p in main_text for p in _NOT_FOUND_PHRASES):
                return True

        # Fallback: check visible body text
        body = page.inner_text("body").lower()
        return any(p in body for p in _NOT_FOUND_PHRASES)

    except Exception:
        return False


def _log_dead(url: str, title: str, company: str, board: str) -> None:
    log_dead_listing(DB_PATH, {
        "job_title":   title,
        "company":     company,
        "job_board":   board,
        "date_found":  datetime.now().strftime("%Y-%m-%d"),
        "listing_url": url,
        "reason":      "job_not_found",
    })
    log_action("job_not_found", title, company, "skipped", f"URL: {url}")
    print(f"  ⚠️  Job not found — logged to dead_listings and skipping.")


# ── Components loader ─────────────────────────────────────────────────────────

def load_components() -> dict:
    if not COMPONENTS_PATH.exists():
        print(f"ERROR: components.yaml not found at {COMPONENTS_PATH}")
        sys.exit(1)
    with open(COMPONENTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Step 1: Read job description ──────────────────────────────────────────────

def read_job_description(url: str, page) -> dict:
    print(f"\n{'='*60}")
    print("STEP 1 — Reading job description")
    print(f"  URL: {url}")
    print(f"{'='*60}")

    print("  Navigating to job posting...")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_load_state("networkidle", timeout=15000)

    job_body = page.query_selector('[id^="job-post-body-"]')
    full_text = job_body.inner_text() if job_body and job_body.is_visible() else page.inner_text("body")
    title_tag = page.title()

    print("  Extracting job details with LLM...")
    prompt = f"""Extract the following from this job posting text.
Return ONLY a JSON object with keys: job_title, company, summary, requirements, responsibilities, pay, address.
Keep each value concise (under 300 words each).
- pay: salary range or hourly rate as a plain string; "Not listed" if absent
- address: office city/state, "Remote", or "Hybrid – [city]"; null if absent

Job posting text:
{full_text[:6000]}

Return only valid JSON, no markdown, no explanation."""

    try:
        response = _ollama_call(
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
        )
    except TimeoutError:
        raise
    except Exception as e:
        print(f"  ⚠️  LLM call failed ({e}) — using fallback values.")
        return {
            "job_title":        title_tag,
            "company":          "Unknown",
            "summary":          full_text[:500],
            "requirements":     "",
            "responsibilities": "",
            "pay":              "Not listed",
            "address":          None,
            "url":              url,
            "full_text":        full_text[:8000],
            "apply_url":        url,
        }
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        job_info = json.loads(raw)
    except json.JSONDecodeError:
        job_info = {
            "job_title":        title_tag,
            "company":          "Unknown",
            "summary":          full_text[:500],
            "requirements":     "",
            "responsibilities": "",
            "pay":              "Not listed",
            "address":          None,
        }

    job_info["url"]       = url
    job_info["full_text"] = full_text[:8000]

    # Find apply URL
    apply_url = page.evaluate("""() => {
        const links = [...document.querySelectorAll('a')];
        const applyLink = links.find(a => /apply/i.test(a.innerText) && a.href && !a.href.includes('builtin'));
        return applyLink ? applyLink.href : null;
    }""")
    job_info["apply_url"] = apply_url or url

    print(f"\n  Job Title : {job_info.get('job_title', 'N/A')}")
    print(f"  Company   : {job_info.get('company', 'N/A')}")
    print(f"  Apply URL : {job_info['apply_url']}")
    print(f"\n  Summary   : {job_info.get('summary', '')[:300]}...")
    return job_info


# ── Step 2: Select position tag ──────────────────────────────────────────────

def select_tag(job_info: dict) -> dict:
    """
    Ask the LLM to pick the best position tag for this job from ALLOWED_TAGS,
    plus 2 backup tags (used if the primary produces too-short documents).
    Returns {"primary_tag": str, "backup_tags": [str, str]}.
    """
    print(f"\n{'='*60}")
    print("STEP 2 — Selecting position tag")
    print(f"  URL: {job_info.get('url', 'N/A')}")
    print(f"{'='*60}")

    tag_descriptions = {
        "software_engineer": "General software / backend / full-stack engineering",
        "app_dev":           "Mobile app development (iOS/Android)",
        "web_dev":           "Web frontend / UI development",
        "data_science":      "Data science, ML, analytics",
        "it_cloud":          "Cloud infrastructure, AWS, DevOps",
        "it_help_desk":      "IT support, help desk, technical support",
        "it_network":        "Networking, network administration",
        "admin":             "Administrative, clerical, office operations",
        "events":            "Event coordination, recreation, program operations",
    }

    prompt = f"""You are a resume tailoring assistant.

Select the single best position tag for the job below, then 2 backup tags
(2nd and 3rd best match) in case the primary produces too-short documents.

Allowed tags and their meanings:
{json.dumps(tag_descriptions, indent=2)}

Job Title       : {job_info.get('job_title')}
Company         : {job_info.get('company')}
Requirements    : {job_info.get('requirements', '')[:500]}
Responsibilities: {job_info.get('responsibilities', '')[:500]}

Return ONLY valid JSON, no markdown:
{{"primary_tag": "<tag>", "backup_tags": ["<tag2>", "<tag3>"]}}"""

    try:
        response = _ollama_call(
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
        )
    except TimeoutError:
        raise
    except Exception as e:
        print(f"  ⚠️  LLM call failed ({e}) — defaulting tag to software_engineer.")
        return {"primary_tag": "software_engineer", "backup_tags": ["it_help_desk", "admin"]}
    raw = _strip_llm_raw(response["message"]["content"])

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"primary_tag": "software_engineer", "backup_tags": ["it_help_desk", "admin"]}

    primary = result.get("primary_tag", "")
    if not validate_tag(primary):
        primary = "software_engineer"
    backups = [t for t in result.get("backup_tags", [])
               if validate_tag(t) and t != primary][:2]

    print(f"\n  Primary tag : {primary}")
    print(f"  Backup tags : {backups}")
    return {"primary_tag": primary, "backup_tags": backups}


# ── Step 3: Generate documents ────────────────────────────────────────────────

def generate_documents(job_info: dict, selected: dict, components: dict) -> dict:
    print(f"\n{'='*60}")
    print("STEP 3 — Generating tailored documents")
    print(f"  URL: {job_info.get('url', 'N/A')}")
    print(f"{'='*60}")
    return _build_documents(
        primary_tag  = selected["primary_tag"],
        job_info     = job_info,
        components   = components,
        output_dir   = OUTPUT_DIR,
        backup_tags  = selected.get("backup_tags", []),
    )


# ── Apply-URL resolver (follows intermediate company career pages) ─────────────

def resolve_apply_url(url: str, page) -> tuple[str, object]:
    """
    Starting from url, follow Apply Now buttons through intermediate pages
    (e.g. listing site → company Phenom page → Workday) until a real
    application form is reached or no more buttons are found.
    Handles same-tab navigation and popup/new-tab openings.
    Returns (final_url, final_page).
    """
    _SELECTORS = [
        'a#applyButton',
        'a[ph-tevent="apply_click"][title="Apply Now"]',
        'a[aria-label="Apply to job"]',
        'a.button.job-apply',
        'a:has-text("Apply Now")',
        'a:has-text("APPLY NOW")',
        'a:has-text("Apply for this job")',
        'a:has-text("Apply for Job")',
        'button:has-text("Apply Now")',
        'button:has-text("APPLY NOW")',
        'button:has-text("Apply for this job")',
        'button:has-text("Apply for Job")',
    ]

    current_url  = url
    current_page = page

    for hop in range(3):
        try:
            current_page.goto(current_url, wait_until="domcontentloaded", timeout=30000)
            current_page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            return current_url, current_page

        has_form = current_page.evaluate("""() => {
            const fields = document.querySelectorAll('input:not([type=hidden]), textarea, select');
            return fields.length >= 5;
        }""")
        if has_form:
            return current_url, current_page

        # Close chatbot overlay before scanning for the Apply button.
        try:
            close_btn = current_page.get_by_role("button", name=re.compile(r"close chatbot", re.IGNORECASE))
            if close_btn.count() > 0:
                close_btn.click()
                print("  Closed chatbot overlay.")
        except Exception:
            pass

        clicked = False
        for sel in _SELECTORS:
            try:
                el = current_page.query_selector(sel)
                if not (el and el.is_visible()):
                    continue
                pre_click_url = current_page.url
                print(f"  ↪  hop {hop + 1}: clicking Apply Now via {sel!r}")
                try:
                    with current_page.expect_popup(timeout=10000) as popup_info:
                        el.click()
                    new_page = popup_info.value
                    new_page.wait_for_load_state("domcontentloaded", timeout=30000)
                    new_page.wait_for_load_state("networkidle", timeout=15000)
                    current_url  = new_page.url
                    current_page = new_page
                    print(f"     Opened new tab: {current_url}")
                except Exception:
                    # No popup — button navigated current page
                    current_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    current_page.wait_for_load_state("networkidle", timeout=15000)
                    current_url = current_page.url
                    print(f"     Landed on: {current_url}")
                    if current_url == pre_click_url:
                        # URL didn't change (e.g. in-page modal) — try next selector
                        continue
                clicked = True
                break
            except Exception:
                pass

        if not clicked:
            break  # no navigating Apply button found — stop

    return current_url, current_page


# ── Page scanner ─────────────────────────────────────────────────────────────

def scan_page_to_log(page, job_info: dict) -> Path:
    """Scrape full HTML from the current page, write to a timestamped log file,
    then optionally append a user note. Returns the log file path."""
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    company    = re.sub(r'[^\w\s-]', '', job_info.get("company", "unknown")).strip().replace(" ", "_")
    log_path   = SCAN_LOG_DIR / f"scan_{company}_{timestamp}.txt"

    html = page.content()
    url  = page.url

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"URL: {url}\n")
        f.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Job: {job_info.get('job_title', 'N/A')} at {job_info.get('company', 'N/A')}\n")
        f.write("=" * 80 + "\n\n")
        f.write(html)

    print(f"\n  📄 Page HTML saved to: {log_path}")

    while True:
        add_note = input("  Add a user note? [y/n]: ").strip().lower()
        if add_note == "y":
            print("  Enter your note (press ENTER twice when done):")
            lines = []
            while True:
                line = input()
                if line == "" and lines and lines[-1] == "":
                    break
                lines.append(line)
            note = "\n".join(lines).strip()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n\n" + "=" * 80 + "\n")
                f.write("USER NOTE:\n")
                f.write(note + "\n")
            print("  ✅ Note appended to scan log.")
            break
        elif add_note == "n":
            break
        else:
            print("  Please enter y or n.")

    return log_path


# ── Step 4: Inspect form ──────────────────────────────────────────────────────

def inspect_application_form(url: str, page) -> list[dict]:
    print(f"\n{'='*60}")
    print("STEP 4 — Inspecting application form")
    print(f"  URL: {url}")
    print(f"{'='*60}")

    if page.url != url:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)

    fields = page.evaluate("""() => {
        const results = [];
        const inputs = document.querySelectorAll('input, textarea, select');
        inputs.forEach(el => {
            if (el.type === 'hidden' || el.type === 'submit') return;
            let label = '';
            if (el.id) {
                const lbl = document.querySelector('label[for="' + el.id + '"]');
                if (lbl) label = lbl.innerText.trim();
            }
            if (!label) label = el.placeholder || el.name || el.type || 'unknown';
            results.push({
                label:      label,
                field_type: el.tagName.toLowerCase() + (el.type ? '[' + el.type + ']' : ''),
                required:   el.required,
                name:       el.name || '',
                id:         el.id || '',
            });
        });
        return results;
    }""")

    print(f"\n  Found {len(fields)} form fields:")
    for f in fields[:20]:
        req = "REQUIRED" if f["required"] else "optional"
        print(f"    [{req}] {f['label']} ({f['field_type']})")

    return fields


def _generic_fill(page, job_info, docs, components, fields,
                  filled_summary, flagged):
    """Basic fill for non-Greenhouse forms."""
    p_info     = components["personal"]
    field_data = {
        "first_name": p_info.get("name", "").split()[0],
        "last_name":  " ".join(p_info.get("name", "").split()[1:]),
        "email":      p_info.get("email", ""),
        "phone":      p_info.get("phone", ""),
        "website":    p_info.get("portfolio", ""),
        "linkedin":   p_info.get("linkedin", ""),
        "location":   p_info.get("location", ""),
    }

    for field in fields:
        field_id   = field.get("id", "")
        field_name = field.get("name", "")
        value      = field_data.get(field_id) or field_data.get(field_name, "")
        if not value:
            continue
        try:
            selector = f"#{field_id}" if field_id else f"[name='{field_name}']"
            el = page.query_selector(selector)
            if el and el.is_visible():
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag in ("input", "textarea"):
                    el.fill(value)
                    filled_summary.append({"field": field_id or field_name, "value": value[:50]})
        except Exception as e:
            flagged.append({"id": field_id, "name": field_name, "error": str(e)})

    # Upload files
    for file_key, file_path in [
        ("resume",       docs.get("resume_path")),
        ("cover_letter", docs.get("cover_letter_path")),
    ]:
        if not file_path or not os.path.exists(file_path):
            continue
        try:
            el = page.query_selector(f"input[type='file'][name*='{file_key}'], input[type='file'][id*='{file_key}']")
            if el:
                el.set_input_files(file_path)
                filled_summary.append({"field": file_key, "value": Path(file_path).name})
        except Exception as e:
            flagged.append({"field": file_key, "error": str(e)})


# ── Step 6: Present summary and submit ───────────────────────────────────────

def present_summary(job_info: dict, docs: dict, fill_result: dict, selected: dict):
    print(f"\n{'='*60}")
    print("STEP 6 — APPLICATION SUMMARY FOR REVIEW")
    print(f"{'='*60}")
    print(f"  Job Title   : {job_info.get('job_title')}")
    print(f"  Company     : {job_info.get('company')}")
    print(f"  URL         : {job_info.get('url')}")
    print(f"  Tag used    : {selected.get('primary_tag')} (backups: {selected.get('backup_tags', [])})")
    print(f"\n  Resume      : {docs['resume_path']}")
    print(f"  Cover Letter: {docs['cover_letter_path']}")
    print(f"\n  Fields filled  : {len(fill_result.get('filled_fields', []))}")

    flagged = [f for f in fill_result.get("flagged", []) if f.get("name") or f.get("id")]
    if flagged:
        print(f"\n  ⚠️  Items requiring your review:")
        for f in flagged:
            print(f"      - {f.get('name') or f.get('id', str(f))}")


def submit_application(page, job_info: dict):
    """Attempt to click the Submit button."""
    ats     = detect_ats(job_info.get("apply_url", ""))
    ats_mod = load_ats_module(ats)
    if ats_mod:
        ats_mod["click_submit"](page)
    else:
        # Generic submit attempt
        for sel in ['button[type="submit"]', 'input[type="submit"]', 'button:has-text("Submit")']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    print("  ✅ Submit clicked.")
                    return
            except Exception:
                pass
        print("  ⚠️  Could not find Submit button — please click it manually.")
        input("  Press ENTER after submitting manually...")


# ── DB logging helper ─────────────────────────────────────────────────────────

def _log_application(job_info: dict, docs: dict, url: str, source_board: str) -> None:
    log_application(DB_PATH, {
        "job_title":    job_info.get("job_title"),
        "company":      job_info.get("company"),
        "job_board":    source_board,
        "date_applied": datetime.now().strftime("%Y-%m-%d"),
        "pay":          job_info.get("pay", "Not listed"),
        "address":      job_info.get("address"),
        "apply_url":    job_info.get("apply_url", url),
        "resume_path":  docs.get("resume_path"),
        "easy_apply":   0,
    })


# ── Per-job pipeline ──────────────────────────────────────────────────────────

def run_application_pipeline(url: str, page, components: dict, source_board: str = "direct"):
    """Run the full Steps 1-6 pipeline for a single job URL."""

    # ── STEP 1 ────────────────────────────────────────────────────────────────
    job_info = read_job_description(url, page)
    job_info["source_board"] = source_board
    log_action("read_job_description",
               job_info.get("job_title", ""), job_info.get("company", ""), "success")

    step1_choice = ask_choice(
        "Step 1 complete — how would you like to proceed?",
        {
            "y": "Continue with application",
            "a": "Already applied — log it and move on",
            "d": "Dead listing — job no longer exists",
            "n": "Skip — don't apply",
        },
    )
    if step1_choice == "a":
        log_application(DB_PATH, {
            "job_title":    job_info.get("job_title"),
            "company":      job_info.get("company"),
            "job_board":    source_board,
            "date_applied": datetime.now().strftime("%Y-%m-%d"),
            "pay":          job_info.get("pay", "Not listed"),
            "address":      job_info.get("address"),
            "apply_url":    job_info.get("apply_url", url),
            "resume_path":  None,
            "easy_apply":   0,
        })
        log_action("already_applied",
                   job_info.get("job_title", ""), job_info.get("company", ""), "applied")
        print("  ✅ Logged as already applied.")
        return
    if step1_choice == "d":
        _log_dead(url, job_info.get("job_title", ""),
                  job_info.get("company", ""), source_board)
        return
    if step1_choice == "n":
        log_action("skipped",
                   job_info.get("job_title", ""), job_info.get("company", ""), "skipped")
        print("  Skipped.")
        return

    # ── Easy Apply check ──────────────────────────────────────────────────────
    easy_apply_info = detect_easy_apply(page)
    if easy_apply_info:
        job_info["is_easy_apply"] = True
        easy_url = easy_apply_info["url"]

        print("\n" + "=" * 60)
        print("⚡ EASY APPLY DETECTED")
        print(f"   Job Title : {job_info.get('job_title')}")
        print(f"   Company   : {job_info.get('company')}")
        print(f"   Board     : {source_board}")
        print()
        print("   Job URL:")
        print(f"   👉  {easy_url}")
        print()
        print("   Generating tailored documents for your reference...")
        print("=" * 60)

        selected = select_tag(job_info)
        docs     = generate_documents(job_info, selected, components)

        print(f"\n  📄 Opening documents for reference...")
        _open_file(docs["resume_path"])
        _open_file(docs["cover_letter_path"])

        print()
        print("  Did you apply to this job?")
        print("    [y] Yes — log as applied")
        print("    [n] No  — save to Apply Later")
        print("    [s] Skip — don't log anything")
        while True:
            answer = input("  Choice [y/n/s]: ").strip().lower()
            if answer in ("y", "n", "s"):
                break
            print("  Please enter y, n, or s.")

        if answer == "y":
            log_application(DB_PATH, {
                "job_title":    job_info.get("job_title"),
                "company":      job_info.get("company"),
                "job_board":    source_board,
                "date_applied": datetime.now().strftime("%Y-%m-%d"),
                "pay":          job_info.get("pay", "Not listed"),
                "address":      job_info.get("address"),
                "apply_url":    easy_url,
                "resume_path":  docs.get("resume_path"),
                "easy_apply":   1,
            })
            log_action("easy_apply_confirmed",
                       job_info.get("job_title", ""), job_info.get("company", ""), "applied")
            print("  ✅ Application logged.")
        elif answer == "n":
            log_apply_later(DB_PATH, {
                "job_title":  job_info.get("job_title"),
                "company":    job_info.get("company"),
                "job_board":  source_board,
                "date_saved": datetime.now().strftime("%Y-%m-%d"),
                "pay":        job_info.get("pay", "Not listed"),
                "address":    job_info.get("address"),
                "apply_url":  easy_url,
                "resume_path": docs.get("resume_path"),
            })
            log_action("easy_apply_later",
                       job_info.get("job_title", ""), job_info.get("company", ""), "apply_later")
            print("  📌 Saved to Apply Later.")
        return

    # ── Full pipeline path ────────────────────────────────────────────────────

    # ── STEP 2 ────────────────────────────────────────────────────────────────
    selected = select_tag(job_info)
    log_action("selected_tag",
               job_info.get("job_title", ""), job_info.get("company", ""), "success")

    # ── STEP 3 ────────────────────────────────────────────────────────────────
    docs = generate_documents(job_info, selected, components)
    log_action("generated_documents",
               job_info.get("job_title", ""), job_info.get("company", ""), "success",
               f"Resume: {docs['resume_path']}")

    print(f"\n  📄 Opening documents for review...")
    _open_file(docs["resume_path"])
    _open_file(docs["cover_letter_path"])

    if not ask_approval("Review and edit the documents, then confirm to proceed to form inspection."):
        log_action("step_3_approval", job_info.get("job_title", ""), job_info.get("company", ""), "rejected")
        print("  Stopped after Step 3. Documents saved.")
        return

    # ── STEPS 4–6: Inspect → Fill → (Next → Re-inspect →...) → Submit ──────────
    # Always start resolve from the original url so we follow the full
    # intermediate-page chain (e.g. listing → Phenom → Workday) rather than
    # jumping directly to whatever href read_job_description happened to extract.
    apply_url, page = resolve_apply_url(url, page)
    job_info["apply_url"] = apply_url

    if detect_job_not_found(page):
        _log_dead(apply_url, job_info.get("job_title", ""),
                  job_info.get("company", ""), source_board)
        return

    all_filled  = []
    all_flagged = []
    page_num    = 0

    while True:
        page_num += 1

        # ── Step 4: Inspect current page ─────────────────────────────────────
        fields = inspect_application_form(apply_url, page)

        if detect_job_not_found(page):
            _log_dead(apply_url, job_info.get("job_title", ""),
                      job_info.get("company", ""), source_board)
            return

        log_action("inspected_form",
                   job_info.get("job_title", ""), job_info.get("company", ""),
                   "success", f"page {page_num}, {len(fields)} fields")

        if len(fields) == 0:
            print("\n  ⚠️  No fields detected — the page may still be loading.")
            print("     Use [r] to wait, navigate to the form, then re-inspect.")

        step4_choice = ask_choice(
            f"Page {page_num} — {len(fields)} field(s) found",
            {
                "y": "Fill this page",
                "s": "Scan — save full page HTML to log file",
                "r": "Navigate manually — re-inspect when ready",
                "d": "Log as dead listing — job no longer exists",
                "n": "Stop here",
            },
        )
        if step4_choice == "d":
            _log_dead(page.url, job_info.get("job_title", ""),
                      job_info.get("company", ""), source_board)
            return
        if step4_choice == "n":
            log_action("step_4_approval",
                       job_info.get("job_title", ""), job_info.get("company", ""), "rejected")
            print("  Stopped. Documents saved.")
            return
        if step4_choice == "s":
            scan_path = scan_page_to_log(page, job_info)
            log_action("page_scan",
                       job_info.get("job_title", ""), job_info.get("company", ""),
                       "success", f"Log: {scan_path}")
            page_num -= 1
            continue
        if step4_choice == "r":
            input("\n  Navigate the browser to the application page, then press ENTER to re-inspect.")
            apply_url = page.url
            job_info["apply_url"] = apply_url
            page_num -= 1
            continue

        # ── Step 5: Detect ATS and fill this page ────────────────────────────
        current_url = page.url
        ats     = detect_ats(current_url)
        ats_mod = load_ats_module(ats)
        print(f"\n  ATS detected : {ats}")
        print("  Filling page — browser is visible, you can click in it at any time.")      

        # Workday may redirect to a login page — handle login before filling
        if ats == "workday":
            page.wait_for_load_state("networkidle")
            from job_agent_support.ats.workday import _is_login_page, _do_login, _handle_application_start_popup
            _handle_application_start_popup(page)
            if _is_login_page(page):
                _do_login(page)
                page.wait_for_load_state("networkidle", timeout=15000)

        if ats_mod:
            result = ats_mod["fill_page"](page, job_info, docs, components)
            if result.get("dead_listing"):
                _log_dead(page.url, job_info.get("job_title", ""),
                          job_info.get("company", ""), source_board)
                return
        else:
            filled_list, flagged_list = [], []
            _generic_fill(page, job_info, docs, components, fields, filled_list, flagged_list)
            result = {"filled_fields": filled_list, "flagged": flagged_list}
            print("\n  Form filled as much as possible — review the browser.")

        all_filled.extend(result.get("filled_fields", []))
        all_flagged.extend(result.get("flagged", []))
        log_action("filled_form",
                   job_info.get("job_title", ""), job_info.get("company", ""),
                   "success", f"page {page_num}, {len(result.get('filled_fields', []))} fields filled")

        # ── Check for next page ───────────────────────────────────────────────
        has_next = ats_mod is not None and ats_mod["has_next_page"](page)

        if has_next:
            next_choice = ask_choice(
                f"Page {page_num} filled — next page detected",
                {
                    "y": "Click Next and re-inspect",
                    "m": "I submitted manually — log and finish",
                    "n": "Stop here without submitting",
                },
            )
            if next_choice == "n":
                return
            if next_choice == "m":
                log_action("final_submission",
                           job_info.get("job_title", ""), job_info.get("company", ""),
                           "manual", "User submitted manually")
                _log_application(job_info, docs, url, source_board)
                print("  ✅ Application logged as manually submitted.")
                return
            ats_mod["click_next"](page)
            page.wait_for_load_state("networkidle", timeout=15000)
            apply_url = page.url
            continue  # back to Step 4 for the next page

        # ── Step 6: Final page — present summary and submit ───────────────────
        present_summary(job_info, docs,
                        {"filled_fields": all_filled, "flagged": all_flagged}, selected)

        submit_choice = ask_choice(
            "Final page reached — how would you like to proceed?",
            {
                "y": "Submit the application now",
                "m": "I already submitted manually — log and finish",
                "r": "Re-inspect and re-fill this page (page was slow to load)",
                "n": "Don't submit — keep documents only",
            },
            "Review any flagged items above before submitting.",
        )

        if submit_choice == "r":
            print("\n  Re-inspecting page...")
            page_num -= 1
            continue

        if submit_choice == "n":
            log_action("final_submission",
                       job_info.get("job_title", ""), job_info.get("company", ""),
                       "rejected", "User chose not to submit")
            print("\n  Application NOT submitted. Documents saved.")
            return

        if submit_choice == "m":
            log_action("final_submission",
                       job_info.get("job_title", ""), job_info.get("company", ""),
                       "manual", "User submitted manually")
            _log_application(job_info, docs, url, source_board)
            print("  ✅ Application logged as manually submitted.")
            return

        submit_application(page, job_info)
        log_action("submitted",
                   job_info.get("job_title", ""), job_info.get("company", ""), "success")
        _log_application(job_info, docs, url, source_board)
        print(f"\n✅ Application submitted.")
        print(f"   Resume      : {docs['resume_path']}")
        print(f"   Cover Letter: {docs['cover_letter_path']}")
        print(f"   Log         : {ACTIONS_LOG}")
        break


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Job Application Agent")
    parser.add_argument("--url",   help="Direct job posting URL (skips board picker)")
    parser.add_argument("--board", choices=["builtin", "governmentjobs", "indeed"],
                        help="Job board to browse")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("JOB APPLICATION AGENT")
    print("Pauses at each step for your approval.")
    print("="*60)

    _check_ollama()
    components = load_components()

    # ── Direct URL mode (no board login) ──────────────────────────────────────
    if args.url:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page    = browser.new_page()
            run_application_pipeline(args.url, page, components)
            browser.close()
        return

    # ── Board browse mode ─────────────────────────────────────────────────────
    board = args.board or pick_board()

    if board == "url":
        url = input("  Enter the job URL: ").strip()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            page    = browser.new_page()
            run_application_pipeline(url, page, components)
            browser.close()
        return

    credentials = load_credentials()
    board_creds = credentials.get(board, {})

    # Only governmentjobs requires stored credentials (builtin/indeed skip sign-in)
    if board == "governmentjobs":
        if not board_creds.get("email") or not board_creds.get("password"):
            print(f"\n  ⚠️  No credentials found for 'governmentjobs' in {CREDENTIALS_PATH}")
            print("  Fill in your email and password and re-run.")
            sys.exit(1)

    board_mod = load_board_module(board)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        # Login
        board_mod["login"](page, board_creds)
        board_mod["browse_jobs"](page)

        # Browse loop
        while True:
            listings_url = page.url  # remember so we can return after each job
            listings     = board_mod["get_job_listings"](page)

            if not listings:
                print("  No listings found on this page.")
            else:
                for listing in listings:
                    print(f"\n{'─'*60}")
                    print(f"  📌  {listing['title']}")
                    print(f"  🏢  {listing['company']}")

                    if already_applied(DB_PATH, url=listing["url"],
                                       job_title=listing["title"],
                                       company=listing["company"]):
                        print("  ⏭  Already applied — skipping.")
                        continue

                    if is_dead_listing(DB_PATH, url=listing["url"],
                                       job_title=listing["title"],
                                       company=listing["company"]):
                        print("  ⏭  Previously not found — skipping.")
                        continue

                    # Navigate to the job detail page for a real preview
                    try:
                        page.goto(listing["url"],
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_load_state("networkidle", timeout=15000)

                        if detect_job_not_found(page):
                            _log_dead(listing["url"], listing["title"],
                                      listing["company"], board)
                            page.goto(listings_url,
                                      wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_load_state("networkidle", timeout=15000)
                            continue

                        preview = page.evaluate("""() => {
                            const sel = [
                                '.job-post-item',
                                '[class*="jobDescription"]',
                                '[class*="job-description"]',
                                '[data-testid="jobDescriptionText"]',
                                '[class*="description-content"]',
                            ].join(', ');
                            const el = document.querySelector(sel);
                            const raw = el ? el.innerText : document.body.innerText;
                            return raw.trim().replace(/\\s+/g, ' ').slice(0, 500);
                        }""")
                        print(f"\n{preview}\n")
                    except Exception as exc:
                        print(f"  (Could not load detail page: {exc})")
                        preview = listing.get("snippet", "")
                        if preview:
                            print(f"  {preview[:300]}\n")

                    apply_choice = ask_choice(
                        f"Apply to '{listing['title']}' at {listing['company']}?",
                        {
                            "y": "Yes — start application",
                            "a": "Already applied — log it",
                            "n": "No  — skip",
                            "d": "Dead listing — job no longer exists",
                        },
                    )
                    if apply_choice == "a":
                        log_application(DB_PATH, {
                            "job_title":    listing["title"],
                            "company":      listing["company"],
                            "job_board":    board,
                            "date_applied": datetime.now().strftime("%Y-%m-%d"),
                            "pay":          "Not listed",
                            "address":      None,
                            "apply_url":    listing["url"],
                            "resume_path":  None,
                            "easy_apply":   0,
                        })
                        log_action("already_applied",
                                   listing["title"], listing["company"], "applied")
                        print("  ✅ Logged as already applied.")
                        page.goto(listings_url,
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        continue
                    if apply_choice == "d":
                        _log_dead(listing["url"], listing["title"],
                                  listing["company"], board)
                        page.goto(listings_url,
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        continue
                    if apply_choice == "n":
                        page.goto(listings_url,
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        continue

                    run_application_pipeline(
                        listing["url"], page, components, source_board=board
                    )

                    # Return to listings after the pipeline finishes
                    page.goto(listings_url,
                              wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_load_state("networkidle", timeout=15000)

            if not ask_approval("Move to the next page of results?"):
                break
            if not board_mod["go_to_next_page"](page):
                print("  No more pages.")
                break

        browser.close()


if __name__ == "__main__":
    main()

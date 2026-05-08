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
                                   is_apply_later, log_apply_later)
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

URL = "https://builtin.com/jobs/remote/hybrid/office/engineering/software-engineering/devops-platform-engineering/qa-test-engineering/security-engineering/systems-engineering/entry-level/junior?city=Los%20Angeles&state=California&country=USA&allLocations=true&page=2"

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

# ── Job-not-found detection ───────────────────────────────────────────────────

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

# ------------------------- Application Pipeline ------------------------------
def run_application_pipeline(url: str, page, components: dict, source_board: str = "direct"):
    """Run the full Steps 1-6 pipeline for a single job URL."""

    # ── STEP 1 ────────────────────────────────────────────────────────────────
    el = page.query_selector("h1.fw-extrabold.fs-xl.mb-sm span")
    if el:
        print(el.inner_text())
    
    _SELECTORS = [
        'a#applyButton',
        'a[ph-tevent="apply_click"][title="Apply Now"]',
        'a[aria-label="Apply to job"]'
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


    # Find apply button
    el = page.query_selector('#applyButton, a[aria-label="Apply to job"]')
    if el:
        href = el.get_attribute("href")
        print(href)
        page.goto(href)
    else: 
        for sel in _SELECTORS:
            try:
                el = current_page.query_selector(sel)
                if not (el and el.is_visible()):
                    
                    continue
                pre_click_url = current_page.url
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

    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(10000)  # milliseconds

    # Close any pop-ups
    close_btn = page.get_by_role("button", name=re.compile(r"close chatbot", re.IGNORECASE))
    if close_btn.count() > 0:
        close_btn.click()
        print("Closed chatbot.")
    
    detect_ats(page.url)

    page.wait_for_timeout(10000)  # milliseconds

    


# ── Components loader ─────────────────────────────────────────────────────────

def load_components() -> dict:
    if not COMPONENTS_PATH.exists():
        print(f"ERROR: components.yaml not found at {COMPONENTS_PATH}")
        sys.exit(1)
    with open(COMPONENTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)

def main(): 
    print("\n" + "="*60)
    print("JOB APPLICATION AGENT")
    print("Pauses at each step for your approval.")
    print("="*60)

    #_check_ollama()
    components = load_components()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        # ── Board browse mode ─────────────────────────────────────────────────────
        board = pick_board()
        board_mod = load_board_module(board)

        credentials = load_credentials()
        board_creds = credentials.get(board, {})

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

        page.wait_for_load_state("networkidle")

    

if __name__ == "__main__":
    main()
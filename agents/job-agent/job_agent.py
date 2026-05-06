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
import sys
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

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ACTIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
SCAN_LOG_DIR.mkdir(parents=True, exist_ok=True)

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
        # Check visible body text
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
    page.wait_for_timeout(2000)
    full_text = page.inner_text("body")
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

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
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

    response = ollama.chat(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
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
    print(f"{'='*60}")
    return _build_documents(
        primary_tag  = selected["primary_tag"],
        job_info     = job_info,
        components   = components,
        output_dir   = OUTPUT_DIR,
        backup_tags  = selected.get("backup_tags", []),
    )


# ── Apply-URL resolver (follows intermediate company career pages) ─────────────

def resolve_apply_url(url: str, page) -> str:
    """
    Navigate to url. If the page is an intermediate company career site
    (has a visible Apply Now button but no application form fields), click it
    and return the URL of the resulting page. Otherwise return url unchanged.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
    except Exception:
        return url

    # Already on a real application form — nothing to resolve
    has_form = page.evaluate("""() => {
        const fields = document.querySelectorAll('input:not([type=hidden]), textarea, select');
        return fields.length > 0;
    }""")
    if has_form:
        return url

    # No form fields — look for an Apply Now button and click it
    selectors = [
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
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                print(f"  ↪  Intermediate page detected — clicking Apply Now...")
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
                final_url = page.url
                print(f"     Landed on: {final_url}")
                return final_url
        except Exception:
            pass
    return url


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
    print(f"{'='*60}")

    if page.url != url:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

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


# ── Step 5: Fill form (page-by-page with approvals) ───────────────────────────

def fill_application_form(url: str, job_info: dict, docs: dict,
                          components: dict, fields: list, page) -> dict:
    print(f"\n{'='*60}")
    print("STEP 5 — Filling application form")
    print("  Browser is visible — you can click in it at any time.")
    print(f"{'='*60}")

    ats      = detect_ats(url)
    ats_mod  = load_ats_module(ats)
    print(f"  ATS detected: {ats}")

    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    all_filled  = []
    all_flagged = []

    if ats_mod:
        # ── Greenhouse / known ATS: page-by-page loop ─────────────────────────
        page_num = 1
        while True:
            print(f"\n  --- Form page {page_num} ---")
            result = ats_mod["fill_page"](page, job_info, docs, components)
            all_filled.extend(result.get("filled_fields", []))
            all_flagged.extend(result.get("flagged", []))

            if ats_mod["has_next_page"](page):
                if not ask_approval(f"Page {page_num} filled. Ready to click Next?"):
                    print("  Stopped — browser left open. Complete manually.")
                    input("  Press ENTER to close browser when done.")
                    break
                ats_mod["click_next"](page)
                page.wait_for_timeout(2000)
                page_num += 1
            else:
                # Final page
                print(f"\n  ✅ All pages filled ({page_num} page(s) total).")
                break
    else:
        # ── Generic fallback: fill what we can, then pause ────────────────────
        _generic_fill(page, job_info, docs, components, fields, all_filled, all_flagged)
        print("\n  ⏸  Form filled. Review the browser, then return here.")
        input("  Press ENTER when ready to proceed to submit step...")

    return {"filled_fields": all_filled, "flagged": all_flagged}


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
                    page.wait_for_timeout(3000)
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
    apply_url = resolve_apply_url(job_info.get("apply_url", url), page)
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

        step4_choice = ask_choice(
            f"Page {page_num} — {len(fields)} field(s) found",
            {
                "y": "Fill this page",
                "s": "Scan — save full page HTML to log file",
                "r": "Navigate manually — re-inspect when ready",
                "n": "Stop here",
            },
        )
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
            from job_agent_support.ats.workday import _is_login_page, _do_login
            if _is_login_page(page):
                _do_login(page)
                page.wait_for_timeout(2000)

        if ats_mod:
            result = ats_mod["fill_page"](page, job_info, docs, components)
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
            page.wait_for_timeout(2000)
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
                        page.wait_for_timeout(2000)

                        if detect_job_not_found(page):
                            _log_dead(listing["url"], listing["title"],
                                      listing["company"], board)
                            page.goto(listings_url,
                                      wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)
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

                    if not ask_approval(
                        f"Apply to '{listing['title']}' at {listing['company']}?"
                    ):
                        # Return to the listings page and move to the next card
                        page.goto(listings_url,
                                  wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                        continue

                    run_application_pipeline(
                        listing["url"], page, components, source_board=board
                    )

                    # Return to listings after the pipeline finishes
                    page.goto(listings_url,
                              wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)

            if not ask_approval("Move to the next page of results?"):
                break
            if not board_mod["go_to_next_page"](page):
                print("  No more pages.")
                break

        browser.close()


if __name__ == "__main__":
    main()

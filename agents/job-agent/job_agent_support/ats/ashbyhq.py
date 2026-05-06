"""
job_agent_support/ats/ashbyhq.py
Form filling for Ashby HQ ATS (jobs.ashbyhq.com).

Ashby applications are single-page; has_next_page always returns False.
System fields use consistent names (_systemfield_*); custom fields use UUIDs
matched by label text. Yes/No questions are answered via button clicks.
"""

from pathlib import Path

import yaml
from playwright.sync_api import Page

_ROOT              = Path(__file__).parent.parent.parent
_ATS_DEFAULTS_PATH = _ROOT / "data/job-apps/ats_defaults.yaml"


def _load_defaults(ats_key: str) -> dict:
    try:
        with open(_ATS_DEFAULTS_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get(ats_key, {})
    except Exception:
        return {}


_DEFAULTS = _load_defaults("ashbyhq")

# Loaded from data/job-apps/ats_defaults.yaml
_SYSTEM_FIELDS   = _DEFAULTS.get("system_fields",   {})
_LABEL_FIELD_MAP = _DEFAULTS.get("label_field_map", {})
_YESNO_ANSWERS   = _DEFAULTS.get("yesno_answers",   {})
_EEOC_LABEL_MAP  = _DEFAULTS.get("eeoc_label_map",  {})


def _navigate_to_application(page: Page) -> None:
    """Click 'Apply for this Job' if we're on the job description landing page."""
    if "/application" in page.url:
        return
    try:
        # Ashby is a React SPA — wait for the JS bundle to render before querying
        page.wait_for_load_state("networkidle", timeout=15000)
        btn = page.wait_for_selector(
            'a[href*="/application"] button, '
            'button:has-text("Apply for this Job"), '
            'button:has-text("Apply Now")',
            state="visible",
            timeout=15000,
        )
        if btn:
            btn.click()
            page.wait_for_url("**/application**", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=10000)
            print("  Navigated to Ashby application form.")
    except Exception as e:
        print(f"  Warning: could not click Apply button: {e}")


def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """Fill all visible fields on the Ashby application form."""
    _navigate_to_application(page)

    # Wait for the React form to render after navigation
    try:
        page.wait_for_selector(
            'input[name="_systemfield_name"], input[id="_systemfield_name"]',
            state="visible",
            timeout=15000,
        )
    except Exception:
        print("  Warning: timed out waiting for Ashby form to render.")

    personal = components.get("personal", {})
    filled_summary: list[dict] = []
    flagged: list[dict] = []

    field_data = {
        "full_name": personal.get("name", ""),
        "email":     personal.get("email", ""),
        "phone":     personal.get("phone", ""),
        "linkedin":  personal.get("linkedin", ""),
        "portfolio": personal.get("portfolio", ""),
        "github":    personal.get("github", personal.get("portfolio", "")),
    }

    # ── 1. System fields (_systemfield_name, _systemfield_email) ──────────────
    for field_name, data_key in _SYSTEM_FIELDS.items():
        try:
            el = page.query_selector(
                f'input[name="{field_name}"], input[id="{field_name}"]'
            )
            if el and el.is_visible():
                value = field_data.get(data_key, "")
                if value:
                    el.fill(value)
                    filled_summary.append({"field": field_name, "value": value[:50]})
        except Exception as e:
            flagged.append({"field": field_name, "error": str(e)})

    # ── 2. Resume upload ───────────────────────────────────────────────────────
    _upload_resume(page, docs.get("resume_path"), filled_summary, flagged)

    # ── 3. UUID-based text fields matched by label text ────────────────────────
    _fill_by_label(page, field_data, filled_summary, flagged)

    # ── 4. Yes/No button questions ─────────────────────────────────────────────
    _fill_yesno_questions(page, filled_summary, flagged)

    # ── 5. EEOC self-identification ─────────────────────────────────────────────
    _fill_eeoc(page, components.get("eeoc", {}), filled_summary, flagged)

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    return {"filled_fields": filled_summary, "flagged": flagged}


def _fill_by_label(page: Page, field_data: dict,
                   filled_summary: list, flagged: list) -> None:
    """Fill UUID-named inputs by matching their associated label text."""
    system_names = set(_SYSTEM_FIELDS.keys())
    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel'], input[type='url']"
    )
    for el in inputs:
        try:
            if not el.is_visible():
                continue
            field_name = el.get_attribute("name") or ""
            field_id   = el.get_attribute("id") or ""
            if field_name in system_names or field_id in system_names:
                continue
            if el.input_value():
                continue

            label_text = ""
            lookup_id = field_id or field_name
            if lookup_id:
                lbl = page.query_selector(f'label[for="{lookup_id}"]')
                if lbl:
                    label_text = lbl.inner_text().strip().lower()

            if not label_text:
                label_text = (
                    (el.get_attribute("aria-label") or "")
                    + " "
                    + (el.get_attribute("placeholder") or "")
                ).lower()

            for keyword, data_key in _LABEL_FIELD_MAP.items():
                if keyword in label_text:
                    value = field_data.get(data_key, "")
                    if value:
                        el.fill(value)
                        filled_summary.append({
                            "field": label_text.strip()[:40] or field_id,
                            "value": value[:80],
                        })
                    break
        except Exception as e:
            flagged.append({"field": field_id or "unknown", "error": str(e)})


def _fill_yesno_questions(page: Page, filled_summary: list, flagged: list) -> None:
    """
    Click Yes or No buttons for Ashby Yes/No questions matched by label text.
    Flags unrecognized questions for manual review.
    """
    containers = page.query_selector_all('div[class*="_yesno_"]')
    if not containers:
        containers = page.query_selector_all('div[class*="yesno"]')

    for container in containers:
        try:
            field_entry = container.evaluate_handle(
                "el => el.closest('[data-field-path]') || el.parentElement.parentElement"
            ).as_element()
            if not field_entry:
                continue

            label_el = field_entry.query_selector(
                'label[class*="_label_"], label[class*="question-title"]'
            )
            if not label_el:
                continue
            label_text = label_el.inner_text().strip().lower()

            answer = None
            for pattern, ans in _YESNO_ANSWERS.items():
                if pattern in label_text:
                    answer = ans
                    break

            if answer is None:
                flagged.append({
                    "field": label_text[:80],
                    "error": "Unrecognized Yes/No question — manual selection required",
                })
                continue

            buttons = container.query_selector_all('button[class*="_option_"]')
            for btn in buttons:
                if btn.inner_text().strip().lower() == answer.lower():
                    btn.click()
                    page.wait_for_timeout(300)
                    filled_summary.append({"field": label_text[:60], "value": answer})
                    break
        except Exception as e:
            flagged.append({"field": "yesno_question", "error": str(e)})


def _fill_eeoc(page: Page, eeoc_answers: dict,
               filled_summary: list, flagged: list) -> None:
    """Fill EEOC self-identification fields by scanning labels for keyword matches."""
    for label_el in page.query_selector_all("label"):
        try:
            if not label_el.is_visible():
                continue
            label_text = label_el.inner_text().strip().lower()

            answer = None
            for keyword, comp_key in _EEOC_LABEL_MAP.items():
                if keyword in label_text:
                    answer = eeoc_answers.get(comp_key, "")
                    break
            if not answer:
                continue

            lbl_for = label_el.get_attribute("for") or ""

            # Native <select>
            sel_el = page.query_selector(f'select[id="{lbl_for}"]') if lbl_for else None
            if sel_el and sel_el.is_visible():
                for opt in sel_el.query_selector_all("option"):
                    if answer.lower() in (opt.inner_text() or "").lower():
                        sel_el.select_option(value=opt.get_attribute("value") or "")
                        filled_summary.append({"field": label_text[:40], "value": answer})
                        break
                continue

            # Radio group in parent container
            parent = label_el.evaluate_handle(
                "el => el.closest('fieldset') || el.closest('div') || el.parentElement"
            ).as_element()
            if parent:
                for radio in parent.query_selector_all('input[type="radio"]'):
                    rid = radio.get_attribute("id") or ""
                    rl  = page.query_selector(f'label[for="{rid}"]')
                    if rl and answer.lower() in (rl.inner_text() or "").lower():
                        if not radio.is_checked():
                            radio.click()
                            page.wait_for_timeout(200)
                        filled_summary.append({"field": label_text[:40], "value": answer})
                        break
        except Exception as e:
            flagged.append({"field": "eeoc", "error": str(e)})


def _upload_resume(page: Page, file_path,
                   filled_summary: list, flagged: list) -> None:
    """Upload resume to Ashby's _systemfield_resume file input."""
    if not file_path:
        flagged.append({"field": "resume", "error": "No resume path provided"})
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": "resume", "error": f"File not found: {file_path}"})
        return
    try:
        el = page.query_selector(
            'input[type="file"][id="_systemfield_resume"], '
            'input[type="file"][name="_systemfield_resume"]'
        )
        if not el:
            el = page.query_selector('input[type="file"]')
        if el:
            el.set_input_files(str(path))
            filled_summary.append({"field": "resume", "value": path.name})
            print(f"  Uploaded resume: {path.name}")
        else:
            flagged.append({"field": "resume", "error": "No file input found"})
    except Exception as e:
        flagged.append({"field": "resume", "error": str(e)})


def has_next_page(page: Page) -> bool:
    """Ashby is single-page — no Next button."""
    return False


def click_next(page: Page) -> None:
    pass  # Ashby is single-page


def click_submit(page: Page) -> None:
    selectors = [
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(3000)
                print("  ✅ Submit clicked.")
                return
        except Exception:
            pass
    print("  Warning: could not find Submit button — check browser manually.")
    input("  Press ENTER after submitting manually...")

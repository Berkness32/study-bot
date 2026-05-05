"""
job_agent_support/ats/workday.py
Form filling for Workday ATS (*.myworkdayjobs.com).

Workday is a React-rendered multi-page wizard. Fields use data-automation-id
attributes. We fill what can be reliably targeted; complex sections (work history
date pickers, education dropdowns, EEOC) are flagged for manual completion.

Typical page order:
  1. My Information  — name, address, phone, email, source
  2. My Experience   — resume upload, work history, education
  3. App Questions   — open-ended / screening questions
  4. Voluntary Disclosures — EEOC (skipped; manual)
  5. Review & Submit
"""

from pathlib import Path
from playwright.sync_api import Page

# Maps data-automation-id fragments → personal component key
AUTOMATION_FIELDS = {
    "legalNameSection_firstName":  "first_name",
    "legalNameSection_lastName":   "last_name",
    "addressSection_addressLine1": "location",
    "phone-number":                "phone",
    "email":                       "email",
    "linkedin":                    "linkedin",
}


def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """Fill visible Workday fields on the current wizard page."""
    personal        = components.get("personal", {})
    filled_summary  = []
    flagged         = []

    name_parts = personal.get("name", "").split()
    field_data = {
        "first_name": name_parts[0] if name_parts else "",
        "last_name":  " ".join(name_parts[1:]),
        "email":      personal.get("email", ""),
        "phone":      personal.get("phone", ""),
        "linkedin":   personal.get("linkedin", ""),
        "location":   personal.get("location", ""),
    }

    page.wait_for_timeout(1500)  # Workday renders fields asynchronously

    # ── data-automation-id text fields ────────────────────────────────────────
    for automation_id, data_key in AUTOMATION_FIELDS.items():
        value = field_data.get(data_key, "")
        if not value:
            continue
        try:
            el = page.query_selector(f'[data-automation-id="{automation_id}"] input, '
                                     f'input[data-automation-id="{automation_id}"]')
            if el and el.is_visible():
                el.triple_click()
                el.type(value, delay=30)
                filled_summary.append({"field": automation_id, "value": value[:50]})
        except Exception as e:
            flagged.append({"field": automation_id, "error": str(e)})

    # ── Generic visible text/email/tel inputs not yet targeted ────────────────
    _fill_generic_inputs(page, field_data, filled_summary, flagged)

    # ── Resume upload (Workday uses a custom drop-zone widget) ────────────────
    _upload_resume(page, docs.get("resume_path"), filled_summary, flagged)

    # ── "How did you hear" / source dropdown ─────────────────────────────────
    _answer_source(page, filled_summary, flagged)

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    print("  ⚠️  Work history dates, education details, and EEOC fields require manual input.")
    return {"filled_fields": filled_summary, "flagged": flagged}


def _fill_generic_inputs(page: Page, field_data: dict,
                         filled_summary: list, flagged: list):
    """Fill any remaining unlabeled visible text inputs by placeholder/aria-label."""
    hint_map = {
        "first":    "first_name",
        "last":     "last_name",
        "email":    "email",
        "phone":    "phone",
        "linkedin": "linkedin",
    }
    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel']"
    )
    for el in inputs:
        try:
            if not el.is_visible():
                continue
            placeholder = (el.get_attribute("placeholder") or "").lower()
            aria_label  = (el.get_attribute("aria-label")  or "").lower()
            hint        = placeholder + " " + aria_label
            for keyword, data_key in hint_map.items():
                if keyword in hint:
                    value = field_data.get(data_key, "")
                    if value and not el.input_value():
                        el.triple_click()
                        el.type(value, delay=30)
                        filled_summary.append({"field": hint.strip()[:40], "value": value[:50]})
                    break
        except Exception:
            pass


def _upload_resume(page: Page, file_path, filled_summary: list, flagged: list):
    """Handle Workday's custom file upload widget."""
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": "resume", "error": f"File not found: {file_path}"})
        return

    selectors = [
        '[data-automation-id="file-upload-input-ref"]',
        '[data-automation-id="resume-upload-section"] input[type="file"]',
        'input[type="file"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.set_input_files(str(path))
                filled_summary.append({"field": "resume", "value": path.name})
                print(f"  Uploaded: resume → {path.name}")
                page.wait_for_timeout(2000)
                return
        except Exception as e:
            flagged.append({"field": "resume", "error": str(e)})
            return

    flagged.append({"field": "resume", "error": "No file input found on this page"})


def _answer_source(page: Page, filled_summary: list, flagged: list):
    """Select 'LinkedIn' for the 'How did you hear about us' dropdown if present."""
    try:
        el = page.query_selector('[data-automation-id="sourceType"] select, '
                                  'select[data-automation-id*="source"]')
        if not el or not el.is_visible():
            return
        opts = el.query_selector_all("option")
        for opt in opts:
            if "linkedin" in (opt.inner_text().lower()):
                opt_val = opt.get_attribute("value")
                el.select_option(opt_val)
                filled_summary.append({"field": "source", "value": "LinkedIn"})
                return
    except Exception:
        pass


_NEXT_SELECTORS = [
    '[data-automation-id="bottom-navigation-next-button"]',
    'button:has-text("Save & Continue")',
    'button:has-text("Save and Continue")',
    'button:has-text("Next")',
]


def has_next_page(page: Page) -> bool:
    """Return True if a Workday continue/next button is present and enabled."""
    for sel in _NEXT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                if "submit" not in (el.inner_text() or "").lower():
                    return True
        except Exception:
            pass
    return False


def click_next(page: Page) -> None:
    for sel in _NEXT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                if "submit" not in (el.inner_text() or "").lower():
                    el.click()
                    page.wait_for_timeout(2500)
                    return
        except Exception:
            pass
    print("  Warning: could not find Save & Continue / Next button — check browser manually.")


def click_submit(page: Page) -> None:
    selectors = [
        '[data-automation-id="bottom-navigation-next-button"]',  # final page reuses this
        'button:has-text("Submit")',
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                el.click()
                page.wait_for_timeout(3000)
                print("  ✅ Submit clicked.")
                return
        except Exception:
            pass
    print("  Warning: could not find Submit button — check browser manually.")
    input("  Press ENTER after submitting manually...")

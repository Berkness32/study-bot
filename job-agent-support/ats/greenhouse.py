"""
job-agent-support/ats/greenhouse.py
Handles page-by-page form filling, Next, and Submit for Greenhouse ATS.
Extracted and expanded from job_agent.py fill_application_form().
"""

import json
from pathlib import Path
from playwright.sync_api import Page


# ── Standard Greenhouse field map ─────────────────────────────────────────────
# Maps common Greenhouse input IDs / names to component keys
FIELD_MAP = {
    "first_name":   "first_name",
    "last_name":    "last_name",
    "email":        "email",
    "phone":        "phone",
    "resume":       "resume",       # file upload — handled separately
    "cover_letter": "cover_letter", # file upload — handled separately
    "website":      "portfolio",
    "linkedin":     "linkedin",
    "location":     "location",
}

# Known dropdown/radio answers for standard Greenhouse compliance questions
GREENHOUSE_STANDARD_ANSWERS = {
    "At the time of applying, are you 18 years of age or older": "Yes",
    "Are you authorized to work in the United States":           "Yes",
    "Will you now, or in the future, require sponsorship":       "No",
    "Have you ever worked for a Sony company previously":        "No",
}


def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """
    Fill all visible fields on the current Greenhouse form page.
    Returns {"filled_fields": [...], "flagged": [...]}
    """
    personal      = components.get("personal", {})
    filled_summary = []
    flagged        = []

    field_data = {
        "first_name": personal.get("name", "").split()[0],
        "last_name":  " ".join(personal.get("name", "").split()[1:]),
        "email":      personal.get("email", ""),
        "phone":      personal.get("phone", ""),
        "portfolio":  personal.get("portfolio", ""),
        "linkedin":   personal.get("linkedin", ""),
        "location":   personal.get("location", ""),
    }

    # ── Text / email / tel inputs ──────────────────────────────────────────────
    inputs = page.query_selector_all("input[type='text'], input[type='email'], input[type='tel'], input[type='url'], textarea")
    for el in inputs:
        try:
            field_id   = el.get_attribute("id")   or ""
            field_name = el.get_attribute("name")  or ""
            key        = FIELD_MAP.get(field_id) or FIELD_MAP.get(field_name)
            value      = field_data.get(key, "") if key else ""

            if value and el.is_visible():
                el.fill(value)
                filled_summary.append({"field": field_id or field_name, "value": value[:50]})
        except Exception as e:
            flagged.append({"id": field_id, "name": field_name, "error": str(e)})

    # ── File uploads ───────────────────────────────────────────────────────────
    _upload_file(page, "resume",        docs.get("resume_path"),       filled_summary, flagged)
    _upload_file(page, "cover_letter",  docs.get("cover_letter_path"), filled_summary, flagged)

    # ── Standard dropdown / radio compliance questions ─────────────────────────
    for question_fragment, answer in GREENHOUSE_STANDARD_ANSWERS.items():
        try:
            matched = page.evaluate(f"""() => {{
                const labels = [...document.querySelectorAll('label, legend, div, span')];
                const lbl = labels.find(l => l.innerText && l.innerText.includes({json.dumps(question_fragment)}));
                if (!lbl) return false;
                const parent = lbl.closest('div.field, fieldset, div') || lbl.parentElement;
                if (!parent) return false;
                const sel = parent.querySelector('select');
                if (sel) {{
                    const opts = [...sel.options];
                    const opt = opts.find(o => o.text.trim().toLowerCase().startsWith({json.dumps(answer.lower())}));
                    if (opt) {{ sel.value = opt.value; sel.dispatchEvent(new Event('change', {{bubbles:true}})); return true; }}
                }}
                const radios = [...(parent.querySelectorAll('input[type=radio]') || [])];
                const radio = radios.find(r => {{
                    const rl = document.querySelector('label[for="'+r.id+'"]');
                    return rl && rl.innerText.trim().toLowerCase().startsWith({json.dumps(answer.lower())});
                }});
                if (radio) {{ radio.click(); return true; }}
                return false;
            }}""")
            if matched:
                filled_summary.append({"field": question_fragment[:40], "value": answer})
                print(f"  Answered: '{question_fragment[:40]}' → {answer}")
        except Exception as e:
            flagged.append({"name": question_fragment[:40], "error": str(e)})

    # ── "How did you hear" ─────────────────────────────────────────────────────
    try:
        el = page.query_selector("#question_15583143004")
        if el and el.is_visible():
            el.fill("LinkedIn")
            filled_summary.append({"field": "How did you hear", "value": "LinkedIn"})
    except Exception:
        pass

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    return {"filled_fields": filled_summary, "flagged": flagged}


def _upload_file(page: Page, field_key: str, file_path, filled_summary: list, flagged: list):
    """Attach a file to a Greenhouse upload input."""
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": field_key, "error": f"File not found: {file_path}"})
        return
    try:
        selector = f'input[type="file"][name*="{field_key}"], input[type="file"][id*="{field_key}"]'
        el = page.query_selector(selector)
        if el:
            el.set_input_files(str(path))
            filled_summary.append({"field": field_key, "value": path.name})
            print(f"  Uploaded: {field_key} → {path.name}")
    except Exception as e:
        flagged.append({"field": field_key, "error": str(e)})


def has_next_page(page: Page) -> bool:
    """Return True if a Next / Continue button is present and visible."""
    selectors = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'input[type="submit"][value*="Next"]',
        'a:has-text("Next")',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def click_next(page: Page) -> None:
    """Click the Next / Continue button."""
    selectors = [
        'button:has-text("Next")',
        'button:has-text("Continue")',
        'input[type="submit"][value*="Next"]',
        'a:has-text("Next")',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                return
        except Exception:
            pass
    print("  Warning: could not find Next button — check browser manually.")


def click_submit(page: Page) -> None:
    """Click the final Submit button."""
    selectors = [
        'button:has-text("Submit")',
        'input[type="submit"][value*="Submit"]',
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

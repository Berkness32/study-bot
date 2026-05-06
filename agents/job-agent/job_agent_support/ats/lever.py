"""
job_agent_support/ats/lever.py
Form filling for Lever ATS (jobs.lever.co).

Lever applications are single-page; has_next_page always returns False.
EEOC / demographic fields at the bottom are flagged for manual completion.
"""

from pathlib import Path
from playwright.sync_api import Page

# Maps Lever input name/id → personal component key
FIELD_MAP = {
    "name":           "full_name",
    "full_name":      "full_name",
    "first_name":     "first_name",
    "last_name":      "last_name",
    "email":          "email",
    "phone":          "phone",
    "urls[LinkedIn]": "linkedin",
    "urls[Portfolio]":"portfolio",
    "urls[Github]":   "portfolio",
    "urls[Other]":    "portfolio",
    "org":            "location",
}


def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    personal        = components.get("personal", {})
    filled_summary  = []
    flagged         = []

    name_parts = personal.get("name", "").split()
    field_data = {
        "full_name":  personal.get("name", ""),
        "first_name": name_parts[0] if name_parts else "",
        "last_name":  " ".join(name_parts[1:]),
        "email":      personal.get("email", ""),
        "phone":      personal.get("phone", ""),
        "linkedin":   personal.get("linkedin", ""),
        "portfolio":  personal.get("portfolio", ""),
        "location":   personal.get("location", ""),
    }

    # ── Text / email / tel / url inputs ───────────────────────────────────────
    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel'], input[type='url']"
    )
    for el in inputs:
        try:
            field_name = el.get_attribute("name") or ""
            field_id   = el.get_attribute("id")   or ""
            key   = FIELD_MAP.get(field_name) or FIELD_MAP.get(field_id)
            value = field_data.get(key, "") if key else ""
            if value and el.is_visible():
                el.fill(value)
                filled_summary.append({"field": field_name or field_id, "value": value[:50]})
        except Exception as e:
            flagged.append({"name": field_name, "error": str(e)})

    # ── Cover letter textarea (Lever uses a free-text box, not a file upload) ─
    try:
        cl_el = page.query_selector(
            "textarea#comments, textarea[name='comments'], "
            "textarea[placeholder*='cover' i], textarea[placeholder*='letter' i]"
        )
        if cl_el and cl_el.is_visible():
            # Read plain text from cover letter docx if possible, else placeholder
            cover_text = _read_docx_text(docs.get("cover_letter_path"))
            cl_el.fill(cover_text or f"Please see attached cover letter.")
            filled_summary.append({"field": "comments/cover", "value": "(cover letter text)"})
    except Exception as e:
        flagged.append({"field": "comments", "error": str(e)})

    # ── Resume file upload ─────────────────────────────────────────────────────
    _upload_file(page, "resume", docs.get("resume_path"), filled_summary, flagged,
                 extra_selectors=['input[type="file"]'])

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    print("  ⚠️  EEOC / demographic fields at the bottom require manual selection.")
    return {"filled_fields": filled_summary, "flagged": flagged}


def _read_docx_text(path) -> str:
    """Extract plain text from a .docx file. Returns empty string on failure."""
    if not path:
        return ""
    try:
        from docx import Document
        doc = Document(path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        return ""


def _upload_file(page: Page, field_key: str, file_path,
                 filled_summary: list, flagged: list,
                 extra_selectors: list | None = None):
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": field_key, "error": f"File not found: {file_path}"})
        return
    selectors = [
        f'input[type="file"][name*="{field_key}"]',
        f'input[type="file"][id*="{field_key}"]',
    ] + (extra_selectors or [])
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.set_input_files(str(path))
                filled_summary.append({"field": field_key, "value": path.name})
                print(f"  Uploaded: {field_key} → {path.name}")
                return
        except Exception as e:
            flagged.append({"field": field_key, "error": str(e)})
            return
    flagged.append({"field": field_key, "error": "No file input found"})


def has_next_page(page: Page) -> bool:
    """Lever is single-page — no Next button."""
    return False


def click_next(page: Page) -> None:
    pass  # Lever is single-page


def click_submit(page: Page) -> None:
    selectors = [
        '#btn-submit',
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

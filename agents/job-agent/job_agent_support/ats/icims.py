"""
job_agent_support/ats/icims.py
Form filling for iCIMS ATS (careers-*.icims.com).

iCIMS is a multi-page application wizard with standard HTML form fields.
Fields are identified by label text or standard name/id attributes.
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


_DEFAULTS = _load_defaults("icims")

# Loaded from data/job-apps/ats_defaults.yaml
FIELD_MAP       = _DEFAULTS.get("field_map",      {})
LABEL_HINTS     = _DEFAULTS.get("label_hints",    {})
_EEOC_LABEL_MAP = _DEFAULTS.get("eeoc_label_map", {})


def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """Fill all visible iCIMS fields on the current wizard page."""
    personal        = components.get("personal", {})
    filled_summary  = []
    flagged         = []

    name_parts  = personal.get("name", "").split()
    loc_parts   = personal.get("location", "").split(",")
    field_data  = {
        "first_name": name_parts[0] if name_parts else "",
        "last_name":  " ".join(name_parts[1:]),
        "email":      personal.get("email", ""),
        "phone":      personal.get("phone", ""),
        "linkedin":   personal.get("linkedin", ""),
        "portfolio":  personal.get("portfolio", ""),
        "location":   personal.get("location", ""),
        "city":       loc_parts[0].strip() if loc_parts else "",
    }

    # ── Name/id-matched inputs ─────────────────────────────────────────────────
    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel'], input[type='url']"
    )
    for el in inputs:
        try:
            if not el.is_visible():
                continue
            field_id   = (el.get_attribute("id")   or "").lower()
            field_name = (el.get_attribute("name")  or "").lower()
            key   = FIELD_MAP.get(field_id) or FIELD_MAP.get(field_name)
            value = field_data.get(key, "") if key else ""
            if value:
                el.fill(value)
                filled_summary.append({"field": field_id or field_name, "value": value[:50]})
        except Exception as e:
            flagged.append({"name": field_name, "error": str(e)})

    # ── Label-hint fallback for any still-empty inputs ─────────────────────────
    _fill_by_labels(page, field_data, filled_summary, flagged)

    # ── Resume upload ──────────────────────────────────────────────────────────
    _upload_file(page, "resume", docs.get("resume_path"), filled_summary, flagged)

    # ── Cover letter upload (iCIMS often has a second upload slot) ────────────
    _upload_file(page, "cover_letter", docs.get("cover_letter_path"), filled_summary, flagged)

    # ── EEOC self-identification ────────────────────────────────────────────────
    _fill_eeoc(page, components.get("eeoc", {}), filled_summary, flagged)

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    return {"filled_fields": filled_summary, "flagged": flagged}


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


def _fill_by_labels(page: Page, field_data: dict,
                    filled_summary: list, flagged: list):
    """Find inputs by scanning nearby label text — catches non-standard name attrs."""
    try:
        results = page.evaluate("""(labelHints) => {
            const labels = [...document.querySelectorAll('label')];
            const filled = [];
            for (const lbl of labels) {
                const text = lbl.innerText.trim().toLowerCase();
                const hint = Object.keys(labelHints).find(h => text.startsWith(h));
                if (!hint) continue;
                const key = labelHints[hint];
                // find associated input
                let input = null;
                if (lbl.htmlFor) input = document.getElementById(lbl.htmlFor);
                if (!input) input = lbl.nextElementSibling?.querySelector?.('input');
                if (!input) input = lbl.closest('div,fieldset')?.querySelector?.('input');
                if (!input || !input.offsetParent) continue;  // not visible
                if (input.value) continue;                    // already filled
                filled.push({ id: input.id, name: input.name, key });
            }
            return filled;
        }""", LABEL_HINTS)

        for item in results:
            value = field_data.get(item["key"], "")
            if not value:
                continue
            try:
                sel = f'#{item["id"]}' if item["id"] else f'[name="{item["name"]}"]'
                el  = page.query_selector(sel)
                if el and el.is_visible() and not el.input_value():
                    el.fill(value)
                    filled_summary.append({"field": item["id"] or item["name"], "value": value[:50]})
            except Exception as e:
                flagged.append({"field": item.get("id", item.get("name")), "error": str(e)})
    except Exception:
        pass


def _upload_file(page: Page, field_key: str, file_path,
                 filled_summary: list, flagged: list):
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": field_key, "error": f"File not found: {file_path}"})
        return
    selectors = [
        f'input[type="file"][name*="{field_key}"]',
        f'input[type="file"][id*="{field_key}"]',
        f'input[type="file"][accept*=".pdf"]',
        'input[type="file"]',
    ]
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
    flagged.append({"field": field_key, "error": "No matching file input found"})


def has_next_page(page: Page) -> bool:
    """Return True if a Next / Continue button is present and not the final Submit."""
    selectors = [
        'input[type="submit"][value*="Next" i]',
        'input[type="submit"][value*="Continue" i]',
        'button:has-text("Next")',
        'button:has-text("Continue")',
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
    selectors = [
        'input[type="submit"][value*="Next" i]',
        'input[type="submit"][value*="Continue" i]',
        'button:has-text("Next")',
        'button:has-text("Continue")',
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
    selectors = [
        'input[type="submit"][value*="Submit" i]',
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

"""
job_agent_support/ats/greenhouse.py
Handles page-by-page form filling, Next, and Submit for Greenhouse ATS.

Greenhouse uses React Select for all dropdowns (compliance AND EEOC).
Native <select> / radio fallback is kept for older Greenhouse form variants.
"""

from pathlib import Path
from playwright.sync_api import Page


# ── Text field map ─────────────────────────────────────────────────────────────
FIELD_MAP = {
    "first_name":   "first_name",
    "last_name":    "last_name",
    "email":        "email",
    "phone":        "phone",
    "resume":       "resume",
    "cover_letter": "cover_letter",
    "website":      "portfolio",
    "linkedin":     "linkedin",
    "location":     "location",
}

# ── Compliance question answers (matched by label text substring) ───────────────
# Keys are lowercase substrings; values are the option text to select.
_COMPLIANCE_ANSWERS = {
    "legally authorized to work in the united states":  "Yes",
    "authorized to work in the united states":          "Yes",
    "require sponsorship for employment visa":          "No",
    "will you now, or in the future, require":         "No",
    "immigration related support or sponsorship":       "No",
    "are you 18 years of age":                         "Yes",
    "how did you hear":                                "LinkedIn",
    "how did you find":                                "LinkedIn",
    "have you ever worked for":                        "No",
}

# ── EEOC defaults ──────────────────────────────────────────────────────────────
# Override any of these in components.yaml under an `eeoc:` key.
_EEOC_DEFAULTS = {
    "gender":             "Decline To Self Identify",
    "hispanic_ethnicity": "Decline To Self Identify",
    "race":               "Decline To Self Identify",
    "veteran_status":     "I don't wish to answer",
    "disability_status":  "I do not want to answer",
}


# ── React Select helper ────────────────────────────────────────────────────────

def _fill_react_select(page: Page, field_id: str, desired_text: str,
                       filled_summary: list, flagged: list,
                       display_label: str = "") -> None:
    """
    Open a Greenhouse React Select dropdown by its input id and pick desired_text.
    Tries exact match first, then prefix match.
    """
    label = display_label or field_id
    try:
        # The clickable control is a sibling of the <label for=field_id>
        control = page.query_selector(
            f'label[for="{field_id}"] ~ div .select__control, '
            f'label[id="{field_id}-label"] ~ div .select__control'
        )
        if not control or not control.is_visible():
            flagged.append({"field": label, "error": "React Select control not found"})
            return

        control.click()
        page.wait_for_timeout(500)

        matched = page.evaluate("""([text]) => {
            const opts = [...document.querySelectorAll('[class*="-option"]')];
            // exact match
            const exact = opts.find(o => o.innerText.trim() === text);
            if (exact) { exact.click(); return text; }
            // prefix match
            const prefix = opts.find(o =>
                o.innerText.trim().toLowerCase().startsWith(text.toLowerCase())
            );
            if (prefix) { prefix.click(); return prefix.innerText.trim(); }
            return null;
        }""", [desired_text])

        if matched:
            filled_summary.append({"field": label, "value": matched[:60]})
            print(f"  Selected: {label} → {matched!r}")
        else:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
            flagged.append({"field": label, "error": f"Option {desired_text!r} not in menu"})

        page.wait_for_timeout(300)

    except Exception as e:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        flagged.append({"field": label, "error": str(e)})


# ── Native select / radio fallback (older Greenhouse forms) ───────────────────

def _fill_native_select_or_radio(page: Page, question_fragment: str,
                                  answer: str,
                                  filled_summary: list, flagged: list) -> bool:
    """Try to fill a native <select> or radio group matching question_fragment."""
    try:
        matched = page.evaluate(f"""([frag, ans]) => {{
            const labels = [...document.querySelectorAll('label, legend')];
            const lbl = labels.find(l => l.innerText &&
                l.innerText.toLowerCase().includes(frag.toLowerCase()));
            if (!lbl) return false;
            const parent = lbl.closest('div.field, fieldset, div') || lbl.parentElement;
            if (!parent) return false;
            const sel = parent.querySelector('select');
            if (sel) {{
                const opt = [...sel.options].find(
                    o => o.text.trim().toLowerCase().startsWith(ans.toLowerCase())
                );
                if (opt) {{
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
            }}
            const radios = [...parent.querySelectorAll('input[type=radio]')];
            const radio = radios.find(r => {{
                const rl = document.querySelector('label[for="' + r.id + '"]');
                return rl && rl.innerText.trim().toLowerCase().startsWith(ans.toLowerCase());
            }});
            if (radio) {{ radio.click(); return true; }}
            return false;
        }}""", [question_fragment, answer])
        if matched:
            filled_summary.append({"field": question_fragment[:40], "value": answer})
            print(f"  Answered (native): '{question_fragment[:40]}' → {answer}")
        return matched
    except Exception as e:
        flagged.append({"name": question_fragment[:40], "error": str(e)})
        return False


# ── Compliance question filler ─────────────────────────────────────────────────

def _fill_compliance_questions(page: Page, filled_summary: list, flagged: list) -> None:
    """
    Find all React Select inputs whose labels match a compliance pattern and
    fill them. Falls back to native select/radio for older Greenhouse variants.
    """
    # Collect all React Select inputs on the page
    react_inputs = page.query_selector_all('input.select__input')
    for inp in react_inputs:
        try:
            field_id = inp.get_attribute("id") or ""
            if not field_id or field_id in _EEOC_DEFAULTS:
                continue  # EEOC fields handled separately

            label_el = page.query_selector(f'label[for="{field_id}"]')
            if not label_el:
                continue
            label_text = label_el.inner_text().strip().lower()

            for pattern, answer in _COMPLIANCE_ANSWERS.items():
                if pattern in label_text:
                    _fill_react_select(
                        page, field_id, answer,
                        filled_summary, flagged,
                        display_label=label_el.inner_text().strip()[:60],
                    )
                    break
        except Exception:
            pass

    # Native fallback for any patterns not yet matched
    for pattern, answer in _COMPLIANCE_ANSWERS.items():
        already = any(pattern in (f.get("field", "").lower()) for f in filled_summary)
        if not already:
            _fill_native_select_or_radio(page, pattern, answer, filled_summary, flagged)


# ── EEOC filler ────────────────────────────────────────────────────────────────

def _fill_eeoc(page: Page, eeoc_prefs: dict,
               filled_summary: list, flagged: list) -> None:
    """Fill EEOC self-identification React Select dropdowns."""
    answers = {**_EEOC_DEFAULTS, **eeoc_prefs}
    for field_id, desired_text in answers.items():
        # Check whether this field exists on the current page
        control = page.query_selector(
            f'label[for="{field_id}"] ~ div .select__control, '
            f'label[id="{field_id}-label"] ~ div .select__control'
        )
        if not control:
            continue
        label_el = page.query_selector(f'label[for="{field_id}"], label[id="{field_id}-label"]')
        display = label_el.inner_text().strip() if label_el else field_id
        _fill_react_select(page, field_id, desired_text, filled_summary, flagged,
                           display_label=display)


# ── Main fill_page ─────────────────────────────────────────────────────────────

def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """
    Fill all visible fields on the current Greenhouse form page.
    Returns {"filled_fields": [...], "flagged": [...]}
    """
    personal        = components.get("personal", {})
    eeoc_prefs      = components.get("eeoc", {})
    filled_summary  = []
    flagged         = []

    name_parts = personal.get("name", "").split()
    field_data = {
        "first_name": name_parts[0] if name_parts else "",
        "last_name":  " ".join(name_parts[1:]),
        "email":      personal.get("email", ""),
        "phone":      personal.get("phone", ""),
        "portfolio":  personal.get("portfolio", ""),
        "linkedin":   personal.get("linkedin", ""),
        "location":   personal.get("location", ""),
    }

    # ── 1. Text / email / tel / url inputs ────────────────────────────────────
    inputs = page.query_selector_all(
        "input[type='text'], input[type='email'], input[type='tel'], "
        "input[type='url'], textarea"
    )
    for el in inputs:
        field_id = field_name = ""
        try:
            field_id   = el.get_attribute("id")   or ""
            field_name = el.get_attribute("name")  or ""
            # Skip React Select inputs (class="select__input") — handled below
            if "select__input" in (el.get_attribute("class") or ""):
                continue
            key   = FIELD_MAP.get(field_id) or FIELD_MAP.get(field_name)
            value = field_data.get(key, "") if key else ""
            if value and el.is_visible():
                el.fill(value)
                filled_summary.append({"field": field_id or field_name, "value": value[:50]})
        except Exception as e:
            flagged.append({"id": field_id, "name": field_name, "error": str(e)})

    # ── 2. Country React Select (always "United States") ─────────────────────
    country_ctrl = page.query_selector(
        'label[for="country"] ~ div .select__control, '
        'label[id="country-label"] ~ div .select__control'
    )
    if country_ctrl and country_ctrl.is_visible():
        _fill_react_select(page, "country", "United States",
                           filled_summary, flagged, display_label="Country")

    # ── 3. File uploads ────────────────────────────────────────────────────────
    _upload_file(page, "resume",       docs.get("resume_path"),       filled_summary, flagged)
    _upload_file(page, "cover_letter", docs.get("cover_letter_path"), filled_summary, flagged)

    # ── 4. URL fields by label text (website / LinkedIn) ─────────────────────
    _fill_url_fields(page, field_data, filled_summary, flagged)

    # ── 5. Compliance question dropdowns (React Select + native fallback) ──────
    _fill_compliance_questions(page, filled_summary, flagged)

    # ── 6. EEOC self-identification dropdowns ─────────────────────────────────
    _fill_eeoc(page, eeoc_prefs, filled_summary, flagged)

    print(f"  Filled {len(filled_summary)} field(s), {len(flagged)} flagged.")
    return {"filled_fields": filled_summary, "flagged": flagged}


# Keyword → field_data key for URL label matching
_URL_LABEL_HINTS = {
    "linkedin":  "linkedin",
    "website":   "portfolio",
    "portfolio": "portfolio",
    "github":    "portfolio",   # fall back to portfolio if no separate github value
    "personal":  "portfolio",
}


def _fill_url_fields(page: Page, field_data: dict,
                     filled_summary: list, flagged: list) -> None:
    """
    Fill URL/text inputs whose associated label mentions a known link type.
    Greenhouse often uses dynamic IDs (e.g. job_application_urls_attributes_0_url)
    so we match by label text rather than by id/name.
    Skips any input that already has a value.
    """
    already_filled = {f["field"] for f in filled_summary}

    inputs = page.query_selector_all(
        "input[type='url'], input[type='text'], input[type='email']"
    )
    for el in inputs:
        try:
            if not el.is_visible():
                continue
            if el.input_value():      # already filled — don't overwrite
                continue
            if "select__input" in (el.get_attribute("class") or ""):
                continue

            field_id = el.get_attribute("id") or ""
            if field_id in already_filled:
                continue

            # Find the label associated with this input
            label_text = ""
            if field_id:
                lbl = page.query_selector(f'label[for="{field_id}"]')
                if lbl:
                    label_text = lbl.inner_text().strip().lower()

            # Also check aria-label and placeholder as fallbacks
            if not label_text:
                label_text = (
                    (el.get_attribute("aria-label") or "")
                    + " "
                    + (el.get_attribute("placeholder") or "")
                ).lower()

            for keyword, data_key in _URL_LABEL_HINTS.items():
                if keyword in label_text:
                    value = field_data.get(data_key, "")
                    if value:
                        el.fill(value)
                        filled_summary.append({"field": field_id or label_text[:40],
                                               "value": value[:80]})
                        print(f"  Filled URL field: '{label_text.strip()[:40]}' → {value}")
                    break
        except Exception as e:
            flagged.append({"field": "url_field", "error": str(e)})


def _upload_file(page: Page, field_key: str, file_path,
                 filled_summary: list, flagged: list):
    """Attach a file to a Greenhouse upload input."""
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": field_key, "error": f"File not found: {file_path}"})
        return
    try:
        selector = (f'input[type="file"][name*="{field_key}"], '
                    f'input[type="file"][id*="{field_key}"]')
        el = page.query_selector(selector)
        if el:
            el.set_input_files(str(path))
            filled_summary.append({"field": field_key, "value": path.name})
            print(f"  Uploaded: {field_key} → {path.name}")
    except Exception as e:
        flagged.append({"field": field_key, "error": str(e)})


def has_next_page(page: Page) -> bool:
    """Return True if a Next / Continue button is present and visible."""
    for sel in ['button:has-text("Next")', 'button:has-text("Continue")',
                'input[type="submit"][value*="Next"]', 'a:has-text("Next")']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def click_next(page: Page) -> None:
    """Click the Next / Continue button."""
    for sel in ['button:has-text("Next")', 'button:has-text("Continue")',
                'input[type="submit"][value*="Next"]', 'a:has-text("Next")']:
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
    for sel in ['button:has-text("Submit")', 'input[type="submit"][value*="Submit"]',
                'button[type="submit"]']:
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

"""
job_agent_support/ats/workday.py
Form filling for Workday ATS (*.myworkdayjobs.com).

Workday is a React-rendered multi-step wizard. Each page is identified by a
data-automation-id on the page container div. We detect the current step and
dispatch to the appropriate filler. Pages we don't recognize are logged in
detail and the user is prompted to fill them manually.

Observed page order (Boeing / generic Workday):
  1. My Information           applyFlowMyInfoPage
  2. My Experience            applyFlowMyExpPage
  3. Application Questions 1  applyFlowPrimaryQuestionsPage
  4. Application Questions 2  applyFlowSecondaryQuestionsPage
  5. Voluntary Disclosures    applyFlowVoluntaryDisclosuresPage
  6. Self Identify            applyFlowSelfIdentifyPage
  7. Review                   (no fill needed)
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

import yaml
from playwright.sync_api import Page

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT            = Path(__file__).parent.parent.parent
_LOG_DIR         = _ROOT / "logs/job_agents_logs"
_COMPONENTS_PATH = _ROOT / "data/job-apps/workday_components.yaml"
_CREDS_PATH      = _ROOT / "job_agent_support/credentials.yaml"

_LOG_DIR.mkdir(parents=True, exist_ok=True)

def _load_workday_creds() -> tuple[str, str]:
    try:
        with open(_CREDS_PATH, encoding="utf-8") as f:
            creds = yaml.safe_load(f)
        wd = creds.get("workday", {})
        return wd.get("email", ""), wd.get("password", "")
    except Exception:
        return "", ""


# ── Workday components loader ─────────────────────────────────────────────────

_workday_components: dict | None = None


def _get_workday_components() -> dict:
    global _workday_components
    if _workday_components is None:
        if _COMPONENTS_PATH.exists():
            with open(_COMPONENTS_PATH, encoding="utf-8") as f:
                _workday_components = yaml.safe_load(f) or {}
        else:
            _workday_components = {}
    return _workday_components


# ── Agent logger ──────────────────────────────────────────────────────────────

_logger: logging.Logger | None = None


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("workday_agent")
        if not _logger.handlers:
            ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
            fh  = logging.FileHandler(_LOG_DIR / f"workday_{ts}.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            _logger.addHandler(fh)
            _logger.setLevel(logging.DEBUG)
    return _logger


def _log_unknown_form(page: Page, page_key: str, container_id: str,
                      job_title: str = "", company: str = "") -> None:
    """Write a detailed JSON log of an unrecognized Workday page."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOG_DIR / f"workday_unknown_form_{ts}.json"

    try:
        elements = page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('[data-automation-id]').forEach(el => {
                items.push({
                    tag:           el.tagName.toLowerCase(),
                    type:          el.type || '',
                    automation_id: el.getAttribute('data-automation-id'),
                    text:          (el.innerText || '').trim().slice(0, 150),
                    value:         el.value || '',
                });
            });
            return items;
        }""")
    except Exception:
        elements = []

    payload = {
        "timestamp":         ts,
        "job_title":         job_title,
        "company":           company,
        "url":               page.url,
        "step_key":          page_key,
        "page_container_id": container_id,
        "page_title":        page.title(),
        "automation_id_elements": elements,
    }
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    _get_logger().warning(
        "UNKNOWN FORM | step=%s | container=%s | url=%s | logged_to=%s",
        page_key, container_id, page.url, log_path,
    )
    return log_path


# ── Apply button & start-application popup ───────────────────────────────────

def _click_apply_button(page: Page) -> bool:
    """Click the Apply adventure button on a Workday job listing page if present."""
    for sel in [
        '[data-automation-id="adventureButton"]',
        'a[role="button"]:has-text("Apply")',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


def _handle_application_start_popup(page: Page) -> bool:
    """Click 'Apply Manually' on the Start Your Application popup if present."""
    try:
        el = page.query_selector('[data-automation-id="applyManually"]')
        if el and el.is_visible():
            el.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


# ── Login ─────────────────────────────────────────────────────────────────────

def _is_login_page(page: Page) -> bool:
    """Return True if the current page looks like a Workday sign-in page."""
    for sel in [
        '[data-automation-id="email"]',
        '[data-automation-id="signInSubmitButton"]',
        'input[type="email"][autocomplete]',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def _do_login(page: Page) -> None:
    """
    Handle Workday authentication.

    Strategy (matches typical Workday flow where the page opens on create-account):
      1. Attempt account creation first.
      2. If Workday responds with "account already exists", switch to sign-in.
      3. If sign-in also fails, prompt the user to handle it manually.
    """
    email, password = _load_workday_creds()
    _get_logger().info("Starting Workday auth for %s — trying create-account first", email)
    print(f"  🔐 Workday auth — attempting account creation for {email}...")

    _create_account(page, email, password)

    # After the create-account submit, check whether Workday rejected it
    # because the account already exists.
    page.wait_for_timeout(1500)
    if _is_account_exists_error(page):
        _get_logger().info("Account already exists — switching to sign-in for %s", email)
        print(f"  ℹ️  Account already exists — signing in as {email}...")
        _sign_in(page, email, password)
        return

    # If we're back on a login/sign-in page without an error, also try sign-in
    # (some Workday tenants redirect back after a duplicate-email attempt).
    if _is_login_page(page) and not _is_create_account_page(page):
        _get_logger().info("Redirected to sign-in page — signing in as %s", email)
        _sign_in(page, email, password)
        return

    _get_logger().info("Workday account creation completed for %s", email)


def _sign_in(page: Page, email: str, password: str) -> None:
    """Fill and submit the Workday sign-in form."""
    # Navigate to the sign-in tab/link if we're still on the create-account page
    for sel in [
        '[data-automation-id="signInLink"]',
        'a:has-text("Sign In")',
        'button:has-text("Sign In")',
        '[data-automation-id="signIn"]',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass

    # Fill email
    for sel in ['[data-automation-id="email"]', 'input[type="email"]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.fill(email)
            break

    # Some tenants use a two-step flow: email → Next → password
    for next_sel in ['[data-automation-id="click_filter"]', 'button:has-text("Next")']:
        el = page.query_selector(next_sel)
        if el and el.is_visible():
            el.click()
            page.wait_for_timeout(1500)
            break

    # Fill password
    for sel in ['[data-automation-id="password"]', 'input[type="password"]']:
        el = page.query_selector(sel)
        if el and el.is_visible():
            el.fill(password)
            break

    # Submit
    submitted = False
    for submit_sel in [
        '[data-automation-id="click_filter"][aria-label="Sign In"]',
        '[data-automation-id="signInSubmitButton"]',
        'button:has-text("Sign In")',
        'button[type="submit"]',
    ]:
        el = page.query_selector(submit_sel)
        if el and el.is_visible():
            el.click()
            page.wait_for_timeout(3000)
            submitted = True
            break

    if not submitted:
        _get_logger().warning("Could not find sign-in submit button — prompting user")
        print("  ⚠️  Could not complete Workday sign-in automatically.")
        input("  Please sign in manually, then press ENTER to continue... ")
        return

    print(f"  🔑 Signed in to Workday as {email}")
    _get_logger().info("Workday sign-in submitted")


def _is_create_account_page(page: Page) -> bool:
    """Return True if the page is the Workday create-account form."""
    for sel in [
        '[data-automation-id="signInContent"]',
        '[data-automation-id="createAccountCheckbox"]',
        '[data-automation-id="verifyPassword"]',
        '[data-automation-id="click_filter"][aria-label="Create Account"]',
        '[data-automation-id="createAccountSubmitButton"]',
        '[data-automation-id="createAccountButton"]',
        '[data-automation-id="createAccountLink"]',
        'button:has-text("Create Account")',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False


def _is_account_exists_error(page: Page) -> bool:
    """Return True if Workday is indicating that this email already has an account."""
    phrases = [
        "account already exists",
        "already have an account",
        "email address is already",
        "email is already in use",
        "already registered",
        "already exists for this email",
    ]
    try:
        body_text = page.inner_text("body").lower()
        return any(phrase in body_text for phrase in phrases)
    except Exception:
        return False


def _create_account(page: Page, email: str, password: str) -> None:
    """
    Walk through the Workday new-account creation flow.

    Workday's create-account form typically asks for:
      - Email (pre-filled or re-entered)
      - Password + Confirm Password
      - First Name / Last Name
      - Optionally: security questions or phone verification

    After successful creation Workday either lands on the application form
    directly or sends a verification email. We handle both cases.
    """
    wdc       = _get_workday_components()
    main_comp = {}
    try:
        import yaml as _yaml
        with open(Path("data/job-apps/components.yaml"), encoding="utf-8") as f:
            main_comp = _yaml.safe_load(f) or {}
    except Exception:
        pass

    personal   = {**wdc.get("personal", {}), **main_comp.get("personal", {})}
    name_parts = personal.get("name", "Aaron Berkness").split()
    first_name = name_parts[0] if name_parts else "Aaron"
    last_name  = " ".join(name_parts[1:]) if len(name_parts) > 1 else "Berkness"

    # ── Fill email ────────────────────────────────────────────────────────────
    for sel in [
        '[data-automation-id="email"]',
        '[data-automation-id="createAccountEmail"]',
        'input[autocomplete="email"]',
        'input[name="email"]',
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(email)
                break
        except Exception:
            pass

    # ── Fill password & verify password ──────────────────────────────────────
    for pw_sel in [
        '[data-automation-id="password"]',
        '[data-automation-id="verifyPassword"]',
    ]:
        try:
            el = page.query_selector(pw_sel)
            if el and el.is_visible():
                el.fill(password)
        except Exception:
            pass

    # Fallback: fill any remaining visible password inputs
    pw_inputs = page.query_selector_all('input[type="password"]')
    for pw_el in pw_inputs:
        try:
            if pw_el.is_visible() and not pw_el.input_value():
                pw_el.fill(password)
        except Exception:
            pass

    # ── Accept terms checkbox ─────────────────────────────────────────────────
    try:
        cb = page.query_selector('[data-automation-id="createAccountCheckbox"]')
        if cb and cb.is_visible() and not cb.is_checked():
            cb.click()
            page.wait_for_timeout(500)
    except Exception:
        pass

    # ── Submit create-account form ────────────────────────────────────────────
    for submit_sel in [
        '[data-automation-id="click_filter"][aria-label="Create Account"]',
        '[data-automation-id="createAccountSubmitButton"]',
        '[data-automation-id="signInSubmitButton"]',
        'button:has-text("Create Account")',
        'button:has-text("Submit")',
        'button[type="submit"]',
    ]:
        try:
            el = page.query_selector(submit_sel)
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(3000)
                break
        except Exception:
            pass

    # ── Handle email verification prompt ─────────────────────────────────────
    page.wait_for_timeout(2000)
    if _is_verification_prompt(page):
        _get_logger().warning("Workday requires email verification for new account")
        print()
        print("=" * 60)
        print("  📧  EMAIL VERIFICATION REQUIRED")
        print(f"     Workday sent a verification email to: {email}")
        print("     Please check your inbox, click the link, then return here.")
        print("=" * 60)
        input("  Press ENTER after verifying your email... ")
        page.wait_for_timeout(2000)

    _get_logger().info("Workday account creation completed for %s", email)
    print(f"  ✅ Workday account created / ready for {email}")


def _is_verification_prompt(page: Page) -> bool:
    """Return True if Workday is asking the user to verify their email."""
    verification_phrases = [
        "verify your email",
        "check your email",
        "verification link",
        "confirm your email",
        "email sent",
    ]
    try:
        body_text = page.inner_text("body").lower()
        return any(phrase in body_text for phrase in verification_phrases)
    except Exception:
        return False


# ── Dead-listing detection ────────────────────────────────────────────────────

def _is_workday_dead_listing(page: Page) -> bool:
    """Return True if Workday is showing its 'page not found' error."""
    try:
        el = page.query_selector("span.css-78pczy")
        if el and "doesn't exist" in (el.inner_text() or "").lower():
            return True
        # Fallback: plain text scan in case the CSS class changes
        body = page.inner_text("body").lower()
        if "the page you are looking for doesn't exist" in body:
            return True
    except Exception:
        pass
    return False


# ── Page detection ────────────────────────────────────────────────────────────

_PAGE_CONTAINERS = {
    "applyFlowMyInfoPage":               "my_information",
    "applyFlowMyExpPage":                "my_experience",
    "applyFlowPrimaryQuestionsPage":     "app_questions_1",
    "applyFlowSecondaryQuestionsPage":   "app_questions_2",
    "applyFlowVoluntaryDisclosuresPage": "voluntary_disclosures",
    "applyFlowSelfIdentifyPage":         "self_identify",
}


def _detect_page(page: Page) -> tuple[str, str]:
    """Return (container_automation_id, page_key) for the current Workday step."""
    for container_id, page_key in _PAGE_CONTAINERS.items():
        try:
            el = page.query_selector(f'[data-automation-id="{container_id}"]')
            if el:
                return container_id, page_key
        except Exception:
            pass
    return "", "unknown"


# ── Page readiness helper ─────────────────────────────────────────────────────

def _wait_for_workday_ready(page: Page, timeout: int = 30000) -> None:
    """
    Wait for Workday's React app to render a recognizable page structure.
    Tries network-idle first, then polls for any known automation-id.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass

    known_ids = (
        list(_PAGE_CONTAINERS.keys())
        + ["createAccountSubmitButton", "signInSubmitButton",
           "email", "adventureButton", "applyManually"]
    )
    selector = ", ".join(f'[data-automation-id="{aid}"]' for aid in known_ids)
    try:
        page.wait_for_selector(selector, state="visible", timeout=timeout)
    except Exception:
        pass  # timed out — proceed anyway and let _detect_page report what it finds


# ── Main fill entry point ─────────────────────────────────────────────────────

def fill_page(page: Page, job_info: dict, docs: dict, components: dict) -> dict:
    """Detect the current Workday wizard step and fill it appropriately."""
    # Wait for Workday's React app to fully render before querying anything
    _wait_for_workday_ready(page)

    if _is_workday_dead_listing(page):
        _get_logger().warning(
            "fill_page | dead listing | url=%s | job=%s",
            page.url, job_info.get("job_title", ""),
        )
        print("  ⚠️  Workday: job page not found — will log as dead listing.")
        return {"dead_listing": True, "filled_fields": [], "flagged": []}

    # Click the Apply button on the job listing page if present
    if _click_apply_button(page):
        _wait_for_workday_ready(page)

    # Handle the "Start Your Application" popup — always pick Apply Manually.
    # If Apply Manually was clicked it triggers a page navigation, so wait for
    # Workday to fully render the next page (create-account or sign-in form)
    # before checking whether we need to authenticate.
    if _handle_application_start_popup(page):
        _wait_for_workday_ready(page)

    # Handle login / create-account page before doing anything else
    if _is_login_page(page) or _is_create_account_page(page):
        _do_login(page)
        _wait_for_workday_ready(page)

    container_id, page_key = _detect_page(page)

    # If the page is still loading, wait and retry detection once
    if page_key == "unknown":
        _get_logger().warning("fill_page | page unknown after first wait — retrying in 8s")
        page.wait_for_timeout(8000)
        _wait_for_workday_ready(page)
        container_id, page_key = _detect_page(page)

    # Merge workday-specific components (from workday_components.yaml)
    wdc      = _get_workday_components()
    personal = {**wdc.get("personal", {}), **components.get("personal", {})}
    workday  = wdc  # the whole file is the workday config

    filled  = []
    flagged = []

    job_title = job_info.get("job_title", "")
    company   = job_info.get("company", "")

    _get_logger().info(
        "fill_page | url=%s | container=%s | page_key=%s",
        page.url, container_id, page_key,
    )

    if page_key == "my_information":
        _fill_my_information(page, personal, workday, filled, flagged)

    elif page_key == "my_experience":
        _fill_my_experience(page, personal, workday, docs, filled, flagged)

    elif page_key in ("app_questions_1", "app_questions_2"):
        _fill_application_questions(page, workday, filled, flagged)

    elif page_key == "voluntary_disclosures":
        _fill_voluntary_disclosures(page, workday, filled, flagged)

    elif page_key == "self_identify":
        _fill_self_identify(page, personal, workday, filled, flagged)

    else:
        log_path = _log_unknown_form(page, page_key, container_id, job_title, company)
        print()
        print("=" * 60)
        print("  ⛔  UNKNOWN WORKDAY FORM PAGE")
        print(f"     The agent does not know how to fill this page.")
        print(f"     Page container : '{container_id or 'not detected'}'")
        print(f"     URL            : {page.url}")
        print(f"     Detailed log   : {log_path}")
        print()
        print("     Please fill this page manually in the browser window.")
        print("=" * 60)
        input("  Press ENTER when you have completed this page manually... ")
        flagged.append({
            "field": "entire_page",
            "error": (
                f"Unknown Workday page '{page_key}' (container: '{container_id}'). "
                "User filled manually."
            ),
        })

    _get_logger().info(
        "fill_page done | filled=%d | flagged=%d", len(filled), len(flagged)
    )
    print(f"  Filled {len(filled)} field(s), {len(flagged)} flagged.")
    return {"filled_fields": filled, "flagged": flagged}


# ── Step 1: My Information ────────────────────────────────────────────────────

def _fill_my_information(page: Page, personal: dict, workday: dict,
                         filled: list, flagged: list) -> None:
    name_parts = personal.get("name", "").split()
    first = name_parts[0] if name_parts else ""
    last  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    _fill_text_in_container(page, "formField-legalName--firstName",  first, filled, flagged)
    _fill_text_in_container(page, "formField-legalName--middleName",
                            personal.get("middle_name", ""), filled, flagged)
    _fill_text_in_container(page, "formField-legalName--lastName",   last, filled, flagged)
    _fill_text_in_container(page, "formField-addressLine1",
                            personal.get("address_line1", ""), filled, flagged)
    _fill_text_in_container(page, "formField-city",
                            personal.get("city", ""), filled, flagged)
    _fill_text_in_container(page, "formField-postalCode",
                            personal.get("postal_code", ""), filled, flagged)
    _fill_text_in_container(page, "formField-phoneNumber",
                            _digits_only(personal.get("phone", "")), filled, flagged)

    # "How Did You Hear About Us?" — Workday multiselect
    source = workday.get("source", "LinkedIn")
    _select_multiselect_option(page, "formField-source", source, filled, flagged)

    # "Have you ever been employed here?" — radio/dropdown
    prev_worker = workday.get("previous_worker", "No")
    _answer_boolean_field(page, "formField-candidateIsPreviousWorker",
                          prev_worker, filled, flagged)


def _digits_only(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


# ── Step 2: My Experience ─────────────────────────────────────────────────────

def _fill_my_experience(page: Page, personal: dict, workday: dict, docs: dict,
                        filled: list, flagged: list) -> None:
    # Resume upload first
    _upload_resume(page, docs.get("resume_path"), filled, flagged)

    # Work experience — fill the first entry visible on the form
    exp_list = workday.get("work_experience", [])
    if exp_list:
        exp = exp_list[0]
        _fill_text_in_container(page, "formField-jobTitle",
                                exp.get("job_title", ""), filled, flagged)
        _fill_text_in_container(page, "formField-companyName",
                                exp.get("company", ""), filled, flagged)
        _fill_text_in_container(page, "formField-location",
                                exp.get("location", ""), filled, flagged)

        if exp.get("start_month") and exp.get("start_year"):
            _fill_date_monthyear(page, "formField-startDate",
                                 exp["start_month"], exp["start_year"], filled, flagged)
        if exp.get("end_month") and exp.get("end_year"):
            _fill_date_monthyear(page, "formField-endDate",
                                 exp["end_month"], exp["end_year"], filled, flagged)

    # Education
    edu = workday.get("education", {})
    if edu.get("school"):
        _select_multiselect_option(page, "formField-school",
                                   edu["school"], filled, flagged)
    if edu.get("degree"):
        _select_dropdown_option(page, "formField-degree",
                                edu["degree"], filled, flagged)
    if edu.get("field_of_study"):
        _select_multiselect_option(page, "formField-fieldOfStudy",
                                   edu["field_of_study"], filled, flagged)
    if edu.get("gpa"):
        _fill_text_in_container(page, "formField-gradeAverage",
                                str(edu["gpa"]), filled, flagged)
    if edu.get("start_year"):
        _fill_text_in_container(page, "formField-firstYearAttended",
                                str(edu["start_year"]), filled, flagged)
    if edu.get("end_year"):
        _fill_text_in_container(page, "formField-lastYearAttended",
                                str(edu["end_year"]), filled, flagged)

    # Skills multiselect
    for skill in workday.get("skills", []):
        _type_to_add_skill(page, skill, filled, flagged)


# ── Step 3 & 4: Application Questions ────────────────────────────────────────

def _fill_application_questions(page: Page, workday: dict,
                                 filled: list, flagged: list) -> None:
    """
    Answer questions by matching label text to known patterns.
    Workday uses dynamic IDs per posting; we match on visible text instead.
    """
    defaults = workday.get("question_defaults", {})

    containers = page.query_selector_all('[data-automation-id^="formField-"]')
    for container in containers:
        try:
            label_text = (container.inner_text() or "").strip().lower()
            answer = _match_question_label(label_text, defaults)
            if answer is None:
                continue
            if _click_radio_option(container, answer):
                filled.append({"field": label_text[:70], "value": answer})
            elif _select_native_option(container, answer):
                filled.append({"field": label_text[:70], "value": answer})
        except Exception as e:
            flagged.append({"field": "app_question", "error": str(e)})

    if not filled:
        print("  ℹ️  No auto-answerable questions matched on this page.")


# Map label substrings → question_defaults key
_QUESTION_PATTERNS: list[tuple[str, str]] = [
    ("18 years or older",               "age_18_plus"),
    ("ever been employed by boeing",    "previous_boeing_worker"),
    ("family member",                   "family_member_conflict"),
    ("deloitte",                        "deloitte_relationship"),
    ("security clearance",              "has_security_clearance"),
    ("bachelor",                        "has_bachelor_degree"),
    ("basic qualifications",            "meets_basic_qualifications"),
    ("programming",                     "has_programming_skills"),
    ("electrical engineering",          "has_ee_degree"),
    ("full-time or part-time",          "served_in_military"),
]


def _match_question_label(label_text: str, defaults: dict) -> str | None:
    for pattern, key in _QUESTION_PATTERNS:
        if pattern in label_text and key in defaults:
            return defaults[key]
    return None


# ── Step 5: Voluntary Disclosures ─────────────────────────────────────────────

def _fill_voluntary_disclosures(page: Page, workday: dict,
                                 filled: list, flagged: list) -> None:
    eeoc = workday.get("eeoc", {})

    if eeoc.get("gender"):
        _select_dropdown_option(page, "formField-gender",
                                eeoc["gender"], filled, flagged)
    if eeoc.get("ethnicity"):
        _select_dropdown_option(page, "formField-ethnicity",
                                eeoc["ethnicity"], filled, flagged)
    if eeoc.get("veteran_status"):
        _select_dropdown_option(page, "formField-veteranStatus",
                                eeoc["veteran_status"], filled, flagged)

    # Accept Boeing Applicant Privacy Notice checkbox
    _click_checkbox_if_unchecked(page, "formField-acceptTermsAndAgreements", filled, flagged)


# ── Step 6: Self Identify ─────────────────────────────────────────────────────

def _fill_self_identify(page: Page, personal: dict, workday: dict,
                        filled: list, flagged: list) -> None:
    full_name = personal.get("name", "")
    _fill_text_in_container(page, "formField-name", full_name, filled, flagged)

    # Date signed — today
    today = datetime.now()
    _fill_date_monthday_year(
        page, "formField-dateSignedOn",
        str(today.month).zfill(2),
        str(today.day).zfill(2),
        str(today.year),
        filled, flagged,
    )

    # Disability status radio group
    disability = workday.get(
        "disability_status",
        "No, I do not have a disability, and have not had one in the past",
    )
    _click_radio_in_fieldset(page, "disabilityStatus-CheckboxGroup",
                             disability, filled, flagged)


# ── Low-level field helpers ───────────────────────────────────────────────────

def _fill_text_in_container(page: Page, container_id: str, value: str,
                             filled: list, flagged: list) -> bool:
    if not value:
        return False
    try:
        el = page.query_selector(
            f'[data-automation-id="{container_id}"] input[type="text"],'
            f'[data-automation-id="{container_id}"] input:not([type]),'
            f'[data-automation-id="{container_id}"] textarea'
        )
        if el and el.is_visible():
            el.fill(value)
            filled.append({"field": container_id, "value": value[:50]})
            return True
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})
    return False


def _fill_date_monthyear(page: Page, container_id: str, month: str, year: str,
                          filled: list, flagged: list) -> None:
    """Fill a Workday MM / YYYY date widget (no day)."""
    try:
        base = f'[data-automation-id="{container_id}"]'
        m_el = page.query_selector(f'{base} [data-automation-id="dateSectionMonth-input"]')
        y_el = page.query_selector(f'{base} [data-automation-id="dateSectionYear-input"]')
        if m_el and m_el.is_visible():
            m_el.fill(str(month))
        if y_el and y_el.is_visible():
            y_el.fill(str(year))
        filled.append({"field": container_id, "value": f"{month}/{year}"})
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})


def _fill_date_monthday_year(page: Page, container_id: str,
                              month: str, day: str, year: str,
                              filled: list, flagged: list) -> None:
    """Fill a Workday MM / DD / YYYY date widget."""
    try:
        base = f'[data-automation-id="{container_id}"]'
        m_el = page.query_selector(f'{base} [data-automation-id="dateSectionMonth-input"]')
        d_el = page.query_selector(f'{base} [data-automation-id="dateSectionDay-input"]')
        y_el = page.query_selector(f'{base} [data-automation-id="dateSectionYear-input"]')
        if m_el and m_el.is_visible():
            m_el.fill(month)
        if d_el and d_el.is_visible():
            d_el.fill(day)
        if y_el and y_el.is_visible():
            y_el.fill(year)
        filled.append({"field": container_id, "value": f"{month}/{day}/{year}"})
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})


def _select_multiselect_option(page: Page, container_id: str, value: str,
                                filled: list, flagged: list) -> bool:
    """Type into a Workday multiselect search box and pick the matching option."""
    try:
        container = page.query_selector(f'[data-automation-id="{container_id}"]')
        if not container:
            return False

        text_input = container.query_selector(
            'input[type="text"], input:not([type="hidden"]):not([type="checkbox"])'
        )
        if text_input and text_input.is_visible():
            text_input.click()
            text_input.press_sequentially(value[:25], delay=40)
            page.wait_for_timeout(800)

            # Prefer exact-text match, fall back to first available option
            option = page.query_selector(
                f'[data-automation-id="promptOption"]:has-text("{value[:25]}")'
            )
            if not option:
                option = page.query_selector('[data-automation-id="promptOption"]')
            if option:
                option.click()
                filled.append({"field": container_id, "value": value[:50]})
                return True
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})
    return False


def _select_dropdown_option(page: Page, container_id: str, value: str,
                             filled: list, flagged: list) -> bool:
    """Select a value from a Workday native <select> or custom listbox."""
    try:
        container = page.query_selector(f'[data-automation-id="{container_id}"]')
        if not container:
            return False

        # Native <select>
        sel_el = container.query_selector("select")
        if sel_el and sel_el.is_visible():
            opts = sel_el.query_selector_all("option")
            for opt in opts:
                if value.lower() in (opt.inner_text() or "").lower():
                    sel_el.select_option(value=opt.get_attribute("value"))
                    filled.append({"field": container_id, "value": value[:50]})
                    return True

        # Workday custom select widget — click trigger then pick option
        trigger = container.query_selector(
            '[data-automation-id="selectWidget"], button[aria-haspopup], '
            '[role="combobox"], [role="listbox"]'
        )
        if trigger and trigger.is_visible():
            trigger.click()
            page.wait_for_timeout(500)
            for opt_sel in [
                f'[role="option"]:has-text("{value}")',
                f'li:has-text("{value}")',
                f'[data-automation-id="promptOption"]:has-text("{value}")',
            ]:
                option = page.query_selector(opt_sel)
                if option:
                    option.click()
                    filled.append({"field": container_id, "value": value[:50]})
                    return True
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})
    return False


def _answer_boolean_field(page: Page, container_id: str, answer: str,
                           filled: list, flagged: list) -> None:
    try:
        container = page.query_selector(f'[data-automation-id="{container_id}"]')
        if not container:
            return
        if _click_radio_option(container, answer):
            filled.append({"field": container_id, "value": answer})
            return
        if _select_native_option(container, answer):
            filled.append({"field": container_id, "value": answer})
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})


def _click_radio_option(container, answer: str) -> bool:
    """Click a radio button or label whose text contains the answer string."""
    try:
        for label in container.query_selector_all("label"):
            if answer.lower() in (label.inner_text() or "").lower():
                label.click()
                return True
        for radio in container.query_selector_all('input[type="radio"]'):
            rid = radio.get_attribute("id") or ""
            label = container.query_selector(f'label[for="{rid}"]')
            if label and answer.lower() in (label.inner_text() or "").lower():
                radio.click()
                return True
    except Exception:
        pass
    return False


def _select_native_option(container, answer: str) -> bool:
    try:
        sel = container.query_selector("select")
        if not sel:
            return False
        for opt in sel.query_selector_all("option"):
            if answer.lower() in (opt.inner_text() or "").lower():
                sel.select_option(value=opt.get_attribute("value"))
                return True
    except Exception:
        pass
    return False


def _click_checkbox_if_unchecked(page: Page, container_id: str,
                                  filled: list, flagged: list) -> None:
    try:
        container = page.query_selector(f'[data-automation-id="{container_id}"]')
        if not container:
            return
        cb = container.query_selector('input[type="checkbox"]')
        if cb and cb.is_visible() and not cb.is_checked():
            cb.click()
            filled.append({"field": container_id, "value": "checked"})
    except Exception as e:
        flagged.append({"field": container_id, "error": str(e)})


def _click_radio_in_fieldset(page: Page, fieldset_id: str, answer: str,
                              filled: list, flagged: list) -> None:
    try:
        fieldset = page.query_selector(f'[data-automation-id="{fieldset_id}"]')
        if not fieldset:
            return
        if _click_radio_option(fieldset, answer):
            filled.append({"field": fieldset_id, "value": answer[:70]})
            return
        # Fallback: checkbox list
        for cb in fieldset.query_selector_all('input[type="checkbox"]'):
            rid   = cb.get_attribute("id") or ""
            label = page.query_selector(f'label[for="{rid}"]')
            if label and answer.lower() in (label.inner_text() or "").lower():
                if not cb.is_checked():
                    cb.click()
                filled.append({"field": fieldset_id, "value": answer[:70]})
                return
    except Exception as e:
        flagged.append({"field": fieldset_id, "error": str(e)})


def _type_to_add_skill(page: Page, skill: str, filled: list, flagged: list) -> None:
    try:
        container = page.query_selector('[data-automation-id="formField-skills"]')
        if not container:
            return
        text_input = container.query_selector(
            'input[type="text"], input:not([type="hidden"])'
        )
        if text_input and text_input.is_visible():
            text_input.click()
            text_input.press_sequentially(skill[:25], delay=40)
            page.wait_for_timeout(600)
            option = page.query_selector(
                f'[data-automation-id="promptOption"]:has-text("{skill[:25]}")'
            )
            if not option:
                option = page.query_selector('[data-automation-id="promptOption"]')
            if option:
                option.click()
                filled.append({"field": "formField-skills", "value": skill[:50]})
    except Exception as e:
        flagged.append({"field": "formField-skills", "error": str(e)})


# ── Resume upload ─────────────────────────────────────────────────────────────

def _upload_resume(page: Page, file_path, filled: list, flagged: list) -> None:
    if not file_path:
        return
    path = Path(file_path)
    if not path.exists():
        flagged.append({"field": "resume", "error": f"File not found: {file_path}"})
        return

    # Workday hides the real <input type="file"> behind a custom drop-zone widget.
    # We attach the file directly to the hidden input to bypass the widget.
    selectors = [
        '[data-automation-id="attachments-FileUpload"] input[type="file"]',
        '[data-automation-id="file-upload-input-ref"]',
        'input[type="file"]',
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                el.set_input_files(str(path))
                filled.append({"field": "resume", "value": path.name})
                print(f"  Uploaded resume → {path.name}")
                page.wait_for_timeout(3000)
                return
        except Exception:
            pass

    flagged.append({"field": "resume", "error": "No file input found for resume upload"})


# ── Navigation ────────────────────────────────────────────────────────────────

_NEXT_SELECTORS = [
    '[data-automation-id="pageFooterNextButton"]',
    '[data-automation-id="bottom-navigation-next-button"]',
    'button:has-text("Save and Continue")',
    'button:has-text("Save & Continue")',
    'button:has-text("Next")',
]


def has_next_page(page: Page) -> bool:
    """Return True if a Workday Save and Continue / Next button is present and enabled."""
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
        '[data-automation-id="pageFooterNextButton"]',
        '[data-automation-id="bottom-navigation-next-button"]',
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
    input("  Press ENTER after submitting manually... ")

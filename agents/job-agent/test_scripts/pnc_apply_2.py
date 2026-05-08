import re
from playwright.sync_api import sync_playwright

URL = "https://careers.pnc.com/global/en/job/PNC1GLOBALR219151/Software-Developer-Tempus-Delphi?utm_source=symphonytalentmpx&utm_medium=phenom-feeds&Codes=15815"


def resolve_apply_url(url: str, page) -> tuple[str, object]:
    """
    Starting from url, follow Apply Now buttons through intermediate pages
    (e.g. listing site → company Phenom page → Workday) until a real
    application form is reached or no more buttons are found.
    """
    _SELECTORS = [
        'a[ph-tevent="apply_click"][title="Apply Now"]',
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
                    current_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    current_page.wait_for_load_state("networkidle", timeout=15000)
                    current_url = current_page.url
                    print(f"     Landed on: {current_url}")
                    if current_url == pre_click_url:
                        continue
                clicked = True
                break
            except Exception:
                pass

        if not clicked:
            break

    return current_url, current_page


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


def debug_page(page, label: str) -> None:
    print(f"\n{'='*60}")
    print(f"DEBUG — {label}")
    print(f"  URL   : {page.url}")
    print(f"  Title : {page.title()}")

    field_info = page.evaluate("""() => {
        const fields = document.querySelectorAll('input:not([type=hidden]), textarea, select');
        return Array.from(fields).map(el => ({
            tag:   el.tagName.toLowerCase(),
            type:  el.type || '',
            id:    el.id || '',
            name:  el.name || '',
            ph:    el.placeholder || '',
            vis:   el.offsetParent !== null,
        }));
    }""")
    visible = [f for f in field_info if f["vis"]]
    print(f"  Visible form fields: {len(visible)} (total incl. hidden: {len(field_info)})")
    for f in visible[:15]:
        print(f"    [{f['tag']}][{f['type']}] id={f['id']!r} name={f['name']!r} ph={f['ph']!r}")

    ats = detect_ats(page.url)
    print(f"  ATS detected: {ats}")
    print(f"{'='*60}")


if __name__ == "__main__":
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()

        print(f"\nNavigating to: {URL}")
        apply_url, result_page = resolve_apply_url(URL, page)

        debug_page(result_page, "After resolve_apply_url")
        print(f"\nFinal apply URL: {apply_url}")

        input("\nBrowser is open for inspection. Press Enter to close...")
        browser.close()

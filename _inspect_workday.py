"""
Temporary script: opens Boeing Workday application page in a visible browser,
waits for manual login, then walks every wizard page and dumps all
data-automation-id fields to stdout.

Run: python _inspect_workday.py
"""
from playwright.sync_api import sync_playwright

URL = (
    "https://boeing.wd1.myworkdayjobs.com/en-US/EXTERNAL_CAREERS/job/"
    "USA---El-Segundo-CA/Associate-Space-Communications-Systems-Engineer-Analyst"
    "_JR2026506515-2/apply/applyManually"
)


def dump_page(page, label):
    page.wait_for_timeout(2500)
    items = page.evaluate("""() => {
        const els = document.querySelectorAll('[data-automation-id]');
        return [...els].map(el => ({
            id:      el.getAttribute('data-automation-id'),
            tag:     el.tagName.toLowerCase(),
            type:    el.getAttribute('type') || '',
            label:   el.getAttribute('aria-label') || el.getAttribute('placeholder') || '',
            visible: el.offsetParent !== null,
            text:    el.innerText ? el.innerText.trim().replace(/\\n/g,' ').slice(0,120) : ''
        }));
    }""")
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  URL: ...{page.url[-70:]}")
    print(f"{'='*70}")
    seen = set()
    for it in items:
        if it['visible'] and it['id'] not in seen:
            seen.add(it['id'])
            print(
                f"  [{it['tag']:6}][{it['type']:8}] "
                f"{it['id']!r:55s} | {it['text']!r:.70s}"
            )


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=80)
        ctx  = browser.new_context()
        page = ctx.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        print("\n" + "=" * 70)
        print("  BROWSER IS OPEN")
        print("  1. Log in to your Boeing/Workday account in the browser window")
        print("  2. Once you reach step 2 (My Information), come back here")
        print("  3. Press ENTER to begin the automated page walk")
        print("=" * 70)
        input("\n  Press ENTER when ready > ")

        for step_num in range(1, 10):
            step_label = page.evaluate("""() => {
                const el = document.querySelector(
                    '[data-automation-id="progressBarActiveStep"]');
                return el ? el.innerText.trim().replace(/\\n/g,' ') : 'unknown';
            }""")
            dump_page(page, f"STEP {step_num}: {step_label}")

            next_el = None
            for sel in [
                '[data-automation-id="bottom-navigation-next-button"]',
                'button:has-text("Save & Continue")',
                'button:has-text("Save and Continue")',
                'button:has-text("Next")',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible() and el.is_enabled():
                        next_el = el
                        break
                except Exception:
                    pass

            if next_el:
                btn_text = (next_el.inner_text() or "").lower().strip()
                if "submit" in btn_text:
                    print(f"\n  Reached Submit page — stopping walk.")
                    break
                print(f"\n  Clicking '{btn_text}' → step {step_num + 1}...")
                next_el.click()
                page.wait_for_timeout(4000)
            else:
                print(f"\n  No Next/Save & Continue button visible — stopping at step {step_num}.")
                break

        print("\n  Done. Review the output above.")
        input("  Press ENTER to close browser > ")
        browser.close()


if __name__ == "__main__":
    main()

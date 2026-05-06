"""
job_agent_support/boards/governmentjobs.py
Handles login, browsing, and listing extraction for governmentjobs.com (NeoGov SSO).

Login strategy: auto-fill email + password; pause for CAPTCHA or MFA if detected;
fall back to a manual-completion prompt if still on login page after submit.
"""

import urllib.parse
from playwright.sync_api import Page


def login(page: Page, credentials: dict) -> None:
    """Auto-fill email + password on the NeoGov SSO login page."""
    print("  🔐 Logging in to governmentjobs.com...")

    # Clear any existing session first
    try:
        page.goto("https://www.governmentjobs.com/home/logout",
                  wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass

    page.goto("https://www.governmentjobs.com/home/login",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Fill email
    email_sel = 'input[name="email"], input[type="email"], #email, #username'
    try:
        page.wait_for_selector(email_sel, timeout=10000)
    except Exception:
        print("  ⚠️  Could not find email field. Pausing for manual login.")
        input("  Complete login in the browser, then press ENTER: ")
        print("  ✅ Login step complete.")
        return

    page.fill(email_sel, credentials["email"])
    page.wait_for_timeout(400)

    # Fill password
    pass_sel = 'input[name="password"], input[type="password"], #password'
    page.fill(pass_sel, credentials["password"])
    page.wait_for_timeout(400)

    # Submit
    page.click('button[type="submit"], input[type="submit"], .btn-primary')
    page.wait_for_timeout(3000)

    # Handle CAPTCHA / reCAPTCHA
    content = page.content().lower()
    if "captcha" in content or "recaptcha" in content:
        print("\n  ⏸  CAPTCHA detected. Complete it in the browser, then return here.")
        input("  Press ENTER when done: ")
        page.wait_for_timeout(2000)

    # If still on login page, let user finish manually
    if "login" in page.url.lower() or "signin" in page.url.lower():
        print("\n  ⚠️  Still on login page — MFA or another step may be required.")
        input("  Complete login in the browser, then press ENTER: ")

    print("  ✅ Login complete.")


def browse_jobs(page: Page, query: str = "") -> None:
    """Navigate to the governmentjobs.com job listings page."""
    if query:
        url = ("https://www.governmentjobs.com/careers/home"
               f"?keyword={urllib.parse.quote(query)}")
    else:
        url = "https://www.governmentjobs.com/careers/home"
    print(f"  🔍 Navigating to listings: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)


def get_job_listings(page: Page) -> list[dict]:
    """
    Scrape job cards from the current governmentjobs.com page.

    NeoGov's HTML varies by agency sub-site. We try the standard card selectors
    first, then fall back to harvesting any /careers/ job links on the page.
    """
    print("  📋 Scraping job listings...")

    listings = page.evaluate(r"""() => {
        // Primary: standard NeoGov job result card selectors
        let cards = [
            ...document.querySelectorAll(
                '.job-listing-item, .job-result, .careers-list li, ' +
                '#search-results li, li[data-job-id], .list-group-item'
            )
        ];

        if (cards.length > 0) {
            return cards.slice(0, 20).map(card => {
                const titleEl   = card.querySelector(
                    'h2 a, h3 a, h4 a, .title a, .job-title a, a[href*="/job/"]'
                );
                const companyEl = card.querySelector(
                    '.employer-name, .department, .agency, .company-name, ' +
                    '.location-name, .location'
                );
                const snippetEl = card.querySelector('.description, .job-description, p');

                const title   = titleEl   ? titleEl.innerText.trim()   : '';
                const company = companyEl ? companyEl.innerText.trim() : '';
                const snippet = snippetEl ? snippetEl.innerText.trim().slice(0, 200) : '';
                let url       = titleEl   ? titleEl.href               : '';

                return { title, company, url, snippet };
            }).filter(j => j.title && j.url);
        }

        // Fallback: collect any job-detail links visible on the page
        const links = [
            ...document.querySelectorAll('a[href*="/careers/"][href*="/job"]')
        ];
        return links.slice(0, 20).map(a => {
            const card    = a.closest('li, article, div[class]');
            const locEl   = card ? card.querySelector('.location, .agency, .department') : null;
            return {
                title:   a.innerText.trim(),
                company: locEl ? locEl.innerText.trim() : '',
                url:     a.href,
                snippet: '',
            };
        }).filter(j => j.title && j.url);
    }""")

    if not listings:
        print("  ⚠️  No listings found — the page may still be loading or the selectors")
        print(f"     may need updating. Current URL: {page.url}")
    else:
        print(f"  Found {len(listings)} listing(s) on this page.")

    return listings


def go_to_next_page(page: Page) -> bool:
    """Click the next-page button. Returns True if navigation succeeded."""
    selectors = [
        'a[aria-label="next"]',
        'a[aria-label="Next"]',
        'a[title="Next Page"]',
        'a[title="next"]',
        'li.next a',
        '.next a',
        'a[rel="next"]',
        'button[aria-label="Next page"]',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(3000)
                return True
        except Exception:
            pass
    return False

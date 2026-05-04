"""
job-agent-support/boards/builtin.py
Handles login, browsing, and listing extraction for builtin.com
"""

from playwright.sync_api import Page


def login(page: Page, credentials: dict) -> None:
    """Log in to builtin.com with email and password."""
    print("  🔐 Logging in to builtin.com...")
    page.goto("https://builtin.com/login", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    # Fill email
    page.fill('input[name="email"], input[type="email"]', credentials["email"])
    page.wait_for_timeout(500)

    # Fill password
    page.fill('input[name="password"], input[type="password"]', credentials["password"])
    page.wait_for_timeout(500)

    # Submit
    page.click('button[type="submit"]')
    page.wait_for_timeout(3000)

    # Check for CAPTCHA or unusual challenge
    if "captcha" in page.content().lower() or "verify" in page.url.lower():
        print("\n  ⏸  CAPTCHA or verification detected.")
        print("  Complete it in the browser window, then return here.")
        input("  Press ENTER when done: ")

    # Check for OTP / email verification
    if "verify" in page.content().lower() or "code" in page.content().lower():
        print("\n  ⏸  A verification code may be required.")
        print("  Check your email, enter the code in the browser, then return here.")
        input("  Press ENTER when done: ")

    print("  ✅ Login step complete.")


def browse_jobs(page: Page, query: str = "") -> None:
    """Navigate to the job listings page on builtin.com."""
    url = "https://builtin.com/jobs"
    if query:
        import urllib.parse
        url = f"https://builtin.com/jobs?search={urllib.parse.quote(query)}"
    print(f"  🔍 Navigating to listings: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)


def get_job_listings(page: Page) -> list[dict]:
    """
    Scrape job cards from the current listings page.
    Returns a list of dicts: {title, company, url, snippet}
    """
    print("  📋 Scraping job listings...")
    listings = page.evaluate("""() => {
        const cards = [...document.querySelectorAll('article, [data-id], .job-bounded-responsive')];
        return cards.slice(0, 20).map(card => {
            const titleEl  = card.querySelector('h2 a, h3 a, [data-testid="job-title"] a, .job-title a');
            const companyEl = card.querySelector('[data-testid="company-title"], .company-title, .company-name');
            const snippetEl = card.querySelector('p, .job-description, [data-testid="job-description"]');
            const linkEl   = card.querySelector('a[href*="/job/"]');

            const title   = titleEl   ? titleEl.innerText.trim()   : "";
            const company = companyEl ? companyEl.innerText.trim() : "";
            const snippet = snippetEl ? snippetEl.innerText.trim().slice(0, 200) : "";
            let url       = linkEl    ? linkEl.href                : "";

            if (url && !url.startsWith("http")) {
                url = "https://builtin.com" + url;
            }
            return { title, company, url, snippet };
        }).filter(j => j.title && j.url);
    }""")

    print(f"  Found {len(listings)} listing(s) on this page.")
    return listings


def go_to_next_page(page: Page) -> bool:
    """
    Click the next-page button if available.
    Returns True if navigation succeeded, False if no more pages.
    """
    try:
        next_btn = page.query_selector('a[aria-label="Next"], a[rel="next"], button[aria-label="Next page"]')
        if next_btn and next_btn.is_visible():
            next_btn.click()
            page.wait_for_timeout(3000)
            return True
    except Exception as e:
        print(f"  Warning: next page error — {e}")
    return False

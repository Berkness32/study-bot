"""
job-agent-support/boards/governmentjobs.py
Handles login, browsing, and listing extraction for governmentjobs.com (NeoGov)
"""

from playwright.sync_api import Page


def login(page: Page, credentials: dict) -> None:
    """Log in to governmentjobs.com."""
    print("  🔐 Logging in to governmentjobs.com...")
    page.goto("https://www.governmentjobs.com/home/logout", wait_until="domcontentloaded", timeout=15000)
    page.goto("https://www.governmentjobs.com/home/login", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    page.fill('#username, input[name="username"], input[type="email"]', credentials["email"])
    page.wait_for_timeout(400)
    page.fill('#password, input[name="password"], input[type="password"]', credentials["password"])
    page.wait_for_timeout(400)
    page.click('button[type="submit"], input[type="submit"]')
    page.wait_for_timeout(3000)

    if "captcha" in page.content().lower():
        print("\n  ⏸  CAPTCHA detected. Complete it in the browser, then return here.")
        input("  Press ENTER when done: ")

    print("  ✅ Login step complete.")


def browse_jobs(page: Page, query: str = "") -> None:
    """Navigate to job listings on governmentjobs.com."""
    import urllib.parse
    base = "https://www.governmentjobs.com/careers/home"
    if query:
        base = f"https://www.governmentjobs.com/careers/home?keyword={urllib.parse.quote(query)}"
    print(f"  🔍 Navigating to listings: {base}")
    page.goto(base, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)


def get_job_listings(page: Page) -> list[dict]:
    """Scrape job cards from the current governmentjobs.com listings page."""
    print("  📋 Scraping job listings...")
    listings = page.evaluate("""() => {
        const cards = [...document.querySelectorAll('.job-result, .jobTitle, li[class*="job"]')];
        return cards.slice(0, 20).map(card => {
            const titleEl   = card.querySelector('h2 a, h3 a, .job-title a, a[href*="/careers/"]');
            const companyEl = card.querySelector('.employer-name, .department, .agency');
            const snippetEl = card.querySelector('.job-description, p');

            const title   = titleEl   ? titleEl.innerText.trim()   : "";
            const company = companyEl ? companyEl.innerText.trim() : "";
            const snippet = snippetEl ? snippetEl.innerText.trim().slice(0, 200) : "";
            let url       = titleEl   ? titleEl.href               : "";

            return { title, company, url, snippet };
        }).filter(j => j.title && j.url);
    }""")

    print(f"  Found {len(listings)} listing(s) on this page.")
    return listings


def go_to_next_page(page: Page) -> bool:
    """Click next page. Returns True if successful."""
    try:
        next_btn = page.query_selector('a[aria-label="next"], .next a, a[title="Next Page"]')
        if next_btn and next_btn.is_visible():
            next_btn.click()
            page.wait_for_timeout(3000)
            return True
    except Exception as e:
        print(f"  Warning: next page error — {e}")
    return False

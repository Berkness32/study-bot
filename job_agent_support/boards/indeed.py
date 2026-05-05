"""
job-agent-support/boards/indeed.py
Handles login, browsing, and listing extraction for indeed.com
"""

from playwright.sync_api import Page


def login(page: Page, credentials: dict) -> None:
    print("  ℹ️  Skipping Indeed sign-in — browsing listings without login.")
    print("  Easy Apply jobs will be flagged; apply to those manually in your browser.")


def browse_jobs(page: Page, query: str = "technology Los Angeles") -> None:
    """Navigate to Indeed job listings."""
    import urllib.parse
    parts = query.split(" ", 1)
    what = urllib.parse.quote(parts[0]) if parts else ""
    where = urllib.parse.quote(parts[1]) if len(parts) > 1 else ""
    url = f"https://www.indeed.com/jobs?q={what}&l={where}"
    print(f"  🔍 Navigating to listings: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)


def get_job_listings(page: Page) -> list[dict]:
    """Scrape job cards from the current Indeed listings page."""
    print("  📋 Scraping job listings...")
    listings = page.evaluate("""() => {
        const cards = [...document.querySelectorAll('.job_seen_beacon, .tapItem, [data-testid="job-title"]')];
        return cards.slice(0, 20).map(card => {
            const titleEl   = card.querySelector('[data-testid="job-title"] a, h2 a, .jobTitle a');
            const companyEl = card.querySelector('[data-testid="company-name"], .companyName');
            const snippetEl = card.querySelector('[data-testid="job-snippet"], .job-snippet');

            const title   = titleEl   ? titleEl.innerText.trim()   : "";
            const company = companyEl ? companyEl.innerText.trim() : "";
            const snippet = snippetEl ? snippetEl.innerText.trim().slice(0, 200) : "";
            let url       = titleEl   ? titleEl.href               : "";

            if (url && !url.startsWith("http")) {
                url = "https://www.indeed.com" + url;
            }
            return { title, company, url, snippet };
        }).filter(j => j.title && j.url);
    }""")

    print(f"  Found {len(listings)} listing(s) on this page.")
    return listings


def go_to_next_page(page: Page) -> bool:
    """Click next page on Indeed. Returns True if successful."""
    try:
        next_btn = page.query_selector('a[aria-label="Next Page"], [data-testid="pagination-page-next"]')
        if next_btn and next_btn.is_visible():
            next_btn.click()
            page.wait_for_timeout(3000)
            return True
    except Exception as e:
        print(f"  Warning: next page error — {e}")
    return False

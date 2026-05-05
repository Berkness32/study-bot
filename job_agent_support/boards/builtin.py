"""
job_agent_support/boards/builtin.py
Handles browsing and listing extraction for builtin.com.

Login is skipped — Google Sign-In doesn't work in the automated browser.
browse_jobs() defaults to Engineering jobs in Los Angeles, CA.
"""

import urllib.parse
from playwright.sync_api import Page

# LA location query params used by builtin's filter system
_LA_PARAMS = "city=Los+Angeles&state=California&country=USA&allLocations=true"

# Category slugs available on builtin.com relevant to software / IT
CATEGORIES = {
    "engineering":       "Software Engineering",
    "ai-machine-learning": "AI & Machine Learning",
    "cyber-security":    "Cybersecurity",
    "data-analytics":    "Data & Analytics",
}


def login(page: Page, credentials: dict) -> None:
    print("  ℹ️  Skipping builtin.com sign-in — browsing listings without login.")
    print("  Easy Apply jobs will be flagged; apply to those manually in your browser.")


def browse_jobs(page: Page, query: str = "") -> None:
    """
    Navigate to Engineering / Entry-Level / Junior jobs in Los Angeles on builtin.com.
    The experience levels and location are baked into the URL path and query params —
    no filter UI interaction needed.
    Pass query= to add an extra keyword search on top.
    """
    base = f"https://builtin.com/jobs/engineering/entry-level/junior?{_LA_PARAMS}"
    if query:
        base += f"&search={urllib.parse.quote(query)}"
    print(f"  🔍 Browsing: {base}")
    page.goto(base, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(3000)


def get_job_listings(page: Page) -> list[dict]:
    """
    Scrape job cards from the current builtin.com listings page.
    Returns a list of dicts: {title, company, url, snippet}
    """
    print("  📋 Scraping job listings...")
    listings = page.evaluate("""() => {
        const cards = [...document.querySelectorAll('[data-id="job-card"]')];
        return cards.map(card => {
            const titleEl   = card.querySelector('[data-id="job-card-title"]');
            const companyEl = card.querySelector('[data-id="company-title"]');
            const linkEl    = card.querySelector('a[href*="/job/"]');
            return {
                title:   titleEl   ? titleEl.innerText.trim()   : '',
                company: companyEl ? companyEl.innerText.trim() : '',
                url:     linkEl    ? 'https://builtin.com' + linkEl.getAttribute('href') : '',
                snippet: '',
            };
        }).filter(j => j.title && j.url);
    }""")
    print(f"  Found {len(listings)} listing(s) on this page.")
    return listings


def go_to_next_page(page: Page) -> bool:
    """Click the next-page button. Returns True if navigation succeeded."""
    selectors = [
        'a[aria-label="Go to Next Page"]',
        'a[aria-label="Next"]',
        'a[rel="next"]',
        'a.page-link[href*="page="]',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(3000)
                return True
        except Exception as e:
            print(f"  Warning: next page error — {e}")
    return False

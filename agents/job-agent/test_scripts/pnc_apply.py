import asyncio
from playwright.async_api import async_playwright
import re

# The URL variable is the website the pops up after I press
# <a id="applyButton" href="https://www.dynatrace.com/careers/jobs/1380701100/?utm_source=BuiltIn" target="_blank" @click="applyClick" class="btn btn-lg bg-pretty-blue border-pretty-blue rounded-3 text-white flex-grow-1 flex-md-grow-0 job-post-sticky-bar-btn text-uppercase" aria-label="Apply to job"><i class="fa-solid fa-pen-line me-xs fs-md" aria-hidden="true"></i><span x-text="['save', 'saved'].includes(getJobStatusText().toLowerCase()) ? 'Apply' : 'Apply Again'">Apply</span></a>
# on built in

URL = "https://careers.pnc.com/global/en/job/PNC1GLOBALR219151/Software-Developer-Tempus-Delphi?utm_source=symphonytalentmpx&utm_medium=phenom-feeds&Codes=15815"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        await page.goto(URL, wait_until="domcontentloaded")

        await page.wait_for_load_state("networkidle")

        close_btn = page.get_by_role("button", name=re.compile(r"close chatbot", re.IGNORECASE))
        if await close_btn.count() > 0:
            await close_btn.click()
            print("Closed chatbot.")

        apply_button = page.locator('a[ph-tevent="apply_click"][title="Apply Now"]').first
        await apply_button.wait_for(state="visible", timeout=15000)
        await apply_button.click()

        print("Clicked 'Apply Now' button.")

        # Wait for the new tab to open when clicking Apply Now
        async with page.expect_popup() as popup_info:
            new_page = await popup_info.value
            await new_page.wait_for_load_state("networkidle")
            print("New tab opened:", new_page.url)

        # ------------------------------------------------------
        # Workday logic on the new page 
        
        # Click Apply Manually in the new Workday tab
        apply_manually = new_page.locator('a[data-automation-id="applyManually"]')
        await apply_manually.wait_for(state="visible", timeout=15000)
        await apply_manually.click()
        print("Clicked 'Apply Manually'.")
        
        await new_page.wait_for_load_state("networkidle")

        await new_page.locator('input[data-automation-id="email"]').fill("your@email.com")
        await new_page.locator('input[data-automation-id="password"]').fill("hello")

        await page.wait_for_timeout(3000)  # waits 3 seconds

        await browser.close()

asyncio.run(main())

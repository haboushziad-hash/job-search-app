"""Single-tenant iCIMS Playwright test — snapon only."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.icims_playwright import ICIMSPlaywrightScraper


async def main():
    from playwright.async_api import async_playwright
    async with ScraperClient(timeout_seconds=20) as client:
        scraper = ICIMSPlaywrightScraper(client=client)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                roles = await scraper._search_tenant_keyword(
                    browser, "Snap-on", "snapon", "manager", 25
                )
                print(f"\nReturned {len(roles)} roles for snap-on")
                for r in roles[:10]:
                    print(f"  {r.job_title[:60]}  ({r.location or '-'})")
                    print(f"    {r.job_url[:80] if r.job_url else 'no url'}")
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

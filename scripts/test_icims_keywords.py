"""Test iCIMS scraper across multiple keywords to confirm robustness."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.icims_playwright import ICIMSPlaywrightScraper


async def main():
    KEYWORDS = ["operations", "analyst", "account manager", "sales"]
    async with ScraperClient(timeout_seconds=20) as client:
        scraper = ICIMSPlaywrightScraper(client=client)
        roles = await scraper.search(keywords=KEYWORDS, limit_per_keyword=10)
        print(f"\nTotal unique roles across {len(KEYWORDS)} keywords: {len(roles)}")

        from collections import Counter
        by_company = Counter(r.company for r in roles)
        print("\nBy company:")
        for c, n in sorted(by_company.items(), key=lambda x: -x[1]):
            print(f"  {n:>3}  {c}")


if __name__ == "__main__":
    asyncio.run(main())

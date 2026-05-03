"""Live test for ICIMSPlaywrightScraper across verified tenants."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.icims_playwright import ICIMSPlaywrightScraper


async def main():
    async with ScraperClient(timeout_seconds=20) as client:
        scraper = ICIMSPlaywrightScraper(client=client)
        print("Searching iCIMS tenants for 'manager'...")
        roles = await scraper.search(keywords=["manager"], limit_per_keyword=10)
        print(f"\nTotal roles returned: {len(roles)}")

        from collections import Counter
        by_company = Counter(r.company for r in roles)
        print("\nBy company:")
        for c, n in sorted(by_company.items(), key=lambda x: -x[1]):
            print(f"  {n:>3}  {c}")

        if roles:
            print("\nSample of first 10:")
            for r in roles[:10]:
                print(f"  {r.company:25s}  {r.job_title[:60]}")
                print(f"    URL: {r.job_url[:80] if r.job_url else '(relative)'}")


if __name__ == "__main__":
    asyncio.run(main())

"""Test The Muse + Remotive scrapers."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.themuse import TheMuseScraper
from backend.scraper.remotive import RemotiveScraper


async def main():
    keywords = ["operations manager", "project manager", "account manager", "policy analyst"]
    async with ScraperClient(timeout_seconds=30) as client:
        print("=== The Muse ===")
        muse = TheMuseScraper(client=client)
        muse_roles = await muse.search(keywords=keywords, limit_per_keyword=20)
        from collections import Counter
        co = Counter(r.company for r in muse_roles)
        print(f"Total: {len(muse_roles)}")
        print(f"Distinct companies: {len(co)}")
        for c, n in sorted(co.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:>3}  {c}")

        print("\n=== Remotive ===")
        remo = RemotiveScraper(client=client)
        remo_roles = await remo.search(keywords=keywords, limit_per_keyword=20)
        co2 = Counter(r.company for r in remo_roles)
        print(f"Total: {len(remo_roles)}")
        print(f"Distinct companies: {len(co2)}")
        for c, n in sorted(co2.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:>3}  {c}")


if __name__ == "__main__":
    asyncio.run(main())

"""Live test USAJOBS scraper."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.usajobs import USAJobsScraper


async def main():
    async with ScraperClient(timeout_seconds=30) as client:
        scraper = USAJobsScraper(client=client)
        roles = await scraper.search(
            keywords=["AI strategy", "environmental engineer", "software engineer", "policy analyst"],
            limit_per_keyword=20,
        )
        print(f"USAJOBS returned {len(roles)} unique roles\n")
        from collections import Counter
        by_co = Counter(r.company for r in roles)
        print("By agency:")
        for c, n in sorted(by_co.items(), key=lambda x: -x[1])[:15]:
            print(f"  {n:>3}  {c}")
        print()
        print(f"Sample of 8:")
        for r in roles[:8]:
            print(f"  {r.job_title[:55]:55s} ({r.company[:30]})")
            print(f"    salary: {r.salary_text or 'n/a'}  loc: {r.location or 'n/a'}")
            print(f"    jd_len: {len(r.job_description_full or '')}")


if __name__ == "__main__":
    asyncio.run(main())

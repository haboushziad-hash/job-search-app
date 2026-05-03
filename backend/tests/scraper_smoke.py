r"""Quick smoke test for scrapers — hits real public APIs.

Run from project root:
  backend\venv\Scripts\python.exe -m backend.tests.scraper_smoke
"""
from __future__ import annotations

import asyncio
import sys

from backend.scraper.greenhouse import GreenhouseScraper


async def smoke_greenhouse() -> None:
    print("=" * 70)
    print("Greenhouse — searching 'AI' + 'consultant' + 'enablement'")
    print("=" * 70)
    async with GreenhouseScraper() as scraper:
        roles = await scraper.search(
            keywords=["AI", "consultant", "enablement", "strategy"],
            posted_within_days=60,
        )
    print(f"\nFound {len(roles)} matching roles")
    if roles:
        print("\nSample of first 10:")
        for r in roles[:10]:
            jd_len = len(r.job_description_full or "")
            print(f"  {(r.job_title or '')[:55]:<55}  @ {(r.company or '')[:25]:<25}  loc={r.location_type:<8}  jd={jd_len:>5}ch")
        print(f"\nDistribution:")
        loc_types: dict[str, int] = {}
        for r in roles:
            loc_types[r.location_type or "?"] = loc_types.get(r.location_type or "?", 0) + 1
        for k, v in sorted(loc_types.items(), key=lambda x: -x[1]):
            print(f"  location_type={k}: {v}")
        print(f"  with JD body: {sum(1 for r in roles if r.job_description_full)}")
        print(f"  with salary:  {sum(1 for r in roles if r.salary_text)}")
        print(f"  unique companies: {len(set((r.company or '') for r in roles))}")


async def smoke_orchestrator() -> None:
    """Test the full scraper orchestrator across all boards."""
    from backend.scraper.orchestrator import scrape_all
    print("\n" + "=" * 70)
    print("Multi-board orchestrator — Greenhouse + Lever + Ashby")
    print("=" * 70)
    roles = await scrape_all(
        keywords=["AI strategy", "AI consultant", "AI enablement", "AI governance"],
        posted_within_days=60,
        log=True,
    )
    print(f"\nTotal deduped roles: {len(roles)}")
    if roles:
        print(f"\nPer-source breakdown:")
        by_src: dict[str, int] = {}
        for r in roles:
            by_src[r.primary_source or "?"] = by_src.get(r.primary_source or "?", 0) + 1
        for k, v in sorted(by_src.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
        print(f"\nLocation type:")
        loc_types: dict[str, int] = {}
        for r in roles:
            loc_types[r.location_type or "?"] = loc_types.get(r.location_type or "?", 0) + 1
        for k, v in sorted(loc_types.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
        print(f"\nWith salary metadata: {sum(1 for r in roles if r.salary_text)}")
        print(f"Unique companies:     {len(set((r.company or '') for r in roles))}")
        print(f"\nTop 15 sample (one per company):")
        seen_co = set()
        shown = 0
        for r in roles:
            if r.company in seen_co:
                continue
            seen_co.add(r.company)
            shown += 1
            print(f"  [{r.primary_source:<10}]  {(r.job_title or '')[:55]:<55}  @ {(r.company or '')[:25]}")
            if shown >= 15:
                break


async def main() -> int:
    await smoke_greenhouse()
    await smoke_orchestrator()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

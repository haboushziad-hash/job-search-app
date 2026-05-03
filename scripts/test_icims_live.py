"""Live test: iCIMS scraper against multiple tenants."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.icims import ICIMSScraper, ICIMS_TENANTS


async def main():
    # Test a sample of tenants across sectors
    sample = [
        ("Hershey",         "hersheys"),
        ("Mars",            "mars"),
        ("Diageo",          "diageo"),
        ("Bayer",           "bayer"),
        ("AstraZeneca",     "astrazeneca"),
        ("IHG",             "ihg"),
        ("Hyatt",           "hyatt"),
        ("UPS",             "ups"),
        ("Honeywell",       "honeywell"),
        ("Liberty Mutual",  "libertymutual"),
        ("Dollar Tree",     "dollartree"),
        ("Macy's",          "macysjobs"),
    ]

    async with ScraperClient(timeout_seconds=20) as client:
        scraper = ICIMSScraper(client=client)
        results = []
        for display, sub in sample:
            try:
                roles = await asyncio.wait_for(
                    scraper._search_tenant_keyword(display, sub, "manager", 5),
                    timeout=25.0,
                )
            except Exception as e:
                roles = []
                err = type(e).__name__
            else:
                err = None
            n = len(roles)
            sample_title = roles[0].job_title[:50] if roles else "(none)"
            print(f"  {n:>3} roles  {display:20s}  {sample_title}  {err or ''}")
            results.append((display, n))

        ok = sum(1 for _, n in results if n > 0)
        print(f"\n{ok}/{len(results)} tenants returned roles")


if __name__ == "__main__":
    asyncio.run(main())

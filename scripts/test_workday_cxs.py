"""Live test: Workday CXS endpoint hits across multiple tenants.

Strategy:
  1. POST to Workday search API for each tenant with keyword "manager"
  2. Pick the first job result from each
  3. Call fetch_jd() and check we get >500 chars of plain text
  4. Compare CXS path vs HTML-fallback path counts

This exercises the live network path, not just unit-test mocking.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.workday import WorkdayScraper, WORKDAY_TENANTS


async def main():
    # Pick 5 tenants we know work for search
    SAMPLE = [
        ("Adobe",          "https://adobe.wd5.myworkdayjobs.com",        "external_experienced"),
        ("Capital One",    "https://capitalone.wd12.myworkdayjobs.com",  "Capital_One"),
        ("Walmart",        "https://walmart.wd5.myworkdayjobs.com",      "WalmartExternal"),
        ("Target",         "https://target.wd5.myworkdayjobs.com",       "targetcareers"),
        ("Salesforce",     "https://salesforce.wd12.myworkdayjobs.com",  "External_Career_Site"),
    ]

    async with ScraperClient(timeout_seconds=30) as client:
        scraper = WorkdayScraper(client=client)

        results = []
        for display, base_url, board in SAMPLE:
            print(f"\n=== {display} ===")
            try:
                roles = await asyncio.wait_for(
                    scraper._search_tenant_keyword(display, base_url, board, "manager", 3),
                    timeout=30.0,
                )
            except Exception as e:
                print(f"  search failed: {type(e).__name__}: {e}")
                continue

            if not roles:
                print(f"  no roles returned")
                continue

            r = roles[0]
            print(f"  picked: {r.job_title[:60]}")
            print(f"  url:    {r.job_url}")

            # Try CXS path explicitly
            cxs_jd = await scraper._fetch_jd_via_cxs(r.job_url)
            cxs_len = len(cxs_jd or "")
            print(f"  CXS len:  {cxs_len:6}  {'OK' if cxs_len > 500 else 'EMPTY'}")

            # Now try the full fetch_jd (which falls back to HTML if CXS fails)
            full_jd = await scraper.fetch_jd(r)
            full_len = len(full_jd or "")
            print(f"  full len: {full_len:6}  {'OK' if full_len > 500 else 'EMPTY'}")

            results.append((display, cxs_len, full_len))

        print("\n" + "=" * 60)
        print(f"{'tenant':20s} {'cxs':>8s} {'full':>8s}")
        print("=" * 60)
        cxs_ok = 0
        for display, c, f in results:
            print(f"{display:20s} {c:8d} {f:8d}")
            if c > 500:
                cxs_ok += 1
        print("=" * 60)
        print(f"CXS success: {cxs_ok}/{len(results)} tenants")


if __name__ == "__main__":
    asyncio.run(main())

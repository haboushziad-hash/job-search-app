"""Systematic audit of every active scraper.

Hits each source with the same set of keywords and reports:
  - Number of roles returned
  - Distinct companies
  - Time elapsed
  - Top 5 companies (so we can spot whether companies look real)

Goal: confirm EVERY source is actually grabbing jobs. If any returns 0
or undersamples, we surface it here.
"""
from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import config  # ensures .env is loaded
from backend.scraper.client import ScraperClient
from backend.scraper.orchestrator import SCRAPER_REGISTRY


KEYWORDS = ["operations manager", "account manager"]
LIMIT_PER_KEYWORD = 50


async def audit_scraper(name: str, scraper_cls, client) -> dict:
    started = time.time()
    try:
        scraper = scraper_cls(client=client)
        roles = await asyncio.wait_for(
            scraper.search(keywords=KEYWORDS, limit_per_keyword=LIMIT_PER_KEYWORD),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        return {"name": name, "status": "TIMEOUT", "elapsed": time.time() - started}
    except Exception as e:
        return {"name": name, "status": f"ERR:{type(e).__name__}",
                "msg": str(e)[:120], "elapsed": time.time() - started}

    companies = Counter(r.company for r in roles)
    return {
        "name": name,
        "status": "OK",
        "n_roles": len(roles),
        "n_companies": len(companies),
        "top_5": companies.most_common(5),
        "elapsed": time.time() - started,
    }


async def main():
    print(f"=== Audit of {len(SCRAPER_REGISTRY)} scrapers ===")
    print(f"Keywords: {KEYWORDS}")
    print(f"Limit per keyword: {LIMIT_PER_KEYWORD}\n")

    results = []
    async with ScraperClient(timeout_seconds=30) as client:
        for name, cls in SCRAPER_REGISTRY.items():
            print(f"  Probing {name}...", flush=True)
            r = await audit_scraper(name, cls, client)
            results.append(r)

    print()
    print(f"{'source':<14} {'status':<12} {'roles':>6} {'companies':>10} {'elapsed_s':>10}")
    print("-" * 70)
    for r in results:
        if r.get("status") == "OK":
            print(f"{r['name']:<14} {'OK':<12} {r['n_roles']:>6} {r['n_companies']:>10} {r['elapsed']:>10.1f}")
        else:
            print(f"{r['name']:<14} {r.get('status','?'):<12} {'-':>6} {'-':>10} {r['elapsed']:>10.1f}")
            if r.get("msg"):
                print(f"  ↳ {r['msg']}")

    print()
    print("Top 5 companies per source:")
    for r in results:
        if r.get("status") != "OK" or not r.get("top_5"):
            continue
        print(f"  {r['name']}:")
        for c, n in r["top_5"]:
            print(f"    {n:>3}  {c[:50]}")

    grand_total = sum(r.get("n_roles", 0) for r in results if r.get("status") == "OK")
    print(f"\nGRAND TOTAL across all sources: {grand_total} roles")


if __name__ == "__main__":
    asyncio.run(main())

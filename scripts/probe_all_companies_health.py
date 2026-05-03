"""Probe every company in our Greenhouse, Lever, and Ashby lists to verify
each one still returns jobs. Report dead/migrated companies so we can clean
the lists."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from backend.scraper.greenhouse import GREENHOUSE_COMPANIES
from backend.scraper.lever import LEVER_COMPANIES
from backend.scraper.ashby import ASHBY_COMPANIES


async def probe_gh(c, name, t):
    try:
        r = await c.get(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs", timeout=10)
        if r.status_code == 200:
            return name, t, len(r.json().get("jobs", []))
    except Exception:
        pass
    return name, t, -1


async def probe_lv(c, name, t):
    try:
        r = await c.get(f"https://api.lever.co/v0/postings/{t}?mode=json", timeout=10)
        if r.status_code == 200 and isinstance(r.json(), list):
            return name, t, len(r.json())
    except Exception:
        pass
    return name, t, -1


async def probe_ab(c, name, slug):
    try:
        r = await c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=10)
        if r.status_code == 200:
            return name, slug, len(r.json().get("jobs", []))
    except Exception:
        pass
    return name, slug, -1


async def audit(label, lst, fn):
    sem = asyncio.Semaphore(15)
    async with httpx.AsyncClient(timeout=12) as c:
        async def b(name, t):
            async with sem:
                return await fn(c, name, t)
        rs = await asyncio.gather(*[b(n, t) for n, t in lst])

    live = [r for r in rs if r[2] > 0]
    dead = [r for r in rs if r[2] == 0]
    err  = [r for r in rs if r[2] == -1]
    total_jobs = sum(r[2] for r in live)
    print(f"\n=== {label}: {len(lst)} configured ===")
    print(f"  LIVE: {len(live)}  ({total_jobs} jobs)")
    print(f"  DEAD (200, 0 jobs): {len(dead)}")
    print(f"  404/ERR: {len(err)}")
    if dead:
        print(f"  Dead samples: {', '.join(n for n, _, _ in dead[:15])}")
    if err:
        print(f"  Error samples: {', '.join(n for n, _, _ in err[:15])}")


async def main():
    await audit("GREENHOUSE", GREENHOUSE_COMPANIES, probe_gh)
    await audit("LEVER",      LEVER_COMPANIES,      probe_lv)
    await audit("ASHBY",      ASHBY_COMPANIES,      probe_ab)


if __name__ == "__main__":
    asyncio.run(main())

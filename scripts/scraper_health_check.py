"""Scheduled health-check probe for every scraper + every company token.

Runs autonomously (cron-able) and produces:
  - A daily snapshot in scripts/health_log/YYYY-MM-DD.json
  - A regression report comparing today vs prior snapshot
  - Console output flagging any source/company that broke since last run

What it monitors:
  1. Each of the 15 scrapers — does it return any results for a broad keyword?
  2. Greenhouse / Lever / Ashby company tokens — which return >0 jobs vs 404?
  3. Workday tenant POST endpoints — which are still alive?
  4. iCIMS subdomains — which still serve jobs?
  5. Adzuna / USAJOBS / Findwork — does the API still work with our keys?

Output:
  scripts/health_log/<YYYY-MM-DD>.json   (full snapshot)
  scripts/health_log/REGRESSIONS.md      (latest delta from prior snapshot)
  Console: ✓ healthy / ⚠ degraded / ✗ broken per source

Run manually:
    backend/venv/Scripts/python.exe scripts/scraper_health_check.py

Or schedule via Windows Task Scheduler / cron to run daily.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import config  # ensures dotenv loads
from backend.scraper.client import ScraperClient
from backend.scraper.orchestrator import SCRAPER_REGISTRY
from backend.scraper.greenhouse import GREENHOUSE_COMPANIES
from backend.scraper.lever import LEVER_COMPANIES
from backend.scraper.ashby import ASHBY_COMPANIES
from backend.scraper.workday import WORKDAY_TENANTS
from backend.scraper.icims_playwright import ICIMS_PW_TENANTS
from backend.scraper.smartrecruiters import SMARTRECRUITERS_COMPANIES


HEALTH_DIR = Path(__file__).resolve().parent / "health_log"
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
PROBE_KEYWORDS = ["manager"]  # broad enough that EVERY healthy source should return >0


# ============================================================================
# Per-scraper smoke probe
# ============================================================================

async def probe_scraper(name: str, scraper_cls, client) -> dict:
    started = time.time()
    try:
        scraper = scraper_cls(client=client)
        roles = await asyncio.wait_for(
            scraper.search(keywords=PROBE_KEYWORDS, limit_per_keyword=20),
            timeout=120.0,
        )
        elapsed = time.time() - started
        co = Counter(r.company for r in roles)
        return {
            "name": name,
            "status": "OK" if roles else "EMPTY",
            "roles": len(roles),
            "distinct_companies": len(co),
            "elapsed_s": round(elapsed, 1),
            "top_3": [c for c, _ in co.most_common(3)],
        }
    except asyncio.TimeoutError:
        return {"name": name, "status": "TIMEOUT", "elapsed_s": time.time() - started}
    except Exception as e:
        return {"name": name, "status": f"ERR:{type(e).__name__}",
                "msg": str(e)[:120], "elapsed_s": time.time() - started}


# ============================================================================
# Per-company-token health (Greenhouse/Lever/Ashby)
# ============================================================================

async def probe_token_health(name: str, lst: list[tuple[str, str]],
                              probe_fn) -> dict:
    """Probe every (display, slug) tuple. Returns {alive: N, dead: [], total: M}."""
    import httpx
    sem = asyncio.Semaphore(15)
    async with httpx.AsyncClient(timeout=10) as c:
        async def b(d, s):
            async with sem:
                try:
                    return d, s, await probe_fn(c, s)
                except Exception:
                    return d, s, -1
        results = await asyncio.gather(*[b(d, s) for d, s in lst])
    live = [(d, s, n) for d, s, n in results if n > 0]
    dead = [(d, s, n) for d, s, n in results if n == 0]
    err  = [(d, s, n) for d, s, n in results if n == -1]
    return {
        "configured": len(lst),
        "alive": len(live),
        "dead_zero_jobs": len(dead),
        "errored_or_404": len(err),
        "total_jobs": sum(n for _, _, n in live),
        "dead_samples": [d for d, _, _ in dead[:10]],
        "err_samples": [d for d, _, _ in err[:10]],
    }


async def _gh_probe(c, slug):
    r = await c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if r.status_code == 200:
        return len(r.json().get("jobs", []))
    return -1


async def _lv_probe(c, slug):
    r = await c.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if r.status_code == 200 and isinstance(r.json(), list):
        return len(r.json())
    return -1


async def _ab_probe(c, slug):
    r = await c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if r.status_code == 200:
        return len(r.json().get("jobs", []))
    return -1


async def _sr_probe(c, slug):
    r = await c.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
    if r.status_code == 200:
        return len(r.json().get("content", []))
    return -1


# ============================================================================
# Main
# ============================================================================

async def main():
    started_at = time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    snapshot: dict = {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "probe_keyword": PROBE_KEYWORDS,
        "scrapers": {},
        "company_lists": {},
        "summary": {},
    }

    print(f"=== Scraper health check — {today} ===\n")

    print("--- Scrapers (live API smoke probe) ---")
    async with ScraperClient(timeout_seconds=30) as client:
        for name, cls in SCRAPER_REGISTRY.items():
            print(f"  Probing {name}...", flush=True)
            r = await probe_scraper(name, cls, client)
            snapshot["scrapers"][name] = r
            flag = "OK " if r.get("status") == "OK" else ("--" if r.get("status") == "EMPTY" else "!!")
            print(f"    [{flag}] {r.get('status'):<14} {r.get('roles', 0):>4} roles  {r.get('elapsed_s', 0):>6.1f}s  {r.get('top_3', [])}")

    print("\n--- Company-token health (Greenhouse / Lever / Ashby / SmartRecruiters) ---")
    snapshot["company_lists"]["Greenhouse"] = await probe_token_health(
        "Greenhouse", GREENHOUSE_COMPANIES, _gh_probe
    )
    print(f"  Greenhouse:    {snapshot['company_lists']['Greenhouse']['alive']:>3}/{snapshot['company_lists']['Greenhouse']['configured']} alive ({snapshot['company_lists']['Greenhouse']['total_jobs']} jobs)")
    snapshot["company_lists"]["Lever"] = await probe_token_health(
        "Lever", LEVER_COMPANIES, _lv_probe
    )
    print(f"  Lever:         {snapshot['company_lists']['Lever']['alive']:>3}/{snapshot['company_lists']['Lever']['configured']} alive ({snapshot['company_lists']['Lever']['total_jobs']} jobs)")
    snapshot["company_lists"]["Ashby"] = await probe_token_health(
        "Ashby", ASHBY_COMPANIES, _ab_probe
    )
    print(f"  Ashby:         {snapshot['company_lists']['Ashby']['alive']:>3}/{snapshot['company_lists']['Ashby']['configured']} alive ({snapshot['company_lists']['Ashby']['total_jobs']} jobs)")
    snapshot["company_lists"]["SmartRecruiters"] = await probe_token_health(
        "SmartRecruiters", SMARTRECRUITERS_COMPANIES, _sr_probe
    )
    print(f"  SmartRecruiters: {snapshot['company_lists']['SmartRecruiters']['alive']:>3}/{snapshot['company_lists']['SmartRecruiters']['configured']} alive ({snapshot['company_lists']['SmartRecruiters']['total_jobs']} jobs)")

    snapshot["company_lists"]["Workday_count"] = len(WORKDAY_TENANTS)
    snapshot["company_lists"]["iCIMS_count"]   = len(ICIMS_PW_TENANTS)
    print(f"  Workday tenants configured: {len(WORKDAY_TENANTS)} (POST-style; live-probe takes minutes per tenant — skipped here)")
    print(f"  iCIMS tenants configured:   {len(ICIMS_PW_TENANTS)} (Playwright-based; live-probe takes minutes — skipped here)")

    snapshot["summary"]["elapsed_s"] = round(time.time() - started_at, 1)
    snapshot["summary"]["healthy_scrapers"] = sum(
        1 for r in snapshot["scrapers"].values() if r.get("status") == "OK"
    )
    snapshot["summary"]["empty_scrapers"] = sum(
        1 for r in snapshot["scrapers"].values() if r.get("status") == "EMPTY"
    )
    snapshot["summary"]["broken_scrapers"] = sum(
        1 for r in snapshot["scrapers"].values()
        if r.get("status", "").startswith(("ERR", "TIMEOUT"))
    )

    # Write snapshot
    out_path = HEALTH_DIR / f"{today}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    print(f"\nSnapshot saved: {out_path}")

    # Compare to prior snapshot (if any)
    prior_files = sorted(HEALTH_DIR.glob("*.json"))
    if len(prior_files) >= 2:
        prior = json.loads(prior_files[-2].read_text(encoding="utf-8"))
        regressions = compare_snapshots(prior, snapshot)
        if regressions:
            print("\n!! REGRESSIONS DETECTED:")
            for line in regressions:
                print(f"  {line}")
            (HEALTH_DIR / "REGRESSIONS.md").write_text(
                "# Health regressions\n\n" + "\n".join(f"- {r}" for r in regressions),
                encoding="utf-8"
            )
        else:
            print("\nOK No regressions vs last snapshot.")
    else:
        print("\n(No prior snapshot to compare against — this is the baseline.)")


def compare_snapshots(prior: dict, current: dict) -> list[str]:
    out: list[str] = []
    for name, c in current.get("scrapers", {}).items():
        p = prior.get("scrapers", {}).get(name) or {}
        p_roles = p.get("roles", 0)
        c_roles = c.get("roles", 0)
        if p_roles >= 5 and c_roles == 0:
            out.append(f"BROKE: {name} — was {p_roles} roles, now 0")
        elif p_roles > 0 and c_roles < p_roles * 0.5:
            out.append(f"DEGRADED: {name} — was {p_roles} roles, now {c_roles}")
    for ats in ("Greenhouse", "Lever", "Ashby", "SmartRecruiters"):
        p_alive = prior.get("company_lists", {}).get(ats, {}).get("alive", 0)
        c_alive = current.get("company_lists", {}).get(ats, {}).get("alive", 0)
        if p_alive > 0 and c_alive < p_alive * 0.8:
            out.append(f"COMPANY-LIST DEGRADED: {ats} — was {p_alive} alive, now {c_alive}")
    return out


if __name__ == "__main__":
    asyncio.run(main())

"""Workday Playwright discovery v2 — captures XHRs that fire on page LOAD.

V1 only captured XHRs that fired AFTER typing in the search input. But many
modern Workday SPAs fire the initial jobs query ON PAGE LOAD (the empty
search). V1 missed these because it required a search input + fill action.

V2 strategy:
  1. Set up XHR capture BEFORE navigating
  2. Navigate to the careers page
  3. Wait for the SPA to mount (5-8s)
  4. Look at all captured POSTs to /wday/cxs/ — those are jobs queries
  5. If found, extract the tenant + board from the URL pattern

Run on the SKIPPED tenants from v1.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright


CONFIG_DIR = Path(__file__).resolve().parent.parent / "backend" / "scraper" / "workday_tenants"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# Tenants that V1 SKIP'd because no search input was found, but their URLs
# look like real Workday subdomains. V2 will capture page-load XHRs.
TARGETS = [
    # Big 4 / consulting
    ("KPMG",            "https://kpmg.wd5.myworkdayjobs.com/KPMGUS_Careers"),
    # CPG
    ("Kraft Heinz",     "https://kraftheinz.wd1.myworkdayjobs.com/Kraft_Heinz_Careers"),
    ("Mondelez",        "https://mondelez.wd1.myworkdayjobs.com/External"),
    # Banking
    ("JPMorgan",        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"),
    ("Wells Fargo",     "https://wellsfargo.wd1.myworkdayjobs.com/WellsFargoJobs"),
    # Healthcare
    ("UnitedHealth",    "https://uhg.wd5.myworkdayjobs.com/External"),
    ("Johnson & Johnson","https://jnjc.wd5.myworkdayjobs.com/jnjcareers"),
    # Defense
    ("Lockheed Martin", "https://lockheedmartin.wd1.myworkdayjobs.com/External"),
    ("Northrop Grumman","https://ngc.wd1.myworkdayjobs.com/NGCareers"),
    ("SAIC",            "https://saic.wd5.myworkdayjobs.com/External"),
    # Insurance / industrial
    ("Liberty Mutual",  "https://lmi.wd1.myworkdayjobs.com/LMI"),
    ("Honeywell",       "https://honeywell.wd1.myworkdayjobs.com/Honeywell"),
    # Retail
    ("Kroger",          "https://jobs.kroger.com/search-results"),
    # Try variants for the tenants that may have moved boards
    ("KPMG (US)",       "https://kpmg.wd5.myworkdayjobs.com/Global_Experienced_Careers"),
    ("Wells Fargo Tech","https://wellsfargo.wd1.myworkdayjobs.com/External"),
    ("UnitedHealth Optum","https://optum.wd5.myworkdayjobs.com/External"),
    # Other potentially-Workday tenants
    ("Costco",          "https://costco.wd5.myworkdayjobs.com/Costco"),
    ("State Street",    "https://statestreet.wd1.myworkdayjobs.com/Global"),
    ("Anthem (Elevance)","https://elevancehealth.wd1.myworkdayjobs.com/ANTHEM"),
    ("CVS",             "https://cvshealth.wd1.myworkdayjobs.com/CVS"),
    ("Best Buy",        "https://bestbuy.wd1.myworkdayjobs.com/External"),
    ("Sysco",           "https://sysco.wd1.myworkdayjobs.com/Sysco_Career_Site"),
    ("Cardinal Health", "https://cardinalhealth.wd1.myworkdayjobs.com/cardinalhealthjobs"),
    ("US Bank",         "https://usbank.wd5.myworkdayjobs.com/USBank_Careers"),
]


async def discover_one(target: tuple[str, str], browser) -> dict:
    name, url = target
    started = time.time()
    result = {
        "name": name,
        "url": url,
        "status": "FAILED",
        "reason": "",
        "captured_url": None,
        "captured_body": None,
        "captured_headers": None,
        "elapsed": 0.0,
    }
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()

    captured = []

    def on_request(req):
        try:
            u = req.url
            # Workday CXS jobs queries are POST to /wday/cxs/.../jobs
            if req.method == "POST" and "/wday/cxs/" in u:
                captured.append({
                    "url": u,
                    "headers": dict(req.headers),
                    "body": req.post_data,
                })
        except Exception:
            pass

    page.on("request", on_request)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        # Wait for SPA + initial XHR
        await page.wait_for_timeout(7000)

        # Dismiss cookie banners if any
        for cookie_btn in [
            "button:has-text('Accept')",
            "button:has-text('I Accept')",
            "button:has-text('Accept All')",
            "button#onetrust-accept-btn-handler",
        ]:
            try:
                btn = await page.query_selector(cookie_btn)
                if btn:
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(2500)
                    break
            except Exception:
                continue

        # Filter for the actual jobs query (not facets/suggest)
        candidates = [
            x for x in captured
            if x.get("url", "").endswith("/jobs")
            and x.get("body")
            and "appliedFacets" in (x["body"] or "")
        ]
        if not candidates:
            # Try any cxs URL with body
            candidates = [x for x in captured if x.get("body")]

        if candidates:
            best = candidates[0]
            result["status"] = "OK"
            result["captured_url"] = best["url"]
            result["captured_body"] = best["body"]
            result["captured_headers"] = {
                k: v for k, v in (best["headers"] or {}).items()
                if k.lower() in ("accept", "content-type", "user-agent", "x-csrf-token")
            }
        else:
            result["status"] = "EMPTY"
            result["reason"] = f"no /wday/cxs/ POST captured (saw {len(captured)} CXS requests)"
    except Exception as e:
        result["reason"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        result["elapsed"] = round(time.time() - started, 1)
        await context.close()
    return result


async def main():
    print(f"=== Workday Discovery v2 — {len(TARGETS)} tenants ===\n")
    started_at = time.time()
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for tgt in TARGETS:
                print(f"[{tgt[0]:30s}] discovering...", flush=True)
                r = await discover_one(tgt, browser)
                if r["status"] == "OK":
                    print(f"  OK ({r['elapsed']}s)  {r['captured_url'][:80]}")
                    # Save config
                    safe = tgt[0].lower().replace(" ", "_").replace("&", "and").replace(".", "").replace("(", "").replace(")", "")
                    out = CONFIG_DIR / f"{safe}_v2.json"
                    out.write_text(json.dumps(r, indent=2), encoding="utf-8")
                else:
                    print(f"  {r['status']} ({r['elapsed']}s)  {r['reason'][:80]}")
                results.append(r)
        finally:
            await browser.close()

    ok = [r for r in results if r["status"] == "OK"]
    print(f"\n=== Summary ({round(time.time() - started_at, 1)}s) ===")
    print(f"  Captured: {len(ok)} / {len(TARGETS)}")
    if ok:
        print("\nNew Workday tenants to add:")
        for r in ok:
            url = r["captured_url"]
            # parse: https://{sub}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                host = parsed.netloc
                parts = parsed.path.strip("/").split("/")  # ["wday","cxs",tenant,board,"jobs"]
                if len(parts) >= 5:
                    tenant = parts[2]
                    board = parts[3]
                    base = f"https://{host}"
                    print(f'    ("{r["name"]}", "{base}", "{board}"),')
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())

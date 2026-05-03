"""Discover what ATS each big-name company actually uses by visiting their
PUBLIC careers landing page (not guessed Workday subdomains).

For each company, navigate Chromium to their real careers URL, capture all
job-search XHRs (any domain, any ATS), then classify:
  - Workday: URL matches /wday/cxs/ or myworkdayjobs.com
  - iCIMS: URL matches icims.com
  - Taleo: URL matches taleo.net
  - SuccessFactors: URL matches successfactors.com
  - Greenhouse: boards-api.greenhouse.io
  - Lever: api.lever.co
  - Ashby: jobs.ashbyhq.com
  - Proprietary: anything else

Output: scripts/ats_landscape.json with full results.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright, Request


# Real public careers URLs for the failed 19 + a few more high-value ones.
# These are landing pages real users visit, not guessed Workday subdomains.
TARGETS: list[tuple[str, str]] = [
    ("JPMorgan Chase",   "https://careers.jpmorgan.com/global/en/jobs"),
    ("Goldman Sachs",    "https://www.goldmansachs.com/careers/professionals/jobs/index.html"),
    ("Wells Fargo",      "https://www.wellsfargojobs.com/en/jobs/"),
    ("PepsiCo",          "https://www.pepsicojobs.com/main/jobs"),
    ("Coca-Cola",        "https://careers.coca-colacompany.com/jobs"),
    ("Procter & Gamble", "https://www.pgcareers.com/global/en/search-results"),
    ("Microsoft",        "https://careers.microsoft.com/v2/global/en/search.html"),
    ("UnitedHealth",     "https://careers.unitedhealthgroup.com/"),
    ("Johnson & Johnson","https://jobs.jnj.com/en/jobs"),
    ("Merck",            "https://jobs.merck.com/us/en/search-results"),
    ("Liberty Mutual",   "https://jobs.libertymutualgroup.com/search-jobs"),
    ("Honeywell",        "https://careers.honeywell.com/us/en/search-results"),
    ("ExxonMobil",       "https://jobs.exxonmobil.com/search/"),
    ("IBM",              "https://www.ibm.com/careers/search"),
    ("Lockheed Martin",  "https://www.lockheedmartinjobs.com/search-jobs"),
    ("SAIC",             "https://jobs.saic.com/jobs/search"),
    ("Northrop Grumman", "https://www.northropgrumman.com/careers/career-search"),
    ("McDonald's",       "https://careers.mcdonalds.com/main/jobs"),
    ("EY",               "https://www.ey.com/en_us/careers/job-search"),
    ("KPMG",             "https://kpmg.recsolu.com/jobs"),
    ("Deloitte",         "https://apply.deloitte.com/en_US/careers/SearchJobs"),
    # Bonus: companies likely on iCIMS/Taleo/SuccessFactors
    ("Aflac",            "https://careers.aflac.com/us/en/search-results"),
    ("Allstate",         "https://www.allstate.jobs/search/"),
    ("MetLife",          "https://careers.metlife.com/global/en/search-results"),
    ("CVS Health",       "https://jobs.cvshealth.com/us/en/search-results"),
    ("Walgreens",        "https://jobs.walgreens.com/en/search-jobs"),
    ("Kroger",           "https://www.thekrogerco.com/careers/"),
    ("Costco",           "https://www.costco.com/jobs.html"),
    ("Home Depot",       "https://careers.homedepot.com/job-search-results/"),
    ("Lowe's",           "https://talent.lowes.com/us/en/search-results"),
]


def classify_url(url: str) -> str:
    """Identify which ATS family a URL belongs to."""
    u = url.lower()
    if "/wday/cxs/" in u or "myworkdayjobs.com" in u:
        return "Workday"
    if "icims.com" in u:
        return "iCIMS"
    if "taleo.net" in u or ".taleo." in u:
        return "Taleo"
    if "successfactors.com" in u or "sfsf" in u:
        return "SuccessFactors"
    if "greenhouse.io" in u:
        return "Greenhouse"
    if "lever.co" in u:
        return "Lever"
    if "ashbyhq.com" in u:
        return "Ashby"
    if "smartrecruiters.com" in u:
        return "SmartRecruiters"
    if "jobvite.com" in u:
        return "Jobvite"
    if "bullhorn.com" in u:
        return "Bullhorn"
    if "recsolu.com" in u:
        return "Recsolu"
    if "phenompeople.com" in u:
        return "Phenom"
    if "eightfold.ai" in u:
        return "Eightfold"
    if "smashfly.com" in u:
        return "Smashfly"
    return "Proprietary"


def looks_like_search_xhr(req: Request) -> bool:
    url = req.url.lower()
    if any(url.endswith(x) for x in (
        ".js", ".css", ".png", ".svg", ".ico", ".woff", ".woff2",
        ".gif", ".jpg", ".webp", ".html", ".htm", ".map", ".json"
    )) and "search" not in url and "jobs" not in url:
        return False
    if any(s in url for s in ("google-analytics", "googletagmanager", "doubleclick",
                                "facebook.com", "datadog", "newrelic", "segment.io",
                                "/track?", "/beacon", "amplitude", "hotjar")):
        return False
    has_marker = any(s in url for s in (
        "/wday/cxs/", "/jobs", "/searchjob", "/job-search", "/search/",
        "/careers/api", "/api/jobs", "/api/career", "/icims", "/taleo",
        "instantsearch", "_search", "joblist", "careers/api", "search-results",
        "graphql", "successfactors", "smartrecruiters",
    ))
    return has_marker


async def discover_one(name: str, url: str, browser) -> dict:
    print(f"[{name}]")
    print(f"  -> {url}")
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    captured: list[dict] = []
    all_xhrs: list[str] = []
    final_url: str = url

    async def on_finished(req: Request):
        all_xhrs.append(req.url)
        if not looks_like_search_xhr(req):
            return
        try:
            resp = await req.response()
            if resp is None:
                return
            body = await resp.text()
        except Exception:
            return
        # Parse for job count
        job_count = None
        try:
            data = json.loads(body) if body and body.strip().startswith(("{", "[")) else None
            if isinstance(data, dict):
                postings = data.get("jobPostings") or data.get("jobs") or data.get("results") or data.get("items") or []
                total = data.get("total") or data.get("totalCount") or data.get("hits", {}).get("found") if isinstance(data.get("hits"), dict) else None
                job_count = total if isinstance(total, int) else (len(postings) if postings else None)
            elif isinstance(data, list):
                job_count = len(data)
        except Exception:
            pass
        captured.append({
            "url": req.url,
            "method": req.method,
            "ats": classify_url(req.url),
            "status": resp.status,
            "job_count": job_count,
            "post_body_preview": (req.post_data or "")[:200] if req.post_data else None,
        })

    page.on("requestfinished", on_finished)

    try:
        nav = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if nav: final_url = nav.url
        await page.wait_for_timeout(7000)  # let XHRs settle
    except Exception as e:
        print(f"  navigation error: {type(e).__name__}: {str(e)[:120]}")

    await context.close()

    # Best capture = highest job_count, or any 200 if none had jobs
    valid = [c for c in captured if c["status"] == 200 and (c["job_count"] or 0) > 0]
    if not valid:
        valid = [c for c in captured if c["status"] == 200]

    # Determine the dominant ATS by counting captured XHRs
    ats_counts: dict[str, int] = {}
    for c in captured:
        if c["status"] == 200:
            ats_counts[c["ats"]] = ats_counts.get(c["ats"], 0) + 1
    primary_ats = max(ats_counts.items(), key=lambda x: x[1])[0] if ats_counts else "Unknown"

    best = max(valid, key=lambda c: c["job_count"] or 0) if valid else None
    if best:
        print(f"  ATS: {primary_ats}  |  best capture: {best['ats']} {best['method']} {best['url'][:100]} ({best.get('job_count')} jobs)")
    else:
        print(f"  ATS: {primary_ats}  |  no usable XHR captured among {len(captured)} candidates")

    return {
        "company": name,
        "input_url": url,
        "final_url": final_url,
        "primary_ats": primary_ats,
        "ats_xhr_counts": ats_counts,
        "total_xhrs_seen": len(all_xhrs),
        "captured_search_xhrs": captured,
        "best_capture": best,
    }


async def main():
    print(f"Probing {len(TARGETS)} public careers URLs to identify ATS landscape...\n")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        results = []
        for name, url in TARGETS:
            try:
                results.append(await discover_one(name, url, browser))
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                results.append({"company": name, "error": str(e)})
        await browser.close()

    Path('C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/ats_landscape.json').write_text(
        json.dumps(results, indent=2), encoding='utf-8'
    )

    # Summarize by ATS
    print("\n" + "=" * 70)
    print("ATS LANDSCAPE SUMMARY")
    print("=" * 70)
    by_ats: dict[str, list[str]] = {}
    for r in results:
        ats = r.get("primary_ats", "Unknown")
        by_ats.setdefault(ats, []).append(r.get("company", "?"))
    for ats, companies in sorted(by_ats.items(), key=lambda x: -len(x[1])):
        print(f"\n  {ats} ({len(companies)}):")
        for c in companies:
            print(f"    {c}")
    print(f"\nFull report: scripts/ats_landscape.json")


if __name__ == "__main__":
    asyncio.run(main())

"""Playwright-based Workday tenant discovery.

For each candidate company, navigates Chromium to their public careers page,
performs a job search, intercepts the actual XHR call that fires, and captures
the real endpoint URL + headers + request body. The discovery output is
written to one JSON config file per tenant in backend/scraper/workday_tenants/

Why this works when our previous POST-with-known-shape approach didn't:
  - Workday tenants customize their search UIs over time. Different deployment
    eras + customer customizations result in different request body shapes,
    different header requirements, and different endpoint paths.
  - By loading the actual page in a real browser, the JS app constructs the
    "right" request shape for that specific tenant. We just record it.
  - Result: a config file that we can replay via plain httpx in production.

Run from project root:
    backend/venv/Scripts/python.exe scripts/discover_workday_tenants.py

Optional: --only "Deloitte" to discover one tenant only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright, Page, Request


# Where discovered configs are written. Each tenant gets one JSON file.
TENANTS_DIR = Path(__file__).resolve().parent.parent / "backend" / "scraper" / "workday_tenants"
TENANTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Targets — top 20 high-value tenants from the prior 422-error list.
# Each entry: (display_name, careers_page_url). The careers URL is what
# Playwright navigates to. For most Workday tenants this is the public
# job-search landing page.
# ---------------------------------------------------------------------------
HIGH_VALUE_TARGETS: list[tuple[str, str]] = [
    ("Deloitte",         "https://apply.deloitte.com/en_US/careers"),
    ("EY",               "https://eyglobalcareers.wd3.myworkdayjobs.com/EYCareers"),
    ("KPMG",             "https://kpmg.wd5.myworkdayjobs.com/KPMG_Careers"),
    ("JPMorgan Chase",   "https://jpmc.wd5.myworkdayjobs.com/jpmc"),
    ("Goldman Sachs",    "https://goldmansachs.wd1.myworkdayjobs.com/Professional_Career_Search"),
    ("Wells Fargo",      "https://wellsfargojobs.wd1.myworkdayjobs.com/Wells_Fargo_Jobs"),
    ("PepsiCo",          "https://pepsico.wd5.myworkdayjobs.com/PepsiCoJobs"),
    ("Coca-Cola",        "https://coca-cola.wd1.myworkdayjobs.com/coca-cola_Careers"),
    ("Procter & Gamble", "https://pg.wd5.myworkdayjobs.com/en-US/PGCareers"),
    ("Microsoft",        "https://careers.microsoft.com/v2/global/en/home.html"),
    ("UnitedHealth",     "https://uhg.wd5.myworkdayjobs.com/External"),
    ("Johnson & Johnson","https://jnjcareers.wd5.myworkdayjobs.com/Search"),
    ("Merck",            "https://merck.wd5.myworkdayjobs.com/SearchJobs"),
    ("Liberty Mutual",   "https://lmi.wd1.myworkdayjobs.com/Liberty_Mutual"),
    ("Honeywell",        "https://honeywell.wd1.myworkdayjobs.com/Honeywell"),
    ("ExxonMobil",       "https://exxonmobil.wd5.myworkdayjobs.com/ExxonMobil"),
    ("IBM",              "https://ibmglobal.wd5.myworkdayjobs.com/IBM_Careers"),
    ("Lockheed Martin",  "https://lmcareers.wd5.myworkdayjobs.com/Lockheed_Martin"),
    ("SAIC",             "https://saic.wd1.myworkdayjobs.com/SAIC_External_Career_Site"),
    ("Northrop Grumman", "https://ngc.wd1.myworkdayjobs.com/NGCExternal"),
    ("McDonald's",       "https://mcdonalds.wd1.myworkdayjobs.com/Corporate"),
]


@dataclass
class CapturedXhr:
    """One captured network request that looks like a Workday job search."""
    url: str
    method: str
    headers: dict[str, str]
    post_body: Optional[str] = None
    response_status: Optional[int] = None
    response_body_preview: Optional[str] = None
    job_count: Optional[int] = None


def _looks_like_search_xhr(req: Request) -> bool:
    """Heuristic: is this a job-search XHR (Workday or otherwise)?

    Broadened from Workday-only to catch proprietary / iCIMS / Taleo /
    custom systems too. We accept any URL that looks job-related and isn't
    a static asset, regardless of domain.
    """
    url = req.url.lower()
    # Skip static assets up front
    if any(url.endswith(x) for x in (".js", ".css", ".png", ".svg", ".ico",
                                       ".woff", ".woff2", ".gif", ".jpg", ".webp",
                                       ".html", ".htm", ".map")):
        return False
    # Skip analytics/tracking
    if any(s in url for s in ("google-analytics", "googletagmanager", "doubleclick",
                                "facebook.com", "datadog", "newrelic", "segment.io",
                                "/track?", "/beacon", "amplitude")):
        return False
    # Look for job-search markers in the URL
    has_job_marker = any(s in url for s in (
        "/wday/cxs/", "/jobs", "/searchjob", "/job-search", "/search/",
        "/careers/api", "/api/jobs", "/api/career", "/icims", "/taleo",
        "instantsearch", "_search", "joblist",
    ))
    if not has_job_marker:
        return False
    return True


async def discover_one(name: str, careers_url: str, browser) -> Optional[CapturedXhr]:
    """Open a tenant's careers page, run a search, capture the XHR.

    We listen on the `requestfinished` event (after both request + response are
    fully captured) so we have the request body + headers AND the response.
    """
    print(f"\n[{name}]")
    print(f"  navigating to {careers_url}")

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()

    captured_per_url: dict[str, CapturedXhr] = {}
    all_xhrs_seen: list[str] = []

    async def on_request_finished(req: Request):
        all_xhrs_seen.append(f"{req.method} {req.url[:120]}")
        if not _looks_like_search_xhr(req):
            return
        try:
            resp = await req.response()
            if resp is None:
                return
            body = await resp.text()
        except Exception:
            return
        # Try parse JSON to count jobs
        job_count = None
        try:
            data = json.loads(body) if body.startswith("{") else None
            if isinstance(data, dict):
                postings = data.get("jobPostings") or []
                total = data.get("total")
                job_count = total if total is not None else len(postings)
        except Exception:
            pass

        captured_per_url[req.url] = CapturedXhr(
            url=req.url,
            method=req.method,
            headers=dict(req.headers),
            post_body=req.post_data,
            response_status=resp.status,
            response_body_preview=body[:500],
            job_count=job_count,
        )

    page.on("requestfinished", on_request_finished)

    try:
        await page.goto(careers_url, wait_until="domcontentloaded", timeout=30000)
        # Initial wait — many Workday pages auto-fire an XHR for "all jobs"
        # on page load even before the user types anything.
        await page.wait_for_timeout(5000)

        # Try search box (may not be visible yet on some tenants)
        searched = False
        for selector in [
            '[data-automation-id="keywordSearchInput"]',
            '[data-automation-id*="search"]',
            'input[placeholder*="search" i]',
            'input[placeholder*="keyword" i]',
            'input[type="search"]',
            'input[aria-label*="search" i]',
            'input[aria-label*="keyword" i]',
        ]:
            try:
                el = await page.wait_for_selector(selector, timeout=2500, state="visible")
                if el:
                    await el.fill("manager")
                    await page.keyboard.press("Enter")
                    searched = True
                    print(f"  searched via selector {selector!r}")
                    break
            except Exception:
                continue

        if not searched:
            print(f"  no search box found; using auto-fired XHRs only")

        # Give the XHRs (whether from explicit search or auto-fire) time to complete
        await page.wait_for_timeout(6000)

    except Exception as e:
        print(f"  navigation/search error: {type(e).__name__}: {str(e)[:120]}")

    # Pick best captured (highest job count, status 200)
    valid = [c for c in captured_per_url.values() if c.response_status == 200 and (c.job_count or 0) > 0]
    if not valid:
        valid = [c for c in captured_per_url.values() if c.response_status == 200]

    await context.close()

    if not valid:
        print(f"  [X] no usable search XHR captured")
        # Debug: show what we DID see
        print(f"  [debug] saw {len(all_xhrs_seen)} XHRs total. Last 10 XHRs containing 'job' or 'workday':")
        relevant = [x for x in all_xhrs_seen if "workday" in x.lower() or "job" in x.lower() or "search" in x.lower()][-10:]
        for x in relevant:
            print(f"    {x}")
        return None

    best = max(valid, key=lambda c: c.job_count or 0)
    print(f"  [OK] captured: {best.method} {best.url}  ({best.job_count} jobs)")
    return best


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Discover one tenant by display name (substring match)")
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--show", action="store_true", help="Show browser (--no-headless)")
    args = parser.parse_args()

    targets = HIGH_VALUE_TARGETS
    if args.only:
        targets = [t for t in targets if args.only.lower() in t[0].lower()]
        if not targets:
            print(f"No targets matched --only={args.only!r}")
            return

    print(f"Discovering {len(targets)} Workday tenants via Playwright...\n")
    print(f"Output dir: {TENANTS_DIR}\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.show)
        results: dict[str, Optional[CapturedXhr]] = {}

        # Run sequentially to avoid overwhelming any single CDN
        for name, url in targets:
            try:
                captured = await discover_one(name, url, browser)
                results[name] = captured
            except Exception as e:
                print(f"  [X] ERROR: {type(e).__name__}: {e}")
                results[name] = None

        await browser.close()

    # Write configs for successful discoveries
    print("\n" + "=" * 70)
    print("DISCOVERY SUMMARY")
    print("=" * 70)
    successes = 0
    workday_count = 0
    proprietary_count = 0
    for name, cap in results.items():
        if cap is None:
            print(f"  [X]  {name}: no usable XHR captured")
            continue
        # Parse the URL to extract base + endpoint
        parsed = urlparse(cap.url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        endpoint_path = parsed.path
        is_workday = "myworkdayjobs.com" in cap.url.lower() or "/wday/cxs/" in cap.url.lower()
        ats_kind = "workday" if is_workday else "proprietary"
        slug_match = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", base_url)
        slug = slug_match.group(1) if slug_match else parsed.netloc.split(".")[0]
        # Extract board from path: /wday/cxs/{slug}/{board}/jobs
        board_match = re.search(r"/wday/cxs/[^/]+/([^/]+)/jobs", endpoint_path)
        board = board_match.group(1) if board_match else "unknown"

        # Parse the captured POST body (if any) so we can replay it
        body_template: Optional[dict] = None
        if cap.post_body:
            try:
                parsed_body = json.loads(cap.post_body)
                # Replace the searched keyword with a placeholder
                if isinstance(parsed_body, dict) and parsed_body.get("searchText") == "manager":
                    parsed_body["searchText"] = "{KEYWORD}"
                body_template = parsed_body
            except Exception:
                # If body isn't JSON, store it raw
                body_template = {"_raw_body": cap.post_body}

        # Filter headers to ones likely required for replay
        replay_headers = {}
        for k, v in cap.headers.items():
            kl = k.lower()
            if kl in ("origin", "referer", "x-csrf-token", "accept", "accept-language",
                     "content-type", "x-requested-with"):
                replay_headers[k] = v

        config = {
            "company": name,
            "ats_kind": ats_kind,
            "base_url": base_url,
            "tenant_slug": slug,
            "board": board,
            "full_endpoint": cap.url,
            "method": cap.method,
            "headers": replay_headers,
            "body_template": body_template,
            "response_jobs_path": "jobPostings",
            "discovered_date": "2026-05-03",
            "verified_job_count": cap.job_count,
            "notes": ("Workday-compatible" if is_workday else
                      "PROPRIETARY ATS - needs custom scraper, not Workday compatible"),
        }
        slug_filename = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + ".json"
        out_path = TENANTS_DIR / slug_filename
        out_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        kind_label = "Workday" if is_workday else "PROPRIETARY"
        print(f"  [OK]  {name}: {cap.job_count} jobs ({kind_label}) -> {out_path.name}")
        successes += 1
        if is_workday: workday_count += 1
        else: proprietary_count += 1

    print(f"\n  Workday-compatible: {workday_count}")
    print(f"  Proprietary (need custom scrapers): {proprietary_count}")

    print(f"\nDiscovered {successes}/{len(targets)} tenants. Configs in: {TENANTS_DIR}")


if __name__ == "__main__":
    asyncio.run(main())

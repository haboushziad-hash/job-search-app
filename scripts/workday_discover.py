"""Workday tenant discovery via Playwright.

For each target company, launches headless Chromium, navigates to the
careers page, types a search keyword, and captures the actual XHR call
that fires. The captured config (URL, headers, body) is saved per-tenant
so the existing WorkdayScraper can use the verified endpoint shape.

The 75 Workday tenants we couldn't reach with default body templates
(Deloitte, PepsiCo, JPMorgan, Microsoft, J&J, etc.) ALL use Workday's
CXS API but with per-tenant request shapes. This script discovers those
shapes mechanically rather than guessing.

Usage:
    backend/venv/Scripts/python.exe scripts/workday_discover.py

Output:
    backend/scraper/workday_tenants/<tenant>.json     per-tenant config
    scripts/workday_discovery_report.md               summary of results
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright, Page, Request


CONFIG_DIR = Path(__file__).resolve().parent.parent / "backend" / "scraper" / "workday_tenants"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = Path(__file__).resolve().parent / "workday_discovery_report.md"


# ============================================================================
# TARGET TENANTS — top 20 priority companies missing from current Workday list
# ============================================================================
# Each entry: dict with display_name, careers_url (where the search lives),
# search_input_hints (placeholder/aria-label/id substrings), and an optional
# pre_actions list (e.g. cookie-banner dismissal).
#
# Priority order matches the audit: maximum coverage gain per minute of work.
TARGETS: list[dict] = [
    # ===== Big 4 / consulting (Ziad, Zach) =====
    {
        "display_name": "Deloitte",
        "careers_url": "https://apply.deloitte.com/careers",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword", "what"],
    },
    {
        "display_name": "EY",
        "careers_url": "https://careers.ey.com/ey/search",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword", "what"],
    },
    {
        "display_name": "KPMG",
        "careers_url": "https://kpmg.wd5.myworkdayjobs.com/KPMGUS_Careers",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    # ===== CPG / consumer (Zach gap) =====
    {
        "display_name": "PepsiCo",
        "careers_url": "https://pepsicocareers.com/main/jobs",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword", "what"],
    },
    {
        "display_name": "Coca-Cola",
        "careers_url": "https://careers.coca-colacompany.com/jobs",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    {
        "display_name": "Kraft Heinz",
        "careers_url": "https://kraftheinz.wd1.myworkdayjobs.com/Kraft_Heinz_Careers",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "Mondelez",
        "careers_url": "https://mondelez.wd1.myworkdayjobs.com/External",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "P&G",
        "careers_url": "https://www.pgcareers.com/global/en/search-results",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    # ===== Banking / finance =====
    {
        "display_name": "JPMorgan",
        "careers_url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    {
        "display_name": "Goldman Sachs",
        "careers_url": "https://higher.gs.com/results",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    {
        "display_name": "Wells Fargo",
        "careers_url": "https://wellsfargo.wd1.myworkdayjobs.com/WellsFargoJobs",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    # ===== Healthcare / pharma =====
    {
        "display_name": "UnitedHealth",
        "careers_url": "https://uhg.wd5.myworkdayjobs.com/External",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "Johnson & Johnson",
        "careers_url": "https://jnjc.wd5.myworkdayjobs.com/jnjcareers",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "Merck",
        "careers_url": "https://msd.wd5.myworkdayjobs.com/SearchJobs",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    # ===== Defense / federal =====
    {
        "display_name": "Lockheed Martin",
        "careers_url": "https://lockheedmartin.wd1.myworkdayjobs.com/External",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "Northrop Grumman",
        "careers_url": "https://ngc.wd1.myworkdayjobs.com/NGCareers",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "SAIC",
        "careers_url": "https://saic.wd5.myworkdayjobs.com/External",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    # ===== Tech (Microsoft Copilot for Ziad) =====
    {
        "display_name": "Microsoft",
        "careers_url": "https://jobs.careers.microsoft.com/global/en/search",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    # ===== Retail / grocery =====
    {
        "display_name": "Kroger",
        "careers_url": "https://jobs.kroger.com/search-results",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
    # ===== Insurance / industrial =====
    {
        "display_name": "Liberty Mutual",
        "careers_url": "https://lmi.wd1.myworkdayjobs.com/LMI",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "Honeywell",
        "careers_url": "https://honeywell.wd1.myworkdayjobs.com/Honeywell",
        "search_keyword": "manager",
        "search_input_hints": ["search"],
    },
    {
        "display_name": "ExxonMobil",
        "careers_url": "https://jobs.exxonmobil.com/jobs/search",
        "search_keyword": "manager",
        "search_input_hints": ["search", "keyword"],
    },
]


async def find_search_input(page: Page, hints: list[str]):
    """Try multiple selectors to find the search input. Returns first match."""
    selectors = [
        # Common ATS patterns
        "input[type='search']",
        "input[placeholder*='earch' i]",
        "input[aria-label*='earch' i]",
        "input[placeholder*='eyword' i]",
        "input[aria-label*='eyword' i]",
        "input[name*='earch' i]",
        "input[name*='eyword' i]",
        "input[id*='earch' i]",
        "input[data-automation-id*='search' i]",
        "input[data-automation-id*='keyword' i]",
        # Workday-specific
        "input[data-automation-id='keywordSearchInput']",
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                return el, sel
        except Exception:
            continue
    return None, None


async def discover_one(target: dict, browser) -> dict:
    """Discover one tenant. Returns result dict (success or skip with reason)."""
    name = target["display_name"]
    url = target["careers_url"]
    keyword = target["search_keyword"]
    started = time.time()

    result = {
        "display_name": name,
        "careers_url": url,
        "status": "FAILED",
        "reason": "",
        "elapsed": 0.0,
        "captured": None,
    }

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()

    captured_xhrs: list[dict] = []

    def on_request(req: Request):
        try:
            u = req.url
            # We're looking for the actual jobs query — typically POST to a
            # /wday/cxs/.../jobs path or a similar JSON-search endpoint
            if req.method == "POST" and any(s in u for s in ("/wday/cxs/", "/jobs", "/search", "career", "fa.oraclecloud", "/api/")):
                body = req.post_data
                captured_xhrs.append({
                    "url": u,
                    "method": req.method,
                    "headers": dict(req.headers),
                    "body": body,
                })
        except Exception:
            pass

    page.on("request", on_request)

    try:
        # Some sites need extra time for SPAs to mount
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        # Light scroll/wait for SPA mount
        await page.wait_for_timeout(2500)

        # Try to dismiss any visible cookie banner
        for cookie_btn in [
            "button:has-text('Accept')",
            "button:has-text('I Accept')",
            "button:has-text('Accept All')",
            "button:has-text('Agree')",
            "button#onetrust-accept-btn-handler",
        ]:
            try:
                btn = await page.query_selector(cookie_btn)
                if btn:
                    await btn.click(timeout=1500)
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue

        # Find the search input
        input_el, used_sel = await find_search_input(page, target.get("search_input_hints") or [])
        if not input_el:
            result["status"] = "SKIP"
            result["reason"] = "no search input found"
            return result

        # Type and submit
        await input_el.fill(keyword)
        await input_el.press("Enter")
        await page.wait_for_timeout(4000)  # let XHRs fire

        # Filter captured XHRs to the most likely candidate
        # Prefer: has 'cxs' OR has a body containing the keyword
        candidates = [
            x for x in captured_xhrs
            if x.get("body") and (
                "cxs" in x["url"]
                or keyword in (x["body"] or "")
                or "searchText" in (x["body"] or "")
                or "keyword" in (x["body"] or "").lower()
            )
        ]
        if not candidates:
            # fallback: any POST with non-empty body
            candidates = [x for x in captured_xhrs if x.get("body")]

        if not candidates:
            result["status"] = "SKIP"
            result["reason"] = f"no XHR captured (saw {len(captured_xhrs)} POSTs total)"
            return result

        # Take the best candidate (first cxs match, else first POST)
        cxs = next((x for x in candidates if "cxs" in x["url"]), None) or candidates[0]
        result["status"] = "OK"
        result["captured"] = {
            "url": cxs["url"],
            "method": cxs["method"],
            "request_body": cxs["body"],
            # Only keep the headers we know matter for replays
            "headers": {
                k: v for k, v in cxs["headers"].items()
                if k.lower() in ("accept", "content-type", "user-agent", "x-csrf-token", "x-requested-with", "authorization")
            },
            "input_selector_used": used_sel,
        }
    except Exception as e:
        result["status"] = "FAILED"
        result["reason"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        result["elapsed"] = round(time.time() - started, 1)
        await context.close()
    return result


async def main():
    started_at = time.time()
    results: list[dict] = []

    print(f"=== Workday Tenant Discovery — {len(TARGETS)} tenants ===\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for tgt in TARGETS:
                print(f"[{tgt['display_name']:24s}] discovering...", flush=True)
                r = await discover_one(tgt, browser)
                if r["status"] == "OK":
                    cap = r["captured"]
                    is_workday_cxs = "/wday/cxs/" in (cap["url"] or "")
                    flag = "WORKDAY_CXS" if is_workday_cxs else "NON_WORKDAY"
                    print(f"  OK ({r['elapsed']}s)  [{flag}]  {cap['url'][:80]}")
                else:
                    print(f"  {r['status']} ({r['elapsed']}s)  {r['reason'][:80]}")
                results.append(r)

                # Save individual config file for each successful discovery
                if r["status"] == "OK":
                    safe_name = tgt["display_name"].lower().replace(" ", "_").replace("&", "and").replace(".", "")
                    out_path = CONFIG_DIR / f"{safe_name}.json"
                    out_path.write_text(json.dumps(r, indent=2), encoding="utf-8")
        finally:
            await browser.close()

    # Write summary report
    elapsed_total = round(time.time() - started_at, 1)
    ok = [r for r in results if r["status"] == "OK"]
    workday_cxs = [r for r in ok if "/wday/cxs/" in (r["captured"]["url"] or "")]
    non_workday = [r for r in ok if r not in workday_cxs]
    skip = [r for r in results if r["status"] == "SKIP"]
    fail = [r for r in results if r["status"] == "FAILED"]

    print(f"\n{'=' * 60}")
    print(f"Summary ({elapsed_total}s):")
    print(f"  OK Workday CXS:   {len(workday_cxs):>3} / {len(TARGETS)}")
    print(f"  OK non-Workday:   {len(non_workday):>3} / {len(TARGETS)}")
    print(f"  SKIP:             {len(skip):>3} / {len(TARGETS)}")
    print(f"  FAIL:             {len(fail):>3} / {len(TARGETS)}")
    print(f"{'=' * 60}")

    # Markdown report
    lines = [
        "# Workday Tenant Discovery Report",
        "",
        f"**Run date:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Total elapsed:** {elapsed_total}s",
        f"**Targets:** {len(TARGETS)}",
        "",
        f"## Results",
        "",
        f"| status | tenant | url | reason / endpoint |",
        f"|---|---|---|---|",
    ]
    for r in results:
        endpoint = ""
        if r["status"] == "OK":
            endpoint = (r["captured"]["url"] or "")[:80]
        else:
            endpoint = r.get("reason", "")[:80]
        lines.append(f"| {r['status']} | {r['display_name']} | {r['careers_url'][:50]} | `{endpoint}` |")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {REPORT_PATH}")
    print(f"Per-tenant configs: {CONFIG_DIR}")


if __name__ == "__main__":
    asyncio.run(main())

"""Scraper orchestrator — runs all configured board scrapers in parallel,
merges results, dedupes across boards, and returns a single Role list.

Cross-board dedup is critical: many companies post to multiple boards
(Greenhouse + their own careers page + LinkedIn). Without cross-dedup, the
cascade scores the same role 3 times.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.client import ScraperClient
from backend.scraper.greenhouse import GreenhouseScraper
from backend.scraper.lever import LeverScraper
from backend.scraper.ashby import AshbyScraper
from backend.scraper.builtin import BuiltInScraper
from backend.scraper.indeed import IndeedScraper
from backend.scraper.wellfound import WellfoundScraper
from backend.scraper.workday import WorkdayScraper
from backend.scraper.icims_playwright import ICIMSPlaywrightScraper
from backend.scraper.usajobs import USAJobsScraper
from backend.scraper.themuse import TheMuseScraper
from backend.scraper.remotive import RemotiveScraper
from backend.scraper.adzuna import AdzunaScraper
from backend.scraper.climatebase import ClimatebaseScraper
from backend.scraper.smartrecruiters import SmartRecruitersScraper
from backend.scraper.arbeitnow import ArbeitnowScraper
from backend.scraper.hn_hiring import HNHiringScraper
from backend.scraper.findwork import FindworkScraper
from backend.scraper.jsearch import JSearchScraper
from backend.scraper import _keyword_match as _kw_match


# Map source_name → scraper class.
#
# ACTIVE scrapers — proven working against live APIs:
#   - Greenhouse: 224 companies, 7700+ roles per scan
#   - Lever:      99 companies, ATS API
#   - Ashby:      76 companies, Public Job Board API
#   - Workday:    41 tenants (Merck added 2026-05-03), CXS API + JD endpoint
#   - iCIMS:      5 verified tenants via Playwright (Liberty Mutual, Six Flags,
#                 Cedar Fair, Snap-on, Chick-fil-A) — covers insurance,
#                 hospitality, industrial-sales sectors that Workday/Greenhouse
#                 under-serve. Heavier than HTTP scrapers (~10s/tenant) but
#                 reaches SPA-rendered iCIMS job boards no other scraper can.
#
# DEFERRED scrapers — registered but not active by default. Each requires
# additional infrastructure to make production-ready:
#   - BuiltIn:   API endpoint changed (404). Need to find new endpoint structure.
#   - Indeed:    Anti-bot 403. Requires Playwright/Stealth or paid proxy.
#   - Wellfound: Anti-bot 403. Requires Playwright/Stealth.
# These will be re-enabled once the proxy server can route through Playwright.
SCRAPER_REGISTRY: dict[str, type[BaseScraper]] = {
    "Greenhouse": GreenhouseScraper,
    "Lever":      LeverScraper,
    "Ashby":      AshbyScraper,
    "Workday":    WorkdayScraper,
    "iCIMS":      ICIMSPlaywrightScraper,
    # Phase C broad aggregators — single API call returns roles across
    # many companies, breaking the company-by-company ceiling.
    "TheMuse":    TheMuseScraper,    # ~10K curated jobs, broad employer mix (free, no key)
    "Remotive":   RemotiveScraper,   # ~5-10K remote jobs across all industries (free, no key)
    "USAJOBS":    USAJobsScraper,    # All federal jobs (free with API key)
    "Adzuna":     AdzunaScraper,     # Aggregator — pulls from ZipRecruiter/Monster/etc (free tier 1K calls/mo)
    "BuiltIn":    BuiltInScraper,    # Tech-adjacent + consumer roles (rebuilt 2026-05-03 via SSR HTML)
    "Climatebase": ClimatebaseScraper,    # Climate / sustainability / clean energy (no key)
    "SmartRecruiters": SmartRecruitersScraper,  # Mid-market employers (Visa, ASOS, LVMH)
    "Arbeitnow":  ArbeitnowScraper,  # EU-leaning + remote (no key)
    "HN-WhoIsHiring": HNHiringScraper,    # Monthly HN thread, startup-direct (no key)
    "Findwork":   FindworkScraper,   # Aggregator (free tier needs FINDWORK_API_KEY)
    # v0.1.4: JSearch (RapidAPI) — legitimate aggregator covering LinkedIn,
    # Indeed, Glassdoor, ZipRecruiter via paid partnership. Free tier 200
    # requests/month, Pro $25/mo for 10K. Routes through Worker proxy in
    # production so testers don't need their own RapidAPI key.
    "JSearch":    JSearchScraper,
}

# Deferred — anti-bot blocked, would require Playwright + ongoing maintenance
DEFERRED_SCRAPERS: dict[str, type[BaseScraper]] = {
    "Indeed":     IndeedScraper,     # 403 anti-bot; TOS-restricted scraping
    "Wellfound":  WellfoundScraper,  # 403 anti-bot; needs Playwright to attempt
}


async def scrape_all(
    *,
    keywords: list[str],
    sources: Optional[list[str]] = None,
    posted_within_days: Optional[int] = 30,
    log: bool = True,
    health_out: Optional[dict] = None,
    extra_jsearch_keywords: Optional[list[str]] = None,
) -> list[Role]:
    """Run all configured scrapers in parallel, return a deduped Role list.

    sources: list of source_name keys from SCRAPER_REGISTRY. None = all.
    health_out: optional dict — gets populated with per-source health stats:
        { source: { "roles": int, "elapsed_s": float, "errored": bool, "error": str|None } }
        Used by the runner to record per-source health into the audit JSON
        and trigger alerts when a scraper returns 0 unexpectedly.
    extra_jsearch_keywords (v0.1.4): additional keywords sent ONLY to JSearch.
        JSearch is the highest-yielding source by qualifying-rate (30.6% vs
        Greenhouse 1.5%) and runs on the paid Pro tier with substantial
        budget headroom, so we feed it an expanded keyword set (search_terms
        + Tier 1/2 specific titles) for maximum recall. Other scrapers stick
        with the base keywords list to keep their per-source budgets in line.
    """
    import time
    selected = sources or list(SCRAPER_REGISTRY.keys())
    if log:
        print(f"[scraper] starting scrape across {len(selected)} sources: {selected}")
        print(f"[scraper] keywords: {keywords}")
        if extra_jsearch_keywords:
            print(f"[scraper] JSearch-only expanded keywords: {len(extra_jsearch_keywords)} terms")
        print(f"[scraper] posted within: {posted_within_days} days")

    timings: dict[str, float] = {}

    async def timed_run(source_name: str, scraper: BaseScraper):
        t0 = time.time()
        try:
            # JSearch gets the expanded keyword list when supplied — paid Pro
            # tier has plenty of budget for broader fan-out. All other sources
            # stick with the base list (their per-source budgets are tighter).
            kw_for_this_source = (
                extra_jsearch_keywords
                if (source_name == "JSearch" and extra_jsearch_keywords)
                else keywords
            )
            roles = await _run_scraper(scraper, kw_for_this_source, posted_within_days, log)
            timings[source_name] = time.time() - t0
            return roles
        except Exception as e:
            timings[source_name] = time.time() - t0
            raise

    # Keep a reference to each scraper instance so we can read its
    # quota_exhausted state after the run finishes (v0.1.4).
    scrapers_by_name: dict[str, BaseScraper] = {}
    async with ScraperClient() as http:
        tasks = []
        valid_sources = []
        for source_name in selected:
            cls = SCRAPER_REGISTRY.get(source_name)
            if not cls:
                continue
            scraper = cls(client=http)
            scrapers_by_name[source_name] = scraper
            valid_sources.append(source_name)
            tasks.append(timed_run(source_name, scraper))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_roles: list[Role] = []
    by_source: dict[str, int] = {}
    health: dict[str, dict] = {}
    for source, result in zip(valid_sources, results):
        elapsed = timings.get(source, 0.0)
        scraper_inst = scrapers_by_name.get(source)
        # v0.1.4: per-source quota state — surfaces "Adzuna hit cap"
        # vs "Adzuna found 0 matches" so testers know which it is.
        quota_exhausted = bool(getattr(scraper_inst, "quota_exhausted", False))
        quota_reason = str(getattr(scraper_inst, "quota_exhausted_reason", "") or "")
        if isinstance(result, Exception):
            if log:
                print(f"[scraper] {source} FAILED: {type(result).__name__}: {result}")
            by_source[source] = 0
            health[source] = {
                "roles": 0,
                "elapsed_s": round(elapsed, 1),
                "errored": True,
                "error": f"{type(result).__name__}: {str(result)[:200]}",
                "quota_exhausted": quota_exhausted,
                "quota_exhausted_reason": quota_reason if quota_exhausted else "",
            }
            continue
        by_source[source] = len(result)
        # v0.3.4: surface per-keyword raw counts when the scraper tracks
        # them. Currently JSearch is the only scraper with this, but the
        # field is on BaseScraper so other scrapers can opt-in trivially.
        per_kw = dict(getattr(scraper_inst, "per_keyword_raw_counts", {}) or {})
        health[source] = {
            "roles": len(result),
            "elapsed_s": round(elapsed, 1),
            "errored": False,
            "error": None,
            "quota_exhausted": quota_exhausted,
            "quota_exhausted_reason": quota_reason if quota_exhausted else "",
            "per_keyword_raw_counts": per_kw,
        }
        if quota_exhausted and log:
            print(f"[scraper] {source} quota exhausted: {quota_reason}")
        all_roles.extend(result)

    # Centralized post-fetch sanity filter.
    # Server-side scrapers (Workday/iCIMS/TheMuse/BuiltIn/Remotive/Adzuna/
    # USAJOBS/Climatebase/Findwork) trust their upstream search to do the
    # matching. Empirically those upstreams return very loose results —
    # Workday returned 8,317 roles for AI/strategy keywords with a Walmart
    # store-manager fraction, TheMuse fetches whole categories. Apply the
    # token-overlap matcher centrally so every scraper's output is sanity-
    # checked against the keyword list. This is a no-op for scrapers that
    # already filter at scrape time (Greenhouse/Lever/Ashby/Arbeitnow/HN/
    # SmartRecruiters) — same matcher = same result.
    keywords_lower = [k.lower() for k in keywords]
    filtered_roles: list[Role] = []
    by_source_filtered: dict[str, int] = {}
    for r in all_roles:
        if _kw_match.matches_any_keyword(
            r.job_title or "",
            r.job_description_full or "",
            keywords_lower,
        ):
            filtered_roles.append(r)
            src = getattr(r, "primary_source", None) or getattr(r, "source", None) or "unknown"
            by_source_filtered[src] = by_source_filtered.get(src, 0) + 1
    if log:
        dropped_total = len(all_roles) - len(filtered_roles)
        if dropped_total > 0:
            print(f"[scraper] post-fetch sanity filter: {len(all_roles)} -> {len(filtered_roles)} "
                  f"(dropped {dropped_total} non-matching)")
            # Per-source delta — useful for diagnosing which sources had
            # loose upstream search.
            for src, raw_n in by_source.items():
                kept = by_source_filtered.get(src, 0)
                if raw_n > 0 and kept < raw_n:
                    print(f"  {src}: {raw_n} -> {kept}")

    deduped = _cross_board_dedupe(filtered_roles)
    capped = _cap_per_company(deduped, max_per_company=50)
    if log:
        print(f"[scraper] per-source: {by_source}")
        print(f"[scraper] total before dedup: {len(filtered_roles)} (after sanity filter), "
              f"after dedup: {len(deduped)}, "
              f"after per-company cap (max 50): {len(capped)}")

    # Populate caller's health dict so the runner can persist this into
    # RunSummary.per_source_counts for audit + monitoring.
    if health_out is not None:
        health_out.update(health)
    return capped


def _cap_per_company(roles: list[Role], *, max_per_company: int = 50) -> list[Role]:
    """Limit how many roles from any single company can advance into scoring.

    Workday tenants like Accenture return thousands of fuzzy matches that
    drown out roles from every other company. Without this cap, Accenture
    has been 94% of qualifying results in production runs. The cap operates
    on the deduped list so cross-board duplicates don't burn quota."""
    seen_count: dict[str, int] = {}
    out: list[Role] = []
    for r in roles:
        key = (r.company or "").strip().lower()
        if not key:
            out.append(r)
            continue
        n = seen_count.get(key, 0)
        if n >= max_per_company:
            continue
        seen_count[key] = n + 1
        out.append(r)
    return out


async def _run_scraper(
    scraper: BaseScraper,
    keywords: list[str],
    posted_within_days: Optional[int],
    log: bool,
) -> list[Role]:
    try:
        roles = await scraper.search(
            keywords=keywords,
            posted_within_days=posted_within_days,
        )
        if log:
            print(f"[scraper] {scraper.source_name}: found {len(roles)} matching roles")
        return roles
    except Exception as e:
        if log:
            print(f"[scraper] {scraper.source_name} ERROR: {type(e).__name__}: {e}")
        raise


def _cross_board_dedupe(roles: list[Role]) -> list[Role]:
    """Dedupe across boards. Same role can appear from Greenhouse + Lever
    when a company uses multiple ATS systems."""
    out: list[Role] = []
    seen_url: set[str] = set()
    seen_title_company: set[tuple[str, str]] = set()

    for r in roles:
        if r.job_url and r.job_url in seen_url:
            continue
        key = (
            (r.job_title or "").strip().lower(),
            (r.company or "").strip().lower(),
        )
        if key[0] and key in seen_title_company:
            continue
        if r.job_url:
            seen_url.add(r.job_url)
        seen_title_company.add(key)
        out.append(r)
    return out

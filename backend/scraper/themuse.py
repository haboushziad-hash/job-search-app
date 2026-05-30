"""The Muse scraper — broad job aggregator with curated company mix.

The Muse aggregates ~10K-30K active job postings across thousands of
companies — heavily weighted toward CPG, retail, hospitality, media,
healthcare, finance, and consumer brands. Much broader employer mix
than Greenhouse/Lever/Ashby (which skew tech).

API: https://www.themuse.com/developers/api/v2
  - Free, public, no key required for basic search
  - Endpoint: GET https://www.themuse.com/api/public/jobs
  - Rate limit: ~25 calls/hour for unkeyed; higher with API key

Returns broad mix that covers the gap our other scrapers leave:
  - Big consumer brands (Coca-Cola, Disney, Marriott, etc.)
  - Healthcare systems
  - Universities
  - Media companies (NBC, NYT, Conde Nast)
  - Government / nonprofit
  - F500 corporates
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper._exception_helpers import handle_scraper_exception


THEMUSE_API_BASE = "https://www.themuse.com/api/public/jobs"


class TheMuseScraper(BaseScraper):
    """Scrapes the Muse public-API job feed."""

    source_name = "TheMuse"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        api_key = config.THEMUSE_API_KEY  # optional — ups rate limit

        sem = asyncio.Semaphore(4)  # respect their rate limit

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword, api_key),
                        timeout=30.0,
                    )
                except Exception as _e:
                    # Wave-2B Phase 2 (FIX 15): surface via shared helper.
                    # Previously this bare except hid every Muse failure
                    # (rate limits, JSON parse, timeouts) and we couldn't
                    # tell why TheMuse occasionally returned 0 roles.
                    handle_scraper_exception(
                        self, "TheMuse", _e, context=f"keyword='{kw}'"
                    )
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen: set[str] = set()
        out: list[Role] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            for role in r or []:
                if role.job_url and role.job_url in seen:
                    continue
                if role.job_url:
                    seen.add(role.job_url)
                out.append(role)
        return out

    async def _search_keyword(
        self, keyword: str, limit: int, api_key: str,
    ) -> list[Role]:
        # The Muse public API doesn't support arbitrary keyword search —
        # only `category`, `level`, `location`, `company` filters. So we
        # map the keyword to a Muse category for coarse pre-filtering, fetch
        # broadly, and let the downstream embedding pre-filter handle
        # keyword-relevance precisely. Net effect: BREADTH (40 jobs from
        # 30+ companies per keyword), not narrow keyword precision.
        category = _keyword_to_category(keyword)
        if category:
            return await self._fetch_category(category, limit, api_key)

        # No mapped category — fan out across a curated subset of the
        # broadest Muse categories rather than hitting the same global
        # recency feed N times. Cap at len(_MUSE_FANOUT_CATEGORIES) to
        # respect the unkeyed ~25 calls/hr free-tier rate limit.
        per_cat_limit = max(10, limit // len(_MUSE_FANOUT_CATEGORIES))
        out: list[Role] = []
        seen: set[str] = set()
        for cat in _MUSE_FANOUT_CATEGORIES:
            roles = await self._fetch_category(cat, per_cat_limit, api_key)
            for role in roles:
                if role.job_url and role.job_url in seen:
                    continue
                if role.job_url:
                    seen.add(role.job_url)
                out.append(role)
                if len(out) >= limit:
                    return out
        return out

    async def _fetch_category(
        self, category: Optional[str], limit: int, api_key: str,
    ) -> list[Role]:
        params: dict = {
            "page": 0,
            "descending": "true",
        }
        if category:
            params["category"] = category
        if api_key:
            params["api_key"] = api_key

        # Paginate up to 8 pages × 20 jobs = 160 jobs per keyword. Stops
        # early when a page returns no results (out of data).
        out: list[Role] = []
        max_pages = min((limit + 19) // 20, 8)
        for page in range(max_pages):
            params["page"] = page
            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    THEMUSE_API_BASE, params=params,
                    headers={"Accept": "application/json"},
                )
            except Exception as _e:
                # Wave-2B Phase 2 (FIX 15): surface via shared helper.
                handle_scraper_exception(
                    self, "TheMuse", _e,
                    context=f"category='{category}' page={page}",
                )
                break
            if resp.status_code != 200:
                break
            try:
                data = resp.json()
            except Exception:
                break
            results = data.get("results") or []
            if not results:
                break

            for j in results:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
                if len(out) >= limit:
                    return out

            if len(results) < 20:
                break
        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("name") or "").strip()
        if not title:
            return None

        # Refs holds the public landing page URL
        refs = j.get("refs") or {}
        url = refs.get("landing_page") or ""

        # Company
        company_obj = j.get("company") or {}
        company = self._normalize_company(
            company_obj.get("name") or company_obj.get("short_name") or ""
        )
        if not company:
            return None

        # Locations — Muse uses [{name: 'New York, NY'}]
        locs = j.get("locations") or []
        loc_str = (locs[0].get("name") if locs else "") or ""
        location_type, _ = self._classify_location(loc_str)
        if "Flexible / Remote" in loc_str:
            location_type = "Remote"

        # Levels — Muse has senior_level/mid_level/etc.
        # JD content
        contents = j.get("contents") or ""
        jd_text = _strip_html(contents) if contents else ""

        posted = j.get("publication_date") or ""

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url or None,
            location=loc_str or None,
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""


# ============================================================================
# Keyword → Muse category mapping
# ============================================================================
# The Muse uses ~30 fixed categories. Map common keyword tokens to the
# closest Muse category for coarse pre-filtering. Returns None to fetch
# from all categories (broadest coverage).

# Curated subset (8 broadest) used for fan-out when a keyword doesn't map
# to any specific category. Picked to maximize cross-industry coverage
# without exceeding the unkeyed ~25 calls/hr free-tier rate limit.
# Wave-2B Phase 2 (FIX 1): 5 of 8 category names were typo'd against
# the actual Muse vocabulary and returned 0 jobs each. Fixed names verified
# live against https://www.themuse.com/api/public/jobs?category=X
# Expanded from 8 → 14 to widen cross-industry coverage. With Semaphore(4)
# this still fits under the unkeyed ~25 calls/hr free-tier rate limit when
# multiple keywords fan out (the global rate budget is shared via the
# semaphore-bounded HTTP layer).
_MUSE_FANOUT_CATEGORIES = (
    "Software Engineering",            # was "Engineering" (dead)
    "Data and Analytics",              # was "Data Science" (dead)
    "Advertising and Marketing",       # was "Marketing" (dead)
    "Sales",
    "Healthcare and Medicine",         # was "Healthcare" (dead)
    "Human Resources and Recruitment", # was "HR" (dead)
    "Business Operations",             # was "Operations" (dead)
    "Customer Service",
    "Legal Services",
    "Writing and Editing",
    "Creative and Design",
    "Education",
    "Project and Program Management",
    "Account Management",
)

# Wave-2B Phase 2 (FIX 4): same vocabulary corrections as FIX 1. Keys
# mapping to dead category names returned 0 jobs and silently degraded
# every role-category fetch. Removed dead targets ("Consulting",
# "Government", "Editorial", "Product", "Legal", "Healthcare", "Project
# Management") that don't exist as Muse categories; remapped to live
# equivalents OR removed so they fall through to fan-out. Verified
# remapping live 2026-05-30.
_MUSE_CATEGORIES = {
    "engineer": "Software Engineering",
    "developer": "Software Engineering",
    "software": "Software Engineering",
    "data scientist": "Data and Analytics",
    "data engineer": "Data and Analytics",
    "designer": "Creative and Design",
    "ux": "Creative and Design",
    "marketing": "Advertising and Marketing",
    "growth": "Advertising and Marketing",
    "sales": "Sales",
    "account executive": "Sales",
    "account manager": "Account Management",
    "operations": "Business Operations",
    "ops": "Business Operations",
    "project manager": "Project and Program Management",
    "program manager": "Project and Program Management",
    "analyst": "Data and Analytics",
    "finance": "Accounting and Finance",
    "accountant": "Accounting and Finance",
    "tax": "Accounting and Finance",
    "audit": "Accounting and Finance",
    "hr": "Human Resources and Recruitment",
    "recruiter": "Human Resources and Recruitment",
    "people": "Human Resources and Recruitment",
    "lawyer": "Legal Services",
    "attorney": "Legal Services",
    "counsel": "Legal Services",
    "nurse": "Healthcare and Medicine",
    "clinical": "Healthcare and Medicine",
    "physician": "Healthcare and Medicine",
    "writer": "Writing and Editing",
    "content": "Writing and Editing",
    "communications": "Writing and Editing",
    "support": "Customer Service",
    "success": "Customer Service",
    # consultant/policy/federal/product manager intentionally fall through
    # to broad fan-out — Muse has no dedicated category, and the fan-out
    # list now covers all 14 broad categories so the candidate's actual
    # role will surface from whichever category houses it.
}


def _keyword_to_category(keyword: str) -> Optional[str]:
    """Best-effort match of a keyword to a Muse category. None = broad."""
    if not keyword:
        return None
    kw = keyword.lower()
    # Exact phrase first
    for k, v in _MUSE_CATEGORIES.items():
        if k in kw:
            return v
    return None

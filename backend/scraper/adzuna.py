"""Adzuna scraper — broad job aggregator (free tier, licensed API).

Adzuna is a global job search aggregator. Their public API is licensed,
terms-compliant, and explicitly designed for third-party consumption.

Coverage: aggregates from ZipRecruiter, CareerBuilder, Monster, Reed, and
many smaller boards. ~1M+ active US listings across all industries.

API: https://developer.adzuna.com/docs/search
  Endpoint: GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
  Auth: app_id + app_key as URL params

Free tier: 1,000 calls/month. Paid plans start ~$50/mo for 10K calls.

For 5-10 testers averaging 5-15 keyword calls/run × 5 runs/week:
  ~250-750 calls/week → 1000-3000/month → may need paid tier
  But for tester pilot start (10-30 calls/run), free tier covers it.

Setup: developer.adzuna.com/signup → ADZUNA_APP_ID + ADZUNA_APP_KEY env vars.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


ADZUNA_API_BASE = "https://api.adzuna.com/v1/api/jobs"


def _adzuna_base_and_keys() -> tuple[str, Optional[str], Optional[str]]:
    """Return (base_url, app_id, app_key) honoring proxy mode (v0.1.4).

    When LLM_PROXY_URL is set (production / tester builds), route through
    the Worker's /v1/scraper/adzuna proxy. The Worker injects app_id +
    app_key from its own secrets, so we send empty strings here and the
    proxy fills them in.

    When LLM_PROXY_URL is unset (local dev), call Adzuna directly with
    keys from .env.
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        # Worker handles auth — pass empty so URL params don't double-set
        return f"{proxy}/v1/scraper/adzuna/v1/api/jobs", "", ""
    return ADZUNA_API_BASE, config.ADZUNA_APP_ID, config.ADZUNA_APP_KEY


class AdzunaScraper(BaseScraper):
    """Scrapes Adzuna's licensed job search API.

    v0.1.4: routes through Worker proxy in production, direct in dev mode."""

    source_name = "Adzuna"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        base_url, app_id, app_key = _adzuna_base_and_keys()
        # In dev mode we need keys; in proxy mode the Worker has them.
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and (not app_id or not app_key):
            return []  # silent no-op when not configured locally

        # Country: profile.target_locations could imply non-US (e.g. UK
        # candidate). Default from .env is "us" but this could be expanded
        # to read profile.acceptable_locations and pick the right country.
        # Adzuna supports: us, gb, au, ca, in, fr, de, nl, br, mx, sg, za,
        # ie, it, es, ru, nz, at, ch, pl, sg
        country = config.ADZUNA_COUNTRY  # us / gb / au / etc.

        sem = asyncio.Semaphore(3)  # respect their rate limit

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword,
                                             country, app_id or "", app_key or "",
                                             posted_within_days, base_url),
                        timeout=25.0,
                    )
                except (asyncio.TimeoutError, Exception):
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
        self, keyword: str, limit: int, country: str,
        app_id: str, app_key: str, max_age_days: Optional[int],
        base_url: str,
    ) -> list[Role]:
        # Adzuna returns 50 results per page max. Paginate until we hit `limit`
        # or run out of results. Cap pages to a sensible number to avoid
        # burning the free-tier quota (1000 calls/month).
        per_page = 50
        max_pages = min((limit + per_page - 1) // per_page, 8)  # cap 8 pages = 400 jobs/keyword

        out: list[Role] = []
        for page in range(1, max_pages + 1):
            url = f"{base_url}/{country}/search/{page}"
            params: dict[str, Any] = {
                "what": keyword,
                "results_per_page": per_page,
                "content-type": "application/json",
                "sort_by": "date",
            }
            # Only include keys when calling Adzuna directly (dev mode).
            # In proxy mode the Worker injects them server-side.
            if app_id and app_key:
                params["app_id"] = app_id
                params["app_key"] = app_key
            if max_age_days:
                params["max_days_old"] = max_age_days

            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    url, params=params,
                    headers={"Accept": "application/json"},
                )
            except Exception:
                break
            if resp.status_code != 200:
                break
            try:
                data = resp.json()
            except Exception:
                break

            items = data.get("results") or []
            if not items:
                break  # exhausted

            for j in items:
                try:
                    role = self._item_to_role(j)
                    if role:
                        out.append(role)
                    if len(out) >= limit:
                        return out
                except Exception:
                    continue

            # If page returned less than per_page, we're at the end
            if len(items) < per_page:
                break
        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("title") or "").strip()
        if not title:
            return None

        # Company
        company_obj = j.get("company") or {}
        company_name = (
            company_obj.get("display_name")
            if isinstance(company_obj, dict)
            else (company_obj or "")
        )
        company = self._normalize_company(str(company_name).strip())
        if not company:
            return None

        # URL — Adzuna gives a redirect URL that opens the source posting
        url = j.get("redirect_url") or ""
        if not url:
            return None

        # Location
        loc_obj = j.get("location") or {}
        loc_str = ""
        if isinstance(loc_obj, dict):
            loc_str = loc_obj.get("display_name") or ""
        location_type, _ = self._classify_location(loc_str)

        # Salary
        salary_min_raw = j.get("salary_min")
        salary_max_raw = j.get("salary_max")
        try:
            salary_min = int(salary_min_raw) if salary_min_raw else None
            salary_max = int(salary_max_raw) if salary_max_raw else None
        except (ValueError, TypeError):
            salary_min = salary_max = None

        salary_text = None
        if salary_min and salary_max:
            salary_text = f"${salary_min:,} - ${salary_max:,}"
        elif salary_min:
            salary_text = f"${salary_min:,}+"

        # JD
        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if desc else ""

        # Posted
        posted = j.get("created") or ""

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

"""JSearch scraper — RapidAPI aggregator for LinkedIn / Indeed / Glassdoor /
ZipRecruiter job listings.

We cannot legally scrape LinkedIn / Indeed / Glassdoor directly (their ToS
explicitly prohibits it and they actively litigate against scrapers). JSearch
is a paid aggregator that has legitimate API partnerships with those
platforms and exposes a unified search endpoint via RapidAPI.

Free tier: 200 requests/month, 1000 requests/hour rate limit.
Pro tier: $25/month, 10,000 requests/month, 5 requests/second.

Per search budget:
  - With v0.1.4 search-terms split (~10 broad phrases): ~10 requests/search
  - 200 free requests = ~20 searches/month on free tier
  - 10,000 paid requests = ~1000 searches/month on Pro

The JD comes back inline (no separate fetch step) — these roles can skip
the liveness check + JD backfill, which is a speed win on top of the
coverage win.

API docs: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


JSEARCH_API_BASE = "https://jsearch.p.rapidapi.com/search"


def _clean_jsearch_employer_name(employer_name: str, item: dict[str, Any]) -> str:
    """Normalize JSearch's employer_name field, which sometimes returns
    a company's internal categorization tag instead of the brand name.

    Known cases:
      - PwC: returns "Line of Service:Assurance" / "Line of Service:Advisory"
        / "Line of Service:Tax" / similar instead of "PwC". Detected by
        the literal "Line of Service:" prefix — PwC is the only firm
        we've seen using this naming convention.

    The audit JSON I'm hardening against showed 3 such roles in a single
    Ziad run; sample upstream JSON suggests this is a deterministic
    JSearch-side parsing of PwC's career page metadata, not noise.

    Add new patterns here as we discover them in tester audits.
    """
    name = (employer_name or "").strip()
    if not name:
        return name

    # PwC's "Line of Service:Assurance" pattern.
    if name.startswith("Line of Service:"):
        return "PwC"

    # Future patterns for other companies go here, e.g.:
    #   if name.startswith("Practice Area:"): return "<Company>"
    return name


def _jsearch_base_and_key() -> tuple[str, Optional[str]]:
    """Return (base_url, rapidapi_key) honoring proxy mode (v0.1.4).

    Proxy mode: route through Worker, key empty (Worker injects).
    Local mode: direct JSearch URL with key from .env.
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        return f"{proxy}/v1/scraper/jsearch/search", ""
    return JSEARCH_API_BASE, getattr(config, "JSEARCH_RAPIDAPI_KEY", "") or ""


class JSearchScraper(BaseScraper):
    """JSearch (RapidAPI aggregator). Returns LinkedIn/Indeed/Glassdoor/
    ZipRecruiter listings via legitimate paid API access.

    Per-search cost: 1 API request per keyword. With ~10 search_terms,
    that's 10 requests per full search. 200 free-tier requests = ~20
    searches/month.
    """

    source_name = "JSearch"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        base_url, api_key = _jsearch_base_and_key()
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and not api_key:
            return []  # silent no-op when key not configured

        # JSearch's date_posted param accepts: "all", "today", "3days",
        # "week", "month". Round to the closest semantic match.
        if posted_within_days is None:
            date_filter = "all"
        elif posted_within_days <= 3:
            date_filter = "3days"
        elif posted_within_days <= 7:
            date_filter = "week"
        else:
            date_filter = "month"

        # Free-tier rate limit is 1000/hour. Pro is 5/sec. Be conservative.
        sem = asyncio.Semaphore(3)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword,
                                             date_filter, api_key, base_url),
                        timeout=25.0,
                    )
                except (asyncio.TimeoutError, Exception):
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Dedupe by URL
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
        self, keyword: str, limit: int, date_filter: str,
        api_key: str, base_url: str,
    ) -> list[Role]:
        # JSearch returns ~10 results per page, ranked by relevance.
        # v0.1.4 (post-Pro-upgrade tuning):
        #   - num_pages=3: pulls 30 results per keyword (vs the prior 10).
        #     JSearch's relevance ranking is strong (uses LinkedIn /
        #     Indeed / Glassdoor signals), so even page 3 is high-quality.
        #     Audit showed 30.6% raw->qualifying conversion at p1; p2/p3
        #     should hold ~25-30% based on the ranking model.
        #   - employment_types=FULLTIME: drops contract/intern/part-time
        #     noise. Most testers want full-time roles; including the
        #     others dilutes the embedding pre-filter without adding
        #     signal. Keeps roughly the same qualifying count from a
        #     smaller raw pool.
        #
        # Math: 11 search_terms × 3 pages = 33 calls/search.
        # Pro tier (10K/month, overage @ $0.003): covers ~300 searches at
        # zero overage. 4 testers × 2 searches/wk × 4 wk = 32 searches/mo
        # = 1,056 calls/mo = 11% of Pro quota.
        params = {
            "query": keyword,
            "page": "1",
            "num_pages": "3",
            "date_posted": date_filter,
            "country": "us",
            "employment_types": "FULLTIME",
        }
        headers: dict[str, str] = {"Accept": "application/json"}
        # In proxy mode, Worker injects X-RapidAPI-Key + X-RapidAPI-Host.
        # In direct mode, we set them from .env.
        if api_key:
            headers["X-RapidAPI-Key"] = api_key
            headers["X-RapidAPI-Host"] = "jsearch.p.rapidapi.com"

        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                base_url, params=params, headers=headers,
            )
        except Exception:
            return []
        if resp.status_code != 200:
            # v0.1.4: surface quota state to orchestrator. JSearch returns
            # 429 when monthly quota is exhausted or rate limit hit.
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = f"JSearch HTTP {resp.status_code} (rate limit or monthly quota)"
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        items = data.get("data") or []
        out: list[Role] = []
        for j in items[:limit]:
            try:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
            except Exception:
                continue
        return out

    async def fetch_jd(self, role: Role) -> str:
        """JSearch returns full JDs inline at search time — no separate fetch
        needed. Just return whatever was already populated. This satisfies
        BaseScraper's abstract method without making a useless API call."""
        return role.job_description_full or ""

    def _item_to_role(self, item: dict[str, Any]) -> Optional[Role]:
        """Map a JSearch result to our Role model."""
        title = (item.get("job_title") or "").strip()
        company = _clean_jsearch_employer_name(
            item.get("employer_name") or "", item
        )
        if not title or not company:
            return None

        # JD comes back full (per JSearch docs) — strip HTML if any then store
        jd_html = item.get("job_description") or ""
        jd_text = _strip_html(jd_html) if "<" in jd_html else jd_html

        city = item.get("job_city")
        state = item.get("job_state")
        country = item.get("job_country")
        is_remote = item.get("job_is_remote") is True

        # Build location string. JSearch is patchy — sometimes city/state
        # are present, sometimes only country. Remote roles often have
        # all three null.
        loc_parts = [p for p in (city, state) if p]
        if loc_parts:
            location = ", ".join(loc_parts)
        elif is_remote:
            location = "Remote"
        elif country:
            location = country
        else:
            location = ""

        location_type = "Remote" if is_remote else (
            "On-site" if loc_parts else None
        )

        # Salaries — JSearch surfaces them when source listings disclose
        salary_min = item.get("job_min_salary")
        salary_max = item.get("job_max_salary")
        salary_period = (item.get("job_salary_period") or "").upper()
        # Normalize hourly to annual (~2080 work hours/year)
        if salary_period == "HOUR":
            if salary_min:
                salary_min = int(salary_min * 2080)
            if salary_max:
                salary_max = int(salary_max * 2080)
        elif salary_period == "MONTH":
            if salary_min:
                salary_min = int(salary_min * 12)
            if salary_max:
                salary_max = int(salary_max * 12)

        # Posted date — JSearch returns ISO 8601
        posted = item.get("job_posted_at_datetime_utc")

        # Apply URL — primary discoverable link
        url = item.get("job_apply_link") or item.get("job_google_link") or ""

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=location[:200] or None,
            location_type=location_type,
            city=city or None,
            state=state or None,
            salary_min=int(salary_min) if salary_min else None,
            salary_max=int(salary_max) if salary_max else None,
            job_description_full=jd_text[:50000] if jd_text else "",
            jd_completeness="Full" if jd_text and len(jd_text) > 500 else "Partial",
            posted_date=posted,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

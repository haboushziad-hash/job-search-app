"""Himalayas scraper — free no-key JSON API of remote roles (US-filtered).

v0.3.38: Himalayas (himalayas.app) is a large curated remote-jobs board
(~107k active). Free public JSON API, no auth. Returns rich structured fields
the RSS boards lack: minSalary/maxSalary/currency, seniority, employmentType,
categories. International by default — we keep only US-eligible roles
(locationRestrictions empty=worldwide OR containing a US indicator), dropping
the heavy LATAM/EU/APAC tail (the app is US-only by owner's call). ~52% of the
feed is US-eligible (measured).

Endpoint: GET https://himalayas.app/jobs/api?limit=20&offset=N
  Returns: {"totalCount", "offset", "limit", "jobs": [{title, companyName,
            applicationLink, locationRestrictions:[...], pubDate(unix int),
            description(html), minSalary, maxSalary, currency, seniority:[...],
            employmentType, categories:[...]}, ...]}
  limit caps at 20/req -> paginate. Newest-first.

Free, no auth, no documented rate limits.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match
from backend.scraper._exception_helpers import handle_scraper_exception


HIMALAYAS_API = "https://himalayas.app/jobs/api"

# US-only by owner's call (2026-06). A role is US-eligible if it has NO location
# restriction (worldwide remote) OR its restrictions include a US indicator.
# Anything restricted to non-US only (LATAM/EU/APAC) is dropped.
_US_INDICATORS = (
    "united states", "usa", "u.s.", "north america", "americas",
    "worldwide", "anywhere",
)
# Number of 20-item pages of RECENT jobs to pull. ~52% are US-eligible, so
# 30 pages -> ~600 raw -> ~310 US -> keyword-filtered down to relevant.
_MAX_PAGES = 30


def _us_eligible(location_restrictions) -> bool:
    if not location_restrictions:
        return True  # no restriction = worldwide remote = US-eligible
    for x in location_restrictions:
        xl = str(x).lower()
        if any(ind in xl for ind in _US_INDICATORS):
            return True
    return False


def _unix_to_iso(ts) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


class HimalayasScraper(BaseScraper):
    """Free Himalayas JSON API — US-filtered remote roles with structured
    salary / seniority / type fields."""

    source_name = "Himalayas"
    attribution: str = "Powered by Himalayas — https://himalayas.app"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        keywords_lower = [k.lower() for k in keywords]
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        cutoff_iso: Optional[str] = None
        if posted_within_days:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            ).isoformat()

        sem = asyncio.Semaphore(6)

        async def _fetch_page(offset: int) -> list[dict]:
            async with sem:
                try:
                    resp = await self.client._client.get(  # type: ignore[union-attr]
                        HIMALAYAS_API,
                        params={"limit": 20, "offset": offset},
                        headers={
                            "Accept": "application/json",
                            "User-Agent": "Mozilla/5.0 (compatible; FindMeSomeDamnJobz/0.3.38)",
                        },
                        timeout=20.0,
                    )
                except Exception as _e:
                    handle_scraper_exception(self, "Himalayas", _e, context=f"offset={offset}")
                    return []
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = f"Himalayas HTTP {resp.status_code}"
                    return []
                try:
                    return (resp.json() or {}).get("jobs") or []
                except Exception:
                    return []

        pages = await asyncio.gather(*(_fetch_page(o * 20) for o in range(_MAX_PAGES)))

        roles: list[Role] = []
        seen: set[str] = set()
        for jobs in pages:
            for j in jobs:
                try:
                    if not _us_eligible(j.get("locationRestrictions")):
                        continue
                    title = (j.get("title") or "").strip()
                    link = (j.get("applicationLink") or "").strip()
                    if not title or not link or link in seen:
                        continue
                    desc_raw = j.get("description") or j.get("excerpt") or ""
                    jd = _strip_html(desc_raw) if "<" in desc_raw else desc_raw
                    if not _kw_match.matches_any_keyword(title, jd, keywords_lower):
                        continue
                    posted_iso = _unix_to_iso(j.get("pubDate"))
                    if cutoff_iso and posted_iso and posted_iso < cutoff_iso:
                        continue
                    seen.add(link)
                    role = self._item_to_role(j, title, link, jd, posted_iso)
                    for kw_raw, kw_low in zip(keywords, keywords_lower):
                        if kw_low and _kw_match.matches_any_keyword(title, jd, [kw_low]):
                            role.matched_keyword = kw_raw
                            per_kw_count[kw_raw] = per_kw_count.get(kw_raw, 0) + 1
                            break
                    roles.append(role)
                except Exception:
                    continue

        self.per_keyword_raw_counts = per_kw_count
        return roles

    def _item_to_role(self, j: dict, title: str, link: str, jd: str,
                      posted_iso: Optional[str]) -> Role:
        company = self._normalize_company(j.get("companyName") or "") or "(unknown)"
        lr = j.get("locationRestrictions") or []
        loc = ", ".join(str(x) for x in lr) if lr else "Remote (US-eligible)"
        et = j.get("employmentType")
        employment_type = (str(et).strip() or None) if et else None

        # Structured salary — only trust explicit USD (the salary gate expects
        # annualized USD); skip other currencies to avoid polluting it.
        salary_min = salary_max = None
        salary_text = None
        try:
            cur = (j.get("currency") or "").upper()
            smin = j.get("minSalary")
            smax = j.get("maxSalary")
            if cur == "USD" and (smin or smax):
                salary_min = int(float(smin)) if smin else None
                salary_max = int(float(smax)) if smax else None
                if salary_min and salary_max:
                    salary_text = f"${salary_min:,} - ${salary_max:,}"
        except (ValueError, TypeError):
            salary_min = salary_max = salary_text = None

        return Role(
            job_title=title[:200],
            company=company[:120],
            source=self.source_name,
            primary_source=self.source_name,
            job_url=link,
            location=loc[:100],
            location_type="Remote",
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full=jd[:8000] if jd else "",
            jd_completeness="Full" if len(jd) > 500 else ("Partial" if jd else "Missing"),
            posted_date=posted_iso,
            employment_type=employment_type,
        )

    async def fetch_jd(self, role: Role) -> str:
        """JD already inline in the JSON — no separate fetch needed."""
        return role.job_description_full or ""

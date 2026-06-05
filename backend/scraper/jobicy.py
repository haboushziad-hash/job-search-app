"""Jobicy scraper — free JSON API (v2) of curated US remote roles.

v0.3.37: migrated from the 21-feed RSS scrape to the v2 JSON API
(https://jobicy.com/api/v2/remote-jobs). The JSON API is cleaner (no XML
namespace parsing), carries STRUCTURED fields the RSS lacked — salaryMin/Max,
jobLevel, jobType, jobIndustry — and supports a working `geo=usa` filter so we
pull only US-eligible remote roles (the app is US-only by owner's call; flip
or make `_GEO` user-conditional to support non-US users later).

We fan out across the valid industry slugs (geo=usa, count=50 each), dedupe by
url, and keyword-filter client-side (same downstream contract as the old RSS).

Endpoint: GET https://jobicy.com/api/v2/remote-jobs?count=50&geo=usa&industry={slug}
  Returns JSON: {"jobCount": N, "jobs": [{jobTitle, companyName, url, jobGeo,
                 pubDate, jobDescription, salaryMin, salaryMax, jobType,
                 jobLevel, ...}, ...]}

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


JOBICY_API_V2 = "https://jobicy.com/api/v2/remote-jobs"

# US-only by owner's call (2026-06): the app targets US jobs across all
# industries; geo=usa drops the heavy international tail Jobicy otherwise
# returns (APAC/EMEA/Europe/France). Set to None or make user-conditional to
# support non-US users later — it's the only line that needs to change.
_GEO: Optional[str] = "usa"

# Valid v2 `industry` slugs — VERIFIED live 2026-06-05 against the API. Six
# slugs from the old RSS list (legal, medical, finance-legal, sales, product,
# customer-support) return HTTP 400 on the v2 `industry` param and are
# intentionally excluded. Fanning out across these with geo=usa returns ~589
# US raw pre-dedup across all industries.
JOBICY_INDUSTRIES = [
    "admin-support", "business", "copywriting", "design-multimedia",
    "supporting", "data-science", "admin", "education",
    "accounting-finance", "healthcare", "hr", "marketing",
    "management", "dev", "seller", "seo", "smm", "engineering",
    "technical-support", "web-app-design",
]


def _parse_iso(raw: str) -> Optional[str]:
    """v2 pubDate is ISO 8601 (e.g. '2026-06-03T13:37:48+00:00')."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class JobicyScraper(BaseScraper):
    """Free Jobicy v2 JSON API — curated US remote jobs with structured
    salary / level / type fields."""

    source_name = "Jobicy"
    # Per Jobicy's docs, attribution is appreciated but not strictly required.
    attribution: str = "Powered by Jobicy — https://jobicy.com"

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

        # Fan out across all valid industry slugs in parallel (geo=usa,
        # count=50 each). Fetch ONCE — keyword matching is applied client-side
        # after, so we don't re-fetch per keyword.
        sem = asyncio.Semaphore(4)

        async def _fetch_industry(industry: str) -> list[dict]:
            async with sem:
                params: dict = {"count": 50, "industry": industry}
                if _GEO:
                    params["geo"] = _GEO
                try:
                    resp = await self.client._client.get(  # type: ignore[union-attr]
                        JOBICY_API_V2,
                        params=params,
                        headers={
                            "Accept": "application/json",
                            "User-Agent": (
                                "Mozilla/5.0 (compatible; FindMeSomeDamnJobz/0.3.37; "
                                "+https://findmesomedamnjobz.com)"
                            ),
                        },
                        timeout=20.0,
                    )
                except Exception as _e:
                    # Surface via shared helper (sets quota_exhausted on
                    # 429/403/503) instead of silently returning [].
                    handle_scraper_exception(
                        self, "Jobicy", _e, context=f"industry={industry}"
                    )
                    return []
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"Jobicy HTTP {resp.status_code} (rate limit or block)"
                        )
                    return []
                try:
                    data = resp.json()
                except Exception:
                    return []
                return data.get("jobs") or []

        batches = await asyncio.gather(
            *(_fetch_industry(s) for s in JOBICY_INDUSTRIES)
        )

        roles: list[Role] = []
        seen_urls: set[str] = set()
        for jobs in batches:
            for j in jobs:
                try:
                    title = (j.get("jobTitle") or "").strip()
                    link = (j.get("url") or "").strip()
                    if not title or not link or link in seen_urls:
                        continue

                    jd_raw = j.get("jobDescription") or j.get("jobExcerpt") or ""
                    jd_clean = _strip_html(jd_raw) if "<" in jd_raw else jd_raw

                    # Jobicy is a broad source — fetch all, let the matcher
                    # filter to roles actually relevant to the candidate.
                    if not _kw_match.matches_any_keyword(title, jd_clean, keywords_lower):
                        continue

                    posted_iso = _parse_iso(j.get("pubDate") or "")
                    if cutoff_iso and posted_iso and posted_iso < cutoff_iso:
                        continue

                    seen_urls.add(link)
                    role = self._item_to_role(j, title, link, jd_clean, posted_iso)
                    if role is None:
                        continue

                    # Identify which keyword matched (for per_keyword_raw_counts)
                    for kw_raw, kw_low in zip(keywords, keywords_lower):
                        if kw_low and _kw_match.matches_any_keyword(title, jd_clean, [kw_low]):
                            role.matched_keyword = kw_raw
                            per_kw_count[kw_raw] = per_kw_count.get(kw_raw, 0) + 1
                            break

                    roles.append(role)
                except Exception:
                    continue

        self.per_keyword_raw_counts = per_kw_count
        return roles

    def _item_to_role(
        self, j: dict, title: str, link: str, jd_clean: str,
        posted_iso: Optional[str],
    ) -> Optional[Role]:
        company = self._normalize_company(j.get("companyName") or "") or "(unknown)"
        geo = (j.get("jobGeo") or "").strip()

        # jobType is a list, e.g. ["Full-Time"]
        jt = j.get("jobType")
        if isinstance(jt, list):
            employment_type = ", ".join(str(x) for x in jt) or None
        else:
            employment_type = (str(jt).strip() or None) if jt else None

        # Structured salary — only trust annual USD (the salary gate expects
        # annualized USD); skip hourly / non-USD to avoid polluting it.
        salary_min = salary_max = None
        salary_text = None
        try:
            cur = (j.get("salaryCurrency") or "").upper()
            per = (j.get("salaryPeriod") or "").lower()
            smin = j.get("salaryMin")
            smax = j.get("salaryMax")
            if cur == "USD" and per in ("yearly", "annual", "year") and (smin or smax):
                salary_min = int(float(smin)) if smin else None
                salary_max = int(float(smax)) if smax else None
                if salary_min and salary_max:
                    salary_text = f"${salary_min:,} - ${salary_max:,}"
        except (ValueError, TypeError):
            salary_min = salary_max = None
            salary_text = None

        return Role(
            job_title=title[:200],
            company=company[:120],
            source=self.source_name,
            primary_source=self.source_name,
            job_url=link,
            location=(geo[:100] if geo else None),
            location_type="Remote",  # Jobicy is remote-only by design
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full=jd_clean[:8000] if jd_clean else "",
            jd_completeness=(
                "Full" if len(jd_clean) > 500 else ("Partial" if jd_clean else "Missing")
            ),
            posted_date=posted_iso,
        )

    async def fetch_jd(self, role: Role) -> str:
        """JD already inline in the v2 JSON — no separate fetch needed."""
        return role.job_description_full or ""

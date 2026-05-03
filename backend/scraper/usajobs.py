"""USAJOBS scraper — federal job aggregator (free public API).

USAJOBS is the official US federal government job board. It indexes ALL
federal civilian jobs (~30K-60K active positions across 600+ agencies).
Ideal coverage for:
  - Federal AI strategy / policy roles (Ziad's wheelhouse)
  - Environmental research / scientists / engineers (EPA, NOAA, NASA, USGS)
  - Healthcare (VA, NIH, HHS)
  - Cybersecurity / IT (DOD, CISA)
  - Finance / accounting (Treasury, GAO, SEC)
  - Lawyers / policy / regulation (DOJ, OMB, agencies)

Public API: https://developer.usajobs.gov/api-reference/
  - Free, public, no rate limit beyond reasonable use
  - Requires User-Agent + Authorization-Key headers
    (User-Agent = your email; Authorization-Key from one-click signup)
  - For our purposes, USAJOBS_USER_AGENT and USAJOBS_API_KEY env vars

Endpoint: GET https://data.usajobs.gov/api/search?Keyword={kw}&ResultsPerPage=500
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper


USAJOBS_API_BASE = "https://data.usajobs.gov/api/search"


class USAJobsScraper(BaseScraper):
    """Scrapes federal jobs from the USAJOBS public API."""

    source_name = "USAJOBS"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # USAJOBS requires a free API key (sign up at developer.usajobs.gov)
        # Without a key, the API returns 401. Skip silently if not configured.
        user_agent = config.USAJOBS_USER_AGENT
        api_key = config.USAJOBS_API_KEY
        if not api_key:
            return []

        sem = asyncio.Semaphore(6)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword, user_agent, api_key),
                        timeout=30.0,
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
        self, keyword: str, limit: int, user_agent: str, api_key: str,
    ) -> list[Role]:
        params = {
            "Keyword": keyword,
            "ResultsPerPage": min(500, limit * 5),
        }
        headers = {
            "User-Agent": user_agent,
            "Host": "data.usajobs.gov",
        }
        if api_key:
            headers["Authorization-Key"] = api_key

        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                USAJOBS_API_BASE, params=params, headers=headers,
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        items = (
            data.get("SearchResult", {}).get("SearchResultItems", [])
            or []
        )
        out: list[Role] = []
        for item in items[:limit]:
            try:
                role = self._item_to_role(item)
                if role:
                    out.append(role)
            except Exception:
                continue
        return out

    def _item_to_role(self, item: dict) -> Optional[Role]:
        descriptor = item.get("MatchedObjectDescriptor") or {}
        if not descriptor:
            return None

        title = (descriptor.get("PositionTitle") or "").strip()
        if not title:
            return None

        # PositionURI is the candidate-facing URL
        url = descriptor.get("PositionURI") or ""
        if not url:
            applylink = (descriptor.get("ApplyURI") or [None])[0]
            url = applylink or ""

        # Agency / department for company name
        org = (descriptor.get("OrganizationName")
               or descriptor.get("DepartmentName") or "")
        company = self._normalize_company(org or "Federal Government")

        # Locations
        locations = descriptor.get("PositionLocation") or []
        loc_str = ""
        if locations:
            ln = locations[0].get("LocationName") or ""
            loc_str = ln
        # USAJOBS doesn't have a clean Remote/Hybrid flag — heuristic
        location_type = "Remote" if "anywhere" in loc_str.lower() or "telework" in loc_str.lower() else "On-site"

        # Salary
        salary_text = None
        remuneration = descriptor.get("PositionRemuneration") or []
        salary_min = salary_max = None
        if remuneration:
            r0 = remuneration[0]
            min_str = r0.get("MinimumRange") or ""
            max_str = r0.get("MaximumRange") or ""
            try:
                salary_min = int(float(min_str)) if min_str else None
                salary_max = int(float(max_str)) if max_str else None
            except (ValueError, TypeError):
                pass
            if salary_min and salary_max:
                salary_text = f"${salary_min:,} - ${salary_max:,}"

        # Posted date
        posted = descriptor.get("PublicationStartDate") or ""
        posted_iso = posted if posted else None

        # JD body — UserArea has a lot, but the QualificationSummary is concise
        ua = descriptor.get("UserArea", {}).get("Details", {}) or {}
        jd_parts = []
        if descriptor.get("QualificationSummary"):
            jd_parts.append(str(descriptor["QualificationSummary"]))
        if ua.get("MajorDuties"):
            jd_parts.append(str(ua["MajorDuties"]))
        if ua.get("Education"):
            jd_parts.append("EDUCATION: " + str(ua["Education"]))
        jd = "\n\n".join(jd_parts)[:8000]

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full=jd,
            jd_completeness="Full" if len(jd) > 500 else "Partial",
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        """USAJOBS roles already have JD bodies populated at search time."""
        return role.job_description_full or ""

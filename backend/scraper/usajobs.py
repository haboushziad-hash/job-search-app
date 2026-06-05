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


def _usajobs_base_and_keys() -> tuple[str, Optional[str], Optional[str]]:
    """Return (base_url, api_key, user_agent) honoring proxy mode (v0.1.4).

    Proxy mode: base = {LLM_PROXY_URL}/v1/scraper/usajobs/api/search
                api_key + user_agent are empty (Worker injects them)
    Local mode: base = USAJOBS_API_BASE, keys read from .env
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        return f"{proxy}/v1/scraper/usajobs/api/search", "", ""
    return USAJOBS_API_BASE, config.USAJOBS_API_KEY, config.USAJOBS_USER_AGENT


class USAJobsScraper(BaseScraper):
    """Scrapes federal jobs from the USAJOBS public API.

    v0.1.4: routes through Worker proxy in production, direct in dev mode."""

    source_name = "USAJOBS"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        base_url, api_key, user_agent = _usajobs_base_and_keys()
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and not api_key:
            return []  # silent no-op when not configured locally

        sem = asyncio.Semaphore(6)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword,
                                             user_agent or "", api_key or "",
                                             base_url,
                                             posted_within_days=posted_within_days),
                        timeout=30.0,
                    )
                except Exception:
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
        base_url: str,
        posted_within_days: Optional[int] = None,
    ) -> list[Role]:
        params: dict = {
            "Keyword": keyword,
            "ResultsPerPage": min(500, limit * 5),
        }
        # FIX 17: wire posted_within_days into the USAJOBS DatePosted param
        # (USAJOBS supports 1-60 days).
        if posted_within_days:
            params["DatePosted"] = min(60, int(posted_within_days))

        # FIX 16: wire self._user_filters into the USAJOBS request.
        # USAJOBS supports LocationName, Radius, RemunerationMinimumAmount.
        #
        # v0.3.17 hotfix: USAJOBS LocationName expects "City, State" format
        # (e.g. "Richmond, Virginia"), NOT abbreviated "Richmond VA".
        # Audit 2026-05-29_19-49 showed USAJOBS returning 1 raw role when
        # passed "Richmond VA" verbatim (was 222 prior). Be conservative:
        # only set LocationName if the user's location_text looks USAJOBS-
        # friendly (contains a comma indicating City, State format).
        # Otherwise omit LocationName and let USAJOBS return US-wide
        # federal results — the downstream hard_filter validates against
        # acceptable_locations.
        #
        # Also: RemunerationMinimumAmount is REMOVED. Federal job postings
        # frequently omit salary entirely (especially GS-series which are
        # implicit by grade). Sending a $130k minimum excludes most
        # legitimate matches whose salary just isn't listed in the API
        # response. Salary filtering happens correctly post-fetch in
        # hard_filters.passes_salary_floor().
        # Wave-2B Phase 2 (FIX 19, 2026-05-30): when user has MULTIPLE
        # acceptable_locations, USAJOBS can only filter by ONE LocationName
        # at the API level. Previously only locs[0] was queried, silently
        # dropping all secondary-location federal job matches. Fix: when
        # multi-location, skip the LocationName + Radius filter so the
        # API returns US-wide federal results and the downstream
        # hard_filter validates against the full acceptable_locations
        # list. Single-location users keep the tight API filter.
        user_filters = getattr(self, "_user_filters", None) or {}
        accept_locs = user_filters.get("acceptable_locations") or []
        loc_text = (user_filters.get("location_text") or "").strip()
        if len(accept_locs) > 1:
            # Multi-location: drop LocationName so all locations are
            # represented in the pull. Downstream is the authority.
            pass
        elif loc_text and "," in loc_text and len(loc_text.split(",")) >= 2:
            # Single-location: keep the tight API filter (City, State).
            params["LocationName"] = loc_text
            radius = user_filters.get("radius_miles", 50)
            try:
                params["Radius"] = int(radius)
            except (ValueError, TypeError):
                params["Radius"] = 50

        # In proxy mode the Worker injects auth + Host. In direct mode we
        # set them ourselves from .env.
        headers: dict[str, str] = {}
        if user_agent:
            headers["User-Agent"] = user_agent
        if api_key:
            headers["Authorization-Key"] = api_key
            headers["Host"] = "data.usajobs.gov"

        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                base_url, params=params, headers=headers,
            )
        except Exception:
            return []
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = f"USAJOBS HTTP {resp.status_code} (rate limit or auth)"
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
        # v0.3.36: was items[:limit]. ResultsPerPage = min(500, limit*5) already
        # FETCHED ~250 roles/keyword, then this slice threw away ~200 of them.
        # Keep every already-fetched federal role (~5x more, zero extra API cost;
        # the embedding prefilter + EN1 800-cap handle relevance/volume downstream).
        for item in items:
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
        # FIX 6: federal jobs typically list 5-20 eligible duty stations.
        # Previously we kept only locations[0] and silently dropped the rest.
        # Now we prefer the first LocationName matching the user's
        # location_text filter (so candidates see "their" duty station),
        # else fall back to joining all LocationName values with "; ".
        locations = descriptor.get("PositionLocation") or []
        loc_str = ""
        if locations:
            user_filters = getattr(self, "_user_filters", None) or {}
            user_loc = (user_filters.get("location_text") or "").strip().lower()
            all_names = [
                (loc.get("LocationName") or "").strip()
                for loc in locations
                if loc.get("LocationName")
            ]
            matched = ""
            if user_loc:
                for name in all_names:
                    if user_loc in name.lower():
                        matched = name
                        break
            if matched:
                loc_str = matched
            elif all_names:
                loc_str = "; ".join(all_names)
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

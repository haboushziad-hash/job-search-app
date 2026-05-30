"""SmartRecruiters scraper — public per-company API.

SmartRecruiters is an ATS used by mid-market and some F500 employers,
especially in retail, fashion, and CPG. Free public API at
``https://api.smartrecruiters.com/v1/companies/{company}/postings``.

Probed live 2026-05-03 — most candidates have the integration but no
current postings (200 response with empty `content`). The 3 verified
working tenants below have real active postings.

Add tenants as discovered: probe with scripts/probe_smartrecruiters.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


SR_API_BASE = "https://api.smartrecruiters.com/v1/companies"


# Verified live 2026-05-03
SMARTRECRUITERS_COMPANIES: list[tuple[str, str]] = [
    ("Visa",    "visa"),    # 43 jobs — global financial services
    ("ASOS",    "asos"),    # 42 jobs — UK fashion retail
    ("LVMH",    "lvmh"),    #  4 jobs — luxury goods
]


class SmartRecruitersScraper(BaseScraper):
    source_name = "SmartRecruiters"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        keywords_lower = [k.lower() for k in keywords]
        sem = asyncio.Semaphore(8)

        async def fetch_company(display: str, slug: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._fetch_company(display, slug),
                        timeout=15.0,
                    )
                except Exception:
                    return []

        tasks = [fetch_company(d, s) for d, s in SMARTRECRUITERS_COMPANIES]
        all_roles_lists = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten, then keyword-filter (token-overlap via _keyword_match.py)
        seen: set[str] = set()
        out: list[Role] = []
        for rl in all_roles_lists:
            if isinstance(rl, BaseException):
                continue
            for role in rl or []:
                # Token-overlap on title + JD (was title-only substring before)
                if not _kw_match.matches_any_keyword(
                    role.job_title or "",
                    role.job_description_full or "",
                    keywords_lower,
                ):
                    continue
                if role.job_url and role.job_url in seen:
                    continue
                if role.job_url:
                    seen.add(role.job_url)
                out.append(role)
        return out

    async def _fetch_company(self, display: str, slug: str) -> list[Role]:
        url = f"{SR_API_BASE}/{slug}/postings"
        params = {"limit": 100}
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                url, params=params,
                headers={"Accept": "application/json"},
            )
        except Exception:
            return []
        if resp.status_code != 200:
            # v0.2.1: surface non-200 in scraper_health so operators can
            # tell whether SmartRecruiters is genuinely empty (200 + empty
            # array) vs API-down/rate-limited. SmartRecruiters has only
            # 3 tenants (Visa/ASOS/LVMH); if all 3 return non-200 the
            # scraper looks "broken" rather than "narrow".
            if resp.status_code in (429, 403, 500, 502, 503):
                self.quota_exhausted = True
                self.quota_exhausted_reason = f"SmartRecruiters HTTP {resp.status_code} on tenant '{slug}'"
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        items = data.get("content") or []

        # Wave-2B Phase 2 (FIX 6c): two-pass — first build roles + capture
        # detail URLs, then fetch each detail endpoint in parallel to fill
        # JD body. Listing payload only has metadata; the rich JD lives at
        # the per-posting detail endpoint exposed via top-level 'ref' field.
        pairs: list[tuple[Role, str]] = []
        for j in items:
            try:
                role = self._item_to_role(j, display, slug)
                if role:
                    detail_url = (j.get("ref") or "").strip()
                    pairs.append((role, detail_url))
            except Exception:
                continue

        # Parallel JD enrichment from the detail endpoint.
        detail_sem = asyncio.Semaphore(4)

        async def _enrich(role: Role, detail_url: str) -> Role:
            if not detail_url or role.job_description_full:
                return role
            try:
                async with detail_sem:
                    d_resp = await self.client._client.get(  # type: ignore[union-attr]
                        detail_url,
                        headers={"Accept": "application/json"},
                        timeout=15.0,
                    )
            except Exception:
                return role
            if d_resp.status_code != 200:
                return role
            try:
                detail = d_resp.json()
            except Exception:
                return role
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            jd_parts = []
            for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
                section = sections.get(k) or {}
                text = section.get("text") or ""
                if text:
                    jd_parts.append(_strip_html(text))
            jd_full = "\n\n".join(jd_parts)[:8000]
            if jd_full:
                role.job_description_full = jd_full
                role.jd_completeness = "Full" if len(jd_full) > 500 else "Partial"
            return role

        enriched = await asyncio.gather(*[_enrich(r, du) for r, du in pairs])
        return list(enriched)

    def _item_to_role(self, j: dict, display: str, slug: str) -> Optional[Role]:
        title = (j.get("name") or "").strip()
        if not title:
            return None

        # Wave-2B Phase 2 (FIX 6a + 6b): SmartRecruiters URL construction.
        # Previously read `refs.jobAd.url` — but the listing response uses
        # `ref` (singular, top-level) as the DETAIL API URL, and there is
        # no `refs` (plural) field. Every Role was shipping with job_url=None
        # which broke click-through AND silently filtered the role out of
        # keyword matching (matches_any_keyword requires a non-None URL via
        # the dedup path). Build the public URL deterministically from
        # slug + posting id — verified URL pattern.
        posting_id = (j.get("id") or "").strip()
        url = ""
        if posting_id:
            url = f"https://jobs.smartrecruiters.com/{slug}/{posting_id}"
        # Defensive fallback (some Wave-2B Phase 1 era responses had refs):
        if not url:
            refs_legacy = j.get("refs") or {}
            url = refs_legacy.get("jobAd", {}).get("url") or ""

        # Location
        loc = j.get("location") or {}
        city = loc.get("city") or ""
        region = loc.get("region") or ""
        country = loc.get("country") or ""
        loc_parts = [p for p in (city, region, country) if p]
        loc_str = ", ".join(loc_parts)
        location_type, _ = self._classify_location(loc_str)
        if loc.get("remote"):
            location_type = "Remote"

        # JD pieces
        sections = (j.get("jobAd", {}) or {}).get("sections", {})
        jd_parts = []
        for k in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
            section = sections.get(k) or {}
            text = section.get("text") or ""
            if text:
                jd_parts.append(_strip_html(text))
        jd = "\n\n".join(jd_parts)[:8000]

        # Posted
        posted = j.get("releasedDate") or j.get("createdOn") or ""

        return Role(
            job_title=title[:200],
            company=self._normalize_company(display),
            job_url=url or None,
            location=loc_str or None,
            location_type=location_type,
            job_description_full=jd,
            jd_completeness="Full" if len(jd) > 500 else ("Partial" if jd else "Missing"),
            posted_date=str(posted) or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

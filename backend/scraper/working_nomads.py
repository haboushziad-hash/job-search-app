"""Working Nomads scraper — free public JSON of all listed remote jobs.

Working Nomads curates ~500-1500 active remote roles, weighted toward:
  - Engineering / dev (still ~half the listings)
  - Marketing / sales / content
  - Customer success / support
  - Recruiting / HR (rare on other remote boards — useful for HR fixtures)
  - Education / instructional design

Endpoint: GET https://www.workingnomads.com/api/exposed_jobs/
  Returns: a JSON list of job objects (no pagination, full active set).

Each item:
  {
    "url": "...",                    # canonical apply URL
    "title": "...",
    "company_name": "...",
    "category_name": "...",
    "tags": "tag1,tag2,...",
    "description": "<html>...</html>",
    "pub_date": "2026-05-04T18:00:12.345Z",
    "location": "Worldwide" | "USA Only" | etc.
  }

No auth, no key. Single fetch returns the whole active list, so we filter
client-side by keyword (mirrors RemoteOK / Remotive pattern).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


WORKINGNOMADS_API = "https://www.workingnomads.com/api/exposed_jobs/"


class WorkingNomadsScraper(BaseScraper):
    """Working Nomads — free remote-only aggregator."""

    source_name = "WorkingNomads"
    attribution: str = "Powered by Working Nomads — https://www.workingnomads.com"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                WORKINGNOMADS_API,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; JobSearchApp/0.3.5)"
                    ),
                },
            )
        except Exception:
            return []
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"WorkingNomads HTTP {resp.status_code}"
                )
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        if not isinstance(data, list):
            return []

        from datetime import timedelta
        cutoff_iso: Optional[str] = None
        if posted_within_days is not None:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            ).isoformat()

        keywords_lower = [k.lower() for k in keywords]
        seen: set[str] = set()
        out: list[Role] = []
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        for j in data:
            if not isinstance(j, dict):
                continue
            try:
                role = self._item_to_role(j)
                if not role or not role.job_url:
                    continue
                if cutoff_iso and role.posted_date and role.posted_date < cutoff_iso:
                    continue
                if not _kw_match.matches_any_keyword(
                    role.job_title or "",
                    role.job_description_full or "",
                    keywords_lower,
                ):
                    continue
                if role.job_url in seen:
                    continue
                seen.add(role.job_url)
                out.append(role)
                for kw in keywords:
                    if _kw_match.matches_any_keyword(
                        role.job_title or "",
                        role.job_description_full or "",
                        [kw.lower()],
                    ):
                        per_kw_count[kw] = per_kw_count.get(kw, 0) + 1
            except Exception:
                continue

        self.per_keyword_raw_counts = per_kw_count
        return out

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("title") or "").strip()
        company = self._normalize_company((j.get("company_name") or "").strip())
        url = (j.get("url") or "").strip()
        if not title or not company or not url:
            return None

        loc_str = (j.get("location") or "Remote").strip() or "Remote"
        location_type = "Remote"  # Working Nomads is remote-only

        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if "<" in desc else desc

        # pub_date is ISO 8601 already; use it verbatim if present.
        posted = (j.get("pub_date") or "").strip() or None

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=loc_str,
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness=(
                "Full" if len(jd_text) > 500
                else ("Partial" if jd_text else "Missing")
            ),
            posted_date=posted,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

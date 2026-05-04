"""Arbeitnow scraper — public API for diverse remote + EU jobs.

Free public API at https://www.arbeitnow.com/api/job-board-api — no auth.
Returns ~100 active jobs across diverse industries with clean structured
data (title, company, description, remote flag, tags, URL).

Skews European and remote-friendly — complements Remotive (US-leaning)
and our broad-aggregator suite.

Endpoint: GET https://www.arbeitnow.com/api/job-board-api
  No parameters — returns the latest active 100 jobs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowScraper(BaseScraper):
    source_name = "Arbeitnow"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Arbeitnow returns the same 100 jobs regardless of keyword (no
        # search param). We fetch once and apply client-side keyword filter.
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                ARBEITNOW_API,
                headers={"Accept": "application/json"},
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        all_jobs = data.get("data") or []
        if not all_jobs:
            return []

        keywords_lower = [k.lower() for k in keywords]

        seen: set[str] = set()
        out: list[Role] = []
        for j in all_jobs:
            title = (j.get("title") or "").strip()
            if not title:
                continue
            # Token-overlap match (shared via _keyword_match.py).
            if not _kw_match.matches_any_keyword(
                title,
                j.get("description") or "",
                keywords_lower,
            ):
                continue
            url = j.get("url") or ""
            if url in seen:
                continue
            if url:
                seen.add(url)
            try:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
            except Exception:
                continue
        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("title") or "").strip()
        if not title:
            return None
        company = self._normalize_company(str(j.get("company_name") or "").strip())
        if not company:
            return None
        url = j.get("url") or ""
        if not url:
            return None

        # Location — Arbeitnow has 'location' (string) and 'remote' (bool)
        loc_str = j.get("location") or ""
        is_remote = bool(j.get("remote"))
        if is_remote:
            location_type = "Remote"
        else:
            location_type, _ = self._classify_location(loc_str)

        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if desc else ""

        # Posted at — Arbeitnow uses Unix epoch in created_at
        created = j.get("created_at")
        posted_iso = None
        try:
            if created:
                posted_iso = datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()
        except (ValueError, TypeError):
            pass

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or ("Remote" if is_remote else None),
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

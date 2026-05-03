"""Remotive scraper — all remote jobs across diverse industries.

Remotive aggregates fully remote postings from thousands of companies.
Free public API at https://remotive.com/api/remote-jobs — no auth, no key.

Coverage: ~5K-10K active remote roles. Industries:
  - Software / engineering
  - Marketing / sales / customer support
  - Design
  - Operations / project management
  - Writing / content
  - Education / teaching
  - Healthcare / clinical (telehealth)
  - Finance / accounting

Best for: testers in any role who are open to remote work. Especially
strong for federal/research/policy candidates who hit walls on tech-only
boards.

Endpoint: GET https://remotive.com/api/remote-jobs?search={kw}&limit=50
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


REMOTIVE_API = "https://remotive.com/api/remote-jobs"


class RemotiveScraper(BaseScraper):
    """Scrapes Remotive's free public job aggregator API."""

    source_name = "Remotive"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        sem = asyncio.Semaphore(4)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword),
                        timeout=20.0,
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

    async def _search_keyword(self, keyword: str, limit: int) -> list[Role]:
        params = {"search": keyword, "limit": min(50, limit)}
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                REMOTIVE_API, params=params,
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

        jobs = data.get("jobs") or []
        out: list[Role] = []
        for j in jobs[:limit]:
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

        url = j.get("url") or ""
        company = self._normalize_company(j.get("company_name") or "")
        if not company or not url:
            return None

        # Remotive marks all roles remote
        loc_str = (j.get("candidate_required_location") or "Worldwide").strip()
        location_type = "Remote"

        # JD
        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if desc else ""

        # Salary text
        salary_text = (j.get("salary") or "").strip() or None

        posted = j.get("publication_date") or ""

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or "Remote",
            location_type=location_type,
            salary_text=salary_text,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

"""Findwork scraper — small but free aggregator with developer-friendly API.

Findwork (findwork.dev) aggregates ~5K-15K active jobs across remote and
on-site, mostly tech-leaning but covers product, design, marketing, sales.

Free tier: requires a token (free, register at https://findwork.dev/developers).
The token goes in env as ``FINDWORK_API_KEY``. No-op silently when unset.

Endpoint: GET https://findwork.dev/api/jobs/?search={kw}&order_by=-date_posted
  Headers: Authorization: Token {key}
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


FINDWORK_API = "https://findwork.dev/api/jobs/"


class FindworkScraper(BaseScraper):
    source_name = "Findwork"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        api_key = config.FINDWORK_API_KEY
        if not api_key:
            return []  # silent no-op when unset

        sem = asyncio.Semaphore(3)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword, api_key),
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

    async def _search_keyword(
        self, keyword: str, limit: int, api_key: str,
    ) -> list[Role]:
        # Findwork accepts `sort_by=date_posted` (not `order_by`); default
        # ordering already returns newest-first so the param is optional.
        params = {"search": keyword}
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                FINDWORK_API, params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Token {api_key}",
                },
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        items = data.get("results") or []
        out: list[Role] = []
        for j in items[:limit]:
            try:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
            except Exception:
                continue
        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("role") or "").strip()
        if not title:
            return None
        company = self._normalize_company(str(j.get("company_name") or "").strip())
        if not company:
            return None
        url = j.get("url") or ""
        if not url:
            return None

        loc_str = (j.get("location") or "").strip()
        is_remote = bool(j.get("remote"))
        if is_remote:
            location_type = "Remote"
        else:
            location_type, _ = self._classify_location(loc_str)

        text = j.get("text") or ""
        jd_text = _strip_html(text) if text else ""

        posted = j.get("date_posted") or ""

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

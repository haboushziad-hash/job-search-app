"""Wellfound scraper (formerly AngelList Talent).

Wellfound is the largest startup-job board, especially strong for AI-native
firms (Anthropic, OpenAI, Mistral, etc. all post here). They have a public
search API though it's lightly documented.

Strategy: search by keyword, fetch search result pages, parse the embedded
JSON. Wellfound is anti-bot but less aggressive than Indeed.

URL pattern (search):
  https://wellfound.com/jobs?keywords=<keyword>&remote=true&page=<n>
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


class WellfoundScraper(BaseScraper):
    source_name = "Wellfound"
    BASE_URL = "https://wellfound.com/jobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 30,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        tasks = [
            self._search_one(kw, limit_per_keyword)
            for kw in keywords
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_roles: list[Role] = []
        seen_url: set[str] = set()
        seen_title_company: set[tuple[str, str]] = set()

        for result in results:
            if isinstance(result, Exception):
                continue
            for role in result:
                if role.job_url and role.job_url in seen_url:
                    continue
                key = (
                    (role.job_title or "").strip().lower(),
                    (role.company or "").strip().lower(),
                )
                if key[0] and key in seen_title_company:
                    continue
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_title_company.add(key)
                all_roles.append(role)
        return all_roles

    async def _search_one(self, keyword: str, limit: int) -> list[Role]:
        roles: list[Role] = []
        page = 1
        max_pages = max(1, limit // 20)
        while page <= max_pages and len(roles) < limit:
            try:
                response = await self.client.get(
                    self.BASE_URL,
                    params={
                        "keywords": keyword,
                        "page": page,
                    },
                )
            except Exception:
                break
            page_roles = self._parse_html(response.text)
            if not page_roles:
                break
            roles.extend(page_roles)
            if len(page_roles) < 5:
                break
            page += 1
        return roles[:limit]

    def _parse_html(self, html: str) -> list[Role]:
        """Wellfound uses Next.js — search results are in __NEXT_DATA__ JSON."""
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, re.DOTALL,
        )
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        # Navigate the Next.js page props — structure varies but the search
        # results usually live under pageProps.searchResults or similar.
        roles: list[Role] = []
        try:
            page_props = data.get("props", {}).get("pageProps", {})
            results = (
                page_props.get("searchResults", {}).get("hits")
                or page_props.get("jobs")
                or page_props.get("data", {}).get("jobs")
                or []
            )
            for r in results:
                try:
                    roles.append(self._hit_to_role(r))
                except Exception:
                    continue
        except Exception:
            pass
        return roles

    def _hit_to_role(self, hit: dict[str, Any]) -> Role:
        title = hit.get("title") or hit.get("name") or ""
        company_obj = hit.get("startup") or hit.get("company") or {}
        company = (
            company_obj.get("name") if isinstance(company_obj, dict) else str(company_obj)
        ) or ""

        # URL
        slug = hit.get("slug") or hit.get("hashId")
        url = hit.get("url") or hit.get("absoluteUrl") or ""
        if not url and slug:
            url = f"https://wellfound.com/jobs/{slug}"

        # Location
        location_text = hit.get("location") or hit.get("locations", [{}])[0].get("name") if hit.get("locations") else ""
        is_remote = hit.get("remoteOk") or "remote" in (location_text or "").lower()
        location_type = "Remote" if is_remote else "On-site"

        # Salary
        salary_text = None
        salary_min = hit.get("salaryMin") or hit.get("compensationMin")
        salary_max = hit.get("salaryMax") or hit.get("compensationMax")
        if salary_min and salary_max:
            try:
                salary_min = int(salary_min)
                salary_max = int(salary_max)
                salary_text = f"${salary_min:,} - ${salary_max:,}"
            except (ValueError, TypeError):
                salary_min = salary_max = None

        # JD
        jd_text = _strip_html(hit.get("description") or hit.get("descriptionRaw") or "")

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location_text or None,
            location_type=location_type,
            job_description_full=jd_text,
            jd_completeness="Full" if jd_text else "Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=hit.get("createdAt") or hit.get("postedAt"),
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        if role.job_description_full and len(role.job_description_full) > 500:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            response = await self.client.get(role.job_url)
            return _strip_html(response.text)
        except Exception:
            return ""

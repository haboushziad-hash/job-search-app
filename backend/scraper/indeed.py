"""Indeed scraper.

Indeed is anti-bot-heavy. Strategy:
  1. Use the public search page (HTML), parse with BeautifulSoup
  2. Slow pacing (3-7s between requests, longer than other boards)
  3. UA rotation (handled by ScraperClient)
  4. Heavy retry tolerance — Indeed sometimes returns 403/429 randomly

Note: Indeed has aggressive anti-bot. We expect occasional rate limits
and accept lower coverage from this source. Greenhouse + Lever + BuiltIn
are our reliable backbones; Indeed adds breadth.

URL pattern:
  https://www.indeed.com/jobs?q=<keyword>&l=<location>&fromage=<days>
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote_plus

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


class IndeedScraper(BaseScraper):
    source_name = "Indeed"
    BASE_URL = "https://www.indeed.com/jobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 30,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        """Search Indeed for each keyword. Returns deduped roles."""
        # Indeed serves slowly; cap concurrency tight via shared client semaphore
        tasks = [
            self._search_one(kw, limit_per_keyword, posted_within_days or 30)
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

    async def _search_one(
        self,
        keyword: str,
        limit: int,
        posted_within_days: int,
    ) -> list[Role]:
        """Fetch search results pages, parse jobs from the embedded JSON."""
        roles: list[Role] = []
        start = 0
        while len(roles) < limit:
            try:
                response = await self.client.get(
                    self.BASE_URL,
                    params={
                        "q": keyword,
                        "fromage": posted_within_days,
                        "start": start,
                        "limit": "10",
                        "sort": "date",
                    },
                )
            except Exception:
                break
            page_roles = self._parse_page(response.text)
            if not page_roles:
                break
            roles.extend(page_roles)
            if len(page_roles) < 10:
                break
            start += 10
        return roles[:limit]

    def _parse_page(self, html: str) -> list[Role]:
        """Indeed embeds search results in a window.mosaic JSON blob.

        Layout of the blob is fragile and changes occasionally; we extract
        what we can and log nothing if the structure changes — caller
        treats empty result as "soft fail" and keeps moving.
        """
        # Find the embedded JSON
        m = re.search(r"window\.mosaic\.providerData\[\"mosaic-provider-jobcards\"\]\s*=\s*({.+?});", html, re.DOTALL)
        if not m:
            # Try alternative pattern
            m = re.search(r'"jobsearch":\s*({.+?})\s*,\s*"jobsearchHooks"', html, re.DOTALL)
            if not m:
                return []
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        # Navigate to the results array — structure varies
        results = (
            data.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results")
            or data.get("results")
            or []
        )
        roles: list[Role] = []
        for r in results:
            try:
                roles.append(self._card_to_role(r))
            except Exception:
                continue
        return roles

    def _card_to_role(self, card: dict[str, Any]) -> Role:
        title = card.get("title") or card.get("displayTitle") or ""
        company = card.get("company") or card.get("companyName") or ""
        location_text = card.get("formattedLocation") or card.get("location") or ""
        is_remote = card.get("remoteWorkModel") == "REMOTE_FULL_TIME" or "remote" in location_text.lower()
        location_type = "Remote" if is_remote else (
            "Hybrid" if "hybrid" in location_text.lower() else "On-site"
        )

        # URL
        jk = card.get("jobkey") or card.get("jobKey") or card.get("jk") or ""
        url = f"https://www.indeed.com/viewjob?jk={jk}" if jk else ""

        # Salary
        salary_text = None
        salary_min = None
        salary_max = None
        sal = card.get("salarySnippet") or card.get("estimatedSalary") or {}
        if isinstance(sal, dict):
            text = sal.get("text") or ""
            if text:
                salary_text = text
                # Parse $X-$Y from text
                m = re.search(r"\$([\d,]+)\s*-\s*\$([\d,]+)", text)
                if m:
                    try:
                        salary_min = int(m.group(1).replace(",", ""))
                        salary_max = int(m.group(2).replace(",", ""))
                    except ValueError:
                        pass

        # Snippet (short JD preview)
        snippet = card.get("snippet") or ""
        snippet = _strip_html(snippet) if snippet else ""

        # Posted age — relative ("3 days ago") or absolute ms
        posted_iso = None
        if "pubDate" in card:
            try:
                ts = int(card["pubDate"])
                posted_iso = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            except (ValueError, TypeError):
                pass

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location_text or None,
            location_type=location_type,
            job_description_full=snippet,  # snippet only — full JD requires fetch_jd()
            jd_completeness="Partial" if snippet else "Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        """Fetch full JD body from Indeed view page."""
        if role.job_description_full and len(role.job_description_full) > 800:
            return role.job_description_full  # already have full JD
        if not role.job_url:
            return ""
        try:
            response = await self.client.get(role.job_url)
            html = response.text
            # Indeed JD is in #jobDescriptionText
            m = re.search(r'<div[^>]+id="jobDescriptionText"[^>]*>(.*?)</div>',
                          html, re.DOTALL)
            if m:
                return _strip_html(m.group(1))
            return _strip_html(html)
        except Exception:
            return ""

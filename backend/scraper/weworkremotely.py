"""WeWorkRemotely scraper — free RSS feed of all active remote roles.

WeWorkRemotely is the longest-running pure-remote job board with a strong
US/CA tilt. The free RSS feed at /remote-jobs.rss includes the current
active set across all categories.

We fetch once, parse RSS XML, and apply client-side keyword filter (same
pattern as Arbeitnow / Remotive).

Endpoint: GET https://weworkremotely.com/remote-jobs.rss
  Returns: standard RSS 2.0 XML.

RSS item shape:
  <item>
    <title>Company: Job Title</title>
    <link>https://weworkremotely.com/remote-jobs/...</link>
    <description><![CDATA[ <html> Region info + JD body </html> ]]></description>
    <pubDate>Mon, 06 May 2026 12:34:56 +0000</pubDate>
    <region>USA Only</region>
    <category>...</category>
  </item>

Title parsing: WWR's title format is "<Company>: <Job Title>" — split on
the first ": " to recover both fields.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


WWR_RSS_URL = "https://weworkremotely.com/remote-jobs.rss"


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _split_company_title(rss_title: str) -> tuple[str, str]:
    """WWR titles are 'Company: Job Title'. If there's no colon, treat the
    full string as title and leave company empty (downstream will skip)."""
    if ": " in rss_title:
        company, title = rss_title.split(": ", 1)
        return company.strip(), title.strip()
    return "", rss_title.strip()


class WeWorkRemotelyScraper(BaseScraper):
    source_name = "WeWorkRemotely"
    # Attribution surfaced via audit JSON (mirrors RemoteOK pattern). WWR's
    # ToS doesn't strictly require it for RSS consumers, but linking back is
    # good citizenship and the dashboard renders it as a source pill.
    attribution: str = "Powered by WeWorkRemotely — https://weworkremotely.com"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                WWR_RSS_URL,
                headers={
                    "Accept": "application/rss+xml, application/xml, text/xml",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; JobSearchApp/0.3.5)"
                    ),
                },
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        # RSS 2.0 — items live at /rss/channel/item.
        channel = root.find("channel")
        if channel is None:
            return []
        items = channel.findall("item")
        if not items:
            return []

        keywords_lower = [k.lower() for k in keywords]
        cutoff_iso: Optional[str] = None
        if posted_within_days is not None:
            from datetime import timedelta
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            ).isoformat()

        seen: set[str] = set()
        out: list[Role] = []
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}
        for it in items:
            try:
                role = self._item_to_role(it)
                if not role:
                    continue
                # Posted-date freshness filter
                if cutoff_iso and role.posted_date and role.posted_date < cutoff_iso:
                    continue
                # Keyword filter
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
                # per-keyword count (best-effort: count match per kw)
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

    def _item_to_role(self, item: ET.Element) -> Optional[Role]:
        title_raw = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title_raw or not url:
            return None

        company, title = _split_company_title(title_raw)
        company = self._normalize_company(company)
        if not title or not company:
            return None

        desc = item.findtext("description") or ""
        jd_text = _strip_html(desc) if "<" in desc else desc

        # Region — WWR puts location info in <region> or sometimes inside the
        # description's first line. Default to "Remote" for the location_type
        # since WWR is a 100% remote board.
        region = (item.findtext("region") or "").strip()
        loc_str = region or "Remote"
        location_type = "Remote"

        pub = _parse_pubdate(item.findtext("pubDate") or "")

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
            posted_date=pub,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

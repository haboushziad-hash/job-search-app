"""Jobicy scraper — free RSS feed of curated remote roles.

Jobicy is a remote-jobs board with a clean structured RSS feed including
employer-facing tags (job_type, company, location). Unlike many WordPress-
based job boards, Jobicy's feed is well-formed and includes per-item
structured data — easy to parse, no HTML scraping needed.

Endpoint: GET https://jobicy.com/feed/job_feed
  Returns: standard RSS 2.0 XML with extra elements:
    <item>
      <title>Job Title</title>
      <id>internal job ID</id>
      <link>https://jobicy.com/jobs/123-job-slug</link>
      <pubDate>Wed, 06 May 2026 11:59:03 +0000</pubDate>
      <description><![CDATA[ HTML body ]]></description>
      <encoded>...</encoded>
      <location>USA</location>
      <job_type>Full Time</job_type>
      <company>DoorDash</company>
    </item>

Feed supports query params for narrower fetches:
  &job_categories=design-multimedia
  &job_types=full-time
  &search_region=usa

We fetch the broad all-jobs feed once, parse, then apply client-side
keyword filter (same pattern as Remotive / WeWorkRemotely / Arbeitnow).

Free, no auth, no rate limits documented.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


JOBICY_RSS_URL = "https://jobicy.com/feed/job_feed"


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class JobicyScraper(BaseScraper):
    """Free RSS feed at jobicy.com/feed/job_feed. Curated remote jobs
    with structured per-item fields (job_type, company, location)."""

    source_name = "Jobicy"
    # Per Jobicy's docs page, attribution is appreciated but not strictly
    # required. We surface it via the audit JSON for tester transparency.
    attribution: str = "Powered by Jobicy — https://jobicy.com"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                JOBICY_RSS_URL,
                headers={
                    "Accept": "application/rss+xml, application/xml, text/xml",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; FindMeSomeDamnJobz/0.3.5; "
                        "+https://findmesomedamnjobz.com)"
                    ),
                },
                timeout=20.0,
            )
        except Exception:
            self.quota_exhausted = True
            self.quota_exhausted_reason = "Jobicy fetch error (transient)"
            return []
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Jobicy HTTP {resp.status_code} (rate limit or block)"
                )
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        keywords_lower = [k.lower() for k in keywords]
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        cutoff_iso = None
        if posted_within_days:
            from datetime import timedelta
            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=posted_within_days)).isoformat()

        roles: list[Role] = []
        seen_urls: set[str] = set()

        for item in channel.findall("item"):
            def _t(tag: str) -> str:
                el = item.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""

            title = _t("title")
            link = _t("link") or _t("guid")
            description = _t("description") or _t("encoded")
            pubdate_raw = _t("pubDate")
            location = _t("location")
            company = _t("company")
            job_type = _t("job_type")

            if not title or not link:
                continue
            if link in seen_urls:
                continue

            posted_iso = _parse_pubdate(pubdate_raw)
            if cutoff_iso and posted_iso and posted_iso < cutoff_iso:
                continue

            jd_clean = _strip_html(description)

            # Match against the supplied keywords. Jobicy is a broad source —
            # we fetch the full feed and let the matcher filter to roles
            # actually relevant to the candidate.
            if not _kw_match.matches_any_keyword(title, jd_clean, keywords_lower):
                continue
            # Identify which keyword matched (for per_keyword_raw_counts)
            matched_kw: Optional[str] = None
            for kw_raw, kw_low in zip(keywords, keywords_lower):
                if not kw_low:
                    continue
                if _kw_match.matches_any_keyword(title, jd_clean, [kw_low]):
                    matched_kw = kw_raw
                    per_kw_count[kw_raw] += 1
                    break

            seen_urls.add(link)
            roles.append(Role(
                job_title=title[:200],
                company=(self._normalize_company(company) or "(unknown)")[:120],
                source=self.source_name,
                primary_source=self.source_name,
                job_url=link,
                location=location[:100] if location else None,
                location_type="Remote",  # Jobicy is remote-only by design
                job_description_full=jd_clean[:8000] if jd_clean else "",
                jd_completeness="Full" if len(jd_clean) > 500 else "Partial",
                posted_date=posted_iso,
                matched_keyword=matched_kw,
                employment_type=job_type or None,
            ))

        self.per_keyword_raw_counts = per_kw_count
        return roles

    async def fetch_jd(self, role: Role) -> str:
        """JD already inline in RSS — no separate fetch needed."""
        return role.job_description_full or ""

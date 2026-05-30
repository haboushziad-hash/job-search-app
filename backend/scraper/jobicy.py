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

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match
from backend.scraper._exception_helpers import handle_scraper_exception


JOBICY_RSS_URL = "https://jobicy.com/feed/job_feed"

NS = {
    "jl": "https://jobicy.com",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

# 21 valid Jobicy category feeds — fan out in parallel to widen coverage
# (the default all-jobs feed only returns the most-recent slice).
JOBICY_CATEGORIES = [
    "admin-support", "business", "copywriting", "design-multimedia",
    "supporting", "data-science", "admin", "education",
    "accounting-finance", "healthcare", "hr", "legal",
    "marketing", "management", "dev", "seller",
    "seo", "smm", "engineering", "technical-support", "web-app-design",
]


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
        keywords_lower = [k.lower() for k in keywords]
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        cutoff_iso = None
        if posted_within_days:
            from datetime import timedelta
            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=posted_within_days)).isoformat()

        roles: list[Role] = []
        seen_urls: set[str] = set()

        # Fan out across all 21 category feeds in parallel (cap concurrency
        # at 4 to be polite). Scrape ONCE at start — keyword matching is
        # applied client-side after parsing, so we don't re-fetch per kw.
        sem = asyncio.Semaphore(4)

        async def _fetch_category(category: str) -> str:
            async with sem:
                url = f"{JOBICY_RSS_URL}?job_categories={category}"
                try:
                    resp = await self.client._client.get(  # type: ignore[union-attr]
                        url,
                        headers={
                            "Accept": "application/rss+xml, application/xml, text/xml",
                            "User-Agent": (
                                "Mozilla/5.0 (compatible; FindMeSomeDamnJobz/0.3.5; "
                                "+https://findmesomedamnjobz.com)"
                            ),
                        },
                        timeout=20.0,
                    )
                except Exception as _e:
                    # Wave-2B Phase 2 (FIX 15): surface via shared helper
                    # instead of silently returning "". Sets
                    # quota_exhausted on 429/403/503.
                    handle_scraper_exception(
                        self, "Jobicy", _e, context=f"category={category}"
                    )
                    return ""
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"Jobicy HTTP {resp.status_code} (rate limit or block)"
                        )
                    return ""
                return resp.text

        feed_bodies = await asyncio.gather(
            *(_fetch_category(cat) for cat in JOBICY_CATEGORIES)
        )

        for body in feed_bodies:
            if not body:
                continue
            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                continue

            channel = root.find("channel")
            if channel is None:
                continue

            for item in channel.findall("item"):
                def _t(tag: str) -> str:
                    el = item.find(tag)
                    return (el.text or "").strip() if el is not None and el.text else ""

                title = _t("title")
                link = _t("link") or _t("guid")
                encoded_el = item.find("content:encoded", NS)
                encoded_text = (encoded_el.text or "").strip() if encoded_el is not None and encoded_el.text else ""
                # Wave-2B Phase 2 (FIX 2): swap order — prefer the full
                # content:encoded body (~7,300 chars) over the truncated
                # <description> teaser (~209 chars). Keyword matcher works
                # against full JD now; previously only the teaser was visible
                # and most roles were silently filtered out.
                description = encoded_text or _t("description")
                pubdate_raw = _t("pubDate")
                location_el = item.find("jl:location", NS)
                location = (location_el.text or "").strip() if location_el is not None and location_el.text else ""
                company_el = item.find("jl:company", NS)
                company = (company_el.text or "").strip() if company_el is not None and company_el.text else ""
                job_type_el = item.find("jl:job_type", NS)
                job_type = (job_type_el.text or "").strip() if job_type_el is not None and job_type_el.text else ""

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

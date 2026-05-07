"""HigherEdJobs scraper — free RSS feeds per category.

HigherEdJobs is the premier higher-education jobs board. Strong source for
testers in admin / ops / IT / research / advancement / HR roles at colleges
and universities — segments mostly absent from corporate ATS scrapers.

Each category has its own RSS feed:
  https://www.higheredjobs.com/rss/categoryFeed.cfm?catID=<ID>

The RSS feed entries are slim — title is the job title, description is
"<Institution> (<City, ST>)". No JD body inline; the JD lives at the
linked URL (downstream JD fetch can pull it later if needed).

Strategy: fetch a small set of categories likely to be relevant across
testers (admin, HR, IT, research, library, marketing, executive, finance,
career services, online ed). Apply client-side keyword filter on the
title (and the institution+location text in description, since some
keywords like "Manager" / "Specialist" can match those weakly).

Per-search cost: 10 RSS fetches × ~1KB each = trivial.
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper import _keyword_match as _kw_match


# Curated set of HigherEd category IDs likely to surface roles for testers
# in administrative / operational / non-faculty paths. Faculty/teaching
# categories are intentionally omitted — they require domain PhDs and
# don't match any of our 9 fixture personas.
HIGHERED_CATEGORIES: list[tuple[int, str]] = [
    (22,  "Career Development and Services"),
    (47,  "Marketing and Sales"),
    (203, "Human Resources"),
    (207, "Other Executive"),
    (220, "Online and Distance Education Programs"),
    (215, "Sales"),
    (129, "Public Relations and Advertising"),
    (36,  "Arts and Museum Administration"),
    (68,  "Higher Education (general)"),
    (227, "Other Administrative"),
]


def _category_url(cat_id: int) -> str:
    return f"https://www.higheredjobs.com/rss/categoryFeed.cfm?catID={cat_id}"


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _split_institution_location(desc: str) -> tuple[str, str]:
    """Description is '<Institution> (City, ST)'. Recover both."""
    if not desc:
        return ("", "")
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", desc.strip())
    if m:
        return (m.group(1).strip(), m.group(2).strip())
    return (desc.strip(), "")


class HigherEdJobsScraper(BaseScraper):
    source_name = "HigherEdJobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        sem = asyncio.Semaphore(4)

        async def fetch_cat(cat_id: int, cat_name: str) -> list[ET.Element]:
            async with sem:
                try:
                    resp = await asyncio.wait_for(
                        self.client._client.get(  # type: ignore[union-attr]
                            _category_url(cat_id),
                            headers={
                                "Accept": "application/rss+xml, application/xml, text/xml",
                                "User-Agent": (
                                    "Mozilla/5.0 (compatible; JobSearchApp/0.3.5)"
                                ),
                            },
                        ),
                        timeout=15.0,
                    )
                except Exception:
                    return []
                if resp.status_code != 200:
                    return []
                try:
                    root = ET.fromstring(resp.text)
                except ET.ParseError:
                    return []
                channel = root.find("channel")
                if channel is None:
                    return []
                return list(channel.findall("item"))

        tasks = [fetch_cat(cid, cname) for cid, cname in HIGHERED_CATEGORIES]
        per_cat = await asyncio.gather(*tasks, return_exceptions=True)

        keywords_lower = [k.lower() for k in keywords]
        cutoff: Optional[datetime] = None
        if posted_within_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)

        seen_url: set[str] = set()
        out: list[Role] = []
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}
        for items in per_cat:
            if isinstance(items, BaseException):
                continue
            for it in items:
                try:
                    role = self._item_to_role(it)
                    if not role:
                        continue
                    if cutoff and role.posted_date:
                        try:
                            d = datetime.fromisoformat(
                                role.posted_date.replace("Z", "+00:00")
                            )
                            if d < cutoff:
                                continue
                        except ValueError:
                            pass
                    # Match against title + institution+location
                    text_for_match = (
                        (role.job_title or "") + " "
                        + (role.job_description_full or "")
                    )
                    if not _kw_match.matches_any_keyword(
                        role.job_title or "",
                        text_for_match,
                        keywords_lower,
                    ):
                        continue
                    if role.job_url and role.job_url in seen_url:
                        continue
                    if role.job_url:
                        seen_url.add(role.job_url)
                    out.append(role)
                    for kw in keywords:
                        if _kw_match.matches_any_keyword(
                            role.job_title or "",
                            text_for_match,
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
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if not title or not url:
            return None

        institution, loc_str = _split_institution_location(desc)
        company = self._normalize_company(institution)
        if not company:
            return None

        location_type, _ = self._classify_location(loc_str)

        pub = _parse_pubdate(item.findtext("pubDate") or "")

        # JD body absent in feed — keep description as the institution
        # blurb so token-overlap can hit institution-name keywords. This
        # is intentionally Partial; downstream JD fetch can backfill.
        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            job_description_full=desc[:1000],
            jd_completeness="Partial" if desc else "Missing",
            posted_date=pub,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

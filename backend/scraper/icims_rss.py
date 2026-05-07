"""iCIMS RSS scraper — lightweight HTTP alternative to the Playwright path.

Most iCIMS-hosted public job boards expose an RSS feed at:
  https://careers-{subdomain}.icims.com/jobs/search/rss?in_iframe=1

This is dramatically cheaper than the Playwright path (no browser, no JS
execution — just one HTTP fetch per tenant + RSS parse). When an RSS feed
is available it should be used preferentially; the Playwright scraper is
the fallback for tenants whose iCIMS theme doesn't expose RSS.

Per-tenant cost: ~200-400ms (vs Playwright's ~10s/tenant). Lets us cover
many more iCIMS tenants without blowing the per-search budget.

Tenants (probed 2026-05-07 — see scripts/probe_icims_rss.py):
  - Liberty Mutual, Six Flags, Cedar Fair, Snap-on, Chick-fil-A
    (already live via Playwright; RSS path complements as a fast fallback)
  - PetSmart, Dollar Tree, Pep Boys, BJ's Wholesale
    (added v0.3.5 — retail/CPG fixtures missed these on Workday/Greenhouse)
"""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


# Display name + iCIMS subdomain. The subdomain is the slug between
# `careers-` and `.icims.com` in the public board URL.
#
# v0.3.5 PROBE RESULT (2026-05-07):
# scripts/probe_icims_rss.py probed 22 candidate tenants across the
# Playwright-verified set + retail/CPG/convenience expansion targets.
# RESULT: 0 working RSS feeds. Modern iCIMS deployments no longer
# expose `/jobs/search/rss` (it 302-redirects to the SPA HTML page) and
# none of the alternative paths probed returned XML. The Playwright
# scraper remains the only working path for iCIMS-hosted boards.
#
# This file + scraper are kept in place so that if iCIMS restores RSS
# in a future product release, we can re-enable by populating this list
# without rewriting the scraper. The orchestrator currently registers
# iCIMSRSS with an empty list — the scraper safely no-ops (returns []
# from `search()` when there are no tenants to probe).
ICIMS_RSS_TENANTS: list[tuple[str, str]] = [
    # Empty — see header note. Re-populate from probe output when iCIMS
    # restores public RSS feeds. Format: ("Display Name", "subdomain").
]


def _rss_url(subdomain: str) -> str:
    return (
        f"https://careers-{subdomain}.icims.com/jobs/search/rss?in_iframe=1"
    )


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _split_title(rss_title: str) -> tuple[str, str]:
    """iCIMS RSS titles often look like 'Job Title - Location' or
    'Job Title - Department - Location'. Pull the first segment as the
    job title; the rest is auxiliary metadata embedded in the title.
    """
    if not rss_title:
        return ("", "")
    # Use ' - ' as the separator (with surrounding spaces) to avoid
    # breaking titles like 'Senior IT-Level Architect'.
    parts = re.split(r"\s+-\s+", rss_title)
    title = parts[0].strip()
    aux = " - ".join(p.strip() for p in parts[1:])
    return (title, aux)


class ICIMSRSSScraper(BaseScraper):
    """iCIMS RSS feed reader — cross-tenant HTTP scraper."""

    source_name = "iCIMSRSS"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        sem = asyncio.Semaphore(6)

        async def fetch_tenant(display: str, sub: str) -> list[Role]:
            async with sem:
                try:
                    resp = await asyncio.wait_for(
                        self.client._client.get(  # type: ignore[union-attr]
                            _rss_url(sub),
                            headers={
                                "Accept": "application/rss+xml, application/xml, text/xml",
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0"
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
                items = channel.findall("item")
                out: list[Role] = []
                for it in items:
                    role = self._item_to_role(it, display)
                    if role:
                        out.append(role)
                return out

        tasks = [fetch_tenant(display, sub) for display, sub in ICIMS_RSS_TENANTS]
        per_tenant = await asyncio.gather(*tasks, return_exceptions=True)

        # Apply post-fetch keyword filter + cross-tenant dedup
        from datetime import timedelta
        cutoff: Optional[datetime] = None
        if posted_within_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)

        keywords_lower = [k.lower() for k in keywords]
        seen: set[str] = set()
        out: list[Role] = []
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}
        for roles in per_tenant:
            if isinstance(roles, BaseException):
                continue
            for role in roles or []:
                if cutoff and role.posted_date:
                    try:
                        d = datetime.fromisoformat(
                            role.posted_date.replace("Z", "+00:00")
                        )
                        if d < cutoff:
                            continue
                    except ValueError:
                        pass
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
                for kw in keywords:
                    if _kw_match.matches_any_keyword(
                        role.job_title or "",
                        role.job_description_full or "",
                        [kw.lower()],
                    ):
                        per_kw_count[kw] = per_kw_count.get(kw, 0) + 1

        self.per_keyword_raw_counts = per_kw_count
        return out

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

    def _item_to_role(self, item: ET.Element, display: str) -> Optional[Role]:
        title_raw = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title_raw or not url:
            return None

        title, aux = _split_title(title_raw)
        if not title:
            return None

        desc_raw = (item.findtext("description") or "").strip()
        jd_text = _strip_html(desc_raw) if "<" in desc_raw else desc_raw

        # iCIMS often embeds location in the title aux. Default to None
        # (downstream filters will pick it up from JD body when relevant).
        location_raw = aux or None
        location_type, _ = self._classify_location(location_raw or "")

        pub = _parse_pubdate(item.findtext("pubDate") or "")

        return Role(
            job_title=title[:200],
            company=display[:120],
            job_url=url,
            location=location_raw,
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

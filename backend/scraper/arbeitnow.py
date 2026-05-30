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
import re
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


ARBEITNOW_API = "https://www.arbeitnow.com/api/job-board-api"

# US state abbreviations + common US city markers — used to short-circuit
# the source for US-only searches (Arbeitnow is overwhelmingly EU/German).
_US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI",
    "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_US_CITY_MARKERS = (
    "united states", "usa", "u.s.a", "u.s.", " us", "washington dc",
    "new york", "san francisco", "los angeles", "chicago", "boston",
    "seattle", "austin", "atlanta", "denver", "houston", "dallas",
    "miami", "philadelphia", "phoenix",
)
# German-language job-posting markers — present in ~100% of German listings.
_GERMAN_MARKERS = (
    "m/w/d", "w/m/d", "m/f/d", "werkstudent", "mitarbeiter",
    "gmbh", "großraum",
)


def _looks_us_location(text: str) -> bool:
    """Best-effort detection of a US-based location string."""
    if not text:
        return False
    low = text.lower()
    if any(marker in low for marker in _US_CITY_MARKERS):
        return True
    # Token scan for two-letter state codes (e.g. "Arlington, VA").
    # Split on non-alphanumeric so "Washington, DC" and "DC" both match.
    tokens = re.findall(r"[A-Za-z]{2,}", text)
    for tok in tokens:
        if len(tok) == 2 and tok.upper() in _US_STATE_ABBR:
            return True
    return False


class ArbeitnowScraper(BaseScraper):
    source_name = "Arbeitnow"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Short-circuit: ~93% of Arbeitnow listings are German-language. If
        # the user is searching for a US-based onsite/hybrid role, skip this
        # source entirely (remote_only users still benefit from EU-remote
        # postings that may be globally open).
        uf = getattr(self, "_user_filters", None) or {}
        loc_text = (uf.get("location_text") or "").strip()
        if loc_text and uf.get("remote_only") is not True:
            if _looks_us_location(loc_text):
                return []

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
            # Filter out German-language postings — these markers appear
            # in ~100% of German JDs and are useless for English-speaking
            # users. "m/w/d" etc. are German gender-neutral job suffixes;
            # "werkstudent" / "mitarbeiter" / "gmbh" / "großraum" are
            # ubiquitous German job-posting vocabulary.
            desc_raw = (j.get("description") or "")
            scan_text = (title + " " + desc_raw[:200]).lower()
            if any(marker in scan_text for marker in _GERMAN_MARKERS):
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

"""GovernmentJobs.com (NEOGOV) scraper — state & local government roles.

Closes the public-sector coverage gap that USAJOBS (federal-only) leaves open.
State/county/city governments post a huge volume of admin, finance, IT, ops,
public-health, social-services, and skilled-trades roles here — segments our
corporate-ATS scrapers (Greenhouse/Lever/Workday) under-serve, and a frequent
destination for the "obscure / general" personas (e.g. a lab admin moving to a
public hospital, an accountant moving to a county finance office).

Ingestion: NEOGOV exposes a per-agency RSS feed (the only public path — there is
NO global keyword/location search endpoint; probed 2026-06-03):

    GET https://www.governmentjobs.com/SearchEngine/JobsFeed?agency=<slug>

Each <item> is rich: title, full HTML JD inline (no separate JD fetch needed),
structured salary (min/max/interval/currency), jobType, category, post date,
canonical URL. We curate a list of high-volume agencies (the Workday-tenant
pattern — starts ~30, grows over time) and keyword-filter client-side.

No API key, no cost. Heavier than single-feed boards (one fetch per agency) but
runs concurrently with the rest of the scrape and is free.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match

_JL = "{http://www.neogov.com/namespaces/JobListing}"
_FEED = "https://www.governmentjobs.com/SearchEngine/JobsFeed?agency={slug}"

# Agency map: slug -> (display_name, location, state). Loaded from the curated
# governmentjobs_agencies.json (87 agencies across 35 states, validated live
# 2026-06-03 by scripts/.gen_neogov_map.py), with a hardcoded fallback so the
# scraper never breaks if the file is missing. Expandable like the Workday
# tenant list. Because GovernmentJobs has NO global search endpoint (per-agency
# RSS only), we fetch one feed per agency — so we select agencies by the user's
# region (location-aware) to keep latency bounded as the list grows.
_FALLBACK_AGENCIES: dict[str, tuple[str, str, str]] = {
    "lacounty": ("County of Los Angeles", "Los Angeles, CA", "CA"),
    "sandiego": ("City of San Diego", "San Diego, CA", "CA"),
    "seattle": ("City of Seattle", "Seattle, WA", "WA"),
    "kingcounty": ("King County", "Seattle, WA", "WA"),
    "denver": ("City and County of Denver", "Denver, CO", "CO"),
    "richmond": ("City of Richmond", "Richmond, VA", "VA"),
    "fairfaxcounty": ("Fairfax County", "Fairfax, VA", "VA"),
    "norfolk": ("City of Norfolk", "Norfolk, VA", "VA"),
    "houston": ("City of Houston", "Houston, TX", "TX"),
    "atlanta": ("City of Atlanta", "Atlanta, GA", "GA"),
    "northcarolina": ("State of North Carolina", "North Carolina", "NC"),
    "washington": ("State of Washington", "Washington", "WA"),
}


def _load_agencies() -> dict[str, tuple[str, str, str]]:
    import json as _json
    from pathlib import Path as _Path
    out = dict(_FALLBACK_AGENCIES)
    try:
        p = _Path(__file__).resolve().parent / "governmentjobs_agencies.json"
        data = _json.loads(p.read_text(encoding="utf-8"))
        for slug, v in data.items():
            if isinstance(v, dict) and v.get("name"):
                out[slug] = (v["name"], v.get("location") or v["name"], (v.get("state") or "").upper())
    except Exception:
        pass
    return out


AGENCIES: dict[str, tuple[str, str, str]] = _load_agencies()

# When a search has no usable location signal (remote / unknown), fetch this
# bounded default set (big metros + populous states) instead of all 87 feeds.
# Govt roles are inherently location-bound, so a no-location user gets little
# from per-region agencies anyway.
_DEFAULT_SLUGS = [
    "lacounty", "sandiego", "santaclara", "riverside", "seattle", "kingcounty",
    "denver", "colorado", "richmond", "fairfaxcounty", "houston", "dallas",
    "atlanta", "boston", "detroit", "indianapolis", "lasvegas", "clarkcounty",
    "northcarolina", "washington", "utah", "nashville", "honolulu", "broward",
]

_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
_STATE_CODES = set(_STATE_NAME_TO_CODE.values()) | {"DC"}


def _states_in(text: str) -> set[str]:
    """Extract US state codes mentioned in a location string (2-letter codes + names)."""
    import re as _re
    if not text:
        return set()
    found: set[str] = set()
    low = text.lower()
    # "Washington DC" means the District, NOT Washington State.
    is_dc = bool(_re.search(r"\bd\.?c\.?\b", low)) or "district of columbia" in low
    up = text.upper()
    for c in _STATE_CODES:
        if c == "WA" and is_dc and "washington state" not in low and "state of washington" not in low:
            continue
        if _re.search(r"\b" + c + r"\b", up):
            found.add(c)
    for name, code in _STATE_NAME_TO_CODE.items():
        if name == "washington" and is_dc and "washington state" not in low and "state of washington" not in low:
            continue
        if _re.search(r"\b" + _re.escape(name) + r"\b", low):
            found.add(code)
    if is_dc:
        found.add("DC")
    return found

_INTERVAL_MULT = {  # annualize salary by interval
    "year": 1, "annual": 1, "annually": 1, "yearly": 1,
    "month": 12, "monthly": 12,
    "biweekly": 26, "bi-weekly": 26,
    "week": 52, "weekly": 52,
    "hour": 2080, "hourly": 2080,
    "day": 260, "daily": 260,
}

_MAX_ITEMS_PER_AGENCY = 600   # bound the 7MB+ feeds (LA County) so one agency can't dominate


class GovernmentJobsScraper(BaseScraper):
    """GovernmentJobs.com / NEOGOV — state & local government roles (free, no key)."""

    source_name = "GovernmentJobs"
    attribution = "Powered by GovernmentJobs.com (NEOGOV)"

    def _select_agencies(self) -> dict[str, tuple[str, str, str]]:
        """Location-aware agency selection. If the user's location names a state,
        fetch only that state's agencies; otherwise fall back to a bounded default
        set. Keeps per-search feed fetches small even as the agency list grows."""
        uf = self._user_filters or {}
        states = _states_in(uf.get("location_text") or "")
        if "DC" in states:   # DC metro spans DC + VA + MD suburbs
            states |= {"VA", "MD"}
        if states:
            sel = {s: v for s, v in AGENCIES.items() if v[2] in states}
            if sel:
                return sel
        return {s: AGENCIES[s] for s in _DEFAULT_SLUGS if s in AGENCIES}

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        headers = {
            "Accept": "application/rss+xml, application/xml, text/xml",
            "User-Agent": "Mozilla/5.0 (compatible; JobSearchApp/0.3.30)",
        }
        cutoff: Optional[datetime] = None
        if posted_within_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=posted_within_days)

        keywords_lower = [k.lower() for k in keywords]
        sem = asyncio.Semaphore(8)

        async def _fetch(slug: str, name: str, location: str) -> list[Role]:
            async with sem:
                try:
                    resp = await self.client._client.get(  # type: ignore[union-attr]
                        _FEED.format(slug=slug), headers=headers, timeout=45.0,
                    )
                except Exception:
                    return []
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = f"GovernmentJobs HTTP {resp.status_code}"
                    return []
                return self._parse_feed(resp.text, name, location, keywords_lower, cutoff)

        selected = self._select_agencies()
        batches = await asyncio.gather(
            *[_fetch(slug, name, loc) for slug, (name, loc, _st) in selected.items()]
        )

        seen: set[str] = set()
        out: list[Role] = []
        per_kw: dict[str, int] = {kw: 0 for kw in keywords}
        for batch in batches:
            for role in batch:
                if not role.job_url or role.job_url in seen:
                    continue
                seen.add(role.job_url)
                out.append(role)
                for kw in keywords:
                    if _kw_match.matches_any_keyword(
                        role.job_title or "", role.job_description_full or "", [kw.lower()]
                    ):
                        per_kw[kw] = per_kw.get(kw, 0) + 1
        self.per_keyword_raw_counts = per_kw
        return out

    def _parse_feed(
        self, text: str, agency_name: str, location: str,
        keywords_lower: list[str], cutoff: Optional[datetime],
    ) -> list[Role]:
        try:
            root = ET.fromstring(text.lstrip("﻿"))
        except Exception:
            return []
        out: list[Role] = []
        for i, item in enumerate(root.iter("item")):
            if i >= _MAX_ITEMS_PER_AGENCY:
                break
            try:
                title = (item.findtext("title") or "").strip()
                url = (item.findtext("link") or item.findtext("guid") or "").strip()
                if not title or not url:
                    continue
                desc_raw = item.findtext("description") or ""
                jd = _strip_html(desc_raw) if "<" in desc_raw else desc_raw

                # Keyword filter (title + JD) — client-side, like the remote boards.
                if not _kw_match.matches_any_keyword(title, jd, keywords_lower):
                    continue

                posted_iso: Optional[str] = None
                pub = item.findtext("pubDate")
                if pub:
                    try:
                        dt = parsedate_to_datetime(pub)
                        if dt is not None:
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            if cutoff is not None and dt < cutoff:
                                continue
                            posted_iso = dt.date().isoformat()
                    except Exception:
                        pass

                smin, smax = self._salary(item)
                loc_type, loc_norm = self._classify_location(location)
                out.append(Role(
                    job_title=title[:200],
                    company=self._normalize_company(agency_name)[:120],
                    job_url=url,
                    location=loc_norm or location,
                    location_type=loc_type,
                    job_description_full=jd[:8000],
                    jd_completeness=("Full" if len(jd) > 500 else ("Partial" if jd else "Missing")),
                    salary_min=smin,
                    salary_max=smax,
                    posted_date=posted_iso,
                    primary_source=self.source_name,
                    date_first_seen=datetime.now(timezone.utc).date().isoformat(),
                ))
            except Exception:
                continue
        return out

    def _salary(self, item) -> tuple[Optional[int], Optional[int]]:
        cur = (item.findtext(_JL + "salaryCurrency") or "USD").strip().upper()
        if cur not in ("USD", "", "US", "$"):
            return (None, None)
        interval = (item.findtext(_JL + "salaryInterval") or "year").strip().lower()
        mult = _INTERVAL_MULT.get(interval, 0)
        if not mult:
            return (None, None)

        def _num(tag: str) -> Optional[float]:
            raw = (item.findtext(_JL + tag) or "").strip().replace(",", "").replace("$", "")
            try:
                v = float(raw)
                return v if v > 0 else None
            except (ValueError, TypeError):
                return None

        lo, hi = _num("minimumSalary"), _num("maximumSalary")
        smin = int(lo * mult) if lo else None
        smax = int(hi * mult) if hi else None
        # Sanity bounds: drop implausible annualized values (bad interval data).
        if smin and (smin < 5000 or smin > 2_000_000):
            smin = None
        if smax and (smax < 5000 or smax > 2_000_000):
            smax = None
        return (smin, smax)

    async def fetch_jd(self, role: Role) -> str:
        # JD body is inline in the feed; nothing to fetch.
        return role.job_description_full or ""

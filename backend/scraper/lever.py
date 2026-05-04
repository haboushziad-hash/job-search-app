"""Lever scraper.

Lever exposes a public JSON API per company:
  https://api.lever.co/v0/postings/{company_slug}?mode=json

Returns: array of {id, text, hostedUrl, applyUrl, categories: {team, location, commitment},
                   description, descriptionPlain, ...}

Strategy: same as Greenhouse — maintain a curated list of companies and
hit each in parallel.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


# Curated list of companies known to use Lever for hiring.
# Expand as we discover more — easy to maintain.
LEVER_COMPANIES: list[tuple[str, str]] = [
    # (display_name, slug)
    # Re-probed 2026-05-04: original prune was too aggressive (probe script
    # didn't distinguish "endpoint live with 0 current postings" from "endpoint
    # 404"). Re-added Palantir (live, 235 jobs), Netflix and KPMG and Plaid
    # (endpoints live with 0 current jobs but may post). Keeping all roles
    # below for periodic re-probe.
    ("Binance",     "binance"),     # 383 jobs - crypto exchange
    ("Palantir",    "palantir"),    # 235 jobs - data/intel
    ("Spotify",     "spotify"),     # 196 jobs - streaming/audio
    ("Whoop",       "whoop"),       # 169 jobs - fitness/wearables
    ("Mistral AI",  "mistral"),     # 161 jobs - AI/EU
    ("Ro",          "ro"),          #  48 jobs - telehealth
    ("Outreach",    "outreach"),    #  30 jobs - sales SaaS
    ("Houzz",       "houzz"),       #  11 jobs - home/design marketplace
    ("Highspot",    "highspot"),    #   6 jobs - sales enablement
    ("Clari",       "clari"),       #   6 jobs - revenue ops
    ("Skillshare",  "skillshare"),  #   2 jobs - education/learning
    # Endpoints live but currently 0 jobs (kept in case they post)
    ("Netflix",     "netflix"),     # 0 current jobs but endpoint live
    ("KPMG",        "kpmg"),        # 0 current jobs but endpoint live
    ("Plaid",       "plaid"),       # 0 current jobs but endpoint live
]


class LeverScraper(BaseScraper):
    source_name = "Lever"
    BASE_URL = "https://api.lever.co/v0/postings"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            if posted_within_days else None
        )
        keywords_lower = [k.lower() for k in keywords]

        tasks = [
            self._fetch_company_jobs(slug, display_name)
            for display_name, slug in LEVER_COMPANIES
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
                if cutoff and role.posted_date:
                    try:
                        # Lever returns createdAt as Unix ms timestamp
                        ts_ms = int(role.posted_date)
                        posted = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                        if posted < cutoff:
                            continue
                    except (TypeError, ValueError):
                        pass
                # Token-overlap match (shared with all other scrapers via
                # _keyword_match.py). Replaces substring matching that
                # missed near-matches like "AI Enablement Services Lead"
                # for keyword "AI Enablement Lead".
                if not _kw_match.matches_any_keyword(
                    role.job_title or "",
                    role.job_description_full or "",
                    keywords_lower,
                ):
                    continue
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_title_company.add(key)
                all_roles.append(role)

        return all_roles

    async def _fetch_company_jobs(self, slug: str, display_name: str) -> list[Role]:
        url = f"{self.BASE_URL}/{slug}"
        try:
            data = await self.client.get_json(url, params={"mode": "json"})
        except Exception:
            return []

        if not isinstance(data, list):
            return []

        roles: list[Role] = []
        for j in data:
            try:
                roles.append(self._job_to_role(j, display_name))
            except Exception:
                continue
        return roles

    def _job_to_role(self, job: dict[str, Any], company: str) -> Role:
        title = job.get("text") or ""
        url = job.get("hostedUrl") or job.get("applyUrl") or ""
        categories = job.get("categories") or {}
        location_text = categories.get("location") or ""
        commitment = categories.get("commitment") or ""
        team = categories.get("team") or ""
        location_type, location = self._classify_location(location_text)

        # Lever returns descriptionPlain (clean text) and description (HTML)
        jd_text = job.get("descriptionPlain") or _strip_html(job.get("description") or "")

        # Add lists section content (responsibilities / requirements)
        for lst in (job.get("lists") or []):
            content = lst.get("content") or ""
            if content:
                jd_text += "\n\n" + (lst.get("text") or "") + ":\n" + _strip_html(content)

        # Salary often hidden; some companies expose via salaryRange
        salary_text = None
        salary_min = None
        salary_max = None
        sr = job.get("salaryRange") or {}
        if sr:
            mn = sr.get("min")
            mx = sr.get("max")
            cur = sr.get("currency") or "USD"
            interval = sr.get("interval") or "year"
            if mn or mx:
                salary_min = int(mn) if mn is not None else None
                salary_max = int(mx) if mx is not None else None
                if mn and mx:
                    salary_text = f"${salary_min:,} - ${salary_max:,} {cur}/{interval}"

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location,
            location_type=location_type,
            job_description_full=jd_text,
            jd_completeness="Full" if jd_text else "Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=str(job.get("createdAt") or ""),  # Unix ms; converted in search()
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
            seniority_level=self._infer_seniority(title, commitment, team),
        )

    @staticmethod
    def _infer_seniority(title: str, commitment: str, team: str) -> Optional[str]:
        t = title.lower()
        if any(s in t for s in ("intern", "junior")):
            return "Junior"
        if any(s in t for s in ("vp ", "vice president", "head of", "chief")):
            return "VP"
        if "director" in t:
            return "Director"
        if any(s in t for s in ("principal", "staff", "lead", "senior", "sr.")):
            return "Senior"
        if "manager" in t:
            return "Manager"
        return None

    async def fetch_jd(self, role: Role) -> str:
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            response = await self.client.get(role.job_url)
            return _strip_html(response.text)
        except Exception:
            return ""

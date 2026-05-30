"""Climatebase scraper — climate / sustainability / clean-energy jobs.

Climatebase is THE job board for climate-aligned employers. ~5K-15K active
postings across:
  - Clean energy (solar, wind, storage, grid)
  - Sustainable agriculture / food systems
  - Carbon removal / climate tech
  - Climate finance / ESG
  - Sustainability consulting
  - Environmental science / research
  - Conservation / wildlife / forestry
  - Climate policy / advocacy / nonprofit

Strategy: their SSR Next.js page exposes the first 100 search results in
``__NEXT_DATA__`` (no API key needed). We parse that JSON directly.

Critical for environmental-research and climate-aligned testers — these
employers are largely invisible to Greenhouse/Lever/Workday scrapers.

URL: https://climatebase.org/jobs?q={keyword}
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper


CLIMATEBASE_URL = "https://climatebase.org/jobs"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


class ClimatebaseScraper(BaseScraper):
    """Scrapes Climatebase's public job board via Next.js SSR data."""

    source_name = "Climatebase"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        sem = asyncio.Semaphore(3)

        async def bounded(kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    # Network slowness / Climatebase SSR hanging — distinct
                    # from API quota issues. Surface it so the orchestrator
                    # silent-zero alert can distinguish "we never heard back"
                    # from "we got a 0-result response".
                    self.quota_exhausted = True
                    self.quota_exhausted_reason = (
                        f"Climatebase timeout (>20s) on keyword '{kw}'"
                    )
                    return []
                except Exception as e:
                    # Unexpected error — bubble the type into the reason so
                    # the audit JSON shows something more useful than 0 roles.
                    self.quota_exhausted = True
                    self.quota_exhausted_reason = (
                        f"Climatebase unexpected error on '{kw}': "
                        f"{type(e).__name__}: {str(e)[:80]}"
                    )
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen: set[int] = set()
        out: list[Role] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            for role in r or []:
                # Dedupe by URL since titles can repeat across employers
                if role.job_url and role.job_url in seen:
                    continue
                if role.job_url:
                    seen.add(role.job_url)
                out.append(role)
        return out

    async def _search_keyword(self, keyword: str, limit: int) -> list[Role]:
        params = {"q": keyword}
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                CLIMATEBASE_URL, params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
                    "Accept": "text/html",
                },
            )
        except Exception as e:
            # Network/transport failure (DNS, TLS, connection reset). Not the
            # same as an HTTP error — server never replied. Surface it so the
            # orchestrator can distinguish from quota issues.
            self.quota_exhausted = True
            self.quota_exhausted_reason = (
                f"Climatebase network error on '{keyword}': "
                f"{type(e).__name__}: {str(e)[:80]}"
            )
            return []
        if resp.status_code != 200:
            # Distinguish 429/403 (rate-limit/block — true quota signal) from
            # other 4xx/5xx (transient or upstream regression). Both set
            # quota_exhausted so the orchestrator silent-zero alert can fire,
            # but the reason text differentiates them in the audit JSON.
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Climatebase HTTP {resp.status_code} on '{keyword}' "
                    "(rate limit or anti-bot block)"
                )
            elif 500 <= resp.status_code < 600:
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Climatebase HTTP {resp.status_code} on '{keyword}' "
                    "(server error)"
                )
            else:
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Climatebase HTTP {resp.status_code} on '{keyword}'"
                )
            return []

        m = NEXT_DATA_RE.search(resp.text or "")
        if not m:
            # SSR page returned 200 but no __NEXT_DATA__ blob — likely a
            # site redesign or a Cloudflare interstitial. This is a scraper
            # regression signal that warrants attention, not a no-results
            # condition.
            self.quota_exhausted = True
            self.quota_exhausted_reason = (
                f"Climatebase missing __NEXT_DATA__ on '{keyword}' "
                "(possible site redesign or anti-bot interstitial)"
            )
            return []
        try:
            data = json.loads(m.group(1))
        except Exception as e:
            # __NEXT_DATA__ present but unparseable — also a regression
            # signal (truncated body, malformed JSON, encoding issue).
            self.quota_exhausted = True
            self.quota_exhausted_reason = (
                f"Climatebase __NEXT_DATA__ JSON parse failure on "
                f"'{keyword}': {type(e).__name__}: {str(e)[:80]}"
            )
            return []

        jobs = (
            data.get("props", {}).get("pageProps", {}).get("jobs", [])
            or []
        )
        # Empty job list in a valid SSR response is normal for niche
        # keywords — DO NOT set quota_exhausted here. The orchestrator's
        # silent-zero alert only fires when the scraper returns 0 across
        # ALL keywords AND quota_exhausted is set, so leaving this branch
        # quiet correctly distinguishes "Climatebase truly has no matches"
        # from "Climatebase broke".
        out: list[Role] = []
        for j in jobs[:limit]:
            try:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
            except Exception:
                # Per-item parse failure (missing field, type error) — skip
                # the row but keep processing siblings. Not a quota signal;
                # individual rows fail in normal operation.
                continue
        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("title") or "").strip()
        if not title:
            return None
        company = self._normalize_company(str(j.get("name_of_employer") or "").strip())
        if not company:
            return None

        # Climatebase URLs are /jobs/{id}
        job_id = j.get("id") or j.get("objectID")
        if not job_id:
            return None
        url = f"https://climatebase.org/jobs/{job_id}"

        # Locations: list of strings like "Boston, MA, US"
        locs = j.get("locations") or []
        loc_str = locs[0] if locs and isinstance(locs[0], str) else ""

        # Remote preference
        remote_prefs = j.get("remote_preferences") or []
        is_remote = any("remote" in str(p).lower() for p in remote_prefs)
        location_type = "Remote" if is_remote else ("Hybrid" if any("hybrid" in str(p).lower() for p in remote_prefs) else "On-site")

        # Salary
        salary_min = salary_max = None
        try:
            sf = j.get("salary_from")
            st = j.get("salary_to")
            salary_min = int(sf) if sf and str(sf).strip() else None
            salary_max = int(st) if st and str(st).strip() else None
        except (ValueError, TypeError):
            pass

        salary_text = None
        if salary_min and salary_max:
            salary_text = f"${salary_min:,} - ${salary_max:,}"
        elif salary_min:
            salary_text = f"${salary_min:,}+"

        # Industry tag from sectors
        sectors = j.get("sectors") or []
        industry = sectors[0] if sectors else "Climate"

        # Posted date
        posted = j.get("activation_date") or ""

        # JD bodies aren't in the SSR data — would need a per-job fetch.
        # Skip for now; downstream Stage 1 prefilter still works on title.
        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            industry=industry[:30] if industry else None,
            job_description_full="",
            jd_completeness="Missing",
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        """Fetch a per-job page for the JD body."""
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                role.job_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
            )
        except Exception:
            return ""
        if resp.status_code != 200:
            return ""
        # Pull the JD from the per-job Next.js data
        m = NEXT_DATA_RE.search(resp.text or "")
        if not m:
            return ""
        try:
            data = json.loads(m.group(1))
        except Exception:
            return ""
        job = data.get("props", {}).get("pageProps", {}).get("job") or {}
        desc = job.get("description") or job.get("job_description") or ""
        # Strip basic HTML
        from backend.scraper.greenhouse import _strip_html
        return _strip_html(desc)[:8000] if desc else ""

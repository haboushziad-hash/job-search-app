"""Google Jobs scraper — Serper.dev API.

Google Jobs is the universal aggregator: every employer career page Google
has indexed (Workday / iCIMS / Greenhouse / Lever / Ashby / SmartRecruiters
/ custom ATS) PLUS LinkedIn / Indeed / Glassdoor / ZipRecruiter. Hitting
Google Jobs broadens our reach beyond the curated tenant lists we maintain
per-ATS — pharma / biotech / industrial employers (Pfizer, Merck, AbbVie,
HCA, Walmart) that aren't in our 27 Workday tenants but ARE indexed by
Google show up here automatically.

We use Serper.dev (https://serper.dev) — the cheaper of the two Google
SERP APIs (vs SerpAPI's 100 calls/$25 paid tier, Serper's free tier is
2,500 calls/month).

Per-search budget:
  - 14 search_terms per user search × ~96 user searches/month = 1,344
    calls/month — well within the free 2,500 cap.
  - 402 Payment Required = free tier exhausted. Surface as quota_exhausted
    so the orchestrator audit JSON shows "Google Jobs hit cap".

API: https://serper.dev/api
  Endpoint: POST https://google.serper.dev/jobs
  Headers:  X-API-KEY: <key>, Content-Type: application/json
  Body:     {"q": "<keyword>", "location": "United States", "page": 1, "num": 10}

Response shape:
  {"jobs": [{
      "title": "...",
      "company_name": "...",
      "location": "...",
      "description": "...",
      "detected_extensions": {"salary": "$120K-$140K a year", "posted_at": "..."},
      "apply_options": [{"title": "LinkedIn", "link": "..."}, ...]
  }, ...]}
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


SERPER_JOBS_API = "https://google.serper.dev/jobs"


def _serper_base_and_key() -> tuple[str, Optional[str]]:
    """Return (base_url, serper_key) honoring proxy mode.

    Proxy mode (LLM_PROXY_URL set): route through the Cloudflare Worker so
    Ziad's Serper key stays server-side and testers don't need their own.
    Local/dev mode: direct call to google.serper.dev with key from .env.
    Mirrors the JSearch / Adzuna pattern so the Worker can adopt a single
    /v1/scraper/* convention.
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        return f"{proxy}/v1/scraper/serper/jobs", ""
    return SERPER_JOBS_API, getattr(config, "SERPER_API_KEY", "") or ""


def _parse_salary_text(salary: str) -> tuple[Optional[int], Optional[int]]:
    """Best-effort range parse from Serper's detected_extensions.salary string.

    Examples seen in the wild:
        "$120K-$140K a year"
        "$50-$60 an hour"
        "$120,000-$140,000 a year"
        "$95K a year"     (single value)
    Hourly is normalized to annual at 2080 hours/year (matches JSearch).
    Returns (min, max) — either may be None if not parseable.
    """
    if not salary:
        return (None, None)
    s = salary.strip()
    nums = re.findall(r"\$?\s*([\d,]+(?:\.\d+)?)\s*([Kk])?", s)
    if not nums:
        return (None, None)

    def to_int(raw: str, k_flag: str) -> Optional[int]:
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            return None
        if k_flag.lower() == "k":
            v *= 1000
        return int(v)

    parsed = [to_int(raw, k_flag) for raw, k_flag in nums]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return (None, None)

    is_hourly = "hour" in s.lower() or "/hr" in s.lower()
    if is_hourly:
        parsed = [p * 2080 for p in parsed]

    if len(parsed) == 1:
        return (parsed[0], None)
    return (min(parsed), max(parsed))


def _date_posted_chip(posted_within_days: Optional[int]) -> Optional[str]:
    """Map our day-window to Google Jobs' chips date_posted enum.

    Google Jobs' date_posted facet supports: today | 3days | week | month.
    None / >30 days = no filter (return None and omit the chip).
    """
    if posted_within_days is None:
        return None
    if posted_within_days <= 1:
        return "today"
    if posted_within_days <= 3:
        return "3days"
    if posted_within_days <= 7:
        return "week"
    if posted_within_days <= 31:
        return "month"
    return None


class GoogleJobsScraper(BaseScraper):
    """Serper.dev wrapper around Google Jobs.

    One API request per keyword (no per-keyword pagination — Google Jobs
    returns its top-N by relevance and pagination beyond page 1 has very
    diminishing returns). Per-keyword raw counts populated for v0.3.4
    audit visibility.
    """

    source_name = "GoogleJobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        base_url, api_key = _serper_base_and_key()
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and not api_key:
            return []  # silent no-op when key not configured

        date_chip = _date_posted_chip(posted_within_days)
        sem = asyncio.Semaphore(5)

        async def bounded(kw: str) -> list[Role]:
            # Short-circuit once any keyword has tripped the quota flag —
            # Serper's free tier is monthly, so once 402'd, every following
            # call will 402 too. Mirrors Adzuna's guard.
            if self.quota_exhausted:
                return []
            async with sem:
                if self.quota_exhausted:
                    return []
                try:
                    raw = await asyncio.wait_for(
                        self._search_keyword(
                            kw, limit_per_keyword, date_chip, api_key, base_url,
                        ),
                        timeout=25.0,
                    )
                    # v0.3.4: per-keyword raw count (pre-dedup) for audit JSON.
                    self.per_keyword_raw_counts[kw] = len(raw)
                    return raw
                except Exception as e:
                    err_str = str(e)
                    if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"Serper.dev rate-limit/quota exhausted on '{kw}'"
                        )
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Cross-keyword dedup on apply URL (same role often surfaces on
        # multiple keyword variants — "AI strategy" and "AI program" both
        # hit "AI Strategy Lead").
        seen_url: set[str] = set()
        seen_pair: set[tuple[str, str]] = set()
        out: list[Role] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            for role in r or []:
                if role.job_url and role.job_url in seen_url:
                    continue
                pair = (
                    (role.company or "").strip().lower(),
                    (role.job_title or "").strip().lower(),
                )
                if pair[1] and pair in seen_pair:
                    continue
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_pair.add(pair)
                out.append(role)
        return out

    async def _search_keyword(
        self,
        keyword: str,
        limit: int,
        date_chip: Optional[str],
        api_key: str,
        base_url: str,
    ) -> list[Role]:
        body: dict[str, Any] = {
            "q": keyword,
            "location": "United States",
            "gl": "us",
            "hl": "en",
            "page": 1,
        }
        if date_chip:
            # Google Jobs date facet — Serper.dev forwards `chips` to the
            # underlying Google query. Documented example: "date_posted:month".
            body["chips"] = f"date_posted:{date_chip}"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Direct mode: send the key. Proxy mode: Worker injects it server-side.
        if api_key:
            headers["X-API-KEY"] = api_key

        try:
            resp = await self.client._client.post(  # type: ignore[union-attr]
                base_url, json=body, headers=headers,
            )
        except Exception:
            return []

        if resp.status_code != 200:
            # 402 = free-tier exhausted; 429 = rate-limited. Either way,
            # following calls will fail too — flag for orchestrator.
            if resp.status_code in (402, 403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Serper.dev HTTP {resp.status_code} "
                    f"(free-tier 2,500/mo cap or rate limit)"
                )
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        items = data.get("jobs") or []
        out: list[Role] = []
        for j in items[:limit]:
            try:
                role = self._item_to_role(j)
                if role:
                    out.append(role)
            except Exception:
                continue
        return out

    async def fetch_jd(self, role: Role) -> str:
        """Serper returns the JD body inline — no separate fetch needed.
        Returns whatever was already populated. Satisfies BaseScraper's
        abstract method without a wasted HTTP call (mirrors JSearch)."""
        return role.job_description_full or ""

    def _item_to_role(self, item: dict[str, Any]) -> Optional[Role]:
        """Map one Serper jobs[] item to our Role model."""
        title = (item.get("title") or "").strip()
        company = self._normalize_company(
            (item.get("company_name") or "").strip()
        )
        if not title or not company:
            return None

        # apply_options is a list of {title, link} (Indeed/LinkedIn/etc).
        # Take the first link as the canonical job_url. If absent, skip —
        # without a URL the role can't be applied to or deduped properly.
        apply_options = item.get("apply_options") or []
        url = ""
        if isinstance(apply_options, list) and apply_options:
            first = apply_options[0]
            if isinstance(first, dict):
                url = (first.get("link") or "").strip()
        if not url:
            # Some Serper responses include `link` at the top level too
            url = (item.get("link") or "").strip()
        if not url:
            return None

        location_raw = (item.get("location") or "").strip()
        location_type, _ = self._classify_location(location_raw)

        # Salary — Serper surfaces it on detected_extensions.salary as a
        # human-readable string ("$120K-$140K a year"). Parse a numeric
        # range when possible so the dashboard's salary chip works.
        det = item.get("detected_extensions") or {}
        salary_text = (det.get("salary") or item.get("salary") or "").strip() or None
        salary_min, salary_max = _parse_salary_text(salary_text or "")

        # JD body — Serper returns it inline. Strip HTML defensively even
        # though Google Jobs descriptions are usually plain text.
        desc_raw = item.get("description") or ""
        jd_text = _strip_html(desc_raw) if "<" in desc_raw else desc_raw

        # posted_at is human-readable ("3 days ago"). We don't try to
        # convert here — downstream date freshness uses the upstream
        # date_chip filter we set on the request. Pass None for posted_date
        # so the post-fetch freshness check doesn't reject these on parse
        # failure.
        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=location_raw[:200] or None,
            location_type=location_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full=jd_text[:50000] if jd_text else "",
            jd_completeness=(
                "Full" if jd_text and len(jd_text) > 500
                else ("Partial" if jd_text else "Missing")
            ),
            posted_date=None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

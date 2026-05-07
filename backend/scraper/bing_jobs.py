"""Bing Jobs scraper — Serper.dev API.

Bing's job index is a useful complement to Google Jobs:
  - Different upstream sources (Bing crawls some employers Google misses)
  - Different ranking (Bing surfaces government / non-profit / enterprise
    listings higher than Google's "popular" bias)
  - SAME Serper.dev key as GoogleJobs — no extra signup, no extra config

Bing Jobs is exposed via Serper.dev's regular `/search` endpoint with the
"jobs" vertical (engine=bing). Different shape than google_jobs above —
results come back inside an `organic` or `jobs` array depending on the
exact query, so we adapt parsing.

Per-search budget:
  - Same Serper.dev free tier (2,500/mo total across Google + Bing).
  - 14 search_terms × 96 user searches/mo = 1,344 calls/mo for Google,
    plus ~1,344 for Bing = 2,688 total — barely over the free tier.
  - At paid pricing (Serper $50 for 50K calls, $0.001/call) the overage is
    ~188 calls × $0.001 = $0.20/mo. Negligible.

API: https://serper.dev/api
  Endpoint: POST https://google.serper.dev/search
  Body:     {"q": "<keyword> jobs", "location": "United States",
             "num": 20, "engine": "bing"}

Response shape:
  {"organic": [{"title": "...", "link": "...", "snippet": "...",
                "source": "...", ...}, ...]}

We parse `organic` (Bing returns its job vertical results in this array
when the query contains "jobs") and synthesize a Role from each entry.
The snippet contains a JD preview; the link points to the employer page.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper


SERPER_SEARCH_API = "https://google.serper.dev/search"


def _serper_search_base_and_key() -> tuple[str, Optional[str]]:
    """Same proxy/local switch as google_jobs._serper_base_and_key."""
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        return f"{proxy}/v1/scraper/serper/search", ""
    return SERPER_SEARCH_API, getattr(config, "SERPER_API_KEY", "") or ""


def _company_from_url(url: str) -> Optional[str]:
    """Best-effort company-from-domain when Bing's snippet doesn't include
    the employer name explicitly. Strips well-known job-board hosts so we
    don't surface "linkedin" / "indeed" as the company.
    """
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return None
    if not host:
        return None

    junk = (
        "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
        "monster.com", "simplyhired.com", "careerbuilder.com",
        "google.com", "bing.com", "duckduckgo.com",
        "myworkdayjobs.com", "icims.com", "greenhouse.io", "lever.co",
        "ashbyhq.com", "smartrecruiters.com", "workable.com",
        "snagajob.com", "dice.com", "jobs.lever.co",
    )
    if any(host.endswith(j) for j in junk):
        return None

    # Strip a few common career subdomains
    for prefix in ("careers.", "jobs.", "apply.", "recruiting."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break

    # Take the registered name (first label) and capitalize.
    label = host.split(".")[0]
    if not label or len(label) > 60:
        return None
    return label.replace("-", " ").title()


def _company_from_snippet(snippet: str) -> Optional[str]:
    """Bing job snippets often contain ` · CompanyName · Location`. Try to
    pull the company chunk when the URL gives us nothing. Cheap heuristic
    — fall back to URL-based extraction if it fails."""
    if not snippet:
        return None
    parts = re.split(r"\s+[·•|]\s+", snippet)
    for p in parts:
        p = p.strip()
        # 2-50 char company-like tokens — exclude obvious non-companies.
        if 2 <= len(p) <= 50 and not any(
            kw in p.lower()
            for kw in ("hour", "day ago", "week ago", "remote", "onsite",
                       "salary", "$", "full-time", "part-time", "apply now")
        ):
            # Skip pure city/state hints
            if re.match(r"^[A-Z][a-z]+,\s*[A-Z]{2}$", p):
                continue
            return p
    return None


class BingJobsScraper(BaseScraper):
    """Bing job vertical via Serper.dev /search with engine=bing.

    Mirrors GoogleJobsScraper's behavior — one request per keyword,
    cross-keyword dedup on URL. Different from GoogleJobs in the response
    parsing (Bing returns flat organic results vs Google Jobs' structured
    jobs[] array)."""

    source_name = "BingJobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 30,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        base_url, api_key = _serper_search_base_and_key()
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and not api_key:
            return []

        sem = asyncio.Semaphore(5)

        async def bounded(kw: str) -> list[Role]:
            if self.quota_exhausted:
                return []
            async with sem:
                if self.quota_exhausted:
                    return []
                try:
                    raw = await asyncio.wait_for(
                        self._search_keyword(
                            kw, limit_per_keyword, api_key, base_url,
                        ),
                        timeout=25.0,
                    )
                    self.per_keyword_raw_counts[kw] = len(raw)
                    return raw
                except Exception as e:
                    err_str = str(e)
                    if "402" in err_str or "429" in err_str or "quota" in err_str.lower():
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"Serper.dev rate-limit/quota exhausted on '{kw}' (Bing)"
                        )
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

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
        api_key: str,
        base_url: str,
    ) -> list[Role]:
        # Anchor query with " jobs" so Bing returns its job vertical
        # rather than generic web results. Serper passes through to Bing.
        body: dict[str, Any] = {
            "q": f"{keyword} jobs",
            "location": "United States",
            "gl": "us",
            "hl": "en",
            "num": min(20, max(10, limit)),
            "engine": "bing",
        }

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["X-API-KEY"] = api_key

        try:
            resp = await self.client._client.post(  # type: ignore[union-attr]
                base_url, json=body, headers=headers,
            )
        except Exception:
            return []

        if resp.status_code != 200:
            if resp.status_code in (402, 403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"Serper.dev (Bing) HTTP {resp.status_code}"
                )
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        # Bing puts results in `organic`; Google-Jobs-style queries against
        # the search endpoint sometimes return a `jobs` block instead.
        items = (data.get("jobs") or []) + (data.get("organic") or [])
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
        return role.job_description_full or ""

    def _item_to_role(self, item: dict[str, Any]) -> Optional[Role]:
        # Both `jobs[]` and `organic[]` shapes flow through here. Pick the
        # first non-empty value for each field.
        title = (
            item.get("title")
            or item.get("job_title")
            or ""
        ).strip()
        url = (
            item.get("link")
            or item.get("apply_link")
            or item.get("url")
            or ""
        ).strip()
        if not title or not url:
            return None

        company = (
            item.get("company_name")
            or item.get("company")
            or item.get("source")
            or ""
        ).strip()
        if not company:
            company = (
                _company_from_snippet(item.get("snippet") or "")
                or _company_from_url(url)
                or ""
            )
        company = self._normalize_company(company)
        if not company:
            return None

        location_raw = (
            item.get("location")
            or item.get("place")
            or ""
        ).strip()
        location_type, _ = self._classify_location(location_raw)

        snippet = (item.get("snippet") or item.get("description") or "").strip()

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=location_raw[:200] or None,
            location_type=location_type,
            job_description_full=snippet[:8000] if snippet else "",
            jd_completeness=(
                "Partial" if snippet else "Missing"
            ),
            posted_date=None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

"""Findwork scraper — small but free aggregator with developer-friendly API.

Findwork (findwork.dev) aggregates ~5K-15K active jobs across remote and
on-site, mostly tech-leaning but covers product, design, marketing, sales.

Free tier: requires a token (free, register at https://findwork.dev/developers).
The token goes in env as ``FINDWORK_API_KEY``. No-op silently when unset.

Endpoint: GET https://findwork.dev/api/jobs/?search={kw}&order_by=-date_posted
  Headers: Authorization: Token {key}
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


FINDWORK_API = "https://findwork.dev/api/jobs/"


def _findwork_base_and_key() -> tuple[str, Optional[str]]:
    """Return (base_url, api_key) honoring proxy mode (v0.1.4).

    Proxy mode: route through Worker, key empty (Worker injects).
    Local mode: direct Findwork URL with key from .env.
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if proxy:
        return f"{proxy}/v1/scraper/findwork/api/jobs/", ""
    return FINDWORK_API, config.FINDWORK_API_KEY


class FindworkScraper(BaseScraper):
    source_name = "Findwork"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 100,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Wave-2B Phase 2 (FIX 14a): default limit raised 50 → 100. Findwork
        # returns up to 100 items per page (its native page size); the prior
        # 50 cap discarded half of each first-page response before
        # downstream filtering even ran. No extra API calls — same page,
        # bigger slice.
        base_url, api_key = _findwork_base_and_key()
        proxy_mode = bool((config.LLM_PROXY_URL or "").strip())
        if not proxy_mode and not api_key:
            return []  # silent no-op when unset locally

        sem = asyncio.Semaphore(1)

        async def bounded(kw: str) -> list[Role]:
            # v0.2.1: short-circuit remaining keywords once any keyword has
            # tripped quota_exhausted. Findwork hit 429 in 5/6 recent runs;
            # without this guard each subsequent keyword wastes its first
            # call. Mirrors the Adzuna fix added in the same patch.
            if self.quota_exhausted:
                return []
            async with sem:
                if self.quota_exhausted:
                    return []
                try:
                    result = await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword,
                                             api_key or "", base_url),
                        timeout=20.0,
                    )
                    # Free-tier authenticated allows 50 req/min = 1.2s
                    # spacing; add 0.1s safety margin. Pace inside the
                    # semaphore so the next keyword waits before acquiring.
                    await asyncio.sleep(1.3)
                    return result
                except Exception:
                    return []

        tasks = [bounded(kw) for kw in keywords]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        seen: set[str] = set()
        out: list[Role] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            for role in r or []:
                if role.job_url and role.job_url in seen:
                    continue
                if role.job_url:
                    seen.add(role.job_url)
                out.append(role)
        return out

    async def _search_keyword(
        self, keyword: str, limit: int, api_key: str,
        base_url: str,
    ) -> list[Role]:
        # Findwork accepts `sort_by=date_posted` (not `order_by`); default
        # ordering already returns newest-first so the param is optional.
        # Wave-2B Phase 2 (FIX 14b): paginate via the response 'next' URL
        # until we hit the per-keyword limit OR run out of pages. Cap at
        # 5 pages to respect the 50 req/min budget (already 1.3s pace from
        # earlier fix). Findwork pages are 100 items each → 5 pages = 500
        # raw items max per keyword.
        params: dict = {"search": keyword}
        # In proxy mode the Worker injects Authorization: Token <key>.
        # In direct mode we set it ourselves from .env.
        headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Token {api_key}"

        out: list[Role] = []
        next_url: Optional[str] = None
        max_pages = 5
        retried_429 = False  # v0.3.37: honor Retry-After once before giving up

        async def _fetch(url_next: Optional[str]):
            # Findwork's 'next' is the absolute URL with cursor already
            # encoded — pass it directly; else hit base_url with params.
            if url_next:
                return await self.client._client.get(  # type: ignore[union-attr]
                    url_next, headers=headers,
                )
            return await self.client._client.get(  # type: ignore[union-attr]
                base_url, params=params, headers=headers,
            )

        for page_idx in range(max_pages):
            try:
                resp = await _fetch(next_url)
            except Exception:
                break
            if resp.status_code != 200:
                # v0.3.37: a single 429 previously quota-killed Findwork for the
                # WHOLE run (~83% of runs hit one => the source zeroed entirely).
                # Honor Retry-After and retry the SAME page once before giving up
                # — converts all-or-nothing zeroed runs into reliable small adds.
                if resp.status_code == 429 and not retried_429:
                    retried_429 = True
                    try:
                        ra = float(resp.headers.get("Retry-After", "") or 0)
                    except (TypeError, ValueError):
                        ra = 0.0
                    await asyncio.sleep(min(max(ra, 1.5), 10.0))
                    try:
                        resp = await _fetch(next_url)
                    except Exception:
                        break
                if resp.status_code != 200:
                    if resp.status_code in (403, 429):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = f"Findwork HTTP {resp.status_code} (rate limit or quota)"
                    break
            try:
                data = resp.json()
            except Exception:
                break

            items = data.get("results") or []
            for j in items:
                try:
                    role = self._item_to_role(j)
                    if role:
                        out.append(role)
                except Exception:
                    continue
                if len(out) >= limit:
                    return out
            # Walk the cursor — Findwork uses DRF-style pagination with
            # 'next' = absolute URL or null when exhausted.
            next_url = data.get("next")
            if not next_url:
                break
            # Respect the 50 req/min budget between pages too.
            await asyncio.sleep(1.3)

        return out

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("role") or "").strip()
        if not title:
            return None
        company = self._normalize_company(str(j.get("company_name") or "").strip())
        if not company:
            return None
        url = j.get("url") or ""
        if not url:
            return None

        loc_str = (j.get("location") or "").strip()
        is_remote = bool(j.get("remote"))
        if is_remote:
            location_type = "Remote"
            # Wave-2B Phase 2 (FIX 24, 2026-05-30): when role is pure-
            # remote AND has no specific city/state, populate location
            # with "Remote" rather than leaving it None. Closes the
            # 4% audit-coverage gap where Findwork pure-remote roles
            # showed 0% location-populated. Downstream hard_filter
            # treats remote roles as location-agnostic so this is
            # display-only — doesn't affect filtering.
            if not loc_str:
                loc_str = "Remote"
        else:
            location_type, _ = self._classify_location(loc_str)

        text = j.get("text") or ""
        jd_text = _strip_html(text) if text else ""

        posted = j.get("date_posted") or ""

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str or None,
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness="Full" if len(jd_text) > 500 else ("Partial" if jd_text else "Missing"),
            posted_date=posted or None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

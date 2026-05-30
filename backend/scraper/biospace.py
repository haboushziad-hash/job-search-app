"""BioSpace scraper — HTML scrape of jobs.biospace.com search results.

BioSpace is the leading life-sciences-specific jobs board (pharma /
biotech / medical devices / clinical research / diagnostics). Closes the
gap our corporate-ATS scrapers leave for life-sciences employers that
aren't on Workday / Greenhouse / Lever (most non-Big-Pharma biotechs).

No public API — they offer RSS for some saved searches but not for
arbitrary keyword queries. Strategy: HTML scrape of the search results
page (mirrors the BuiltIn pattern).

URL pattern:
  GET https://jobs.biospace.com/jobs/?keywords=<query>&location=United+States

HTML pattern (verified 2026-05-07):
  <li class="lister__item cf ..." id="item-<jobId>">
    <h3 class="lister__header"><a href="/job/<id>/<slug>/" class="js-clickable-area-link">
        <span>Job Title</span>
      </a></h3>
    <ul class="lister__meta">
      <li class="lister__meta-item lister__meta-item--location">Location</li>
      <li class="lister__meta-item lister__meta-item--recruiter">Company</li>
    </ul>
    <p class="lister__description js-clamp-2">Truncated description...</p>
  </li>

Per-search cost: ~3 page fetches per keyword (default pagination ~25/page,
fetch up to ~75 jobs/keyword). Worth scaling later if conversion is good.
"""
from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


BIOSPACE_BASE = "https://jobs.biospace.com"
BIOSPACE_SEARCH = f"{BIOSPACE_BASE}/jobs/"

# Match a single <li class="lister__item ..."> block. Greedy on the inner
# content but anchored on the next <li class="lister__item or </ul of the
# enclosing list — uses a tempered token so we don't accidentally absorb
# multiple items.
ITEM_BLOCK_RE = re.compile(
    r'<li[^>]+class="lister__item[^"]*"[^>]*id="item-(?P<id>\d+)"[^>]*>'
    r'(?P<inner>.*?)'
    r'(?=<li[^>]+class="lister__item[^"]*"|</ul>)',
    re.DOTALL | re.IGNORECASE,
)
ITEM_TITLE_RE = re.compile(
    r'<h3[^>]*class="lister__header"[^>]*>\s*<a[^>]+href="\s*([^"]+?)\s*"[^>]*>\s*'
    r'<span[^>]*>([^<]+)</span>',
    re.DOTALL | re.IGNORECASE,
)
ITEM_LOCATION_RE = re.compile(
    r'<li[^>]+class="lister__meta-item lister__meta-item--location"[^>]*>'
    r'\s*([^<]+)\s*</li>',
    re.IGNORECASE,
)
ITEM_RECRUITER_RE = re.compile(
    r'<li[^>]+class="lister__meta-item lister__meta-item--recruiter"[^>]*>'
    r'\s*([^<]+)\s*</li>',
    re.IGNORECASE,
)
ITEM_DESCRIPTION_RE = re.compile(
    r'<p[^>]+class="lister__description[^"]*"[^>]*>([^<]*?)</p>',
    re.DOTALL | re.IGNORECASE,
)


def _decode(s: str) -> str:
    return html.unescape((s or "").strip())


class BioSpaceScraper(BaseScraper):
    source_name = "BioSpace"

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
                    raw = await asyncio.wait_for(
                        self._search_keyword(kw, limit_per_keyword),
                        timeout=30.0,
                    )
                    self.per_keyword_raw_counts[kw] = len(raw)
                    return raw
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

    async def _search_keyword(self, keyword: str, limit: int) -> list[Role]:
        # ~25 results per page; honor `limit` by fetching up to ceil(limit/25)
        # pages but cap at 4 to keep cost bounded.
        pages_needed = max(1, (limit + 19) // 20)
        max_pages = min(pages_needed, 4)

        out: list[Role] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            params = {
                "keywords": keyword,
                "location": "United States",
                "sort": "Date",
            }
            if page > 1:
                params["page"] = str(page)
            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    BIOSPACE_SEARCH, params=params,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0"
                        ),
                        "Accept": "text/html",
                    },
                )
            except Exception:
                break
            if resp.status_code != 200:
                break
            text = resp.text
            if "lister__item" not in text:
                break

            new_count_this_page = 0
            for m in ITEM_BLOCK_RE.finditer(text):
                if len(out) >= limit:
                    break
                jid = m.group("id")
                inner = m.group("inner")
                role = self._parse_item(jid, inner)
                if not role or not role.job_url:
                    continue
                if role.job_url in seen:
                    continue
                seen.add(role.job_url)
                out.append(role)
                new_count_this_page += 1
            if new_count_this_page == 0:
                break  # no new items — pagination exhausted
            if len(out) >= limit:
                break
        return out

    async def fetch_jd(self, role: Role) -> str:
        """Fetch full JD body when only the truncated snippet is present.

        BioSpace job-detail pages put the body inside
        <div class="job-details__description"> (sometimes also under
        the schema.org JobPosting microdata). Best-effort regex pull;
        if anything fails, return what we already had.
        """
        if not role.job_url:
            return role.job_description_full or ""
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                role.job_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0"
                    ),
                    "Accept": "text/html",
                },
            )
        except Exception:
            return role.job_description_full or ""
        if resp.status_code != 200:
            return role.job_description_full or ""
        text = resp.text

        # Try schema.org JobPosting first (cleanest body)
        m = re.search(
            r'itemprop="description"[^>]*>(.*?)</div>',
            text, re.DOTALL | re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'<div[^>]+class="job-details__description"[^>]*>(.*?)</div>\s*</div>',
                text, re.DOTALL | re.IGNORECASE,
            )
        if not m:
            return role.job_description_full or ""
        body = _strip_html(m.group(1))
        body = re.sub(r"\s+", " ", body).strip()
        return body[:50000]

    def _parse_item(self, jid: str, inner: str) -> Optional[Role]:
        tm = ITEM_TITLE_RE.search(inner)
        if not tm:
            return None
        href_raw = tm.group(1).strip()
        title = _decode(tm.group(2))
        if not title:
            return None

        # Resolve relative href
        if href_raw.startswith("http"):
            url = href_raw
        else:
            url = BIOSPACE_BASE + (href_raw if href_raw.startswith("/") else "/" + href_raw)

        loc_m = ITEM_LOCATION_RE.search(inner)
        location_raw = _decode(loc_m.group(1)) if loc_m else ""

        rec_m = ITEM_RECRUITER_RE.search(inner)
        company_raw = _decode(rec_m.group(1)) if rec_m else ""
        company = self._normalize_company(company_raw)
        if not company:
            return None

        desc_m = ITEM_DESCRIPTION_RE.search(inner)
        snippet = _decode(desc_m.group(1)) if desc_m else ""

        location_type, _ = self._classify_location(location_raw)

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=location_raw[:200] or None,
            location_type=location_type,
            job_description_full=snippet[:8000],
            jd_completeness="Partial" if snippet else "Missing",
            posted_date=None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

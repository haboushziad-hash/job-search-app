"""BuiltIn scraper — public job board for tech-adjacent and consumer roles.

Original API ``/api/v1/jobs`` was retired. Their new ``api.builtin.com``
endpoints reject unauthenticated requests. BUT the public HTML page at
``builtin.com/jobs?search=...`` is server-rendered and contains job
anchor links directly in the markup — no JS rendering needed.

Strategy (rebuilt 2026-05-03):
  1. GET ``https://builtin.com/jobs?search={keyword}``
  2. Parse anchors matching ``<a href="/job/{slug}/{id}">{title}</a>``
  3. Fetch each role detail page for company/location/JD body

Coverage: BuiltIn aggregates ~50K active tech-adjacent roles. Strong on
software, data, marketing, sales, ops, design across both tech and
non-tech companies (BuiltIn explicitly courts tech-forward employers in
finance, retail, healthcare, etc.).

This is verified working as of 2026-05-03 — direct anchor extraction
returns 25+ jobs per keyword search.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


SEARCH_URL = "https://builtin.com/jobs"
JOB_ANCHOR_RE = re.compile(
    r'<a[^>]+href="(/job/[a-z0-9\-]+/(\d+))"[^>]*>([^<]{4,200})</a>',
    re.IGNORECASE,
)


class BuiltInScraper(BaseScraper):
    source_name = "BuiltIn"

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
                        timeout=25.0,
                    )
                except (asyncio.TimeoutError, Exception):
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
        # BuiltIn returns 25 jobs per page. To honor `limit`, fetch ceil(limit/25)
        # pages. For default limit=50 that's 2 pages (50 jobs); for limit=200
        # it's 8 pages. Stop early if a page returns no new unique IDs.
        seen: set[str] = set()
        out: list[Role] = []
        pages_needed = max(1, (limit + 24) // 25)
        max_pages = min(pages_needed, 20)  # safety: cap at 20 pages = 500 jobs

        for page in range(1, max_pages + 1):
            params = {"search": keyword}
            if page > 1:
                params["page"] = str(page)
            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    SEARCH_URL, params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
                        "Accept": "text/html",
                    },
                )
            except Exception:
                break
            if resp.status_code != 200:
                # BuiltIn returns 403 on some keyword queries (anti-bot or
                # rate-limit). Skip this keyword/page and move on instead of
                # treating it as "no results." The scraper still tries other
                # keywords; one query failing doesn't kill the whole search.
                if resp.status_code == 403:
                    try:
                        print(f"[builtin] 403 on '{keyword}' page {page} — skipping")
                    except Exception:
                        pass
                break
            body = resp.text or ""

            page_added = 0
            for match in JOB_ANCHOR_RE.finditer(body):
                href, job_id, title = match.group(1), match.group(2), match.group(3)
                if job_id in seen:
                    continue
                seen.add(job_id)
                title_clean = re.sub(r"\s+", " ", title).strip()
                if not title_clean or len(title_clean) < 4:
                    continue
                url = f"https://builtin.com{href}"

                # Company appears BEFORE the title anchor — look back ~2000 chars
                ctx_before = body[max(0, match.start() - 2000): match.start()]
                company = _extract_company_before(ctx_before, job_id)

                # Location appears AFTER the title anchor
                ctx_after = body[match.end(): match.end() + 1500]
                location = _extract_location_after(ctx_after)

                out.append(Role(
                    job_title=title_clean[:200],
                    company=self._normalize_company(company or "BuiltIn"),
                    job_url=url,
                    location=location or None,
                    location_type=("Remote" if "remote" in (location or "").lower() else "On-site"),
                    job_description_full="",
                    jd_completeness="Missing",
                    primary_source=self.source_name,
                    date_first_seen=datetime.now(timezone.utc).date().isoformat(),
                ))
                page_added += 1
                if len(out) >= limit:
                    return out
            # If a page returned 0 new unique jobs, we've exhausted the
            # result set (BuiltIn sometimes echos page 1 for invalid pages).
            if page_added == 0:
                break

        return out

    async def fetch_jd(self, role: Role) -> str:
        """Fetch the role detail page and pull the JD body from rendered HTML."""
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
        body = resp.text or ""

        # Pull the JD section
        for pat in (
            r'<div[^>]+class="[^"]*job-description[^"]*"[^>]*>([\s\S]+?)</div>\s*</div>',
            r'<section[^>]+data-testid="job-description"[^>]*>([\s\S]+?)</section>',
            r'<div[^>]+id="job-content"[^>]*>([\s\S]+?)</div>\s*<',
        ):
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                return _strip_html(m.group(1))[:8000]
        return _strip_html(body)[:8000]


# Helper extractors for company/location around a job anchor.
# BuiltIn HTML pattern observed live 2026-05-03:
#   ...
#   <a href="/company/draftkings" data-builtin-track-job-id="8945838" ...>
#       <span>DraftKings</span></a>
#   ...
#   <a href="/job/operations-manager/8945838">Operations Manager</a>   ← title
#   ...
#   <span class="font-barlow text-gray-04">New Jersey, USA</span>      ← location
def _extract_company_before(ctx_before: str, job_id: str) -> str:
    """Pull the company name from the marketup BEFORE the title anchor.
    The matching company-link has the same data-builtin-track-job-id attribute."""
    # Direct match using job_id correlation (most reliable)
    pat = re.compile(
        r'<a[^>]+href="/company/[^"]+"[^>]*data-builtin-track-job-id="' +
        re.escape(job_id) +
        r'"[^>]*>\s*<span>([^<]+)</span>',
        re.IGNORECASE,
    )
    m = pat.search(ctx_before)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    # Fallback: any /company/ anchor in the look-back window
    fallback = re.search(
        r'<a[^>]+href="/company/[^"]+"[^>]*>\s*<span>([^<]+)</span>',
        ctx_before, re.IGNORECASE,
    )
    if fallback:
        return re.sub(r"\s+", " ", fallback.group(1)).strip()
    return ""


_LOC_PATTERN = re.compile(
    r'<span class="font-barlow text-gray-04">([^<]+)</span>',
    re.IGNORECASE,
)


def _extract_location_after(ctx_after: str) -> str:
    """Pull the location from the markup AFTER the title anchor."""
    # Skip the first match (which is "Remote or Hybrid" arrangement),
    # take the second which is the actual city/state.
    matches = _LOC_PATTERN.findall(ctx_after)
    for m in matches:
        text = m.strip()
        # Heuristic: ignore "Remote or Hybrid" header, take first city/state
        if text.lower() in ("remote or hybrid", "hybrid", "remote"):
            continue
        if "," in text or "USA" in text or text.istitle():
            return text
    # Last resort: first match
    return matches[0].strip() if matches else ""

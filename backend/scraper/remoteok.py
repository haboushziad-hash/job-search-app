"""Remote OK scraper — free public JSON of all active remote jobs.

Remote OK aggregates ~1K-3K active fully-remote roles across software,
marketing, sales, support, ops, design, writing, management — broader
than Remotive in some verticals (especially product / management).

Free, no auth required. The API returns the full active job set in one
call; we filter client-side by keyword.

Endpoint: GET https://remoteok.com/api
  Returns: a JSON array. The first element is a metadata header
           ({"legal": "...", "info": "..."}); the rest are job objects.

Per their ToS we surface a "Powered by Remote OK" string somewhere visible
— we wire this into the audit JSON's `attributions` block so the UI can
render the pill alongside the source name.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


REMOTEOK_API = "https://remoteok.com/api"

# Persona-spanning tag slices. The base API returns one page of recent jobs
# regardless of vertical; fanning out across tag-filtered endpoints surfaces
# medical / finance / education / etc. roles that would otherwise be drowned
# out by dev-heavy default ordering. Empty string == the un-tagged default.
# Wave-2B Phase 2 (FIX 10): expanded 10 → 16 slices to cover
# non-tech personas. Each tag adds ~100 unique jobs upstream; union
# grows 277 → ~532 with these additions. Must combine with FIX 7
# (Semaphore(2) gating below) to stay under RemoteOK's Cloudflare
# burst challenge threshold.
TAG_SLICES = [
    "",
    "medical",
    "finance",
    "education",
    "marketing",
    "sales",
    "design",
    "dev",
    "customer-support",
    "ops",
    "non-tech",
    "healthcare",
    "legal",
    "teaching",
    "hr",
    "excel",
]


class RemoteOKScraper(BaseScraper):
    """Remote OK — free public JSON aggregator. Attribution required."""

    source_name = "RemoteOK"
    # Surfaced in the audit JSON / dashboard so we satisfy Remote OK's
    # attribution requirement without hard-coding a UI pill anywhere.
    attribution: str = "Powered by Remote OK — https://remoteok.com"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Fan out across persona-spanning tag slices in parallel. The base
        # endpoint heavily favors dev roles; tag-filtered endpoints surface
        # medical / finance / education / etc. that would otherwise be
        # invisible to non-tech personas. Dedupe by job id afterward.
        headers = {
            "Accept": "application/json",
            # Remote OK's CDN occasionally blocks default httpx UA.
            "User-Agent": (
                "Mozilla/5.0 (compatible; JobSearchApp/0.3.5; "
                "+https://remoteok.com)"
            ),
        }

        # Wave-2B Phase 2 (FIX 7): RemoteOK sits behind Cloudflare which
        # issues burst-challenge interstitials when >3 requests fire in
        # ~300ms from one client. Previously 10 parallel slices triggered
        # the challenge — 8 of 9 tag fetches were silently failing with
        # bare except. Two changes: gate with Semaphore(2) so at most 2
        # tag fetches are in flight at once, AND log every exception with
        # status code so future regressions don't go invisible.
        slice_sem = asyncio.Semaphore(2)

        async def _fetch_slice(tag: str) -> list[dict]:
            url = REMOTEOK_API if not tag else f"{REMOTEOK_API}?tag={tag}"
            async with slice_sem:
                try:
                    resp = await self.client._client.get(  # type: ignore[union-attr]
                        url, headers=headers,
                    )
                except Exception as _e:
                    # FIX 7: surface what failed — previously a bare except
                    # ate Cloudflare-burst challenges silently.
                    err_class = type(_e).__name__
                    try:
                        print(
                            f"[remoteok] {err_class} on tag='{tag or 'default'}': "
                            f"{str(_e)[:160]}",
                            flush=True,
                        )
                    except Exception:
                        pass
                    status = getattr(getattr(_e, "response", None), "status_code", None)
                    if status in (429, 403, 503):
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"RemoteOK tag='{tag}' HTTP {status} "
                            f"(likely Cloudflare burst challenge)"
                        )
                    return []
            if resp.status_code != 200:
                if resp.status_code in (403, 429, 503):
                    try:
                        print(
                            f"[remoteok] HTTP {resp.status_code} on tag='{tag or 'default'}'",
                            flush=True,
                        )
                    except Exception:
                        pass
                    self.quota_exhausted = True
                    self.quota_exhausted_reason = (
                        f"RemoteOK HTTP {resp.status_code} on tag='{tag}'"
                    )
                return []
            try:
                data = resp.json()
            except Exception as _e:
                try:
                    print(
                        f"[remoteok] JSON parse failed on tag='{tag or 'default'}': {_e}",
                        flush=True,
                    )
                except Exception:
                    pass
                return []
            if not isinstance(data, list):
                return []
            # First element is metadata; skip it via the id guard below.
            return [j for j in data if isinstance(j, dict) and j.get("id")]

        # Dispatch each slice with a small stagger (100-300ms) so we don't
        # burst the API and trip the shared 429 ceiling all at once. With
        # Semaphore(2) above this distributes 16 tag fetches across
        # ~3-4 seconds wall-clock instead of 300ms (which burned to CF).
        async def _staggered(idx: int, tag: str) -> list[dict]:
            if idx:
                await asyncio.sleep(0.1 + 0.02 * idx)
            return await _fetch_slice(tag)

        slice_results = await asyncio.gather(
            *[_staggered(i, t) for i, t in enumerate(TAG_SLICES)],
            return_exceptions=True,
        )

        # Dedupe by job id across slices (the same role can appear under
        # multiple tag filters — e.g. a marketing+sales hybrid posting).
        items: list[dict] = []
        seen_ids: set = set()
        for r in slice_results:
            if isinstance(r, BaseException):
                continue
            for j in r or []:
                jid = j.get("id")
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                items.append(j)

        keywords_lower = [k.lower() for k in keywords]
        seen: set[str] = set()
        out: list[Role] = []
        for j in items:
            title = (j.get("position") or j.get("title") or "").strip()
            if not title:
                continue
            jd = j.get("description") or ""
            if not _kw_match.matches_any_keyword(title, jd, keywords_lower):
                continue
            try:
                role = self._item_to_role(j)
                if not role or not role.job_url:
                    continue
                if role.job_url in seen:
                    continue
                seen.add(role.job_url)
                out.append(role)
            except Exception:
                continue

        # Loose per-keyword raw count — we serve the same fetch to all
        # keywords, so populate a single flat estimate.
        for kw in keywords:
            self.per_keyword_raw_counts[kw] = sum(
                1 for j in items
                if _kw_match.matches_any_keyword(
                    (j.get("position") or "").strip(),
                    j.get("description") or "",
                    [kw.lower()],
                )
            )
        return out

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("position") or j.get("title") or "").strip()
        if not title:
            return None
        company = self._normalize_company((j.get("company") or "").strip())
        if not company:
            return None
        url = (j.get("url") or j.get("apply_url") or "").strip()
        if not url:
            return None

        # Remote OK marks every role remote (whole point of the board).
        loc_str = (j.get("location") or "").strip() or "Remote"
        location_type = "Remote"

        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if "<" in desc else desc

        # Salary — Remote OK exposes salary_min/salary_max as integers.
        salary_min = j.get("salary_min") or None
        salary_max = j.get("salary_max") or None
        salary_text: Optional[str] = None
        if salary_min and salary_max:
            salary_text = f"${int(salary_min):,}-${int(salary_max):,}"
        elif salary_min:
            salary_text = f"${int(salary_min):,}+"

        posted = (j.get("date") or j.get("epoch") or "")
        posted_iso: Optional[str] = None
        try:
            if isinstance(posted, str) and posted:
                posted_iso = posted
            elif isinstance(posted, (int, float)) and posted:
                posted_iso = datetime.fromtimestamp(
                    int(posted), tz=timezone.utc,
                ).isoformat()
        except Exception:
            pass

        return Role(
            job_title=title[:200],
            company=company,
            job_url=url,
            location=loc_str,
            location_type=location_type,
            salary_min=int(salary_min) if salary_min else None,
            salary_max=int(salary_max) if salary_max else None,
            salary_text=salary_text,
            job_description_full=jd_text[:8000],
            jd_completeness=(
                "Full" if len(jd_text) > 500
                else ("Partial" if jd_text else "Missing")
            ),
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

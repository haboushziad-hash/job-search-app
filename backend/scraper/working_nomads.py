"""Working Nomads scraper — free public JSON of all listed remote jobs.

Working Nomads curates ~500-1500 active remote roles, weighted toward:
  - Engineering / dev (still ~half the listings)
  - Marketing / sales / content
  - Customer success / support
  - Recruiting / HR (rare on other remote boards — useful for HR fixtures)
  - Education / instructional design

Endpoint: GET https://www.workingnomads.com/api/exposed_jobs/
  Returns: a JSON list of job objects (no pagination, full active set).

Each item:
  {
    "url": "...",                    # canonical apply URL
    "title": "...",
    "company_name": "...",
    "category_name": "...",
    "tags": "tag1,tag2,...",
    "description": "<html>...</html>",
    "pub_date": "2026-05-04T18:00:12.345Z",
    "location": "Worldwide" | "USA Only" | etc.
  }

No auth, no key. Single fetch returns the whole active list, so we filter
client-side by keyword (mirrors RemoteOK / Remotive pattern).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


WORKINGNOMADS_API = "https://www.workingnomads.com/jobsapi/_search"
WORKINGNOMADS_API_FALLBACK = "https://www.workingnomads.com/api/exposed_jobs/"


class WorkingNomadsScraper(BaseScraper):
    """Working Nomads — free remote-only aggregator."""

    source_name = "WorkingNomads"
    attribution: str = "Powered by Working Nomads — https://www.workingnomads.com"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (compatible; JobSearchApp/0.3.5)"
            ),
        }

        # Try new ElasticSearch endpoint first (5,742 active jobs vs
        # 22-item marketing teaser feed on the legacy endpoint).
        data: list = []
        used_fallback = False
        page_size = 500
        # Cap total fetched at limit_per_keyword * number of keywords as
        # rough upper bound to avoid pulling all 5k+ when not needed.
        max_total = max(page_size, limit_per_keyword * max(len(keywords), 1))
        offset = 0
        es_ok = True
        while offset < max_total:
            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    WORKINGNOMADS_API,
                    params={"size": page_size, "from": offset},
                    headers=headers,
                )
            except Exception:
                es_ok = False
                break
            if resp.status_code != 200:
                es_ok = False
                if resp.status_code in (403, 429):
                    self.quota_exhausted = True
                    self.quota_exhausted_reason = (
                        f"WorkingNomads HTTP {resp.status_code}"
                    )
                break
            try:
                page = resp.json()
            except Exception:
                es_ok = False
                break
            # ElasticSearch shape: {"hits": {"hits": [{"_source": {...}}, ...]}}
            hits: list = []
            if isinstance(page, dict):
                inner = page.get("hits")
                if isinstance(inner, dict):
                    raw_hits = inner.get("hits")
                    if isinstance(raw_hits, list):
                        hits = raw_hits
            if not hits:
                break
            data.extend(hits)
            if len(hits) < page_size:
                break
            offset += page_size

        if not es_ok or not data:
            # Defensive fallback to legacy endpoint.
            try:
                resp = await self.client._client.get(  # type: ignore[union-attr]
                    WORKINGNOMADS_API_FALLBACK,
                    headers=headers,
                )
            except Exception:
                return []
            if resp.status_code != 200:
                if resp.status_code in (403, 429):
                    self.quota_exhausted = True
                    self.quota_exhausted_reason = (
                        f"WorkingNomads HTTP {resp.status_code}"
                    )
                return []
            try:
                data = resp.json()
            except Exception:
                return []
            if not isinstance(data, list):
                return []
            used_fallback = True

        from datetime import timedelta
        cutoff_iso: Optional[str] = None
        if posted_within_days is not None:
            cutoff_iso = (
                datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            ).isoformat()

        keywords_lower = [k.lower() for k in keywords]
        seen: set[str] = set()
        out: list[Role] = []
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        for j in data:
            if not isinstance(j, dict):
                continue
            try:
                # ES hits wrap the doc in _source; legacy items are flat.
                if not used_fallback and "_source" in j and isinstance(j["_source"], dict):
                    item = j["_source"]
                else:
                    item = j
                role = self._item_to_role(item)
                if not role or not role.job_url:
                    continue
                if cutoff_iso and role.posted_date and role.posted_date < cutoff_iso:
                    continue
                if not _kw_match.matches_any_keyword(
                    role.job_title or "",
                    role.job_description_full or "",
                    keywords_lower,
                ):
                    continue
                if role.job_url in seen:
                    continue
                seen.add(role.job_url)
                out.append(role)
                for kw in keywords:
                    if _kw_match.matches_any_keyword(
                        role.job_title or "",
                        role.job_description_full or "",
                        [kw.lower()],
                    ):
                        per_kw_count[kw] = per_kw_count.get(kw, 0) + 1
            except Exception:
                continue

        self.per_keyword_raw_counts = per_kw_count
        return out

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

    def _item_to_role(self, j: dict) -> Optional[Role]:
        title = (j.get("title") or "").strip()
        # ES index may use either company_name or company.
        company_raw = (
            j.get("company_name")
            or j.get("company")
            or ""
        )
        company = self._normalize_company(company_raw.strip())
        url = (j.get("url") or j.get("apply_url") or "").strip()
        if not title or not company or not url:
            return None

        loc_str = (j.get("location") or "Remote").strip() or "Remote"
        location_type = "Remote"  # Working Nomads is remote-only

        desc = j.get("description") or ""
        jd_text = _strip_html(desc) if "<" in desc else desc

        # pub_date / publication_date is ISO 8601; use verbatim if present.
        posted = (
            (j.get("pub_date") or j.get("publication_date") or "").strip()
            or None
        )

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=loc_str,
            location_type=location_type,
            job_description_full=jd_text[:8000],
            jd_completeness=(
                "Full" if len(jd_text) > 500
                else ("Partial" if jd_text else "Missing")
            ),
            posted_date=posted,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

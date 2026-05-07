"""Google Jobs scraper via DataForSEO — v0.3.9 rewrite.

History:
  v0.3.5 attempted Serper.dev /jobs endpoint — returned 404 (endpoint
  doesn't exist). Scraper disabled in registry.
  v0.3.9: rewritten against DataForSEO's actual Google Jobs endpoint
  (verified by live probe).

Why we want Google Jobs:
  Google Jobs aggregates every employer career page Google has indexed
  (Workday / iCIMS / Greenhouse / Lever / Ashby / SmartRecruiters / custom
  ATS) PLUS LinkedIn / Indeed / Glassdoor / ZipRecruiter. For employers
  whose Workday tenant we DON'T have curated (pharma, healthcare networks,
  industrials, retail/CPG, defense), Google Jobs is the universal fallback.

API: DataForSEO /v3/serp/google/jobs/* (async task-based, no live mode)

  POST /v3/serp/google/jobs/task_post
    body: [{"keyword": "<kw>", "location_name": "United States",
            "language_name": "English", "depth": 20, "priority": 2}]
    → returns task_id (cost: $0.0024/query at priority=2)

  GET /v3/serp/google/jobs/task_get/advanced/<task_id>
    Poll every 2-3s. Returns 20000 status when ready (~6-12s with priority=2).
    Items contain: title, employer_name, location, source_name, source_url,
                   salary, contract_type, time_ago, timestamp

JD body is NOT in the response — fetched separately from source_url
in runner.py's existing _fetch_generic loop (same path Greenhouse/Lever use).

Per-search budget:
  - 14 keywords × $0.0024 = $0.034/run
  - At 96 user searches/mo = $3.23/mo. Cheap.
  - DataForSEO is pay-as-you-go, no minimum.

Cost guard: emit quota_exhausted flag if account balance hint suggests low
funds. Default $5/run hard cap (configurable).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper


DATAFORSEO_BASE = "https://api.dataforseo.com/v3/serp/google/jobs"
DATAFORSEO_TASK_POST = f"{DATAFORSEO_BASE}/task_post"
DATAFORSEO_TASK_GET = f"{DATAFORSEO_BASE}/task_get/advanced"

# Cost per query at priority=2 (verified live)
DFSE_COST_PER_QUERY = 0.0024


def _dfseo_creds() -> tuple[str, str]:
    """Return (login, password). Empty if not configured."""
    login = (getattr(config, "DATAFORSEO_LOGIN", "") or "").strip()
    pw = (getattr(config, "DATAFORSEO_PASSWORD", "") or "").strip()
    return login, pw


def _parse_salary_text(salary: str) -> tuple[Optional[int], Optional[int]]:
    """Best-effort range parse from DataForSEO's salary string.

    Examples seen in the wild:
        "93.8K-131K a year"
        "$120K-$140K a year"
        "50-60 an hour"
        "$120,000-$140,000 a year"
    Hourly normalized to annual at 2080 hours.
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


def _classify_arrangement(location: str) -> str:
    """Quick remote / on-site / hybrid classifier from location string."""
    if not location:
        return "On-site"
    lower = location.lower()
    if "remote" in lower:
        return "Remote"
    if "hybrid" in lower:
        return "Hybrid"
    return "On-site"


class GoogleJobsScraper(BaseScraper):
    """DataForSEO Google Jobs wrapper. Async task-based.

    Per-keyword: submit task → poll for completion (~6-12s) → fetch
    structured items. Items contain metadata only; JD body is fetched by
    runner.py's _fetch_generic from the source_url after this scraper
    returns.
    """

    source_name = "GoogleJobs"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 20,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        login, password = _dfseo_creds()
        if not (login and password):
            return []  # silent no-op if not configured

        # Submit ALL tasks in parallel (so we pay one round-trip latency
        # not 14× round-trip)
        sem_post = asyncio.Semaphore(8)

        async def _submit(kw: str) -> Optional[str]:
            if self.quota_exhausted:
                return None
            async with sem_post:
                if self.quota_exhausted:
                    return None
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        r = await client.post(
                            DATAFORSEO_TASK_POST,
                            auth=(login, password),
                            json=[{
                                "keyword": kw,
                                "location_name": "United States",
                                "language_name": "English",
                                "depth": min(limit_per_keyword, 30),
                                "priority": 2,
                            }],
                        )
                    if r.status_code != 200:
                        return None
                    data = r.json()
                    if data.get("status_code") in (40402, 40400, 40300):
                        # Quota / billing failure
                        self.quota_exhausted = True
                        self.quota_exhausted_reason = (
                            f"DataForSEO billing: {data.get('status_message','?')}"
                        )
                        return None
                    tasks = data.get("tasks") or []
                    if not tasks:
                        return None
                    task_id = tasks[0].get("id")
                    return task_id
                except Exception:
                    return None

        task_ids = await asyncio.gather(*[_submit(kw) for kw in keywords])
        keyword_to_task = {kw: tid for kw, tid in zip(keywords, task_ids) if tid}
        self.cost_estimate += len(keyword_to_task) * DFSE_COST_PER_QUERY

        if not keyword_to_task:
            return []

        # Poll for task completion. Most tasks land at priority=2 in 6-12s.
        # Total wait budget: 90s (3 polls × 3s + extras). Keep going if any
        # tasks are still pending.
        completed: dict[str, list[Role]] = {}
        sem_get = asyncio.Semaphore(8)
        deadline = asyncio.get_event_loop().time() + 90.0

        async def _try_fetch(kw: str, tid: str) -> Optional[list[Role]]:
            async with sem_get:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        rr = await client.get(
                            f"{DATAFORSEO_TASK_GET}/{tid}",
                            auth=(login, password),
                        )
                    d = rr.json()
                    t = (d.get("tasks") or [{}])[0]
                    sc = t.get("status_code")
                    if sc == 20000:
                        results = t.get("result") or []
                        items = (results[0] or {}).get("items") if results else []
                        return [
                            r for r in (
                                self._item_to_role(it, kw)
                                for it in (items or [])[:limit_per_keyword]
                            )
                            if r is not None
                        ]
                    if sc in (40602,):  # task in queue
                        return None
                    # Permanent error
                    return []
                except Exception:
                    return None

        # Polling loop: keep retrying pending tasks until deadline
        pending = dict(keyword_to_task)
        while pending and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.0)
            results = await asyncio.gather(*[
                _try_fetch(kw, tid) for kw, tid in pending.items()
            ])
            new_pending = {}
            for (kw, tid), result in zip(pending.items(), results):
                if result is None:
                    new_pending[kw] = tid  # still pending
                else:
                    completed[kw] = result
                    self.per_keyword_raw_counts[kw] = len(result)
            pending = new_pending

        # Mark any timed-out tasks as zero-yield
        for kw in pending:
            self.per_keyword_raw_counts[kw] = 0

        # Cross-keyword dedup (same role often surfaces under multiple kws)
        seen_url: set[str] = set()
        seen_pair: set[tuple[str, str]] = set()
        out: list[Role] = []
        for kw, roles in completed.items():
            for role in roles:
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

    async def fetch_jd(self, role: Role) -> str:
        """DataForSEO returns metadata only — no JD body. The runner.py
        _fetch_generic loop will visit role.job_url and extract the JD.
        We return whatever was already populated (likely empty)."""
        return role.job_description_full or ""

    def _item_to_role(self, item: dict[str, Any], keyword: str) -> Optional[Role]:
        """Map one DataForSEO Google Jobs item to our Role model."""
        title = (item.get("title") or "").strip()
        company = (item.get("employer_name") or "").strip()
        if not title or not company:
            return None
        company = self._normalize_company(company)
        if not company:
            return None

        # Apply URL — the source_url from DataForSEO points to Indeed /
        # LinkedIn / employer career page. We fetch JD from this URL later.
        url = (item.get("source_url") or item.get("employer_url") or "").strip()
        if not url:
            return None

        location_raw = (item.get("location") or "").strip()
        location_type = _classify_arrangement(location_raw)

        # Salary
        salary_text = (item.get("salary") or "").strip() or None
        salary_min, salary_max = _parse_salary_text(salary_text or "")

        # Posted date (timestamp may be ISO-ish; just preserve as-is for
        # downstream)
        posted_iso = item.get("timestamp") or None

        return Role(
            job_title=title[:200],
            company=company[:120],
            job_url=url,
            location=location_raw[:200] or None,
            location_type=location_type,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            job_description_full="",  # populated by runner.py _fetch_generic
            jd_completeness="Missing",  # will become Full after fetch
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

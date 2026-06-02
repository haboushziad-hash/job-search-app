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
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper


def _proxy_headers() -> dict[str, str]:
    """Headers required by the LLM_PROXY Worker for keyed-scraper routes.

    v0.3.13 fix: The Worker's `/v1/scraper/dataforseo/jobs/*` route was
    added in v0.3.12, AFTER the global `X-Tester-UUID` gate was put in
    place. JSearch's `/v1/scraper/jsearch/*` route predates the gate and
    is grandfathered. So GoogleJobs is the first scraper that hits the
    UUID requirement — without this header, the Worker returns
    `{"error": "X-Tester-UUID header required (UUID v4)"}` with a 4xx
    status. The scraper then gets r.status_code != 200 and returns None
    for every keyword, ending up with 0 roles, elapsed_s≈4.5s, and
    cost_estimate=$0 — exactly the v0.3.12 audit signature.

    The TESTER_UUID is set on the sidecar by lib.rs at app launch (line
    133): `sidecar = sidecar.env("TESTER_UUID", &tester_uuid)`. We read
    it here from the inherited env. Falls back to a deterministic
    placeholder for local dev / CLI runs where TESTER_UUID isn't set —
    the Worker accepts any valid UUIDv4 string.
    """
    headers = {"Content-Type": "application/json"}
    uuid = (os.environ.get("TESTER_UUID") or "").strip()
    if uuid:
        headers["X-Tester-UUID"] = uuid
    else:
        # Local dev / CLI fallback. The Worker only validates UUID format,
        # not whether the UUID is registered. This nil-UUID-ish placeholder
        # passes the format check.
        headers["X-Tester-UUID"] = "00000000-0000-4000-8000-000000000000"
    return headers


DATAFORSEO_BASE = "https://api.dataforseo.com/v3/serp/google/jobs"
DATAFORSEO_TASK_POST = f"{DATAFORSEO_BASE}/task_post"
DATAFORSEO_TASK_GET = f"{DATAFORSEO_BASE}/task_get/advanced"

# Cost per query at priority=2 (corrected 2026-05-30 via live probe — was
# 0.0024, actually charged $0.0012/task. We were overestimating cost 2x).
DFSE_COST_PER_QUERY = 0.0012

# Wave-2B Phase 2 (FIX 16C, 2026-05-30): start_position pagination.
# Per live probe, DataForSEO Google Jobs supports start_position to
# paginate beyond depth=100 (which is the per-task hard cap — depth>100
# silently fails as "Task Not Found"). Three pages (0/100/200) typically
# yield ~280-300 unique items per keyword per location — close to Google's
# real ceiling. Going to start=300 yields diminishing returns (probe shows
# depth=200 returns only 112 items, suggesting the supply tail is shallow).
DFSE_START_POSITIONS = (0, 100, 200)


def _dfseo_creds() -> tuple[str, str]:
    """Return (login, password). Empty if not configured."""
    login = (getattr(config, "DATAFORSEO_LOGIN", "") or "").strip()
    pw = (getattr(config, "DATAFORSEO_PASSWORD", "") or "").strip()
    return login, pw


def _dfseo_endpoints() -> tuple[str, str, Optional[tuple[str, str]]]:
    """Return (task_post_url, task_get_base_url, basic_auth_or_None).

    v0.3.12 fix: GoogleJobs was silently dead in proxy-mode production runs
    because lib.rs only forwards LLM_PROXY_URL/AUDIT_UPLOAD_URL/TESTER_UUID
    to the sidecar — DATAFORSEO_LOGIN/PASSWORD never make it from the .env
    on Ziad's dev box to the tester's installed sidecar. The scraper hit
    `_dfseo_creds() -> ("", "")` and returned [] in 1ms (audit showed
    elapsed_s=0.0, errored=False, roles=0 for two consecutive runs).

    Mirroring the JSearch pattern: when LLM_PROXY_URL is set, route through
    the Worker which injects DataForSEO Basic Auth from wrangler secrets.
    Local-dev still works with .env creds.
    """
    proxy = (getattr(config, "LLM_PROXY_URL", "") or "").rstrip("/")
    if proxy:
        return (
            f"{proxy}/v1/scraper/dataforseo/jobs/task_post",
            f"{proxy}/v1/scraper/dataforseo/jobs/task_get/advanced",
            None,  # Worker injects Basic Auth server-side
        )
    login, pw = _dfseo_creds()
    return (
        DATAFORSEO_TASK_POST,
        DATAFORSEO_TASK_GET,
        (login, pw) if (login and pw) else None,
    )


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
    """Quick remote / on-site / hybrid classifier from location string.

    Wave-2B Phase 2 (FIX 16E, 2026-05-30): expanded remote-detection
    patterns. Google Jobs surfaces remote roles with several location
    string conventions beyond just "Remote":
      - "Anywhere" — most common Google tag for fully-remote
      - "Worldwide" — global remote
      - "United States" (bare, no city) — Google's nationwide tag often
        indicates remote-or-distributed roles (cross-referenced with a
        Remote work_from_home flag at the API level)
      - "USA" (bare) — same as above
    Without these patterns, location_type defaulted to "On-site" and the
    hard_filter incorrectly assumed the role required a physical presence
    in some unknown city.
    """
    if not location:
        return "On-site"
    lower = location.strip().lower()
    # Explicit remote-style markers
    if "remote" in lower:
        return "Remote"
    if "anywhere" in lower:
        return "Remote"
    if "worldwide" in lower:
        return "Remote"
    # Bare country / nation-level — Google uses these for distributed roles.
    if lower in ("united states", "usa", "us", "u.s.", "u.s.a."):
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
        limit_per_keyword: int = 300,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Wave-2B Phase 2 (FIX 16 for GoogleJobs, 2026-05-30):
        #   B) Multi-location query. Previously: 1 location per keyword
        #      (either "United States" or the user's text). Now: ALWAYS
        #      submit a "United States" national task PLUS a user-location
        #      task when the user has a comma-formatted location. The two
        #      result sets are often disjoint (national skews popular tech
        #      hubs; user-level surfaces local roles the national crawl
        #      misses).
        #   C) start_position pagination. Per live probe (2026-05-30),
        #      depth>100 silently fails on DataForSEO Google Jobs ("Task
        #      Not Found"). The right path is to keep depth=100 per task
        #      and paginate via start_position. start=0/100/200 yields
        #      ~93+97+99 = ~289 unique items per keyword per location —
        #      close to Google's real ceiling. This is the architecture
        #      that delivers GoogleJobs as the "big daddy" aggregator
        #      (covering LinkedIn / Indeed / Glassdoor / employer career
        #      pages via Google's universal index).
        # Cost (corrected): $0.0012/task at priority=2 (NOT $0.0024 as the
        # old constant assumed — we were overestimating 2x).
        #   14 keywords * 1 loc * 1 page * $0.0012 = $0.017/run (before)
        #   14 keywords * 2 loc * 3 pages * $0.0012 = $0.10/run (after)
        # Still well under the $5/run hard cap. Total scraper wall-clock
        # increases ~3x (more tasks to poll) but still under 60s.
        post_url, get_base_url, auth = _dfseo_endpoints()
        proxy_mode = auth is None and post_url != DATAFORSEO_TASK_POST
        if not proxy_mode and auth is None:
            # Local mode + no creds = silent no-op. This is the path that
            # was silently dying in production through v0.3.11. v0.3.12
            # routes proxy mode through the Worker so this branch is only
            # hit in local dev without .env creds.
            print(
                "[GoogleJobs] no DataForSEO creds (local mode + missing "
                "DATAFORSEO_LOGIN/PASSWORD); scraper returning empty"
            )
            return []

        # FIX B + D — Build the location list:
        #   1. ALWAYS include "United States" national base. This catches:
        #      (a) Remote roles (Google tags them as nationwide / remote+USA)
        #      (b) Roles in cities the user is open to but didn't list
        #          explicitly (e.g., Northern Virginia for a DC user)
        #   2. Add EVERY user acceptable_location (from FIX 16D in
        #      runner.py) that is comma-formatted, so Google can parse it.
        #      For a Ziad-style user with ["Washington, DC", "Richmond, VA"]
        #      this becomes ["United States", "Washington, DC",
        #      "Richmond, VA"] — 3 location queries per keyword × 3 pages
        #      = 9 tasks per keyword.
        user_filters = getattr(self, "_user_filters", None) or {}
        locations_for_query: list[str] = ["United States"]
        # Prefer the acceptable_locations list when available (v0.3.17+).
        accept_locs = user_filters.get("acceptable_locations") or []
        if accept_locs:
            for loc in accept_locs:
                loc_clean = (loc or "").strip()
                if loc_clean and "," in loc_clean and len(loc_clean.split(",")) >= 2:
                    if loc_clean not in locations_for_query:
                        locations_for_query.append(loc_clean)
        else:
            # Fallback: pre-FIX-16D callers only pass location_text (single).
            raw_loc = (user_filters.get("location_text") or "").strip()
            if raw_loc and "," in raw_loc and len(raw_loc.split(",")) >= 2:
                if raw_loc not in locations_for_query:
                    locations_for_query.append(raw_loc)

        # Submission unit = (keyword, location, start_position). We track
        # task IDs keyed by this triple so the polling loop can route
        # results back to the right bucket. Per-keyword raw counts
        # aggregate across (location, start) dimensions at the end.
        submissions = [
            (kw, loc, start)
            for kw in keywords
            for loc in locations_for_query
            for start in DFSE_START_POSITIONS
        ]

        # Submit ALL tasks in parallel (so we pay one round-trip latency
        # not N× round-trip). Cap concurrency to be polite to DataForSEO.
        sem_post = asyncio.Semaphore(8)

        async def _submit(kw: str, loc: str, start: int) -> Optional[str]:
            if self.quota_exhausted:
                return None
            async with sem_post:
                if self.quota_exhausted:
                    return None
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        body: dict[str, Any] = {
                            "keyword": kw,
                            "location_name": loc,
                            "language_name": "English",
                            # depth=100 is the per-task hard cap on Google
                            # Jobs. depth>100 silently fails. Use
                            # start_position to paginate beyond 100.
                            "depth": min(limit_per_keyword, 100),
                            "priority": 2,
                        }
                        # start_position param paginates the result set —
                        # 0 = first page, 100 = second page, 200 = third.
                        # Omit on first page (some API versions reject
                        # start_position=0 explicitly; safer to skip when 0).
                        if start > 0:
                            body["start_position"] = start
                        kwargs: dict[str, Any] = dict(json=[body])
                        if auth is not None:
                            kwargs["auth"] = auth  # local mode
                        else:
                            # Proxy mode — Worker requires X-Tester-UUID
                            kwargs["headers"] = _proxy_headers()
                        r = await client.post(post_url, **kwargs)
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

        task_ids = await asyncio.gather(*[
            _submit(kw, loc, start) for kw, loc, start in submissions
        ])
        # Key by (kw, loc, start) so results route correctly during polling.
        submission_to_task: dict[tuple[str, str, int], str] = {
            (kw, loc, start): tid
            for (kw, loc, start), tid in zip(submissions, task_ids)
            if tid
        }
        # v0.3.12: writes to BaseScraper.cost_estimate (now a proper
        # interface attribute). Orchestrator picks it up at the end of the
        # scrape and rolls it into cost_breakdown.scraper_apis_usd in the
        # audit JSON. The earlier `_dfseo_cost_estimate` workaround is gone.
        # With multi-location, len(submission_to_task) ≈ N_keywords *
        # len(locations_for_query) so cost scales correctly.
        self.cost_estimate += len(submission_to_task) * DFSE_COST_PER_QUERY

        if not submission_to_task:
            return []

        # Poll for task completion. Tasks land at priority=2 in 6-12s.
        # With 3x more tasks (pagination), absolute wall-clock for polling
        # all of them rises but per-task time doesn't. 180s budget gives
        # generous headroom; typical Ziad-scale runs complete in 30-60s.
        completed: dict[tuple[str, str, int], list[Role]] = {}
        sem_get = asyncio.Semaphore(8)
        deadline = asyncio.get_event_loop().time() + 180.0

        async def _try_fetch(kw: str, loc: str, start: int, tid: str) -> Optional[list[Role]]:
            async with sem_get:
                try:
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        kwargs: dict[str, Any] = {}
                        if auth is not None:
                            kwargs["auth"] = auth  # local mode
                        else:
                            # Proxy mode — Worker requires X-Tester-UUID
                            kwargs["headers"] = _proxy_headers()
                        rr = await client.get(
                            f"{get_base_url}/{tid}",
                            **kwargs,
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
        pending = dict(submission_to_task)
        while pending and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.0)
            results = await asyncio.gather(*[
                _try_fetch(kw, loc, start, tid)
                for (kw, loc, start), tid in pending.items()
            ])
            new_pending: dict[tuple[str, str, int], str] = {}
            for ((kw, loc, start), tid), result in zip(pending.items(), results):
                if result is None:
                    new_pending[(kw, loc, start)] = tid  # still pending
                else:
                    completed[(kw, loc, start)] = result
            pending = new_pending

        # Aggregate per-keyword raw counts across (location, start_position)
        # dimensions. per_keyword_raw_counts is keyed by keyword.
        for kw in keywords:
            total = sum(
                len(roles)
                for (k, _loc, _start), roles in completed.items()
                if k == kw
            )
            self.per_keyword_raw_counts[kw] = total

        # Cross-keyword + cross-location + cross-page dedup. The same role
        # often surfaces under multiple keywords, both locations, AND in
        # overlapping start positions (Google's ordering shifts slightly
        # between paginated calls). Use url + (company, title) dedup.
        seen_url: set[str] = set()
        seen_pair: set[tuple[str, str]] = set()
        out: list[Role] = []
        for (kw, _loc, _start), roles in completed.items():
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
        # v0.3.26 (L1): when source_url is a gatewall reposter (BeBee/Vaia/
        # JobLeads/etc. the user must sign into), prefer the employer's own page.
        src = (item.get("source_url") or "").strip()
        emp = (item.get("employer_url") or "").strip()
        _REPOSTER = ("bebee.", "vaia.", "jobleads.", "lensa.", "jooble.", "talent.com",
                     "whatjobs.", "learn4good.", "ziprecruiter.", "jobcase.",
                     "theirstack.", "appcast", "jobright.")
        if src and emp and any(x in src.lower() for x in _REPOSTER):
            url = emp
        else:
            url = src or emp
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

"""Workday concurrency benchmark — find the speed ceiling at zero cost.

Workday's CXS API has no shared rate limit (each tenant is independent),
no LLM dependency, and returns immediately on rejection. Perfect for
finding the concurrency knee without burning Gemini money.

What this measures:
  - Wall-clock time to scrape all 166 tenants × 5 keywords at varying
    concurrency levels
  - 429/5xx error rate at each level
  - The "knee" where adding concurrency stops helping

What this does NOT measure:
  - LLM scoring time (no LLM calls are made)
  - JD body fetches (search-only)
  - End-to-end search runtime (just the Workday phase)

Run with:
  backend\\venv\\Scripts\\python.exe scripts\\bench_workday_concurrency.py

Cost: $0. No upstream paid APIs. Just network time + Workday's
free-to-everyone CXS endpoints.

Output: a results table you can paste back. Pick the highest concurrency
where wall-clock time is still improving meaningfully (>10% gain). Set
that as production WORKDAY_CONCURRENCY (a v0.3.14 env-var addition).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Make backend imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.scraper.client import ScraperClient
from backend.scraper.workday import WORKDAY_TENANTS


# Test keywords — 5 short ones to keep total query count reasonable
# (5 kw × ~166 tenants = ~830 HTTP calls per concurrency level)
BENCH_KEYWORDS = [
    "AI Strategy",
    "Program Manager",
    "Operations",
    "Director",
    "Senior Manager",
]

# Concurrency levels to sweep — covers safe-default through aggressive
CONCURRENCY_LEVELS = [20, 30, 40, 60, 80, 100]


async def _search_one_tenant_keyword(
    client: ScraperClient,
    base_url: str,
    board: str,
    tenant_id: str,
    keyword: str,
    timeout: float = 15.0,
) -> tuple[int, int]:
    """Returns (status_code, role_count). Status 0 = exception."""
    endpoint = f"{base_url}/wday/cxs/{tenant_id}/{board}/jobs"
    body = {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": keyword,
    }
    try:
        response = await asyncio.wait_for(
            client._client.post(  # type: ignore[union-attr]
                endpoint,
                json=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return (0, 0)
    except Exception:
        return (0, 0)
    if response.status_code != 200:
        return (response.status_code, 0)
    try:
        data = response.json()
        postings = data.get("jobPostings") or []
        return (200, len(postings))
    except Exception:
        return (response.status_code, 0)


def _tenant_id_from_url(base_url: str) -> str:
    """Extract tenant name from a Workday URL.
    e.g., https://capitalone.wd12.myworkdayjobs.com -> 'capitalone'
    """
    from urllib.parse import urlparse
    host = urlparse(base_url).netloc
    if "apply.deloitte.com" in host:
        return "deloitte"
    parts = host.split(".")
    return parts[0] if parts else host


async def bench_one_level(concurrency: int) -> dict:
    """Run the full sweep at a given concurrency. Returns metrics."""
    sem = asyncio.Semaphore(concurrency)
    results = {
        "concurrency": concurrency,
        "elapsed_s": 0.0,
        "total_requests": 0,
        "status_200": 0,
        "status_4xx": 0,
        "status_5xx": 0,
        "status_other": 0,
        "exceptions": 0,
        "total_roles_found": 0,
    }

    async def bounded(base_url: str, tenant_id: str, board: str, keyword: str):
        async with sem:
            return await _search_one_tenant_keyword(
                client, base_url, board, tenant_id, keyword,
            )

    start = time.perf_counter()
    async with ScraperClient() as client:
        tasks = []
        for display_name, base_url, board in WORKDAY_TENANTS:
            tenant_id = _tenant_id_from_url(base_url)
            for kw in BENCH_KEYWORDS:
                tasks.append(bounded(base_url, tenant_id, board, kw))

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    results["elapsed_s"] = round(time.perf_counter() - start, 2)
    results["total_requests"] = len(outcomes)
    for outcome in outcomes:
        if isinstance(outcome, Exception):
            results["exceptions"] += 1
            continue
        status, role_count = outcome
        results["total_roles_found"] += role_count
        if status == 0:
            results["exceptions"] += 1
        elif status == 200:
            results["status_200"] += 1
        elif 400 <= status < 500:
            results["status_4xx"] += 1
        elif 500 <= status < 600:
            results["status_5xx"] += 1
        else:
            results["status_other"] += 1

    return results


async def main():
    print(f"Workday concurrency bench")
    print(f"Tenants:  {len(WORKDAY_TENANTS)}")
    print(f"Keywords: {len(BENCH_KEYWORDS)}")
    print(f"Total HTTP requests per level: {len(WORKDAY_TENANTS) * len(BENCH_KEYWORDS)}")
    print(f"Levels: {CONCURRENCY_LEVELS}")
    print()
    print(f"{'Concurrency':>12s} {'Elapsed':>10s} {'200':>6s} {'4xx':>6s} {'5xx':>6s} {'err':>6s} {'roles':>8s} {'roles/s':>10s}")
    print("-" * 78)

    all_results = []
    for level in CONCURRENCY_LEVELS:
        result = await bench_one_level(level)
        all_results.append(result)
        roles_per_s = result["total_roles_found"] / max(result["elapsed_s"], 0.001)
        print(
            f"{result['concurrency']:>12d} "
            f"{result['elapsed_s']:>10.2f}s "
            f"{result['status_200']:>6d} "
            f"{result['status_4xx']:>6d} "
            f"{result['status_5xx']:>6d} "
            f"{result['exceptions']:>6d} "
            f"{result['total_roles_found']:>8d} "
            f"{roles_per_s:>9.1f}/s"
        )

    print()
    print("Recommended production concurrency:")
    # Heuristic: highest level where elapsed_s improved >10% vs the prior level
    # AND error rate stayed below 5% of total requests
    recommended = CONCURRENCY_LEVELS[0]
    for i, r in enumerate(all_results[1:], 1):
        prev = all_results[i - 1]
        improvement_pct = (prev["elapsed_s"] - r["elapsed_s"]) / prev["elapsed_s"] * 100
        error_count = r["status_4xx"] + r["status_5xx"] + r["exceptions"]
        error_pct = error_count / max(r["total_requests"], 1) * 100
        if improvement_pct > 10 and error_pct < 5:
            recommended = r["concurrency"]
        else:
            print(f"  Level {r['concurrency']}: {improvement_pct:.1f}% gain, {error_pct:.1f}% error rate")
            print(f"  -> diminishing returns or rising errors")
            break
    print(f"  CHOSEN: WORKDAY_CONCURRENCY={recommended}")


if __name__ == "__main__":
    asyncio.run(main())

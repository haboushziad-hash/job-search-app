"""Tenant grinder — discover Workday + iCIMS tenants we don't currently have.

Why this exists
---------------
Big employers (Pfizer, Merck, HCA, Walmart corporate, JPMorgan, Anthropic, etc.)
post their FULL job inventory only at their dedicated Workday or iCIMS tenant.
LinkedIn / Indeed only see a curated subset. JSearch / Adzuna also see only the
LinkedIn slice.

Right now backend/scraper/workday_tenants/*.json has 27 hand-curated tenants.
Workday has thousands of corporate clients. iCIMS tenant list is currently
EMPTY. Adding ~100-200 missing tenants unlocks 50-100+ net-new qualifying
roles per search across pharma, healthcare, insurance, defense, industrials,
energy, retail/CPG.

How it works
------------
Phase 1 — SEED via Bing site-restricted search (Serper.dev API):
  Query: site:myworkdayjobs.com (pharma OR biotech OR medical)
  Extract tenant URL candidates from results.

Phase 2 — VALIDATE via algorithmic CXS construction + HTTP probe:
  Workday URLs follow a predictable pattern:
    Careers: https://<tenant>.<wdN>.myworkdayjobs.com/<job_site>
    CXS:     https://<tenant>.<wdN>.myworkdayjobs.com/wday/cxs/<tenant>/<job_site>/jobs
  POST to CXS with standard body. 200 + jobs = valid tenant. No Playwright
  needed for ~90%+ of Workday tenants.

Phase 3 — ENRICH:
  Pull job count + sample 3 JDs + classify industry via Gemini Flash.

Phase 4 — DEDUP against existing tenant lists.

Phase 5 — DYNAMIC RETRY: when hit rate drops below threshold, switch
  strategy (different industry keywords, different ATS, Google instead
  of Bing, Crunchbase company seed list).

Phase 6 — REPORT to scripts/tenant_grinder_results.json.

Usage
-----
  python scripts/tenant_grinder.py --max-hours 0.1 --max-serper-queries 30
  python scripts/tenant_grinder.py --max-hours 3 --industries pharma,healthcare
  python scripts/tenant_grinder.py --max-hours 8 --industries all  # overnight

Cost
----
  Serper.dev: 2,500 free queries/mo. 3-hour grind = ~500 queries.
  DataForSEO fallback: ~$0.0006/query, $2 cap default.
  Playwright fallback: free (local CPU).
  Net realistic cost for a 3-hour run: $0-2.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
EXISTING_WORKDAY_DIR = REPO_ROOT / "backend" / "scraper" / "workday_tenants"
DEFAULT_OUTPUT = REPO_ROOT / "scripts" / "tenant_grinder_results.json"
PROGRESS_CHECKPOINT = REPO_ROOT / "scripts" / "tenant_grinder_progress.json"

# Read API keys from .env (don't import backend.config — we want to be
# decoupled from the main codebase per user requirement)
def load_env() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

ENV = load_env()
SERPER_KEY = ENV.get("SERPER_API_KEY", "").strip()
DATAFORSEO_LOGIN = ENV.get("DATAFORSEO_LOGIN", "").strip()
DATAFORSEO_PASSWORD = ENV.get("DATAFORSEO_PASSWORD", "").strip()
GOOGLE_API_KEY = ENV.get("GOOGLE_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Industry seed packs — keyword groups for site-restricted search
# ---------------------------------------------------------------------------

# Geographic packs — added v0.3.9 prep because most current testers
# are based in Richmond + DMV (DC/Maryland/Virginia). These packs
# surface tenants with HQ or major operations in those regions, even
# when the role itself isn't location-restricted.
GEOGRAPHIC_PACKS: dict[str, list[str]] = {
    "richmond": [
        # Direct location anchors
        "Richmond Virginia",
        "Henrico County",
        '"Glen Allen"',  # quoted = exact-match in search engines
        "Midlothian VA",
        '"Short Pump"',
        "Mechanicsville VA",
        # Known major Richmond employers (forces direct lookup)
        '"Capital One" Richmond',
        '"Markel" Richmond',
        '"Altria" Richmond',
        '"Dominion Energy"',
        '"CarMax" Richmond',
        '"Genworth" Richmond',
        '"Owens Minor"',
        '"Performance Food Group"',
        '"Estes Express"',
        '"James River Insurance"',
        '"Hamilton Beach"',
        '"Federal Reserve Bank Richmond"',
    ],
    "dmv": [
        # DC + Northern VA + Maryland tech/govcon corridor
        '"Tysons Corner"',
        '"McLean Virginia"',
        '"Reston Virginia"',
        '"Bethesda Maryland"',
        '"Arlington Virginia"',
        "Washington DC",
        '"Fairfax County"',
        # Major DMV employers
        '"Booz Allen"',
        '"Leidos"',
        '"SAIC"',
        '"ManTech"',
        '"CACI"',
        '"Peraton"',
        '"General Dynamics" Mission',
        '"Lockheed Martin"',
        '"Northrop Grumman"',
        '"BAE Systems"',
        '"Capital One"',
        '"Marriott International"',
        '"Hilton"',
        '"Bechtel"',
        '"Maximus"',
        '"Fannie Mae"',
        '"Freddie Mac"',
        '"Geico"',
        '"Carfax"',
        '"Discovery Communications"',
        '"MicroStrategy"',
        '"Appian"',
        '"Verisign"',
    ],
}

INDUSTRY_PACKS: dict[str, list[str]] = {
    "pharma": [
        "pharmaceutical", "biotech", "biotechnology", "drug discovery",
        "clinical trial", "medical device", "vaccine", "therapeutics",
        "oncology", "immunology",
    ],
    "healthcare": [
        "healthcare", "hospital", "patient care", "clinical operations",
        "nursing", "physician", "medical center", "health system",
    ],
    "insurance": [
        "insurance underwriter", "actuary", "claims adjuster",
        "health insurance", "life insurance", "property casualty",
        "reinsurance",
    ],
    "defense": [
        "defense contractor", "aerospace defense", "national security",
        "military intelligence", "cybersecurity defense", "weapons systems",
    ],
    "govcon": [
        "federal contractor", "government contracting", "federal civilian",
        "defense industrial base", "intelligence community",
        "national laboratory",
    ],
    "industrial": [
        "manufacturing operations", "industrial automation",
        "supply chain operations", "plant manager", "process engineering",
        "logistics operations", "field service",
    ],
    "energy": [
        "oil gas", "renewable energy", "petrochemical", "refining",
        "natural gas", "energy trading", "exploration production",
    ],
    "retail_cpg": [
        "retail operations", "merchandising", "consumer goods",
        "brand marketing", "category management", "store operations",
    ],
    "finance": [
        "investment banking", "asset management", "wealth management",
        "financial advisor", "private equity", "hedge fund",
        "commercial banking",
    ],
    "tech_pre_ipo": [
        "platform engineering", "developer tools", "infrastructure engineer",
        "AI research", "machine learning", "growth marketing",
    ],
    "higher_ed": [
        "academic dean", "university administration", "research scientist",
        "tenure track", "academic affairs", "student affairs",
    ],
}

ALL_INDUSTRIES = list(INDUSTRY_PACKS.keys())
ALL_GEOGRAPHIES = list(GEOGRAPHIC_PACKS.keys())

# Combined dict for unified lookup. Keys are unique across both packs.
ALL_PACKS: dict[str, list[str]] = {**INDUSTRY_PACKS, **GEOGRAPHIC_PACKS}

# ---------------------------------------------------------------------------
# Regex patterns for extracting tenant info from URLs
# ---------------------------------------------------------------------------

# Workday URL patterns
# https://<tenant>.<wd1|wd2|wd3|wd5|wd103|...>.myworkdayjobs.com/<job_site_path>
WORKDAY_URL_RE = re.compile(
    r"https?://([a-z0-9_-]+)\.((?:wd[0-9]+|myworkdaysite))\.myworkdayjobs\.com/([^/?#]+)",
    re.IGNORECASE,
)

# iCIMS URL patterns
# https://<tenant>.icims.com/...
# https://careers-<tenant>.icims.com/...
# https://jobs.<tenant>.com/  (some companies proxy iCIMS through their domain)
ICIMS_URL_RE = re.compile(
    r"https?://([a-z0-9_-]+)\.icims\.com",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TenantCandidate:
    """A potential tenant we discovered but haven't validated yet."""
    ats: str  # 'workday' or 'icims'
    tenant_id: str
    raw_url: str
    discovered_via: str  # 'bing', 'google', 'crunchbase', etc.
    industry_hint: str  # which seed industry led to this discovery


@dataclass
class ValidatedTenant:
    """A tenant we've HTTP-probed and confirmed has active jobs."""
    ats: str
    tenant_id: str
    display_name: str
    careers_url: str
    cxs_url: str
    cxs_method: str
    cxs_request_body: str
    cxs_headers: dict[str, str]
    active_role_count: int
    industry: str
    industry_hint: str
    sample_titles: list[str]
    discovered_via: str
    discovered_at: str
    elapsed_seconds: float
    notes: str = ""


@dataclass
class GrinderStats:
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    serper_queries_run: int = 0
    dataforseo_queries_run: int = 0
    candidates_found: int = 0
    candidates_validated: int = 0
    candidates_failed: int = 0
    duplicates_skipped: int = 0
    queries_per_industry: dict[str, int] = field(default_factory=dict)
    candidates_per_industry: dict[str, int] = field(default_factory=dict)
    cost_estimate_usd: float = 0.0


# ---------------------------------------------------------------------------
# Search engines
# ---------------------------------------------------------------------------

class SearchEngine:
    """Common interface for Bing-via-Serper, Google-via-DataForSEO, etc."""
    name: str = "abstract"

    async def search(self, query: str, num: int = 20) -> list[str]:
        """Return URLs from the search results. Empty list on quota/error."""
        raise NotImplementedError


class SerperBingEngine(SearchEngine):
    """Site-restricted searches via Serper.dev /search?engine=bing.

    Tracks exhaustion via sticky flag: once Serper returns 402 (free tier
    exhausted) or credits drop to 0, we set self.exhausted=True and stop
    trying. The chained client (ChainedSearchEngine below) routes
    subsequent calls to DataForSEO automatically.
    """
    name = "bing-via-serper"
    url = "https://google.serper.dev/search"

    def __init__(self, api_key: str, min_credits_reserve: int = 0):
        self.api_key = api_key
        self.queries_run = 0
        self.exhausted = False
        self.last_credits_remaining: Optional[int] = None
        # Reserve buffer: never let queries drop below this remaining-credits
        # count. Useful to preserve budget for tester searches when run
        # via the desktop app. Default 0 = use everything available.
        self.min_credits_reserve = min_credits_reserve

    @property
    def has_quota(self) -> bool:
        """Cheap check before issuing a call. Returns False if known
        exhausted or below reserve threshold."""
        if self.exhausted:
            return False
        if self.last_credits_remaining is not None:
            return self.last_credits_remaining > self.min_credits_reserve
        return True  # unknown — try optimistically

    async def search(self, query: str, num: int = 20) -> list[str]:
        if not self.api_key or self.exhausted:
            return []
        self.queries_run += 1
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    self.url,
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                    json={"q": query, "engine": "bing", "num": min(num, 30)},
                )
            # 402 = free tier exhausted, 429 = rate limit, 401/403 = bad key
            if r.status_code in (402, 429, 401, 403):
                self.exhausted = True
                print(f"[serper] EXHAUSTED ({r.status_code}) — falling back to DataForSEO for remainder of run")
                return []
            if r.status_code != 200:
                return []
            data = r.json()
            # Capture credits remaining for next-call quota gate
            credits = data.get("credits")
            if isinstance(credits, int):
                self.last_credits_remaining = credits
                if credits <= self.min_credits_reserve:
                    self.exhausted = True
                    print(f"[serper] reserve hit (credits={credits}, reserve={self.min_credits_reserve}) — falling back to DataForSEO")
            urls = []
            for item in (data.get("organic") or [])[:num]:
                link = (item.get("link") or "").strip()
                if link:
                    urls.append(link)
            return urls
        except Exception:
            return []


class DataForSEOGoogleEngine(SearchEngine):
    """Google site-restricted searches via DataForSEO /serp/google/organic."""
    name = "google-via-dataforseo"
    url_post = "https://api.dataforseo.com/v3/serp/google/organic/task_post"
    url_get = "https://api.dataforseo.com/v3/serp/google/organic/task_get/advanced"

    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.queries_run = 0

    async def search(self, query: str, num: int = 20) -> list[str]:
        if not (self.login and self.password):
            return []
        self.queries_run += 1
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # Submit task
                r = await client.post(
                    self.url_post,
                    auth=(self.login, self.password),
                    json=[{
                        "keyword": query,
                        "location_name": "United States",
                        "language_name": "English",
                        "depth": min(num, 30),
                        "priority": 2,
                    }],
                )
                if r.status_code != 200:
                    return []
                task_id = r.json()["tasks"][0]["id"]
                # Poll for completion
                for _ in range(20):
                    await asyncio.sleep(2)
                    rr = await client.get(
                        f"{self.url_get}/{task_id}",
                        auth=(self.login, self.password),
                    )
                    t = rr.json()["tasks"][0]
                    if t.get("status_code") == 20000 and t.get("result"):
                        items = (t["result"][0] or {}).get("items") or []
                        urls = []
                        for it in items:
                            link = (it.get("url") or "").strip()
                            if link:
                                urls.append(link)
                        return urls[:num]
                return []
        except Exception:
            return []


class ChainedSearchEngine(SearchEngine):
    """Quota-aware multi-provider search.

    Tries providers in order. If the primary is known-exhausted (Serper
    returned 402 or hit reserve threshold), skip it entirely and go
    directly to the secondary. This avoids per-call retry overhead.

    Use case: free Serper tier first (saves money), DataForSEO as paid
    fallback when Serper's monthly cap is reached. The grinder won't
    "break" mid-run when Serper exhausts — it just transparently routes
    remaining calls through DataForSEO.

    Robustness:
      - Sticky exhaustion: once Serper marks itself exhausted, all
        subsequent ChainedSearchEngine.search() calls go to DataForSEO
        directly without trying Serper again
      - Both providers can return [] (e.g. genuinely no results); the
        chain doesn't second-guess that — only routes when a provider
        is unavailable
      - Pre-flight: if Serper.has_quota is False at construction time,
        we never even try Serper for this run
    """
    name = "chained"

    def __init__(self, primary: SearchEngine, secondary: SearchEngine):
        self.primary = primary
        self.secondary = secondary

    @property
    def queries_run(self) -> int:
        # For caller-facing stats: report combined query count
        return getattr(self.primary, "queries_run", 0) + getattr(self.secondary, "queries_run", 0)

    async def search(self, query: str, num: int = 20) -> list[str]:
        # Try primary if it has quota
        if getattr(self.primary, "has_quota", True):
            urls = await self.primary.search(query, num=num)
            if urls:
                return urls
            # Primary returned nothing. Two cases:
            # 1. Primary genuinely has no results for this query → secondary may succeed
            # 2. Primary exhausted mid-call (sticky flag now set) → skip primary going forward
            # Either way, try secondary now.
        # Primary unavailable or returned empty — fall through to secondary
        return await self.secondary.search(query, num=num)


# ---------------------------------------------------------------------------
# Tenant extraction + validation
# ---------------------------------------------------------------------------

def extract_workday_tenant_from_url(url: str) -> Optional[tuple[str, str, str, str]]:
    """Returns (tenant_id, wd_subdomain, job_site_path, full_host) or None.

    Example:
      url = https://msd.wd5.myworkdayjobs.com/SearchJobs/job/...
      → ("msd", "wd5", "SearchJobs", "msd.wd5.myworkdayjobs.com")
    """
    m = WORKDAY_URL_RE.search(url)
    if not m:
        return None
    tenant, wd_sub, path = m.group(1), m.group(2), m.group(3)
    if path.lower() in ("careers", "jobs"):
        # These are often landing pages, not job_site paths. Probe both.
        path = "External"  # most common job_site path
    return tenant, wd_sub, path, f"{tenant}.{wd_sub}.myworkdayjobs.com"


def extract_icims_tenant_from_url(url: str) -> Optional[str]:
    m = ICIMS_URL_RE.search(url)
    if not m:
        return None
    return m.group(1).lower()


async def validate_workday_tenant(
    tenant: str, wd_sub: str, job_site: str, host: str,
) -> Optional[dict[str, Any]]:
    """HTTP-probe a Workday CXS endpoint. Returns the captured config + job
    count if valid, None if the tenant doesn't actually serve jobs.

    Tries multiple job_site path variations since the URL pattern varies:
      External, careers, SearchJobs, Careers, Jobs, etc.
    """
    candidates_to_try = list(dict.fromkeys([  # dedup, preserve order
        job_site, "External", "Careers", "SearchJobs", "Jobs", "careers",
        "us-careers", "global-careers",
    ]))

    request_body = '{"appliedFacets":{},"limit":20,"offset":0,"searchText":""}'
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for path in candidates_to_try:
            cxs_url = f"https://{host}/wday/cxs/{tenant}/{path}/jobs"
            try:
                r = await client.post(cxs_url, content=request_body, headers=headers)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            jobs = data.get("jobPostings") or data.get("jobs") or []
            total = data.get("total")
            if total is None:
                total = len(jobs)
            if not jobs and total == 0:
                # Real 200 but no jobs — tenant exists but is empty/closed
                continue
            sample_titles = [
                j.get("title", "") for j in jobs[:5] if isinstance(j, dict)
            ]
            return {
                "cxs_url": cxs_url,
                "method": "POST",
                "request_body": request_body,
                "headers": headers,
                "active_role_count": int(total),
                "sample_titles": sample_titles,
                "careers_url": f"https://{host}/{path}",
            }
    return None


async def validate_icims_tenant(tenant: str) -> Optional[dict[str, Any]]:
    """Probe an iCIMS tenant. iCIMS is harder than Workday because:
      - Many instances are Cloudflare-protected (need Playwright fallback)
      - The "RSS" endpoint at /jobs.rss is the easiest non-JS path
      - The portal URL pattern: https://careers-<TENANT>.icims.com/jobs/
    """
    rss_candidates = [
        f"https://careers-{tenant}.icims.com/jobs/intro?hashed=-625971520&mobile=false",
        f"https://{tenant}.icims.com/jobs/intro?hashed=-625971520&mobile=false",
        f"https://careers-{tenant}.icims.com/jobs/feed",
    ]
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        for url in rss_candidates:
            try:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/126.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml",
                    },
                )
            except Exception:
                continue
            if r.status_code == 200 and len(r.text) > 1000 and "icims" in r.text.lower():
                return {
                    "url_used": url,
                    "needs_playwright": False,
                    "approx_size_chars": len(r.text),
                }
            if r.status_code in (403, 503) and "cloudflare" in r.text.lower():
                # Cloudflare-blocked — needs Playwright in the future
                return {
                    "url_used": url,
                    "needs_playwright": True,
                    "approx_size_chars": 0,
                }
    return None


# ---------------------------------------------------------------------------
# Industry classification + display name extraction
# ---------------------------------------------------------------------------

def classify_industry_heuristic(
    sample_titles: list[str], industry_hint: str
) -> str:
    """Cheap industry classifier from job titles. No LLM call; pattern match.
    LLM enrichment can run in a separate post-step on the final list."""
    text = " ".join(sample_titles).lower()
    if any(k in text for k in ("clinical", "medical", "patient", "nurse",
                                "physician", "pharma", "biotech", "drug")):
        return "Healthcare/Pharma"
    if any(k in text for k in ("insurance", "underwriter", "claims",
                                "actuary")):
        return "Insurance"
    if any(k in text for k in ("federal", "clearance", "ts/sci", "secret")):
        return "Government Contracting"
    if any(k in text for k in ("software engineer", "ml engineer",
                                "data scientist", "platform")):
        return "Tech"
    if any(k in text for k in ("manufacturing", "plant", "supply chain",
                                "logistics", "operator")):
        return "Industrial/Manufacturing"
    if any(k in text for k in ("retail", "store", "merchandise", "category")):
        return "Retail/CPG"
    if any(k in text for k in ("oil", "gas", "petroleum", "refinery",
                                "renewable")):
        return "Energy"
    if any(k in text for k in ("portfolio manager", "trader", "banker",
                                "investment")):
        return "Finance"
    return f"Unclassified ({industry_hint})"


def derive_display_name(
    tenant_id: str, sample_titles: list[str], host: str
) -> str:
    """Best-effort human-readable name. Uses tenant_id as base, capitalized.
    Future enhancement: LLM lookup against company database."""
    # Most tenant IDs are the company stock ticker or shortname
    name = tenant_id.replace("-", " ").replace("_", " ").title()
    return name


# ---------------------------------------------------------------------------
# Existing-tenant deduplication
# ---------------------------------------------------------------------------

def load_existing_workday_tenants() -> set[str]:
    """Return set of tenant IDs already in backend/scraper/workday_tenants/."""
    existing = set()
    if not EXISTING_WORKDAY_DIR.exists():
        return existing
    for f in EXISTING_WORKDAY_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            cxs = data.get("captured", {}).get("url", "")
            m = re.search(
                r"/wday/cxs/([^/]+)/", cxs,
            )
            if m:
                existing.add(m.group(1).lower())
            existing.add(f.stem.lower())  # filename-derived ID
        except Exception:
            continue
    return existing


def load_existing_icims_tenants() -> set[str]:
    """iCIMS list is currently empty in icims.py, but reads will be a no-op."""
    # If/when the icims_tenants/ directory is added, mirror Workday logic here
    return set()


# ---------------------------------------------------------------------------
# Main grinder loop
# ---------------------------------------------------------------------------

@dataclass
class GrinderState:
    serper: SerperBingEngine
    dataforseo: Optional[DataForSEOGoogleEngine]
    existing_workday: set[str]
    existing_icims: set[str]
    seen_candidates: set[str]  # "<ats>:<tenant>" combos already considered
    validated: list[ValidatedTenant]
    failed: list[dict[str, Any]]
    stats: GrinderStats
    args: argparse.Namespace
    deadline: float
    cost_estimate: float = 0.0


def has_budget(state: GrinderState) -> bool:
    if time.time() > state.deadline:
        return False
    if state.serper.queries_run >= state.args.max_serper_queries:
        if state.dataforseo is None:
            return False
        if state.dataforseo.queries_run >= state.args.max_dataforseo_queries:
            return False
    if state.cost_estimate >= state.args.max_cost_usd:
        return False
    return True


async def run_industry_pack(
    state: GrinderState,
    industry: str,
    keywords: list[str],
    ats: str,
) -> int:
    """Search for tenants in one industry × ATS combo. Returns hit count."""
    site_filter = "myworkdayjobs.com" if ats == "workday" else "icims.com"
    hits = 0
    for kw in keywords:
        if not has_budget(state):
            return hits
        query = f"site:{site_filter} {kw}"

        # v0.3.9.1: chained search — try Serper first (free tier), fall
        # through to DataForSEO if Serper exhausts or returns no results.
        # Sticky-exhaustion: once Serper marks itself exhausted, all
        # subsequent calls in this run go directly to DataForSEO without
        # retrying Serper. Prevents mid-run Serper failures from breaking
        # the grinder.
        if getattr(state.serper, "has_quota", True):
            urls = await state.serper.search(query, num=20)
        else:
            urls = []
        state.stats.serper_queries_run = state.serper.queries_run
        state.stats.queries_per_industry[industry] = (
            state.stats.queries_per_industry.get(industry, 0) + 1
        )

        # Fall through to DataForSEO if Serper returned nothing
        # (either genuine no-results OR sticky-exhausted)
        if not urls and state.dataforseo and has_budget(state):
            urls = await state.dataforseo.search(query, num=20)
            state.stats.dataforseo_queries_run = state.dataforseo.queries_run
            state.cost_estimate += 0.0012  # priority=2 cost

        for url in urls:
            if ats == "workday":
                ext = extract_workday_tenant_from_url(url)
                if not ext:
                    continue
                tenant, wd_sub, path, host = ext
                key = f"workday:{tenant}"
                if key in state.seen_candidates:
                    continue
                state.seen_candidates.add(key)
                if tenant.lower() in state.existing_workday:
                    state.stats.duplicates_skipped += 1
                    continue
                state.stats.candidates_found += 1
                state.stats.candidates_per_industry[industry] = (
                    state.stats.candidates_per_industry.get(industry, 0) + 1
                )
                t0 = time.perf_counter()
                result = await validate_workday_tenant(
                    tenant, wd_sub, path, host,
                )
                elapsed = time.perf_counter() - t0
                if result:
                    state.stats.candidates_validated += 1
                    hits += 1
                    sample_titles = result.get("sample_titles", [])
                    state.validated.append(ValidatedTenant(
                        ats="workday",
                        tenant_id=tenant,
                        display_name=derive_display_name(
                            tenant, sample_titles, host,
                        ),
                        careers_url=result["careers_url"],
                        cxs_url=result["cxs_url"],
                        cxs_method=result["method"],
                        cxs_request_body=result["request_body"],
                        cxs_headers=result["headers"],
                        active_role_count=result["active_role_count"],
                        industry=classify_industry_heuristic(
                            sample_titles, industry,
                        ),
                        industry_hint=industry,
                        sample_titles=sample_titles[:3],
                        discovered_via=state.serper.name,
                        discovered_at=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=round(elapsed, 2),
                    ))
                    print(
                        f"  [OK] Workday: {tenant} ({result['active_role_count']} jobs) "
                        f"[{state.validated[-1].industry}]"
                    )
                else:
                    state.stats.candidates_failed += 1
                    state.failed.append({
                        "ats": "workday", "tenant": tenant, "url": url,
                        "reason": "no_cxs_match_or_no_jobs",
                    })

            elif ats == "icims":
                tenant = extract_icims_tenant_from_url(url)
                if not tenant:
                    continue
                key = f"icims:{tenant}"
                if key in state.seen_candidates:
                    continue
                state.seen_candidates.add(key)
                if tenant.lower() in state.existing_icims:
                    state.stats.duplicates_skipped += 1
                    continue
                state.stats.candidates_found += 1
                state.stats.candidates_per_industry[industry] = (
                    state.stats.candidates_per_industry.get(industry, 0) + 1
                )
                t0 = time.perf_counter()
                result = await validate_icims_tenant(tenant)
                elapsed = time.perf_counter() - t0
                if result and not result.get("needs_playwright"):
                    state.stats.candidates_validated += 1
                    hits += 1
                    state.validated.append(ValidatedTenant(
                        ats="icims",
                        tenant_id=tenant,
                        display_name=derive_display_name(tenant, [], ""),
                        careers_url=result.get("url_used", ""),
                        cxs_url=result.get("url_used", ""),
                        cxs_method="GET",
                        cxs_request_body="",
                        cxs_headers={},
                        active_role_count=0,  # iCIMS RSS doesn't expose count
                        industry=f"Unclassified ({industry})",
                        industry_hint=industry,
                        sample_titles=[],
                        discovered_via=state.serper.name,
                        discovered_at=datetime.now(timezone.utc).isoformat(),
                        elapsed_seconds=round(elapsed, 2),
                        notes="iCIMS, RSS-validated, scraping logic TODO",
                    ))
                    print(f"  [OK] iCIMS: {tenant}")
                elif result and result.get("needs_playwright"):
                    state.failed.append({
                        "ats": "icims", "tenant": tenant, "url": url,
                        "reason": "cloudflare_protected_needs_playwright",
                    })
                    state.stats.candidates_failed += 1
                else:
                    state.stats.candidates_failed += 1
                    state.failed.append({
                        "ats": "icims", "tenant": tenant, "url": url,
                        "reason": "no_response_or_invalid",
                    })

        # Polite delay between queries
        await asyncio.sleep(0.5)
    return hits


async def grind(args: argparse.Namespace) -> None:
    if not SERPER_KEY:
        print("ERROR: SERPER_API_KEY not set in .env. Cannot run.")
        sys.exit(1)
    serper = SerperBingEngine(
        SERPER_KEY,
        min_credits_reserve=getattr(args, "serper_reserve", 0),
    )
    dataforseo = (
        DataForSEOGoogleEngine(DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD)
        if (DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD)
        else None
    )

    existing_workday = load_existing_workday_tenants()
    existing_icims = load_existing_icims_tenants()
    print(f"Loaded {len(existing_workday)} existing Workday tenants for dedup")
    print(f"Loaded {len(existing_icims)} existing iCIMS tenants for dedup")
    print()

    # v0.3.9 prep: support both industry packs AND geographic packs in
    # the same --industries flag. Most testers are Richmond/DMV-based, so
    # we want geo packs to be runnable alongside (or in priority over)
    # the industry packs.
    if args.industries == "all":
        # "all" defaults to ALL packs (industry + geo) — geo runs first
        # because regional employers are higher-priority for current testers.
        industries = ALL_GEOGRAPHIES + ALL_INDUSTRIES
    elif args.industries == "geo":
        industries = ALL_GEOGRAPHIES
    elif args.industries == "industries":
        industries = ALL_INDUSTRIES
    else:
        industries = [i.strip() for i in args.industries.split(",") if i.strip()]
        unknown = [i for i in industries if i not in ALL_PACKS]
        if unknown:
            print(f"Unknown packs: {unknown}")
            print(f"Available industries: {ALL_INDUSTRIES}")
            print(f"Available geographies: {ALL_GEOGRAPHIES}")
            print(f'Or use --industries all (geo first), geo (geos only), '
                  f'or industries (industry only)')
            sys.exit(1)

    deadline = time.time() + args.max_hours * 3600
    state = GrinderState(
        serper=serper,
        dataforseo=dataforseo,
        existing_workday=existing_workday,
        existing_icims=existing_icims,
        seen_candidates=set(),
        validated=[],
        failed=[],
        stats=GrinderStats(
            started_at=datetime.now(timezone.utc).isoformat(),
        ),
        args=args,
        deadline=deadline,
    )

    print(f"=" * 60)
    print(f"TENANT GRINDER — starting")
    print(f"=" * 60)
    print(f"  Time budget: {args.max_hours:.2f}h (deadline {datetime.fromtimestamp(deadline)})")
    print(f"  Serper queries cap: {args.max_serper_queries}")
    print(f"  DataForSEO queries cap: {args.max_dataforseo_queries}")
    print(f"  Cost cap: ${args.max_cost_usd:.2f}")
    print(f"  Industries: {industries}")
    print(f"  ATS targets: {args.ats}")
    print()

    ats_targets = ["workday", "icims"] if args.ats == "both" else [args.ats]

    try:
        for industry in industries:
            keywords = ALL_PACKS[industry]  # works for both industry + geo
            for ats in ats_targets:
                if not has_budget(state):
                    print(f"\n[BUDGET REACHED — stopping early]")
                    break
                print(f"\n--- Industry={industry} ATS={ats} ---")
                hits = await run_industry_pack(state, industry, keywords, ats)
                print(f"  → {hits} validated tenants this pass")
                # Periodic checkpoint
                _write_checkpoint(state)
            if not has_budget(state):
                break
    except KeyboardInterrupt:
        print("\n[INTERRUPTED — saving partial results]")

    # Finalize
    state.stats.finished_at = datetime.now(timezone.utc).isoformat()
    state.stats.duration_seconds = round(
        time.time() - (deadline - args.max_hours * 3600), 1,
    )
    state.stats.cost_estimate_usd = round(state.cost_estimate, 4)

    _write_results(state, args.output)


def _write_checkpoint(state: GrinderState) -> None:
    """Write progress every iteration so a crash mid-run doesn't lose data."""
    PROGRESS_CHECKPOINT.write_text(
        json.dumps({
            "stats": asdict(state.stats),
            "validated_count": len(state.validated),
            "failed_count": len(state.failed),
        }, indent=2),
        encoding="utf-8",
    )


def _write_results(state: GrinderState, output_path: Path) -> None:
    workday_validated = [asdict(t) for t in state.validated if t.ats == "workday"]
    icims_validated = [asdict(t) for t in state.validated if t.ats == "icims"]

    # Sort by active_role_count descending — biggest finds first
    workday_validated.sort(key=lambda t: -(t.get("active_role_count") or 0))
    icims_validated.sort(key=lambda t: t.get("tenant_id", ""))

    by_industry: dict[str, int] = {}
    for t in state.validated:
        by_industry[t.industry] = by_industry.get(t.industry, 0) + 1

    output = {
        "summary": {
            **asdict(state.stats),
            "by_industry": by_industry,
            "validated_workday": len(workday_validated),
            "validated_icims": len(icims_validated),
            "total_validated": len(state.validated),
        },
        "workday_new": workday_validated,
        "icims_new": icims_validated,
        "failed": state.failed,
    }
    output_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print()
    print(f"=" * 60)
    print(f"GRINDER COMPLETE")
    print(f"=" * 60)
    print(f"  Duration: {state.stats.duration_seconds:.1f}s")
    print(f"  Serper queries: {state.stats.serper_queries_run}")
    if state.dataforseo:
        print(f"  DataForSEO queries: {state.stats.dataforseo_queries_run}")
    print(f"  Cost estimate: ${state.stats.cost_estimate_usd:.4f}")
    print(f"  Candidates found: {state.stats.candidates_found}")
    print(f"  Validated tenants: {state.stats.candidates_validated}")
    print(f"    Workday: {len(workday_validated)}")
    print(f"    iCIMS:   {len(icims_validated)}")
    print(f"  Duplicates skipped: {state.stats.duplicates_skipped}")
    print(f"  Failures: {state.stats.candidates_failed}")
    print(f"  By industry:")
    for ind, count in sorted(by_industry.items(), key=lambda kv: -kv[1]):
        print(f"    {ind:30s} {count}")
    print()
    print(f"  Output: {output_path}")
    print(f"  Checkpoint: {PROGRESS_CHECKPOINT}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--max-hours", type=float, default=0.1,
        help="Hard wall-clock cap in hours (default: 0.1 = 6 min sanity test). "
             "User-adjustable: 3 = 3 hours, 8 = overnight.",
    )
    p.add_argument(
        "--max-serper-queries", type=int, default=400,
        help="Cap on Serper.dev queries (free tier 2,500/mo, default 400).",
    )
    p.add_argument(
        "--serper-reserve", type=int, default=0,
        help="Reserve N Serper queries for non-grinder use (e.g. tester "
             "searches or v0.3.10 backfill). Default 0. Recommended 100 "
             "if you want to ensure tester searches don't hit a depleted "
             "Serper free tier.",
    )
    p.add_argument(
        "--max-dataforseo-queries", type=int, default=2000,
        help="Cap on DataForSEO fallback queries (default 2000 ~= $2 budget).",
    )
    p.add_argument(
        "--max-cost-usd", type=float, default=2.0,
        help="Hard cost cap. Stops grinding when reached.",
    )
    p.add_argument(
        "--industries", default="all",
        help=(
            f"Comma-separated packs to target.\n"
            f"  Industry packs: {','.join(ALL_INDUSTRIES)}\n"
            f"  Geographic packs: {','.join(ALL_GEOGRAPHIES)}\n"
            f"Special values:\n"
            f"  'all'        = geo packs first, then industry packs (default)\n"
            f"  'geo'        = geographic packs only (richmond, dmv)\n"
            f"  'industries' = industry packs only\n"
            f"Or pass any combination: --industries richmond,dmv,pharma,defense"
        ),
    )
    p.add_argument(
        "--ats", default="both", choices=["workday", "icims", "both"],
        help="Which ATS to target.",
    )
    p.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="JSON output file path.",
    )
    args = p.parse_args()
    asyncio.run(grind(args))


if __name__ == "__main__":
    main()

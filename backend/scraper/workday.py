"""Workday scraper.

Workday powers ~15-20% of Fortune 500 careers pages including most major
consultancies (Deloitte, PwC, EY, KPMG), banks (JPMorgan, Capital One,
Wells Fargo), and large enterprise (Salesforce, ServiceNow, Cisco, Adobe).

Endpoint pattern:
  POST https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{board}/jobs
  Headers: Accept: application/json, Content-Type: application/json
  Body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "<keyword>"}

  Response: {
    "total": <int>,
    "jobPostings": [
      {
        "title": "...",
        "locationsText": "...",
        "postedOn": "Posted Yesterday|3 Days Ago|...",
        "externalPath": "/job/USA-Washington-DC/...",
        "bulletFields": [...]
      }, ...
    ]
  }

JD body (separate request):
  GET https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{board}{externalPath}
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


# Curated Workday tenants — VERIFIED WORKING via test calls.
# Each entry: (display_name, base_url, board_path)
# Each represents an actual confirmed Workday API endpoint we can hit.
# Add new tenants only after verifying with a test POST returns 200.
WORKDAY_TENANTS: list[tuple[str, str, str]] = [
    # Trimmed 2026-05-04 from 41 → 27. Dropped tenants with low AI-strategy/
    # consulting/governance role density (Walmart, Target, CarMax — retail;
    # Boeing, 3M, Old Dominion — heavy industry; Pfizer/Gilead/Amgen/Merck/
    # McKesson/Eli Lilly — pharma; Disney/Comcast — consumer media; Intel —
    # mostly hardware; HP/Cisco — enterprise hardware-heavy).
    # Kept tenants align with the candidate persona: federal contractors,
    # Big 4, consulting, banks, insurance, asset mgmt, AI-investing tech.
    # Estimated time saving: ~5 min/run + better signal-to-noise.
    # Sectors:
    #   Big 4 / consulting:   Accenture, Booz Allen, PwC, BDO
    #   Federal contractors:  Leidos, GDIT, CACI
    #   Banks:                Citi, PNC, Capital One, Truist, Visa, Mastercard,
    #                         Morgan Stanley, BlackRock, State Street
    #   Insurance:            Travelers, Allstate, AIG, Prudential
    #   Tech / SaaS:          Adobe, Salesforce
    #   Telecom / real estate / policy: T-Mobile, JLL, RAND
    ("Accenture",          "https://accenture.wd103.myworkdayjobs.com",        "AccentureCareers"),
    ("Citi",               "https://citi.wd5.myworkdayjobs.com",               "2"),
    ("PNC",                "https://pnc.wd5.myworkdayjobs.com",                "External"),
    ("PwC",                "https://pwc.wd3.myworkdayjobs.com",                "Global_Experienced_Careers"),
    ("Leidos",             "https://leidos.wd5.myworkdayjobs.com",             "External"),
    ("Booz Allen",         "https://bah.wd1.myworkdayjobs.com",                "BAH_Jobs"),
    ("JLL",                "https://jll.wd1.myworkdayjobs.com",                "jllcareers"),
    ("T-Mobile",           "https://tmobile.wd1.myworkdayjobs.com",            "External"),
    ("Adobe",              "https://adobe.wd5.myworkdayjobs.com",              "external_experienced"),
    ("Capital One",        "https://capitalone.wd12.myworkdayjobs.com",        "Capital_One"),
    ("Truist",             "https://truist.wd1.myworkdayjobs.com",             "Careers"),
    ("Visa",               "https://visa.wd5.myworkdayjobs.com",               "Visa"),
    ("GDIT",               "https://gdit.wd5.myworkdayjobs.com",               "External_Career_Site"),
    ("Mastercard",         "https://mastercard.wd1.myworkdayjobs.com",         "CorporateCareers"),
    ("Prudential",         "https://prudential.wd3.myworkdayjobs.com",         "Prudential"),
    ("Salesforce",         "https://salesforce.wd12.myworkdayjobs.com",        "External_Career_Site"),
    ("Morgan Stanley",     "https://ms.wd5.myworkdayjobs.com",                 "External"),
    ("Allstate",           "https://allstate.wd5.myworkdayjobs.com",           "Allstate_Careers"),
    ("Travelers",          "https://travelers.wd5.myworkdayjobs.com",          "External"),
    ("CACI",               "https://caci.wd1.myworkdayjobs.com",               "External"),
    ("AIG",                "https://aig.wd1.myworkdayjobs.com",                "aig"),
    ("BlackRock",          "https://blackrock.wd1.myworkdayjobs.com",          "BlackRock_Professional"),
    ("BDO",                "https://bdo.wd3.myworkdayjobs.com",                "Bdo"),
    ("RAND Corporation",   "https://rand.wd5.myworkdayjobs.com",               "External_Career_Site"),
    # Discovered 2026-05-03 via load-time XHR capture (workday_discover_v2.py)
    ("State Street",       "https://statestreet.wd1.myworkdayjobs.com",        "Global"),
    # ===========================================================================
    # v0.3.5 expansion (2026-05-07) — verified by scripts/workday_direct_probe.py
    # against 60+ candidate tenant/pod/board combos. These six returned 200 with
    # >=20 jobPostings on a "manager" probe; the rest of the candidates 422'd
    # (body shape rejected — likely require Playwright discovery to capture
    # the live request shape) or 404'd (wrong subdomain/board path).
    #
    # Why these matter:
    #   - Pfizer / Amgen / Novartis / Sanofi / Merck — life sciences. v0.3.4
    #     audit showed the synthetic "operations QA / lab" persona scored 0
    #     STRONG roles because pharma employers were absent from the curated
    #     tenant list. Adding 5 of the top US/EU pharma employers closes
    #     that gap directly.
    #   - Walmart — the largest US private employer. Operations / supply
    #     chain / merchandising roles surface here that no other ATS scraper
    #     reaches at this scale.
    # ===========================================================================
    ("Pfizer",             "https://pfizer.wd1.myworkdayjobs.com",             "PfizerCareers"),
    ("Amgen",              "https://amgen.wd1.myworkdayjobs.com",              "Careers"),
    ("Novartis",           "https://novartis.wd3.myworkdayjobs.com",           "Novartis_Careers"),
    ("Sanofi",             "https://sanofi.wd3.myworkdayjobs.com",             "SanofiCareers"),
    ("Merck",              "https://msd.wd5.myworkdayjobs.com",                "SearchJobs"),
    ("Walmart",            "https://walmart.wd5.myworkdayjobs.com",            "WalmartExternal"),
]

# Tenants STILL FAILING after smart cluster-debug verification (2026-05-02):
#   Deloitte, EY, KPMG, JPMorgan, Goldman Sachs, Wells Fargo, BoA, US Bank,
#   Liberty Mutual, MetLife, PepsiCo, Coca-Cola, P&G, Mondelez, Kroger,
#   Costco, Microsoft, Oracle, IBM, SAP, J&J, Merck, BMS, UnitedHealth, CVS,
#   Marriott, Hilton, Delta, United, AA, CBRE, AECOM, etc.
# Their endpoints return 422 (body-shape rejected, but no body variant
# unlocked them) or 404 (URL/board pattern wrong). Both require Playwright-
# based discovery to load the live careers page, intercept the actual XHR
# call, capture auth tokens, and reverse-engineer per-tenant request shape.
# Tracked as a Phase 1.5 / Phase 2 deliverable.


def _parse_posted_on(posted_on: str) -> Optional[str]:
    """Workday returns 'Posted Yesterday', 'Posted 3 Days Ago' etc.
    Convert to ISO date string approximation."""
    if not posted_on:
        return None
    text = posted_on.lower()
    days_ago = 0
    if "today" in text:
        days_ago = 0
    elif "yesterday" in text:
        days_ago = 1
    else:
        m = re.search(r"(\d+)\s+(day|week|month)", text)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            days_ago = n if unit == "day" else (n * 7 if unit == "week" else n * 30)
    posted = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return posted.isoformat()


class WorkdayScraper(BaseScraper):
    source_name = "Workday"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Workday tenants are independent — fan out across all tenants × keywords
        # Concurrency cap (max 12 in-flight) prevents the whole pipeline from
        # stalling when 1-2 slow tenants would otherwise hold up `gather()`.
        # Per-tenant-keyword timeout (30s wall-clock, was 45s) ensures one slow
        # tenant can't hang the run.
        sem = asyncio.Semaphore(12)

        # Per-tenant 500-error tracker. If a tenant returns 500 to >=3 keyword
        # queries in a row, skip remaining keywords for that tenant. Run 3
        # showed Accenture and Citi returning 500s repeatedly across many
        # keywords — wasted ~10s per dead query × ~20 keywords = ~200s wasted.
        tenant_500_count: dict[str, int] = {}
        SKIP_THRESHOLD = 3

        async def bounded(display_name, base_url, board, kw):
            async with sem:
                # Fast-path skip if tenant has a 500-error streak
                if tenant_500_count.get(display_name, 0) >= SKIP_THRESHOLD:
                    return []
                try:
                    result = await asyncio.wait_for(
                        self._search_tenant_keyword(
                            display_name, base_url, board, kw, limit_per_keyword,
                            error_counter=tenant_500_count,
                        ),
                        timeout=30.0,
                    )
                    return result
                except asyncio.TimeoutError:
                    return []

        tasks = []
        for display_name, base_url, board in WORKDAY_TENANTS:
            for kw in keywords:
                tasks.append(bounded(display_name, base_url, board, kw))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            if posted_within_days else None
        )

        all_roles: list[Role] = []
        seen_url: set[str] = set()
        seen_title_company: set[tuple[str, str]] = set()

        for result in results:
            if isinstance(result, Exception):
                continue
            for role in result:
                if role.job_url and role.job_url in seen_url:
                    continue
                key = (
                    (role.job_title or "").strip().lower(),
                    (role.company or "").strip().lower(),
                )
                if key[0] and key in seen_title_company:
                    continue
                # Posted-date freshness
                if cutoff and role.posted_date:
                    try:
                        posted = datetime.fromisoformat(role.posted_date.replace("Z", "+00:00"))
                        if posted < cutoff:
                            continue
                    except (ValueError, TypeError):
                        pass
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_title_company.add(key)
                all_roles.append(role)
        return all_roles

    async def _search_tenant_keyword(
        self,
        display_name: str,
        base_url: str,
        board: str,
        keyword: str,
        limit: int,
        *,
        error_counter: Optional[dict] = None,
    ) -> list[Role]:
        """Search one Workday tenant for one keyword.

        error_counter (optional): per-tenant 500-error counter shared across
        keywords. When this method gets a 500 response, it increments the
        counter; the caller uses that to short-circuit further keyword
        queries for tenants stuck in a 500 loop.
        """
        endpoint = f"{base_url}/wday/cxs/{self._tenant_from_url(base_url)}/{board}/jobs"
        roles: list[Role] = []
        offset = 0
        while len(roles) < limit:
            body = {
                "appliedFacets": {},
                "limit": 20,
                "offset": offset,
                "searchText": keyword,
            }
            try:
                response = await self.client._client.post(  # type: ignore[union-attr]
                    endpoint,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
            except Exception:
                break
            if response.status_code >= 400:
                # Track 500-class errors for backoff (5xx = upstream tenant
                # is broken right now; further attempts likely waste time).
                # 4xx errors not counted — those are usually pagination ends.
                if 500 <= response.status_code < 600 and error_counter is not None:
                    error_counter[display_name] = error_counter.get(display_name, 0) + 1
                break
            try:
                data = response.json()
            except Exception:
                break
            postings = data.get("jobPostings") or []
            if not postings:
                break
            for j in postings:
                try:
                    roles.append(self._posting_to_role(j, display_name, base_url, board))
                except Exception:
                    continue
            if len(postings) < 20:
                break
            offset += 20
        return roles[:limit]

    @staticmethod
    def _tenant_from_url(base_url: str) -> str:
        """Extract tenant name from base URL.
        e.g., 'https://capitalone.wd1.myworkdayjobs.com' -> 'capitalone'.
        """
        from urllib.parse import urlparse
        host = urlparse(base_url).netloc
        # Special case: 'apply.deloitte.com' -> guess 'deloitte'
        if "apply.deloitte.com" in host:
            return "deloitte"
        # Otherwise tenant is the first label
        parts = host.split(".")
        if parts:
            return parts[0]
        return host

    def _posting_to_role(
        self,
        posting: dict[str, Any],
        company: str,
        base_url: str,
        board: str,
    ) -> Role:
        title = posting.get("title") or ""
        external_path = posting.get("externalPath") or ""
        url = f"{base_url}{external_path}" if external_path else ""

        location_text = posting.get("locationsText") or ""
        loc_low = location_text.lower()
        if "remote" in loc_low:
            location_type = "Remote"
        elif "hybrid" in loc_low:
            location_type = "Hybrid"
        else:
            location_type = "On-site"

        posted_iso = _parse_posted_on(posting.get("postedOn") or "")

        # bulletFields sometimes contains salary/job-id
        salary_text = None
        salary_min = None
        salary_max = None
        for field in (posting.get("bulletFields") or []):
            if not field:
                continue
            field_str = str(field)
            if "$" in field_str:
                salary_text = field_str
                # Parse $ amounts so the dashboard shows a range, not just text.
                # v0.2.1: use module-level `re` (line 32) — was `import re as _re`
                # inside this method, which runs 4-5K times per Workday scrape.
                nums = re.findall(
                    r"\$?(\d{2,3}(?:,\d{3})+|\d{2,3}[Kk])", field_str,
                )
                parsed: list[int] = []
                for n in nums[:2]:
                    n2 = n.replace(",", "")
                    if n2.lower().endswith("k"):
                        parsed.append(int(float(n2[:-1]) * 1000))
                    else:
                        parsed.append(int(float(n2)))
                if parsed:
                    salary_min = parsed[0]
                    if len(parsed) > 1:
                        salary_max = parsed[1]
                break

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location_text or None,
            location_type=location_type,
            job_description_full="",   # JD is fetched lazily — search results don't include
            jd_completeness="Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=posted_iso,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        """Fetch full JD for a Workday role.

        Two-tier strategy:
          1. CXS JSON detail endpoint (preferred). Returns structured JSON
             with the full JD body — no JavaScript rendering required.
             Endpoint shape: {base_url}/wday/cxs/{tenant}/{board}{externalPath}
          2. Public HTML page fallback. JS-rendered, often returns a stub
             with no JD, but worth trying when CXS fails.

        Audit observation: prior implementation only used (2), giving 34-45%
        JD coverage. Switching to (1) typically pushes coverage to 70-80%
        because Workday's careers pages are SPA-rendered.
        """
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""

        cxs_jd = await self._fetch_jd_via_cxs(role.job_url)
        if cxs_jd:
            return cxs_jd

        # Fallback: public HTML page (often a JS shell, but try anyway)
        try:
            response = await self.client.get(role.job_url)
            return _strip_html(response.text)
        except Exception:
            return ""

    async def _fetch_jd_via_cxs(self, job_url: str) -> str:
        """Fetch JD from Workday's CXS detail endpoint.

        URL transformation:
          input:  https://capitalone.wd12.myworkdayjobs.com/job/USA-VA-Mclean/Engineer_R12345
          output: https://capitalone.wd12.myworkdayjobs.com/wday/cxs/capitalone/Capital_One/job/USA-VA-Mclean/Engineer_R12345

        Returns plain-text JD or "" on any failure (caller falls back).
        """
        try:
            from urllib.parse import urlparse
            parsed = urlparse(job_url)
            host = parsed.netloc
            path = parsed.path
            if not host or not path:
                return ""

            # Find matching tenant config to get the board name
            board: Optional[str] = None
            for _name, base_url, b in WORKDAY_TENANTS:
                base_host = urlparse(base_url).netloc
                if base_host == host:
                    board = b
                    break
            if not board:
                return ""

            # Some Workday URLs embed the board ("/Capital_One/job/..."); strip
            # it so external_path starts with "/job/..." for the CXS endpoint.
            external_path = path
            for prefix in (f"/{board}/", f"/en-US/{board}/"):
                if path.startswith(prefix):
                    external_path = path[len(prefix) - 1:]  # keep leading slash
                    break

            tenant = self._tenant_from_url(f"https://{host}")
            cxs_url = f"https://{host}/wday/cxs/{tenant}/{board}{external_path}"

            resp = await self.client._client.get(  # type: ignore[union-attr]
                cxs_url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            if resp.status_code != 200:
                return ""
            try:
                data = resp.json()
            except Exception:
                return ""

            # Workday CXS detail response: { "jobPostingInfo": { "jobDescription": "<html>...</html>" } }
            if not isinstance(data, dict):
                return ""
            jd_info = data.get("jobPostingInfo") or {}
            jd_html = jd_info.get("jobDescription") or ""
            if not jd_html:
                return ""
            return _strip_html(jd_html)
        except Exception:
            return ""

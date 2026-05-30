"""Greenhouse scraper.

Greenhouse exposes a free public JSON API for company job boards:
  https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true

Pros: clean structured data, includes salary/location/JD, no anti-bot
Cons: must scrape per-company (no global search) — we maintain a list of
      companies to query, generated from past data + curated roster.

Strategy:
  1. Maintain a list of `~150 known companies` using Greenhouse
  2. Hit each company's API in parallel, get all open roles
  3. Filter by keyword match against title + JD
  4. Filter by posted_date freshness
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.models import Role
from backend.scraper.base import BaseScraper


# Token-overlap matcher logic was extracted to backend.scraper._keyword_match
# in v0.1.3 so all 14 scrapers can share the same matching rules. Re-export
# the public functions under the original names for back-compat with anything
# that imported them directly from this module.
from backend.scraper import _keyword_match as _kw_match
_STOPWORDS = _kw_match.STOPWORDS  # back-compat
_tokenize = _kw_match.tokenize    # back-compat


def _matches_any_keyword(role: Role, keywords_lower: list[str]) -> bool:
    """Back-compat shim — delegates to the shared matcher.

    Boolean: does any keyword's content tokens overlap with this role's
    title+JD (excerpt)? See _keyword_match.matches_any_keyword for rules.
    """
    return _kw_match.matches_any_keyword(
        role.job_title or "",
        role.job_description_full or "",
        keywords_lower,
    )


# Curated list of companies known to use Greenhouse. Expand as we discover more.
# These are seeded from publicly observable Greenhouse boards.
GREENHOUSE_COMPANIES: list[tuple[str, str]] = [
    # (company_display_name, company_slug)
    ("Anthropic", "anthropic"),
    ("Stripe", "stripe"),
    ("Databricks", "databricks"),
    ("Scale AI", "scaleai"),
    ("Snorkel AI", "snorkelai"),
    ("AssemblyAI", "assemblyai"),
    ("Figma", "figma"),
    ("Airtable", "airtable"),
    ("Brex", "brex"),
    ("Mercury", "mercury"),
    ("Webflow", "webflow"),
    ("Fireworks AI", "fireworksai"),
    ("Together AI", "togetherai"),
    ("Sigma Computing", "sigmacomputing"),
    ("Vercel", "vercel"),
    ("Asana", "asana"),
    ("Discord", "discord"),
    ("Roblox", "roblox"),
    ("Coursera", "coursera"),
    ("Duolingo", "duolingo"),
    ("CourseHero", "coursehero"),
    ("Khan Academy", "khanacademy"),
    ("Klaviyo", "klaviyo"),
    ("Toast", "toast"),
    ("Squarespace", "squarespace"),
    ("Hubspot", "hubspot"),
    ("Cloudflare", "cloudflare"),
    ("Twilio", "twilio"),
    ("Block", "block"),
    ("Instacart", "instacart"),
    ("Lyft", "lyft"),
    ("Reddit", "reddit"),
    ("Pinterest", "pinterest"),
    ("Coinbase", "coinbase"),
    ("Robinhood", "robinhood"),
    ("Affirm", "affirm"),
    ("Chime", "chime"),
    ("Carta", "carta"),
    ("Gusto", "gusto"),
    ("Justworks", "justworks"),
    ("Lattice", "lattice"),
    ("Calendly", "calendly"),
    ("Smartsheet", "smartsheet"),
    ("Dropbox", "dropbox"),
    ("Workato", "workato"),
    ("Okta", "okta"),
    ("DataDog", "datadog"),
    ("New Relic", "newrelic"),

    # ========== Wave-2B Phase 2 (FIX 11, 2026-05-30) — 11 new tenants ==
    # Cross-vertical coverage: healthcare (Oscar/Omada), enterprise SaaS
    # (GitLab/LaunchDarkly/BeyondTrust), fintech (SumUp/Sezzle/Jumio),
    # marketplace (TaskRabbit — also first Greenhouse skilled-trades source),
    # consumer (ThirdLove), infra (RunPod). Each slug verified live by the
    # deep-dive workflow. If any 404 in production, the existing
    # dead_tenants tracker (added in v0.3.5) prunes them.
    #
    # Wave-2B Phase 2 post-validation prune (2026-05-30 Ziad smoke run):
    # Removed Gong (slug 'gong' returned 404 — migrated to Ashby, covered
    # there in FIX 8) and Jamf (slug 'jamfsoftware' returned 404 — also
    # migrated to Ashby, covered there). Three 404s remain to investigate
    # next round (opendoor was pre-existing, not added by FIX 11).
    ("SumUp",         "sumup"),
    ("Oscar Health",  "oscar"),
    ("Sezzle",        "sezzle"),
    ("GitLab",        "gitlab"),
    ("BeyondTrust",   "beyondtrust"),
    ("LaunchDarkly",  "launchdarkly"),
    ("Omada Health",  "omadahealth"),
    ("RunPod",        "runpod"),
    ("Jumio",         "jumio"),
    ("TaskRabbit",    "taskrabbit"),
    ("ThirdLove",     "thirdlove"),
    ("Elastic", "elastic"),
    ("MongoDB", "mongodb"),
    ("PagerDuty", "pagerduty"),
    ("Sumo Logic", "sumologic"),
    ("Pendo", "pendo"),
    ("Amplitude", "amplitude"),
    ("Mixpanel", "mixpanel"),
    ("Iterable", "iterable"),
    ("Customer.io", "customerio"),
    ("Braze", "braze"),

    # ========== Phase 1 expansion (2026-05-03) — industry-targeted adds ==========

    # AI consulting / mid-market AI (gaps for senior strategy candidates)
    ("KUNGFU.AI", "kungfuai"),
    ("Cresta", "cresta"),

    # Healthtech (insurance/payer/provider/digital-health gap)
    ("Komodo Health", "komodohealth"),
    ("Flatiron Health", "flatironhealth"),
    ("Veracyte", "veracyte"),

    # Fintech (broader fintech gap beyond what we already had)
    ("SoFi", "sofi"),
    ("Marqeta", "marqeta"),
    ("Bill.com", "billcom"),
    ("Pulley", "pulley"),

    # Real estate tech (CRE / housing gap — relevant to Ziad's CoStar background)
    ("Opendoor", "opendoor"),
    ("VTS", "vts"),

    # Retail tech / DTC brands (CPG-adjacent gap)
    ("Faire", "faire"),
    ("Stitch Fix", "stitchfix"),
    ("Glossier", "glossier"),
    ("Allbirds", "allbirds"),

    # Mid-market SaaS (broader operations/consulting candidate appeal)
    ("Salesloft", "salesloft"),

    # Auto / EV / mobility (gap entirely)
    ("Lucid Motors", "lucidmotors"),
    ("Waymo", "waymo"),

    # Travel / hospitality tech (gap entirely)
    ("Airbnb", "airbnb"),

    # Logistics / supply chain tech
    ("Flexport", "flexport"),
    ("Project44", "project44"),

    # Education / edtech expansion
    ("Outschool", "outschool"),

    # Misc consumer / media (filling out variety)

    # Cybersecurity (was thin — relevant for many tech profiles)

    # Phase C1 — non-tech / consumer / retail / finance / wellness / media
    # (verified live 2026-05-03 via scripts/probe_non_tech_atses.py)
    ("HelloFresh",      "hellofresh"),       # 368 jobs — meal kit / consumer
    ("Intercom",        "intercom"),         # 174 jobs — SaaS but broad
    ("Compass",         "urbancompass"),     # 130 jobs — real estate
    ("Peloton",         "peloton"),          #  52 jobs — fitness / consumer
    ("Sweetgreen",      "sweetgreen"),       #  44 jobs — food service / hospitality
    ("Betterment",      "betterment"),       #  31 jobs — fintech / wealth
    ("Vox Media",       "voxmedia"),         #  23 jobs — media / journalism
    ("Talkspace",       "talkspace"),        #  20 jobs — health / mental wellness
    ("BuzzFeed",        "buzzfeed"),         #  10 jobs — media / journalism
    ("Olipop",          "olipop"),           #   2 jobs — CPG / beverage
    ("Liquid Death",    "liquiddeath"),      #   1 jobs — CPG / beverage
    ("Calm",            "calm"),             #   1 jobs — health / wellness

    # Phase C1 v2 — environmental / climate / health / hospitality / consumer
    # (verified live 2026-05-03 via probe_non_tech_v2.py)
    ("Tripadvisor",     "tripadvisor"),                 #  82 jobs — travel / hospitality
    ("Wayve",           "wayve"),                       #  74 jobs — autonomous / AI
    ("2U",              "2u"),                          #  35 jobs — education
    ("Mindbody",        "mindbody"),                    #  33 jobs — wellness / hospitality
    ("Recursion",       "recursionpharmaceuticals"),    #  28 jobs — biotech
    ("Maven Clinic",    "mavenclinic"),                 #  24 jobs — health / clinical
    ("Ritual",          "ritual"),                      #  15 jobs — CPG / vitamins
    ("Ginkgo Bioworks", "ginkgobioworks"),              #  11 jobs — biotech / synthbio
    ("Harry's",         "harrys"),                      #   9 jobs — CPG / personal care
    ("Watershed",       "watershed"),                   #   8 jobs — climate / sustainability
    ("PivotBio",        "pivotbio"),                    #   8 jobs — agtech / sustainability
    ("Code for America","codeforamerica"),              #   8 jobs — civic / public sector
    ("Master Class",    "masterclass"),                 #   6 jobs — education / consumer
    ("Manscaped",       "manscaped"),                   #   5 jobs — CPG / personal care

    # Phase C2 — Greenhouse expansion targeted at underserved sectors
    # (verified live 2026-05-03 via probe_greenhouse_expansion.py)
    # Defense / aerospace
    ("Anduril",            "andurilindustries"),         # 1865 jobs — defense / autonomy
    ("Astranis",           "astranis"),                  #  135 jobs — satellites / aerospace
    ("KoBold Metals",      "koboldmetals"),              #   26 jobs — AI mining / climate
    ("Remora",             "remoracarbon"),              #    4 jobs — carbon capture
    # Healthcare / biotech
    ("Strive Health",      "strivehealth"),              #   43 jobs — kidney care
    ("Iterative Health",   "iterativehealth"),           #   37 jobs — GI / AI healthcare
    ("Beam Therapeutics",  "beamtherapeutics"),          #   26 jobs — gene editing
    ("Twin Health",        "twinhealth"),                #   23 jobs — diabetes / metabolic
    ("Modern Health",      "modernhealth"),              #   14 jobs — mental wellness
    ("Hone Health",        "honehealth"),                #   13 jobs — men's health
    ("Forge Biologics",    "forgebiologics"),            #   11 jobs — gene therapy
    # FinTech expansion
    ("Cross River Bank",   "crossriverbank"),            #   22 jobs — banking infra
    ("Earnin",             "earnin"),                    #   42 jobs — earned wage access
    ("Alloy",              "alloy"),                     #   19 jobs — identity / KYC
    # Tech / data
    ("Fivetran",           "fivetran"),                  #  173 jobs — data integration
    ("Hightouch",          "hightouch"),                 #   64 jobs — reverse ETL
    ("Mozilla",            "mozilla"),                   #   53 jobs — browser / open source
    # Logistics / supply chain
    ("FourKites",          "fourkites"),                 #   11 jobs — supply visibility
    ("Loop",               "loop"),                      #   20 jobs — returns infrastructure
    # Energy / mobility
    ("ChargePoint",        "chargepoint"),               #   48 jobs — EV charging
    # Real estate / consumer
    ("Rent the Runway",    "renttherunway"),             #   31 jobs — fashion rental
    ("Pacaso",             "pacaso"),                    #   10 jobs — co-ownership real estate
    # Education
    ("Newsela",            "newsela"),                   #   30 jobs — K-12 edtech
    # Civic / nonprofit
    ("ACLU",               "aclu"),                      #   37 jobs — civil rights nonprofit
    # Marketplace / consumer
    ("Upwork",             "upwork"),                    #   32 jobs — freelance marketplace
    ("Cameo",              "cameo"),                     #    3 jobs — celebrity video
]


def _strip_html(html: str) -> str:
    """Crude HTML→text. Good enough for JD content; full parsing happens later."""
    # Remove style/script blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Decode common entities
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&#39;", "'").replace("&quot;", '"')
                .replace("&rsquo;", "'").replace("&lsquo;", "'")
                .replace("&rdquo;", '"').replace("&ldquo;", '"'))
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_iso_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Greenhouse uses ISO 8601 with timezone
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class GreenhouseScraper(BaseScraper):
    source_name = "Greenhouse"
    BASE_URL = "https://boards-api.greenhouse.io/v1/boards"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        """Fetch open roles from every configured Greenhouse company,
        filter by keyword + posted_date.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=posted_within_days)
            if posted_within_days else None
        )
        keywords_lower = [k.lower() for k in keywords]
        # FIX 26 (Wave-2B Phase 1): per-tenant error accumulator. We hit ~150
        # Greenhouse boards per run, and previously every 404 / timeout /
        # parse error was silently swallowed (`except Exception: pass`).
        # That made dead tenants invisible — boards that had renamed their
        # slug or pulled their public API would just contribute 0 roles
        # indistinguishably from live boards with no matches. The
        # orchestrator picks up `dead_tenants` via getattr() and surfaces
        # it on the health_out["Greenhouse"]["dead_tenants"] dict so the
        # audit JSON records {tenant_slug: error_type} for triage.
        self.dead_tenants: dict[str, str] = {}

        # Fetch from all companies in parallel.
        # v0.2.1: cap concurrent in-flight requests at 20 to prevent
        # connection-pool exhaustion on machines with limited sockets and
        # to be a better citizen of Greenhouse's API. ScraperClient already
        # paces inter-request delay per-domain, but doesn't bound concurrent
        # count — so all 138 tenants used to fan out at once. Pure
        # backpressure: same roles fetched, just less burst-y.
        sem = asyncio.Semaphore(20)
        async def _bounded(slug: str, display_name: str) -> list[Role]:
            async with sem:
                return await self._fetch_company_jobs(slug, display_name)
        tasks = [
            _bounded(slug, display_name)
            for display_name, slug in GREENHOUSE_COMPANIES
        ]
        all_company_results = await asyncio.gather(*tasks, return_exceptions=True)

        all_roles: list[Role] = []
        seen_url: set[str] = set()
        seen_title_company: set[tuple[str, str]] = set()

        # Build a parallel index of (display_name, slug) so we can attribute
        # gather() exceptions back to the right tenant for FIX 26 logging.
        tenant_index = list(GREENHOUSE_COMPANIES)
        for idx, result in enumerate(all_company_results):
            if isinstance(result, Exception):
                # FIX 26: surface the failing tenant + error type instead of
                # swallowing. asyncio.gather(..., return_exceptions=True)
                # captures errors that escaped _fetch_company_jobs (e.g. a
                # bug in _bounded, semaphore acquisition failure, or a
                # CancelledError propagated up). _fetch_company_jobs itself
                # also writes to self.dead_tenants for the upstream-side
                # errors (404 / timeout / parse) — this branch covers
                # everything else.
                if idx < len(tenant_index):
                    _display, _slug = tenant_index[idx]
                    err_type = type(result).__name__
                    self.dead_tenants[_slug] = f"gather:{err_type}"
                    print(
                        f"[Greenhouse] tenant '{_slug}' raised "
                        f"{err_type}: {str(result)[:120]}",
                        flush=True,
                    )
                continue  # company API failed, just skip
            for role in result:
                # Dedup by URL (exact)
                if role.job_url and role.job_url in seen_url:
                    continue
                # Dedup by (title, company) — Greenhouse posts same role to
                # multiple location boards with different URLs
                key = (
                    (role.job_title or "").strip().lower(),
                    (role.company or "").strip().lower(),
                )
                if key[0] and key in seen_title_company:
                    continue
                # Filter by posted_date
                if cutoff and role.posted_date:
                    posted = _parse_iso_date(role.posted_date)
                    if posted and posted < cutoff:
                        continue
                # Filter by keyword match — token overlap, not exact substring.
                # Exact substring was missing roles where the title reorders the
                # keyword (e.g. "AI Enablement Lead" misses "Lead, AI Enablement"
                # and "Senior Lead - AI Enablement"). For each keyword, we keep
                # the role if at least 60% of the keyword's content tokens appear
                # in the title, OR 50%+ in the title+JD. Stopwords are ignored so
                # short keywords like "AI Coach" still need both content words.
                if not _matches_any_keyword(role, keywords_lower):
                    continue
                if role.job_url:
                    seen_url.add(role.job_url)
                seen_title_company.add(key)
                all_roles.append(role)

        return all_roles

    async def _fetch_company_jobs(self, slug: str, display_name: str) -> list[Role]:
        """Hit the Greenhouse API for a single company, return all open roles."""
        url = f"{self.BASE_URL}/{slug}/jobs"
        try:
            data = await self.client.get_json(url, params={"content": "true"})
        except Exception as e:
            # FIX 26 (Wave-2B Phase 1): structured error capture instead of
            # silent pass. Classify the error so dead_tenants tells us *why*
            # the tenant is dead (404 = slug renamed/pulled; timeout =
            # transient; other = HTTP error or JSON decode failure). The
            # orchestrator reads self.dead_tenants via getattr() and writes
            # it into health_out["Greenhouse"]["dead_tenants"] for audit.
            err_type = type(e).__name__
            msg = str(e).lower()
            if "404" in msg or "not found" in msg:
                category = "404"
            elif "timeout" in msg or "timed out" in msg:
                category = "timeout"
            elif "json" in msg or "decode" in msg:
                category = "parse_error"
            else:
                category = f"fetch_error:{err_type}"
            self.dead_tenants[slug] = category
            print(
                f"[Greenhouse] tenant '{slug}' fetch failed ({category}): "
                f"{str(e)[:120]}",
                flush=True,
            )
            return []

        jobs = data.get("jobs") or []
        roles: list[Role] = []
        parse_failures = 0
        for j in jobs:
            try:
                roles.append(self._job_to_role(j, display_name))
            except Exception as e:
                # FIX 26: track per-role parse failures so a partially broken
                # board (e.g. one malformed job in 50) still returns the good
                # roles but we surface the parse_error in dead_tenants.
                parse_failures += 1
                if parse_failures == 1:  # log only the first to avoid spam
                    print(
                        f"[Greenhouse] tenant '{slug}' role parse error: "
                        f"{type(e).__name__}: {str(e)[:120]}",
                        flush=True,
                    )
                continue
        # FIX 26: gate accumulation with `if jobs` so legitimate zero-result
        # tenants (board exists, just no open roles right now) don't spam
        # logs or show up as dead. Only mark a tenant when there were jobs
        # AND we failed to parse some of them.
        if jobs and parse_failures > 0:
            self.dead_tenants[slug] = f"parse_error:{parse_failures}/{len(jobs)}"
        return roles

    def _job_to_role(self, job: dict[str, Any], company: str) -> Role:
        """Convert a Greenhouse job JSON object to our Role schema."""
        title = job.get("title") or ""
        url = job.get("absolute_url") or ""
        location_text = (job.get("location") or {}).get("name") or ""
        location_type, location = self._classify_location(location_text)

        # JD comes back as HTML
        content = job.get("content") or ""
        jd_text = _strip_html(content) if content else ""

        # Salary metadata (Greenhouse boards optionally publish this)
        salary_text = None
        salary_min = None
        salary_max = None
        for meta in (job.get("metadata") or []):
            name = (meta.get("name") or "").lower()
            value = meta.get("value")
            if not value:
                continue
            if "salary" in name or "compensation" in name or "pay" in name:
                if salary_text is None:
                    salary_text = str(value)
                # Try to extract min/max
                nums = re.findall(r"\$?(\d{2,3}(?:[,]\d{3})*(?:\.\d+)?[Kk]?)", str(value))
                parsed: list[int] = []
                for n in nums[:2]:
                    n = n.replace(",", "")
                    if n.lower().endswith("k"):
                        parsed.append(int(float(n[:-1]) * 1000))
                    else:
                        try:
                            parsed.append(int(float(n)))
                        except ValueError:
                            pass
                if parsed:
                    salary_min = parsed[0]
                    if len(parsed) > 1:
                        salary_max = parsed[1]

        return Role(
            job_title=title,
            company=self._normalize_company(company),
            job_url=url,
            location=location,
            location_type=location_type,
            job_description_full=jd_text,
            jd_completeness="Full" if jd_text else "Missing",
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            posted_date=job.get("updated_at") or job.get("first_published"),
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        """Greenhouse search returns full JD already; this is a fallback if missing."""
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            response = await self.client.get(role.job_url)
            return _strip_html(response.text)
        except Exception:
            return ""

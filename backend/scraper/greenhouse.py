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


# Common short words ignored when comparing keyword tokens. Keeping the list
# tight — we don't want to mistake "ai" or "lead" as stopwords, but "and" /
# "of" / "the" carry no signal.
_STOPWORDS = frozenset({
    "a", "an", "and", "or", "for", "of", "to", "the", "in", "on",
    "with", "at", "by", "as", "&",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase + split on non-word chars. Filters stopwords + 1-char tokens."""
    return [
        t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if t and len(t) > 1 and t not in _STOPWORDS
    ]


def _matches_any_keyword(role: Role, keywords_lower: list[str]) -> bool:
    """Token-overlap match: keep role if any keyword's content tokens have
    sufficient coverage in the title (or weaker coverage in title+JD)."""
    title = role.job_title or ""
    jd_excerpt = (role.job_description_full or "")[:2000]
    title_tokens = set(_tokenize(title))
    full_tokens = title_tokens | set(_tokenize(jd_excerpt))

    for kw in keywords_lower:
        kw_tokens = _tokenize(kw)
        if not kw_tokens:
            continue
        # Require ALL content tokens for short keywords (1-2 tokens), since
        # missing one inverts the meaning ("AI Coach" without "AI" is wrong).
        if len(kw_tokens) <= 2:
            if all(t in full_tokens for t in kw_tokens):
                return True
            continue
        # Longer keywords: 60% of tokens in title OR 50%+ in title+JD.
        title_overlap = sum(1 for t in kw_tokens if t in title_tokens) / len(kw_tokens)
        full_overlap = sum(1 for t in kw_tokens if t in full_tokens) / len(kw_tokens)
        if title_overlap >= 0.60 or full_overlap >= 0.75:
            return True
    return False


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
    ("Weights & Biases", "weightsbiases"),
    ("Modal Labs", "modallabs"),
    ("Fireworks AI", "fireworksai"),
    ("Together AI", "togetherai"),
    ("Glean", "glean"),
    ("Sigma Computing", "sigmacomputing"),
    ("Hex", "hex"),
    ("dbt Labs", "dbtlabs"),
    ("Mode Analytics", "modeanalytics"),
    ("Vercel", "vercel"),
    ("Asana", "asana"),
    ("Discord", "discord"),
    ("Roblox", "roblox"),
    ("Coursera", "coursera"),
    ("Duolingo", "duolingo"),
    ("CourseHero", "coursehero"),
    ("Khan Academy", "khanacademy"),
    ("Pearson", "pearson"),
    ("Klaviyo", "klaviyo"),
    ("Toast", "toast"),
    ("Squarespace", "squarespace"),
    ("Hubspot", "hubspot"),
    ("Asana", "asana"),
    ("Atlassian", "atlassian"),
    ("Cloudflare", "cloudflare"),
    ("Twilio", "twilio"),
    ("Block", "block"),
    ("DoorDash", "doordash"),
    ("Instacart", "instacart"),
    ("Lyft", "lyft"),
    ("Reddit", "reddit"),
    ("Pinterest", "pinterest"),
    ("Coinbase", "coinbase"),
    ("Robinhood", "robinhood"),
    ("Affirm", "affirm"),
    ("Chime", "chime"),
    ("Wealthfront", "wealthfront"),
    ("Carta", "carta"),
    ("Etsy", "etsy"),
    ("Shopify", "shopify"),
    ("Wayfair", "wayfair"),
    ("Wise", "wise"),
    ("Gusto", "gusto"),
    ("Justworks", "justworks"),
    ("Rippling", "rippling"),
    ("Lattice", "lattice"),
    ("15Five", "15five"),
    ("Calendly", "calendly"),
    ("Loom", "loom"),
    ("Miro", "miro"),
    ("Mural", "mural"),
    ("Monday.com", "mondaycom"),
    ("ClickUp", "clickup"),
    ("Smartsheet", "smartsheet"),
    ("Box", "box"),
    ("Dropbox", "dropbox"),
    ("Workato", "workato"),
    ("Tray.io", "trayio"),
    ("MuleSoft", "mulesoft"),
    ("Salesforce", "salesforce"),
    ("Tableau", "tableau"),
    ("ServiceNow", "servicenow"),
    ("Workday", "workday"),
    ("Okta", "okta"),
    ("Auth0", "auth0"),
    ("DataDog", "datadog"),
    ("New Relic", "newrelic"),
    ("Splunk", "splunk"),
    ("Confluent", "confluent"),
    ("Elastic", "elastic"),
    ("MongoDB", "mongodb"),
    ("Redis", "redis"),
    ("HashiCorp", "hashicorp"),
    ("PagerDuty", "pagerduty"),
    ("Sumo Logic", "sumologic"),
    ("Sentry", "sentry"),
    ("Lightcast", "lightcast"),
    ("Pendo", "pendo"),
    ("Amplitude", "amplitude"),
    ("Mixpanel", "mixpanel"),
    ("Heap", "heap"),
    ("Iterable", "iterable"),
    ("Customer.io", "customerio"),
    ("Braze", "braze"),
    ("OneSignal", "onesignal"),
    ("Sendgrid", "sendgrid"),
    ("Postmark", "postmark"),
    ("Pendo", "pendo"),

    # ========== Phase 1 expansion (2026-05-03) — industry-targeted adds ==========

    # AI consulting / mid-market AI (gaps for senior strategy candidates)
    ("BRG", "brg"),
    ("Casper Studios", "casperstudios"),
    ("Tenex Labs", "tenexlabs"),
    ("KUNGFU.AI", "kungfuai"),
    ("Pragmatico", "pragmatico"),
    ("Adept", "adept"),
    ("Character AI", "characterai"),
    ("Inflection", "inflection"),
    ("Perplexity", "perplexity"),
    ("Mistral", "mistral"),
    ("EvenUp", "evenup"),
    ("Dust", "dustai"),
    ("Sierra", "sierra"),
    ("Cresta", "cresta"),
    ("Decagon", "decagon"),

    # Healthtech (insurance/payer/provider/digital-health gap)
    ("Komodo Health", "komodohealth"),
    ("Tempus", "tempuslabs"),
    ("Flatiron Health", "flatironhealth"),
    ("Oscar Health", "oscarhealth"),
    ("Carbon Health", "carbonhealth"),
    ("Hims & Hers", "forhims"),
    ("Ro", "roman"),
    ("Veracyte", "veracyte"),
    ("23andMe", "23andme"),
    ("Devoted Health", "devotedhealth"),
    ("Cityblock Health", "cityblockhealth"),
    ("Olive AI", "oliveai"),

    # Fintech (broader fintech gap beyond what we already had)
    ("Affirm", "affirm"),
    ("SoFi", "sofi"),
    ("Chime", "chime"),
    ("Marqeta", "marqeta"),
    ("Bill.com", "billcom"),
    ("Navan", "navan"),
    ("Wise", "wise"),
    ("Toast", "toast"),  # already have but ensure
    ("Carta", "carta"),
    ("Pulley", "pulley"),
    ("Mercury Financial", "mercuryfinancial"),
    ("Paddle", "paddle"),

    # Real estate tech (CRE / housing gap — relevant to Ziad's CoStar background)
    ("Zillow", "zillow"),
    ("Redfin", "redfin"),
    ("Opendoor", "opendoor"),
    ("Compass", "compass"),
    ("VTS", "vts"),
    ("Cherre", "cherre"),
    ("Keyway", "keyway"),

    # Retail tech / DTC brands (CPG-adjacent gap)
    ("Faire", "faire"),
    ("Stitch Fix", "stitchfix"),
    ("Warby Parker", "warbyparker"),
    ("Glossier", "glossier"),
    ("Chewy", "chewy"),
    ("Allbirds", "allbirds"),
    ("Casper", "casper"),
    ("Wayfair", "wayfair"),
    ("Instacart", "instacart"),
    ("DoorDash", "doordash"),
    ("Gopuff", "gopuff"),

    # Mid-market SaaS (broader operations/consulting candidate appeal)
    ("HubSpot", "hubspot"),
    ("Atlassian", "atlassian"),
    ("Asana", "asana2"),
    ("Monday.com", "mondayio"),
    ("ClickUp", "clickup"),
    ("Freshworks", "freshworks"),
    ("Gainsight", "gainsight"),
    ("Gong", "gong"),
    ("Highspot", "highspot"),
    ("Seismic", "seismic"),
    ("Clari", "clari"),
    ("Outreach", "outreachio"),
    ("Salesloft", "salesloft"),
    ("Lattice", "lattice"),
    ("Rippling", "rippling"),
    ("Gusto", "gusto"),
    ("Justworks", "justworks"),
    ("Deel", "deel"),
    ("BambooHR", "bamboohr"),
    ("ServiceNow", "servicenow"),

    # Auto / EV / mobility (gap entirely)
    ("Tesla", "tesla"),
    ("Rivian", "rivian"),
    ("Lucid Motors", "lucidmotors"),
    ("Cruise", "cruise"),
    ("Waymo", "waymo"),

    # Travel / hospitality tech (gap entirely)
    ("Airbnb", "airbnb"),
    ("Hopper", "hopper"),
    ("Sonder", "sonder"),
    ("Booking Holdings", "bookingholdings"),

    # Logistics / supply chain tech
    ("Flexport", "flexport"),
    ("Convoy", "convoy"),
    ("Project44", "project44"),

    # Education / edtech expansion
    ("Coursera", "coursera"),
    ("Duolingo", "duolingo"),
    ("Khan Academy", "khanacademy"),
    ("Quizlet", "quizlet"),
    ("Outschool", "outschool"),

    # Misc consumer / media (filling out variety)
    ("Spotify", "spotify"),
    ("SoundCloud", "soundcloud"),
    ("Squarespace", "squarespace"),
    ("Shopify", "shopify"),
    ("Etsy", "etsy"),
    ("Pinterest", "pinterest"),
    ("Reddit", "reddit"),
    ("Discord", "discord"),

    # Cybersecurity (was thin — relevant for many tech profiles)
    ("CrowdStrike", "crowdstrike"),
    ("SentinelOne", "sentinelone"),
    ("Palo Alto Networks", "paloaltonetworks"),
    ("Wiz", "wizio"),
    ("1Password", "1password"),
    ("Cloudflare", "cloudflare"),

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
    ("Toast",           "toast"),                       # 332 jobs — restaurant tech / hospitality
    ("Airbnb",          "airbnb"),                      # 235 jobs — hospitality / travel
    ("Lucid Motors",    "lucidmotors"),                 # 229 jobs — auto / clean energy
    ("Flexport",        "flexport"),                    # 113 jobs — logistics / supply chain
    ("Tripadvisor",     "tripadvisor"),                 #  82 jobs — travel / hospitality
    ("Wayve",           "wayve"),                       #  74 jobs — autonomous / AI
    ("Bill.com",        "billcom"),                     #  46 jobs — fintech / SMB
    ("Komodo Health",   "komodohealth"),                #  42 jobs — health data
    ("project44",       "project44"),                   #  38 jobs — supply chain
    ("2U",              "2u"),                          #  35 jobs — education
    ("Mindbody",        "mindbody"),                    #  33 jobs — wellness / hospitality
    ("Marqeta",         "marqeta"),                     #  33 jobs — fintech
    ("Recursion",       "recursionpharmaceuticals"),    #  28 jobs — biotech
    ("Maven Clinic",    "mavenclinic"),                 #  24 jobs — health / clinical
    ("Flatiron Health", "flatironhealth"),              #  17 jobs — oncology / health
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

        # Fetch from all companies in parallel
        tasks = [
            self._fetch_company_jobs(slug, display_name)
            for display_name, slug in GREENHOUSE_COMPANIES
        ]
        all_company_results = await asyncio.gather(*tasks, return_exceptions=True)

        all_roles: list[Role] = []
        seen_url: set[str] = set()
        seen_title_company: set[tuple[str, str]] = set()

        for result in all_company_results:
            if isinstance(result, Exception):
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
        except Exception:
            return []

        jobs = data.get("jobs") or []
        roles: list[Role] = []
        for j in jobs:
            try:
                roles.append(self._job_to_role(j, display_name))
            except Exception:
                continue
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

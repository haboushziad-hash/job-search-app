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
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from backend.config import config
from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


# Path to per-tenant JSON config files written by the grinder
# (scripts/tenant_grinder.py + integrate_grinder_tenants.py). These augment
# the hardcoded HARDCODED_WORKDAY_TENANTS list below; the merged superset is
# what the scraper actually iterates over at runtime.
#
# Schema (matches what integrate_grinder_tenants.build_tenant_record writes):
#   {
#     "display_name": "Capital One",
#     "careers_url":  "https://capitalone.wd12.myworkdayjobs.com/Capital_One",
#     "captured": {
#       "url":     "https://capitalone.wd12.myworkdayjobs.com/wday/cxs/capitalone/Capital_One/jobs",
#       ...
#     }
#   }
#
# v0.3.12: dual-path resolution for dev mode AND PyInstaller bundle mode.
# In dev: `__file__` is backend/scraper/workday.py on disk. parent is
#   backend/scraper/, so `parent/workday_tenants` is the source dir.
# In bundle: PyInstaller extracts files to `sys._MEIPASS`. If the spec file
#   correctly bundles workday_tenants/*.json under "backend/scraper/workday_tenants",
#   then `__file__` resolves to `<MEIPASS>/backend/scraper/workday.py` and the
#   parent-relative lookup still works.
# To be extra defensive: if `__file__`-relative path doesn't exist OR is
#   empty, fall back to `sys._MEIPASS/backend/scraper/workday_tenants`. This
#   covers a class of PyInstaller weirdness where `__file__` may point at a
#   stub path while data files are extracted elsewhere.
import sys as _sys

def _resolve_tenants_dir() -> Path:
    """Find the workday_tenants directory in both dev and bundled modes."""
    primary = Path(__file__).resolve().parent / "workday_tenants"
    if primary.is_dir() and any(primary.glob("*.json")):
        return primary
    # PyInstaller fallback: data files extracted to _MEIPASS
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        bundle_path = Path(meipass) / "backend" / "scraper" / "workday_tenants"
        if bundle_path.is_dir() and any(bundle_path.glob("*.json")):
            return bundle_path
    # Fall back to primary even if empty — caller logs the situation
    return primary

TENANTS_DIR = _resolve_tenants_dir()

# Display-name overrides for opaque tenant IDs. Keep in sync with
# scripts/integrate_grinder_tenants.py — same dict, but duplicated here so
# the scraper doesn't depend on the scripts/ tree at runtime (scripts/ is
# excluded from PyInstaller bundles).
_DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "ngc": "Northrop Grumman",
    "bah": "Booz Allen Hamilton",
    "capitalone": "Capital One",
    "leidos": "Leidos",
    "kbr": "KBR",
    "ms": "Morgan Stanley",  # in our hardcoded set; if a different tenant uses 'ms' as ID we override here
    "rb": "Federal Reserve System",  # rb.wd5.myworkdayjobs.com/FRS — Fed Reserve Banks system
    "vca": "VCA Animal Hospitals",
    "geaerospace": "GE Aerospace",
    "mars": "Mars Inc",
    "wwwinc": "Worthington Industries",
    "moog": "Moog Inc",
    "mymvw": "Marriott Vacations Worldwide",
    "geico": "GEICO",
    "warnerbros": "Warner Bros Discovery",
    "fanniemae": "Fannie Mae",
    "freddiemac": "Freddie Mac",
    "intel": "Intel",
    "cisco": "Cisco",
    "rand": "RAND Corporation",
    "washpost": "The Washington Post",
    "atlanticmedia": "The Atlantic",
    "mwaa": "Metropolitan Washington Airports Authority",
    "erickson": "Erickson Senior Living",
    "tapestry": "Tapestry",
    "wawa": "Wawa",
    "carmax": "CarMax",
    "markelcorp": "Markel",
    "owensminor": "Owens & Minor",
    "shm": "Sentara Healthcare",
    "bmc": "Boston Medical Center",
    "lvhn": "Lehigh Valley Health Network",
    "highmarkhealth": "Highmark Health",
    "sluhn": "St Luke's University Health Network",
    "carilionclinic": "Carilion Clinic",
    "wellstar": "Wellstar Health",
    "ummh": "UMass Memorial Health",
    "memorialhermann": "Memorial Hermann Health",
    "elevancehealth": "Elevance Health",
    "trinityhealth": "Trinity Health",
    "massgeneralbrigham": "Mass General Brigham",
    "wvumedicine": "WVU Medicine",
    "jeffersonhealth": "Jefferson Health",
    "guidehouse": "Guidehouse",
    "medline": "Medline Industries",
    "choa": "Children's Healthcare of Atlanta",
    "virtua": "Virtua Health",
    "asmglobal": "ASM Global",
    "dickssportinggoods": "Dick's Sporting Goods",
    "redrobin": "Red Robin",
    "flextronics": "Flex",
    "bjswholesaleclub": "BJ's Wholesale Club",
    "signetjewelers": "Signet Jewelers",
    "fifththird": "Fifth Third Bank",
    "bdx": "Becton Dickinson",
    "snc": "Sierra Nevada Corporation",
    "avav": "AeroVironment",
    "ultra": "Ultra Electronics",
    "townepark": "Towne Park",
    "calistacorp": "Calista Corporation",
    "outrigger": "Outrigger Hotels",
    "rwlasvegas": "Resorts World Las Vegas",
    "shawinc": "Shaw Industries",
    "sierraspace": "Sierra Space",
    "marymount": "Marymount University",
    "tsc": "Tractor Supply Company",
    "seic": "SEI Investments",
    "oregon": "State of Oregon",
    "aero": "Aerospace Corporation",
    "pg": "Procter & Gamble",
    "cartech": "Carpenter Technology",
    "ntst": "Netsmart Technologies",
    "mosaic": "Mosaic Co",
    "gaig": "Great American Insurance Group",
    "tel": "TE Connectivity",
    "patientfirst": "Patient First",
    "bmo": "BMO Bank",
    "axosbank": "Axos Bank",
    "hcmportal": "HCM Portal (multi-tenant)",
    "myhrabc": "ABC HR Portal",
    "path": "Pathstone (multi-tenant)",
    "richmond": "City of Richmond VA",
    "vcuhealth": "VCU Health",
    "dukeenergy": "Duke Energy",
    "petco": "Petco",
    "agiliti": "Agiliti Health",
    "pfg": "Performance Food Group",
    "alcoa": "Alcoa",
    "aes": "AES Corporation",
    "ameresco": "Ameresco",
    "regeneron": "Regeneron",
    "takeda": "Takeda Pharmaceuticals",
    "catalent": "Catalent",
    "hdsupply": "HD Supply",
    "wk": "Wolters Kluwer",
    "biibhr": "Biogen",
    "integer": "Integer Holdings",
    "williams": "Williams Companies",
    "worldwide": "Worldwide Express",
    "conagrabrands": "Conagra Brands",
    "exxonmobil": "ExxonMobil",
    "microsoft": "Microsoft",
    "merck": "Merck",
    "pandg": "Procter & Gamble",
    "coca-cola": "Coca-Cola",
    "deloitte": "Deloitte",
    "ey": "EY",
    "skookum": "Skookum",
    "interstate": "Interstate Hotels",
    "druryhotels": "Drury Hotels",
    "aritzia": "Aritzia",
    "bilh": "Beth Israel Lahey Health",
    "slihrms": "Sun Life",
    "crateandbarrel": "Crate & Barrel",
    "vibewell": "Vibewell",
    "clark": "Clark Construction",
    "acrt": "ACRT",
    "groundswell": "Groundswell",
    "calwatergroup": "California Water Service",
    "performant": "Performant Financial",
    "saif": "SAIF Corporation",
    "axie": "Axie",
    "ngs": "NGS",
    "kmkp": "Kentucky Machine & Welding",
    "nghs": "Northeast Georgia Health System",
    "phoebehealth": "Phoebe Putney Health System",
    "ambgroup": "Arthur M Blank Family of Businesses",
    "wmeimg": "WME-IMG",
    "calix": "Calix Inc",
    "web": "Web.com",
    "hhc": "Hartford HealthCare",
    "biospace": "BioSpace Corp",
    "pinerest": "Pine Rest Christian Mental Health",
    "gohealthuc": "GoHealth Urgent Care",
    "cresta": "Cresta",
    "sharecare": "Sharecare",
    "caresource": "CareSource",
    "hollandhospital": "Holland Hospital",
    "bcbsa": "Blue Cross Blue Shield Association",
    "bcbsnj": "Horizon BCBS NJ",
    "solutionhealth": "SolutionHealth",
    "usacs": "US Acute Care Solutions",
    "choicehotels": "Choice Hotels",
    "premierinc": "Premier Inc",
    "gen": "Gen Digital",
    "martin": "Martin Health",
    "state_street_v2": "State Street",
    "workday": "Workday Inc",
}


def _parse_careers_url(careers_url: str) -> Optional[tuple[str, str]]:
    """Parse a careers_url like https://markelcorp.wd5.myworkdayjobs.com/GlobalCareers
    into (base_url, board_path).

    Returns None if the URL doesn't match the Workday pattern.
    """
    try:
        p = urlparse(careers_url.strip())
    except Exception:
        return None
    if not p.scheme or not p.netloc or "myworkdayjobs.com" not in p.netloc:
        return None
    base_url = f"{p.scheme}://{p.netloc}"
    # Path may have a leading /, optional locale segment (en-US), and the board.
    # Examples seen:  /External, /en-US/External, /Capital_One, /BAH_Jobs
    parts = [s for s in p.path.split("/") if s]
    if not parts:
        return None
    # Strip locale prefix if present (en-US, en_US, en-GB, etc.)
    if re.match(r"^[a-z]{2}[-_][A-Z]{2}$", parts[0]):
        parts = parts[1:]
    if not parts:
        return None
    board = parts[0]
    return base_url, board


def _load_dynamic_tenants() -> list[tuple[str, str, str]]:
    """Scan workday_tenants/*.json and return a list of (display_name,
    base_url, board) tuples ready to merge with HARDCODED_WORKDAY_TENANTS.

    Silently skips malformed files — never raises. The hardcoded list is the
    safety net; missing dynamic files just means we lose those bonus tenants
    for one run.
    """
    out: list[tuple[str, str, str]] = []
    if not TENANTS_DIR.is_dir():
        return out
    for path in sorted(TENANTS_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # Skip non-OK entries (grinder writes status="OK" for verified tenants)
        status = data.get("status")
        if status and status != "OK":
            continue
        careers_url = (data.get("careers_url") or "").strip()
        if not careers_url:
            continue
        parsed = _parse_careers_url(careers_url)
        if not parsed:
            continue
        base_url, board = parsed
        tenant_id = path.stem
        # Display name resolution: explicit override > grinder-supplied >
        # tenant ID title-cased
        raw_display = (data.get("display_name") or "").strip()
        display = (
            _DISPLAY_NAME_OVERRIDES.get(tenant_id)
            or (raw_display if raw_display and raw_display != tenant_id.title() else None)
            or tenant_id.replace("-", " ").replace("_", " ").title()
        )
        out.append((display, base_url, board))
    return out


# Curated Workday tenants — VERIFIED WORKING via test calls.
# Each entry: (display_name, base_url, board_path)
# Each represents an actual confirmed Workday API endpoint we can hit.
# Add new tenants only after verifying with a test POST returns 200.
#
# v0.3.12: Renamed from WORKDAY_TENANTS to HARDCODED_WORKDAY_TENANTS. The
# runtime-merged superset (hardcoded + JSON files in workday_tenants/) is
# now exposed as WORKDAY_TENANTS at module load. This was a no-op rename
# for readers but a critical fix for the scraper itself: prior to v0.3.12,
# the 145 grinder-discovered JSON tenants in workday_tenants/ were
# completely ignored — the scraper only iterated the hardcoded 31. See
# _load_dynamic_tenants() above.
HARDCODED_WORKDAY_TENANTS: list[tuple[str, str, str]] = [
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


def _merge_tenants(
    hardcoded: list[tuple[str, str, str]],
    dynamic: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Merge hardcoded + dynamic tenants, deduping on (base_url, board).

    Dedup key uses the lowercased base_url + board so that a hardcoded entry
    like ('Capital One', 'https://capitalone.wd12.myworkdayjobs.com',
    'Capital_One') is recognized as the same tenant as a JSON file at
    capitalone.json with the same careers_url. Hardcoded entries win on
    collision (their display names are hand-curated).
    """
    seen: set[tuple[str, str]] = set()
    merged: list[tuple[str, str, str]] = []
    for entry in hardcoded:
        _, base_url, board = entry
        key = (base_url.lower().rstrip("/"), board.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    for entry in dynamic:
        _, base_url, board = entry
        key = (base_url.lower().rstrip("/"), board.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


# Runtime tenant superset: hardcoded base + JSON-file extras from
# workday_tenants/. Computed at module import. Any code that previously read
# `WORKDAY_TENANTS` keeps working — this is a drop-in replacement, just with
# 4-5x more entries in production.
_dynamic_tenants = _load_dynamic_tenants()
WORKDAY_TENANTS: list[tuple[str, str, str]] = _merge_tenants(
    HARDCODED_WORKDAY_TENANTS,
    _dynamic_tenants,
)

# v0.3.12: emit a one-line summary at module import so we can verify the
# tenant loader is actually reading JSON files in production. If you see
# "dynamic=0 (using hardcoded only)" in the audit's stdout log, the
# PyInstaller bundle didn't include the workday_tenants/*.json files —
# this is the first thing to check when STRONG count regresses or
# total_scraped is suspiciously low.
print(
    f"[workday] tenant loader: hardcoded={len(HARDCODED_WORKDAY_TENANTS)} "
    f"dynamic={len(_dynamic_tenants)} "
    f"merged={len(WORKDAY_TENANTS)} "
    f"tenants_dir={TENANTS_DIR}",
    flush=True,
)

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

    def __init__(self, client=None):  # type: ignore[no-untyped-def]
        super().__init__(client=client)
        # Wave-2B Phase 2 (FIX 25, 2026-05-30): per-tenant facets-support
        # tracker. Workday tenants are independently configured — some
        # accept our standard facet keys (locations/postingDateRange),
        # others reject with 400/422. Once a tenant fails facets, remember
        # it so subsequent keyword queries for that tenant skip facets
        # entirely (avoid wasting 1 doomed request per keyword). Empty set
        # at construction; populated by _search_tenant_keyword as it
        # discovers failures during the scrape.
        self._tenants_without_facet_support: set[str] = set()

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Workday tenants are independent — fan out across all tenants × keywords
        # v0.3.9: bumped concurrency 12 → 20 per peer review.
        # v0.3.13: env-var override added. Run `bench_workday_concurrency.py`
        # to find the production sweet spot. Each tenant has its own API
        # endpoint (no shared rate limit), so the ceiling is mostly about
        # network egress and per-tenant rate limits. Likely 40-80 is fine.
        # Per-tenant-keyword timeout (30s wall-clock) preserved as safety.
        import os as _os
        concurrency = int(_os.environ.get("WORKDAY_CONCURRENCY", "20"))
        sem = asyncio.Semaphore(concurrency)

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
                            posted_within_days=posted_within_days,
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
        posted_within_days: Optional[int] = None,
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
        # FIX 11 (Wave-2B Phase 1, 2026-05-29): build appliedFacets from
        # self._user_filters so the tenant filters server-side instead of
        # us pulling everything and post-filtering. Drops 30-50% of off-
        # topic roles before they enter the pipeline. The facet keys
        # (`locations`, `workerSubType`, `postingDateRange`) are the ones
        # Workday's careers UI sends; values are tenant-agnostic enum
        # strings on the date-range facet, free text on locations.
        # Some tenants reject unknown facet keys with HTTP 422 — when
        # that happens on the first request, we retry once with an empty
        # facets dict so the tenant still produces results.
        # Wave-2B Phase 2 (FIX 25): if we've already discovered this
        # tenant rejects facets during an earlier keyword in this run,
        # skip facets entirely for the rest of its keywords. Avoids
        # wasting one 400/422 request per keyword on tenants that we
        # already know don't support them.
        tenant_slug = self._tenant_from_url(base_url)
        use_facets = bool(config.WORKDAY_USE_FACETS)
        if use_facets and tenant_slug in self._tenants_without_facet_support:
            use_facets = False
        uf = getattr(self, "_user_filters", None) or {}
        applied_facets: dict[str, Any] = {}
        if use_facets:
            loc_text = (uf.get("location_text") or "").strip()
            if loc_text:
                applied_facets["locations"] = [loc_text]
            if uf.get("remote_only"):
                applied_facets["workerSubType"] = ["Remote"]
            if posted_within_days:
                # Workday's postingDateRange enum buckets. Map our day
                # window to the closest supported bucket; anything above
                # 30d falls into the broadest bucket Workday supports.
                if posted_within_days <= 1:
                    applied_facets["postingDateRange"] = ["1"]
                elif posted_within_days <= 7:
                    applied_facets["postingDateRange"] = ["7"]
                else:
                    applied_facets["postingDateRange"] = ["30"]
        facets_disabled = False  # flips True after a 422-retry
        while len(roles) < limit:
            body = {
                "appliedFacets": {} if facets_disabled else applied_facets,
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
            # FIX 11 (Phase 1) + FIX 25 (Phase 2, 2026-05-30): rejection
            # on the first request usually means the tenant rejected one
            # of our facet keys. The Phase 1 audit showed Workday tenants
            # return BOTH 400 AND 422 for this — Phase 1 only caught 422.
            # FIX 25 broadens to 400 AND caches the failure per-tenant so
            # subsequent keywords for the same tenant skip facets from
            # the start (saves N-1 wasted requests per tenant per run).
            if (
                response.status_code in (400, 422)
                and not facets_disabled
                and applied_facets
                and offset == 0
            ):
                print(
                    f"[workday] tenant={display_name} HTTP {response.status_code} "
                    f"with facets — retrying without (and marking tenant "
                    f"facet-incompatible for remainder of run)",
                    flush=True,
                )
                facets_disabled = True
                self._tenants_without_facet_support.add(tenant_slug)
                continue
            if response.status_code >= 400:
                # FIX 37 (Wave-2B Phase 1, 2026-05-29): distinguish 429
                # from generic 4xx. 429 = tenant is rate-limiting us,
                # worth tracking separately so we can spot which
                # tenants throttle (and decide whether to slow that
                # tenant down or back off entirely).
                if response.status_code == 429:
                    print(
                        f"[workday] tenant={display_name} HTTP 429 "
                        f"(rate-limited) on keyword='{keyword}' offset={offset}",
                        flush=True,
                    )
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

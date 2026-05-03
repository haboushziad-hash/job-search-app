"""Smart Workday tenant verification — debugs 422s and 404s in clusters.

Strategy:
  1. For each candidate, try multiple BODY VARIANTS against the supplied URL.
     The 75 422 errors clustered together suggest 3-4 distinct Workday API
     configurations across tenants. Trying 5 variants typically unlocks each.
  2. If the supplied URL is 404 (wrong subdomain/board), cycle through
     wd1/wd3/wd5/wd12 datacenter prefixes × ~6 common board name patterns.
  3. Report the WORKING (url, board, body_variant) per tenant for ingestion
     into the production WORKDAY_TENANTS list.

Run from project root:
    backend/venv/Scripts/python.exe scripts/verify_workday_smart.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx


# ---------------------------------------------------------------------------
# Body variants — tried in order, first 200-with-jobs wins
# ---------------------------------------------------------------------------
BODY_VARIANTS: list[tuple[str, dict]] = [
    ("v1_minimal",         {"searchText": "manager"}),
    ("v2_with_pagination", {"searchText": "manager", "limit": 20, "offset": 0}),
    ("v3_with_facets",     {"searchText": "manager", "limit": 20, "offset": 0, "appliedFacets": {}}),
    ("v4_with_locations",  {"searchText": "manager", "limit": 20, "offset": 0, "appliedFacets": {"locations": []}}),
    ("v5_query_keyword",   {"query": "manager", "limit": 20, "offset": 0}),
    ("v6_legacy",          {"searchText": "manager", "limit": 20, "offset": 0, "appliedFacets": {}, "categories": []}),
]

# URL fallback combinations for 404s
WD_DCS = ["wd1", "wd3", "wd5", "wd12", "wd103"]
COMMON_BOARDS = [
    "External",
    "External_Career_Site",
    "Careers",
    "en-US",
    "External_Careers",
    "ExternalCareerSite",
    "{slug}_External",
    "{Slug}_Careers",  # capitalized
    "{Slug}",
    "global",
]


# ---------------------------------------------------------------------------
# Candidate list — expanded with the other Claude's additions
# ---------------------------------------------------------------------------
CANDIDATES: list[tuple[str, str, str]] = [
    # Currently-verified working
    ("PwC",                "https://pwc.wd3.myworkdayjobs.com",                "Global_Experienced_Careers"),
    ("Accenture",          "https://accenture.wd103.myworkdayjobs.com",        "AccentureCareers"),
    ("Booz Allen",         "https://bah.wd1.myworkdayjobs.com",                "BAH_Jobs"),
    ("Salesforce",         "https://salesforce.wd12.myworkdayjobs.com",        "External_Career_Site"),
    ("Adobe",              "https://adobe.wd5.myworkdayjobs.com",              "external_experienced"),
    ("Mastercard",         "https://mastercard.wd1.myworkdayjobs.com",         "CorporateCareers"),
    ("Capital One",        "https://capitalone.wd12.myworkdayjobs.com",        "Capital_One"),
    ("Visa",               "https://visa.wd5.myworkdayjobs.com",               "Visa"),
    ("Pfizer",             "https://pfizer.wd1.myworkdayjobs.com",             "PfizerCareers"),

    # Big 4 + consulting (Tier 1)
    ("Deloitte",           "https://deloitte.wd1.myworkdayjobs.com",           "Deloitte_External"),
    ("EY",                 "https://ey.wd5.myworkdayjobs.com",                 "EY_External_Site"),
    ("KPMG",               "https://kpmg.wd5.myworkdayjobs.com",               "KPMG_Careers"),
    ("McKinsey",           "https://mckinsey.wd5.myworkdayjobs.com",           "McKinsey_External_Careers"),
    ("BDO",                "https://bdo.wd1.myworkdayjobs.com",                "BDO"),
    ("RSM",                "https://rsmus.wd5.myworkdayjobs.com",              "RSM_Careers"),
    ("Grant Thornton",     "https://gt.wd5.myworkdayjobs.com",                 "Grant_Thornton_Careers"),

    # Federal contractors
    ("Leidos",             "https://leidos.wd5.myworkdayjobs.com",             "External"),
    ("SAIC",               "https://saic.wd1.myworkdayjobs.com",               "SAIC_External_Career_Site"),
    ("CACI",               "https://caci.wd1.myworkdayjobs.com",               "External"),
    ("ManTech",            "https://mantech.wd5.myworkdayjobs.com",            "ManTech_External"),
    ("Peraton",            "https://peraton.wd5.myworkdayjobs.com",            "Peraton_Careers"),
    ("GDIT",               "https://gdit.wd5.myworkdayjobs.com",               "External"),
    ("MITRE",              "https://mitre.wd5.myworkdayjobs.com",              "MITRE"),
    ("Lockheed Martin",    "https://lmcareers.wd5.myworkdayjobs.com",          "Lockheed_Martin"),
    ("Northrop Grumman",   "https://ngc.wd1.myworkdayjobs.com",                "NGCExternal"),
    ("Raytheon (RTX)",     "https://rtx.wd5.myworkdayjobs.com",                "RTX"),
    ("BAE Systems",        "https://baesystems.wd1.myworkdayjobs.com",         "BAESystems_USA_Careers"),
    ("L3Harris",           "https://l3harris.wd1.myworkdayjobs.com",           "L3Harris_External"),

    # Banking
    ("JPMorgan Chase",     "https://jpmc.wd5.myworkdayjobs.com",               "External_experienced_careers"),
    ("Goldman Sachs",      "https://goldmansachs.wd1.myworkdayjobs.com",       "Professional_Career_Search"),
    ("Morgan Stanley",     "https://ms.wd5.myworkdayjobs.com",                 "External"),
    ("Wells Fargo",        "https://wellsfargojobs.wd1.myworkdayjobs.com",     "External"),
    ("Citi",               "https://citi.wd5.myworkdayjobs.com",               "2"),
    ("Bank of America",    "https://bankofamerica.wd1.myworkdayjobs.com",      "External"),
    ("US Bank",            "https://usbank.wd1.myworkdayjobs.com",             "External"),
    ("PNC",                "https://pnc.wd5.myworkdayjobs.com",                "External"),
    ("Charles Schwab",     "https://schwab.wd5.myworkdayjobs.com",             "External"),
    ("BlackRock",          "https://blackrock.wd5.myworkdayjobs.com",          "BlackRock_Professional"),
    ("Fidelity",           "https://fidelity.wd1.myworkdayjobs.com",           "External"),
    ("Truist",             "https://truist.wd5.myworkdayjobs.com",             "External"),
    ("TD Bank",            "https://tdbank.wd1.myworkdayjobs.com",             "External"),

    # Insurance
    ("Allstate",           "https://allstate.wd5.myworkdayjobs.com",           "External"),
    ("Liberty Mutual",     "https://lmi.wd1.myworkdayjobs.com",                "Liberty_Mutual"),
    ("Progressive",        "https://progressive.wd5.myworkdayjobs.com",        "External"),
    ("MetLife",            "https://metlife.wd5.myworkdayjobs.com",            "MetLife"),
    ("Prudential",         "https://prudential.wd1.myworkdayjobs.com",         "PRUExternal"),
    ("Travelers",          "https://travelers.wd5.myworkdayjobs.com",          "External"),
    ("Nationwide",         "https://nationwide.wd1.myworkdayjobs.com",         "Nationwide_Jobs"),
    ("AIG",                "https://aig.wd1.myworkdayjobs.com",                "aig"),
    ("Aflac",              "https://aflac.wd5.myworkdayjobs.com",              "External"),

    # CPG
    ("PepsiCo",            "https://pepsico.wd5.myworkdayjobs.com",            "PepsiCoCareers"),
    ("Coca-Cola",          "https://coca-cola.wd1.myworkdayjobs.com",          "coca-cola_Careers"),
    ("Anheuser-Busch",     "https://ab-inbev.wd3.myworkdayjobs.com",           "External"),
    ("Procter & Gamble",   "https://pg.wd5.myworkdayjobs.com",                 "P_GCareers"),
    ("Unilever",           "https://unilever.wd3.myworkdayjobs.com",           "External"),
    ("Mondelez",           "https://mondelez.wd1.myworkdayjobs.com",           "External"),
    ("Kraft Heinz",        "https://kraftheinz.wd1.myworkdayjobs.com",         "kraftheinz"),
    ("General Mills",      "https://generalmills.wd1.myworkdayjobs.com",       "generalmills"),
    ("Kellogg",            "https://kellogg.wd5.myworkdayjobs.com",            "kellogg"),
    ("Colgate-Palmolive",  "https://colgate.wd1.myworkdayjobs.com",            "Colgate_Careers"),

    # Retail
    ("Target",             "https://target.wd5.myworkdayjobs.com",             "targetcareers"),
    ("Walmart",            "https://walmart.wd5.myworkdayjobs.com",            "WalmartExternal"),
    ("Kroger",             "https://kroger.wd5.myworkdayjobs.com",             "Kroger_Careers"),
    ("Costco",             "https://costco.wd5.myworkdayjobs.com",             "External"),
    ("Lowe's",             "https://lowes.wd1.myworkdayjobs.com",              "Lowes_External"),
    ("Best Buy",           "https://bestbuy.wd1.myworkdayjobs.com",            "External"),
    ("Macy's",             "https://macys.wd1.myworkdayjobs.com",              "Macys"),

    # QSR / Food service (NEW from other Claude)
    ("Starbucks",          "https://starbucks.wd5.myworkdayjobs.com",          "External"),
    ("McDonald's",         "https://mcdonalds.wd1.myworkdayjobs.com",          "Corporate"),
    ("Chipotle",           "https://chipotle.wd5.myworkdayjobs.com",           "External"),
    ("Yum Brands",         "https://yum.wd5.myworkdayjobs.com",                "Yum_External"),

    # Logistics
    ("UPS",                "https://ups.wd5.myworkdayjobs.com",                "UPSCareers"),
    ("FedEx",              "https://fedex.wd1.myworkdayjobs.com",              "fedexcareers"),
    ("DHL",                "https://dhl.wd3.myworkdayjobs.com",                "External"),
    ("J.B. Hunt",          "https://jbhunt.wd1.myworkdayjobs.com",             "JBHCareers"),
    ("XPO",                "https://xpo.wd5.myworkdayjobs.com",                "XPO_External_Career_Site"),
    ("Old Dominion",       "https://odfl.wd1.myworkdayjobs.com",               "OldDominion"),

    # Healthcare / pharma
    ("Johnson & Johnson",  "https://jnjcareers.wd5.myworkdayjobs.com",         "Search"),
    ("Merck",              "https://merck.wd5.myworkdayjobs.com",              "External"),
    ("Eli Lilly",          "https://lilly.wd5.myworkdayjobs.com",              "LLY"),
    ("AbbVie",             "https://abbvie.wd5.myworkdayjobs.com",             "External"),
    ("Bristol Myers",      "https://bms.wd5.myworkdayjobs.com",                "BMS_Careers"),
    ("Amgen",              "https://amgen.wd1.myworkdayjobs.com",              "Amgen"),
    ("Gilead",             "https://gilead.wd1.myworkdayjobs.com",             "gileadcareers"),
    ("Moderna",            "https://moderna.wd5.myworkdayjobs.com",            "External"),
    ("UnitedHealth",       "https://uhg.wd5.myworkdayjobs.com",                "External"),
    ("CVS Health",         "https://cvshealth.wd1.myworkdayjobs.com",          "CVS_Health_External_Career_Site"),
    ("McKesson",           "https://mckesson.wd5.myworkdayjobs.com",           "External_Career_Site"),
    ("Cardinal Health",    "https://cardinalhealth.wd1.myworkdayjobs.com",     "cardinalhealth"),
    ("HCA Healthcare",     "https://hca.wd1.myworkdayjobs.com",                "Hospital_Search"),

    # Health systems (NEW from other Claude)
    ("Kaiser Permanente",  "https://kaiserpermanente.wd1.myworkdayjobs.com",   "External"),
    ("Mayo Clinic",        "https://mayoclinic.wd1.myworkdayjobs.com",         "MayoClinicJobs"),
    ("Cleveland Clinic",   "https://clevelandclinic.wd1.myworkdayjobs.com",    "Cleveland_Clinic"),
    ("Ascension",          "https://ascension.wd1.myworkdayjobs.com",          "Ascension"),

    # Tech (non-Greenhouse)
    ("Microsoft",          "https://microsoft.wd1.myworkdayjobs.com",          "external"),
    ("Oracle",             "https://oracle.wd5.myworkdayjobs.com",             "External"),
    ("IBM",                "https://ibmglobal.wd5.myworkdayjobs.com",          "IBM_Careers"),
    ("SAP",                "https://sap.wd3.myworkdayjobs.com",                "SAPCareers"),
    ("Cisco",              "https://cisco.wd5.myworkdayjobs.com",              "External_Career_Site"),
    ("Dell",               "https://dell.wd1.myworkdayjobs.com",               "External"),
    ("HP",                 "https://hp.wd5.myworkdayjobs.com",                 "ExternalCareerSite"),
    ("Intel",              "https://intel.wd1.myworkdayjobs.com",              "External"),

    # Energy
    ("ExxonMobil",         "https://exxonmobil.wd5.myworkdayjobs.com",         "ExxonMobil"),
    ("Chevron",            "https://chevron.wd5.myworkdayjobs.com",            "Chevron"),
    ("ConocoPhillips",     "https://copclng.wd1.myworkdayjobs.com",            "ConocoPhillips_External"),
    ("Duke Energy",        "https://duke-energy.wd5.myworkdayjobs.com",        "Duke_External"),
    ("NextEra Energy",     "https://nextera.wd5.myworkdayjobs.com",            "External"),
    ("Dominion Energy",    "https://dominionenergy.wd1.myworkdayjobs.com",     "Dominion_Energy_Jobs"),

    # Manufacturing
    ("GE",                 "https://ge.wd5.myworkdayjobs.com",                 "GE_External_Careers"),
    ("3M",                 "https://3m.wd1.myworkdayjobs.com",                 "Search"),
    ("Honeywell",          "https://honeywell.wd1.myworkdayjobs.com",          "Honeywell"),
    ("Caterpillar",        "https://caterpillar.wd5.myworkdayjobs.com",        "External"),
    ("Emerson",            "https://emerson.wd5.myworkdayjobs.com",            "Emerson"),
    ("Eaton",              "https://eaton.wd1.myworkdayjobs.com",              "Eaton_External_Site"),

    # Telecom + media
    ("Verizon",            "https://verizon.wd5.myworkdayjobs.com",            "VerizonCareers"),
    ("Comcast",            "https://comcast.wd5.myworkdayjobs.com",            "Comcast_Careers"),
    ("T-Mobile",           "https://tmobile.wd5.myworkdayjobs.com",            "External"),
    ("AT&T",               "https://att.wd1.myworkdayjobs.com",                "AT_T"),
    ("Disney",             "https://disney.wd5.myworkdayjobs.com",             "disneycareer"),

    # Hospitality / travel
    ("Marriott",           "https://marriott.wd1.myworkdayjobs.com",           "Marriott_HR"),
    ("Hilton",             "https://hilton.wd5.myworkdayjobs.com",             "Hilton_Careers"),
    ("Delta Air Lines",    "https://delta.wd5.myworkdayjobs.com",              "External_Career_Site"),
    ("United Airlines",    "https://united.wd1.myworkdayjobs.com",             "ualcareers"),
    ("American Airlines",  "https://aa.wd1.myworkdayjobs.com",                 "AmericanAirlinesCareers"),

    # Real estate / CRE
    ("CBRE",               "https://cbre.wd1.myworkdayjobs.com",               "CBRE"),
    ("JLL",                "https://jll.wd5.myworkdayjobs.com",                "jllcareers"),
    ("Cushman & Wakefield","https://cushwake.wd1.myworkdayjobs.com",           "CushWake"),

    # Environmental / engineering services
    ("AECOM",              "https://aecom.wd1.myworkdayjobs.com",              "ExternalCareerSite"),
    ("WSP",                "https://wsp.wd3.myworkdayjobs.com",                "WSP_USA"),
    ("Tetra Tech",         "https://tetratech.wd1.myworkdayjobs.com",          "Tetra_Tech_Careers"),
    ("Jacobs",             "https://jacobs.wd1.myworkdayjobs.com",             "Jacobs"),

    # Aerospace
    ("Boeing",             "https://boeing.wd1.myworkdayjobs.com",             "External"),

    # Staffing firms (NEW from other Claude)
    ("Robert Half",        "https://roberthalf.wd1.myworkdayjobs.com",         "External"),
    ("Randstad",           "https://randstad.wd5.myworkdayjobs.com",           "External"),
    ("Kforce",             "https://kforce.wd1.myworkdayjobs.com",             "External"),

    # Policy/research (NEW)
    ("RAND Corporation",   "https://rand.wd5.myworkdayjobs.com",               "External"),

    # Regional Richmond/DMV employers
    ("Markel",             "https://markel.wd1.myworkdayjobs.com",             "Markel"),
    ("CarMax",             "https://carmax.wd1.myworkdayjobs.com",             "CarMax"),
    ("Altria",             "https://altria.wd5.myworkdayjobs.com",             "Altria_External_Career_Site"),
]


def tenant_slug_from_url(base_url: str) -> str:
    m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", base_url)
    if m:
        return m.group(1)
    m = re.match(r"https?://([^.]+)\.", base_url)
    return m.group(1) if m else ""


def base_url_for_slug(slug: str, dc: str) -> str:
    return f"https://{slug}.{dc}.myworkdayjobs.com"


def expand_board_template(board: str, slug: str) -> str:
    return board.replace("{slug}", slug.lower()).replace("{Slug}", slug.capitalize())


async def try_one(client: httpx.AsyncClient, base_url: str, board: str, body: dict) -> tuple[int, int]:
    """Returns (http_status, jobs_count)."""
    slug = tenant_slug_from_url(base_url)
    if not slug:
        return (0, 0)
    endpoint = f"{base_url}/wday/cxs/{slug}/{board}/jobs"
    try:
        r = await client.post(
            endpoint, json=body, timeout=15,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
        if r.status_code != 200:
            return (r.status_code, 0)
        try:
            data = r.json()
            postings = data.get("jobPostings") or []
            total = data.get("total")
            count = total if total is not None else len(postings)
            return (200, count)
        except Exception:
            return (200, 0)
    except Exception:
        return (-1, 0)


async def smart_verify(
    client: httpx.AsyncClient,
    name: str,
    base_url: str,
    board: str,
    sem: asyncio.Semaphore,
) -> tuple[str, Optional[str], Optional[str], Optional[str], int, str]:
    """Try the supplied (base_url, board) with all body variants. If still
    failing, try alternate (datacenter, board) combinations.
    Returns (name, working_base_url, working_board, working_body_variant, count, note).
    """
    async with sem:
        # Step 1: try supplied URL with all body variants
        for vname, body in BODY_VARIANTS:
            status, count = await try_one(client, base_url, board, body)
            if status == 200 and count > 0:
                return (name, base_url, board, vname, count, f"OK (variant {vname})")
            if status == 200:
                # 200 but 0 jobs — wrong board? continue to URL fallback
                pass

        # Step 2: URL/board fallback for 404s. Try other DCs + common boards.
        slug = tenant_slug_from_url(base_url)
        if not slug:
            return (name, None, None, None, 0, "couldn't parse slug")

        candidates_to_try: list[tuple[str, str]] = []
        for dc in WD_DCS:
            for b_template in COMMON_BOARDS + [board]:
                b = expand_board_template(b_template, slug)
                u = base_url_for_slug(slug, dc)
                if (u, b) != (base_url, board):
                    candidates_to_try.append((u, b))

        # Limit total fallback attempts to keep runtime reasonable
        # (5 DCs × 11 boards = 55 combos × 6 body variants = 330 tries). We'll
        # cap at 30 combos × first body variant first to find working URL,
        # then test variants on that URL.
        fallback_url: Optional[str] = None
        fallback_board: Optional[str] = None
        for u, b in candidates_to_try[:30]:
            status, count = await try_one(client, u, b, BODY_VARIANTS[0][1])
            if status == 200 and count > 0:
                return (name, u, b, "v1_minimal", count, f"OK fallback ({u} / {b})")
            if status == 200 and count == 0 and fallback_url is None:
                fallback_url, fallback_board = u, b  # save in case

        if fallback_url:
            # Found a 200-but-0-jobs URL — try other body variants on it
            for vname, body in BODY_VARIANTS[1:]:
                status, count = await try_one(client, fallback_url, fallback_board, body)
                if status == 200 and count > 0:
                    return (name, fallback_url, fallback_board, vname, count,
                            f"OK fallback+variant ({vname})")

        return (name, None, None, None, 0, "all attempts failed")


async def main() -> None:
    print(f"Smart-verifying {len(CANDIDATES)} Workday tenants...")
    print(f"  body variants per attempt: {len(BODY_VARIANTS)}")
    print(f"  url fallbacks if needed: up to {len(WD_DCS)} datacenters × {len(COMMON_BOARDS)} board names\n")

    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(8)  # be polite, don't hammer
        results = await asyncio.gather(*[
            smart_verify(client, name, url, board, sem) for name, url, board in CANDIDATES
        ])

    working = [r for r in results if r[1] is not None]
    failing = [r for r in results if r[1] is None]

    print(f"=== WORKING ({len(working)}/{len(CANDIDATES)}) ===")
    for name, url, board, variant, count, note in sorted(working, key=lambda x: -x[4]):
        print(f"  ok  [{count:>5}]  {name:25s}  variant={variant}")

    print(f"\n=== STILL FAILING ({len(failing)}/{len(CANDIDATES)}) ===")
    for name, _, _, _, _, note in failing:
        print(f"  fail   {name}")

    # Variant distribution
    by_variant: dict[str, int] = {}
    for _, _, _, v, _, _ in working:
        if v:
            by_variant[v] = by_variant.get(v, 0) + 1
    print(f"\n=== VARIANT BREAKDOWN ===")
    for v, n in sorted(by_variant.items(), key=lambda x: -x[1]):
        print(f"  {v:25s} {n} tenants")

    # Emit production-ready Python list
    out_lines = [
        "# Auto-generated by scripts/verify_workday_smart.py",
        "# Each entry: (display_name, base_url, board_path, body_variant)",
        "WORKDAY_TENANTS_VERIFIED = [",
    ]
    for name, url, board, variant, count, _ in sorted(working, key=lambda x: -x[4]):
        out_lines.append(f'    ({name!r:30s}, {url!r:60s}, {board!r:40s}, {variant!r}),  # {count} jobs')
    out_lines.append("]")
    Path('C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/workday_verified.py').write_text(
        "\n".join(out_lines), encoding='utf-8')
    print(f'\nProduction list written to scripts/workday_verified.py')


if __name__ == "__main__":
    asyncio.run(main())

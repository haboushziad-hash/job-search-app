"""Verify a list of candidate Workday tenants by hitting their actual search
endpoint with a generic keyword. Only tenants that return >0 jobs survive.

Why this matters: a 200 status on the homepage doesn't mean the tenant is
serving jobs publicly. Some Workday tenants restrict external access or
require region selection before returning results. We need a real search
result to confirm a tenant is usable.

Run from project root:
    backend/venv/Scripts/python.exe scripts/verify_workday_tenants.py
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx


# Candidate tenants — categorized for transparency. Each entry:
#   (display_name, base_url, board_path)
# Many of these are inferred from common Workday tenant naming. Verifier
# script will weed out the ones that don't actually serve a public board.
CANDIDATES: list[tuple[str, str, str]] = [
    # ---- Currently in production (verified previously) ----
    ("PwC",                "https://pwc.wd3.myworkdayjobs.com",                "Global_Experienced_Careers"),
    ("Accenture",          "https://accenture.wd103.myworkdayjobs.com",        "AccentureCareers"),
    ("Booz Allen",         "https://bah.wd1.myworkdayjobs.com",                "BAH_Jobs"),
    ("Salesforce",         "https://salesforce.wd12.myworkdayjobs.com",        "External_Career_Site"),
    ("Adobe",              "https://adobe.wd5.myworkdayjobs.com",              "external_experienced"),
    ("Mastercard",         "https://mastercard.wd1.myworkdayjobs.com",         "CorporateCareers"),
    ("Capital One",        "https://capitalone.wd12.myworkdayjobs.com",        "Capital_One"),
    ("Bank of America",    "https://ghr.wd1.myworkdayjobs.com",                "en-us"),
    ("Visa",               "https://visa.wd5.myworkdayjobs.com",               "Visa"),
    ("Pfizer",             "https://pfizer.wd1.myworkdayjobs.com",             "PfizerCareers"),
    ("Boeing",             "https://boeing.wd1.myworkdayjobs.com",             "en-US"),
    ("AT&T",               "https://att.wd1.myworkdayjobs.com",                "en-US"),

    # ---- Big 4 + consulting ----
    ("Deloitte",           "https://deloitte.wd1.myworkdayjobs.com",           "Deloitte_External"),
    ("Deloitte (US)",      "https://apply.deloitte.com/wday/cxs/deloitte/Deloitte_External/jobs",  "Deloitte_External"),  # alt path probe
    ("EY",                 "https://ey.wd5.myworkdayjobs.com",                 "EY_External_Site"),
    ("KPMG",               "https://kpmg.wd5.myworkdayjobs.com",               "KPMG_Careers"),
    ("McKinsey",           "https://mckinsey.wd5.myworkdayjobs.com",           "McKinsey_External_Careers"),
    ("BDO",                "https://bdo.wd1.myworkdayjobs.com",                "BDO"),
    ("RSM",                "https://rsmus.wd5.myworkdayjobs.com",              "RSM_Careers"),
    ("Grant Thornton",     "https://gt.wd5.myworkdayjobs.com",                 "Grant_Thornton_Careers"),

    # ---- Federal contractors ----
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

    # ---- Banking + financial services ----
    ("JPMorgan Chase",     "https://jpmc.wd5.myworkdayjobs.com",               "External_experienced_careers"),
    ("Goldman Sachs",      "https://goldmansachs.wd1.myworkdayjobs.com",       "Professional_Career_Search"),
    ("Morgan Stanley",     "https://ms.wd5.myworkdayjobs.com",                 "External"),
    ("Wells Fargo",        "https://wd1.myworkdaysite.com",                    "WellsFargoJobs"),  # uncertain
    ("Citi",               "https://citi.wd5.myworkdayjobs.com",               "2"),
    ("US Bank",            "https://usbank.wd1.myworkdayjobs.com",             "USBank_Careers"),
    ("PNC",                "https://pnc.wd5.myworkdayjobs.com",                "External"),
    ("Charles Schwab",     "https://schwab.wd5.myworkdayjobs.com",             "SchwabJobs"),
    ("BlackRock",          "https://blackrock.wd5.myworkdayjobs.com",          "BlackRock_Professional"),

    # ---- Insurance ----
    ("Allstate",           "https://allstate.wd5.myworkdayjobs.com",           "External"),
    ("Liberty Mutual",     "https://lmi.wd1.myworkdayjobs.com",                "Liberty_Mutual"),
    ("Progressive",        "https://progressive.wd5.myworkdayjobs.com",        "External"),
    ("MetLife",            "https://metlife.wd5.myworkdayjobs.com",            "MetLife"),
    ("Prudential",         "https://prudential.wd1.myworkdayjobs.com",         "PRUExternal"),
    ("Travelers",          "https://travelers.wd5.myworkdayjobs.com",          "External"),
    ("Nationwide",         "https://nationwide.wd1.myworkdayjobs.com",         "Nationwide_Jobs"),
    ("AIG",                "https://aig.wd1.myworkdayjobs.com",                "aig"),
    ("Aflac",              "https://aflac.wd5.myworkdayjobs.com",              "External"),

    # ---- CPG / consumer ----
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

    # ---- Retail ----
    ("Target",             "https://target.wd5.myworkdayjobs.com",             "targetcareers"),
    ("Walmart",            "https://walmart.wd5.myworkdayjobs.com",            "WalmartExternal"),
    ("Kroger",             "https://kroger.wd5.myworkdayjobs.com",             "Kroger_Careers"),
    ("Costco",             "https://costco.wd5.myworkdayjobs.com",             "External"),
    ("Lowe's",             "https://lowes.wd1.myworkdayjobs.com",              "Lowes_External"),
    ("Best Buy",           "https://bestbuy.wd1.myworkdayjobs.com",            "External"),

    # ---- Logistics + distribution ----
    ("UPS",                "https://ups.wd5.myworkdayjobs.com",                "UPSCareers"),
    ("FedEx",              "https://fedex.wd1.myworkdayjobs.com",              "fedexcareers"),
    ("DHL",                "https://dhl.wd3.myworkdayjobs.com",                "External"),
    ("J.B. Hunt",          "https://jbhunt.wd1.myworkdayjobs.com",             "JBHCareers"),
    ("XPO",                "https://xpo.wd5.myworkdayjobs.com",                "XPO_External_Career_Site"),
    ("Old Dominion",       "https://odfl.wd1.myworkdayjobs.com",               "OldDominion"),

    # ---- Healthcare / pharma ----
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

    # ---- Tech (non-Greenhouse) ----
    ("Microsoft",          "https://microsoft.wd1.myworkdayjobs.com",          "external"),
    ("Oracle",             "https://oracle.wd5.myworkdayjobs.com",             "External"),
    ("IBM",                "https://ibmglobal.wd5.myworkdayjobs.com",          "IBM_Careers"),
    ("SAP",                "https://sap.wd3.myworkdayjobs.com",                "SAPCareers"),
    ("Cisco",              "https://cisco.wd5.myworkdayjobs.com",              "External_Career_Site"),
    ("Dell",               "https://dell.wd1.myworkdayjobs.com",               "External"),
    ("HP",                 "https://hp.wd5.myworkdayjobs.com",                 "ExternalCareerSite"),
    ("Intel",              "https://intel.wd1.myworkdayjobs.com",              "External"),

    # ---- Energy ----
    ("ExxonMobil",         "https://exxonmobil.wd5.myworkdayjobs.com",         "ExxonMobil"),
    ("Chevron",            "https://chevron.wd5.myworkdayjobs.com",            "Chevron"),
    ("ConocoPhillips",     "https://copclng.wd1.myworkdayjobs.com",            "ConocoPhillips_External"),
    ("Duke Energy",        "https://duke-energy.wd5.myworkdayjobs.com",        "Duke_External"),
    ("NextEra Energy",     "https://nextera.wd5.myworkdayjobs.com",            "External"),
    ("Dominion Energy",    "https://dominionenergy.wd1.myworkdayjobs.com",     "Dominion_Energy_Jobs"),

    # ---- Manufacturing ----
    ("GE",                 "https://ge.wd5.myworkdayjobs.com",                 "GE_External_Careers"),
    ("3M",                 "https://3m.wd1.myworkdayjobs.com",                 "Search"),
    ("Honeywell",          "https://honeywell.wd1.myworkdayjobs.com",          "Honeywell"),
    ("Caterpillar",        "https://caterpillar.wd5.myworkdayjobs.com",        "External"),
    ("Emerson",            "https://emerson.wd5.myworkdayjobs.com",            "Emerson"),
    ("Eaton",              "https://eaton.wd1.myworkdayjobs.com",              "Eaton_External_Site"),

    # ---- Telecom + media ----
    ("Verizon",            "https://verizon.wd5.myworkdayjobs.com",            "VerizonCareers"),
    ("Comcast",            "https://comcast.wd5.myworkdayjobs.com",            "Comcast_Careers"),
    ("T-Mobile",           "https://tmobile.wd5.myworkdayjobs.com",            "External"),
    ("Disney",             "https://disney.wd5.myworkdayjobs.com",             "disneycareer"),

    # ---- Hospitality / travel ----
    ("Marriott",           "https://marriott.wd1.myworkdayjobs.com",           "Marriott_HR"),
    ("Hilton",             "https://hilton.wd5.myworkdayjobs.com",             "Hilton_Careers"),
    ("Delta Air Lines",    "https://delta.wd5.myworkdayjobs.com",              "External_Career_Site"),
    ("United Airlines",    "https://united.wd1.myworkdayjobs.com",             "ualcareers"),
    ("American Airlines",  "https://aa.wd1.myworkdayjobs.com",                 "AmericanAirlinesCareers"),

    # ---- Real estate / CRE ----
    ("CBRE",               "https://cbre.wd1.myworkdayjobs.com",               "CBRE"),
    ("JLL",                "https://jll.wd5.myworkdayjobs.com",                "jllcareers"),
    ("Cushman & Wakefield","https://cushwake.wd1.myworkdayjobs.com",           "CushWake"),

    # ---- Environmental / engineering services ----
    ("AECOM",              "https://aecom.wd1.myworkdayjobs.com",              "ExternalCareerSite"),
    ("WSP",                "https://wsp.wd3.myworkdayjobs.com",                "WSP_USA"),
    ("Tetra Tech",         "https://tetratech.wd1.myworkdayjobs.com",          "Tetra_Tech_Careers"),
    ("Jacobs",             "https://jacobs.wd1.myworkdayjobs.com",             "Jacobs"),

    # ---- Regional Richmond/DMV employers ----
    ("Markel",             "https://markel.wd1.myworkdayjobs.com",             "Markel"),
    ("CarMax",             "https://carmax.wd1.myworkdayjobs.com",             "CarMax"),
    ("Altria",             "https://altria.wd5.myworkdayjobs.com",             "Altria_External_Career_Site"),
]


def tenant_slug_from_url(base_url: str) -> str:
    """Pull the tenant identifier from a base URL like
    'https://accenture.wd103.myworkdayjobs.com' -> 'accenture'."""
    m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", base_url)
    if m:
        return m.group(1)
    # Fallback for non-standard paths
    m = re.match(r"https?://([^.]+)\.", base_url)
    return m.group(1) if m else ""


async def verify_one(client: httpx.AsyncClient, name: str, base_url: str, board: str) -> tuple[str, bool, int, str]:
    """Hit a tenant's search endpoint with 'manager' and report (name, ok, count, note)."""
    slug = tenant_slug_from_url(base_url)
    if not slug:
        return (name, False, 0, "couldn't parse slug")
    endpoint = f"{base_url}/wday/cxs/{slug}/{board}/jobs"
    body = {"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": "manager"}
    try:
        r = await client.post(
            endpoint,
            json=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=20,
        )
        if r.status_code != 200:
            return (name, False, 0, f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            return (name, False, 0, "non-JSON response")
        # Workday returns {"jobPostings":[...], "total": N}
        postings = data.get("jobPostings") or []
        total = data.get("total")
        if total is None:
            total = len(postings)
        return (name, total > 0, total, f"{total} jobs")
    except httpx.HTTPError as e:
        return (name, False, 0, f"{type(e).__name__}")
    except Exception as e:
        return (name, False, 0, f"{type(e).__name__}: {str(e)[:80]}")


async def main() -> None:
    print(f"Verifying {len(CANDIDATES)} candidate Workday tenants (POST searchText=manager)...\n")
    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(15)
        async def guarded(args):
            async with sem:
                return await verify_one(client, *args)
        results = await asyncio.gather(*[guarded(a) for a in CANDIDATES])

    working = [r for r in results if r[1]]
    failing = [r for r in results if not r[1]]

    print(f"=== WORKING ({len(working)}/{len(CANDIDATES)}) ===")
    for name, _, total, note in sorted(working, key=lambda x: -x[2]):
        print(f"  ok    [{total:>5}]  {name}")
    print(f"\n=== FAILING ({len(failing)}/{len(CANDIDATES)}) ===")
    for name, _, _, note in failing:
        print(f"  fail  ({note})  {name}")

    # Emit the verified list as Python tuples ready to paste into workday.py
    out = ["# === VERIFIED tenants (auto-generated " + Path(__file__).name + ") ==="]
    for entry in CANDIDATES:
        name = entry[0]
        if any(r[0] == name and r[1] for r in results):
            base = entry[1]
            board = entry[2]
            out.append(f'    ({name!r:30s}, {base!r:60s}, {board!r}),')
    Path('C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/workday_verified.txt').write_text(
        '\n'.join(out), encoding='utf-8')
    print(f'\nVerified list written to scripts/workday_verified.txt ({len(working)} tenants)')


if __name__ == "__main__":
    asyncio.run(main())

"""Expanded verification — 120 additional candidates from sectors likely
to deploy Workday with the same v1_minimal config our 40 working tenants use."""
import asyncio, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx

NEW_CANDIDATES: list[tuple[str, str, str]] = [
    # Finance (same era as Citi/PNC/Morgan Stanley)
    ("US Bancorp",         "https://usbank.wd5.myworkdayjobs.com",             "USBank_Careers"),
    ("Citizens Financial", "https://citizensbank.wd1.myworkdayjobs.com",       "External"),
    ("Regions Bank",       "https://regions.wd1.myworkdayjobs.com",            "External_Career_Site"),
    ("KeyBank",            "https://keybank.wd5.myworkdayjobs.com",            "External"),
    ("M&T Bank",           "https://mtb.wd1.myworkdayjobs.com",                "MTB"),
    ("Synchrony",          "https://synchrony.wd5.myworkdayjobs.com",          "External"),
    ("Discover Financial", "https://discover.wd1.myworkdayjobs.com",           "Careers"),
    ("Fifth Third Bank",   "https://53.wd1.myworkdayjobs.com",                 "53_External"),

    # Insurance
    ("Hartford Financial", "https://thehartford.wd1.myworkdayjobs.com",        "TheHartford"),
    ("CNA Financial",      "https://cna.wd1.myworkdayjobs.com",                "CNAcareers"),
    ("Unum",               "https://unum.wd1.myworkdayjobs.com",               "Unum"),
    ("Principal Financial","https://principal.wd5.myworkdayjobs.com",          "External"),
    ("USAA",               "https://usaa.wd1.myworkdayjobs.com",               "Search"),

    # Pharma/Healthcare
    ("AstraZeneca",        "https://astrazeneca.wd3.myworkdayjobs.com",        "Careers"),
    ("Novo Nordisk",       "https://novonordisk.wd1.myworkdayjobs.com",        "External"),
    ("Regeneron",          "https://regeneron.wd1.myworkdayjobs.com",          "Regeneron_Careers"),
    ("Vertex",             "https://vrtx.wd1.myworkdayjobs.com",               "vertex"),
    ("Biogen",             "https://biogen.wd5.myworkdayjobs.com",             "External"),
    ("Baxter",             "https://baxter.wd1.myworkdayjobs.com",             "Careers"),
    ("Becton Dickinson",   "https://bd.wd1.myworkdayjobs.com",                 "External"),
    ("Stryker",            "https://stryker.wd1.myworkdayjobs.com",            "stryker"),
    ("Medtronic",          "https://medtronic.wd5.myworkdayjobs.com",          "External"),
    ("Abbott",             "https://abbott.wd5.myworkdayjobs.com",             "Abbott_Careers"),
    ("Edwards Lifesciences","https://edwards.wd1.myworkdayjobs.com",           "Edwards"),
    ("Boston Scientific",  "https://bsci.wd5.myworkdayjobs.com",               "External"),
    ("Zimmer Biomet",      "https://zimmerbiomet.wd1.myworkdayjobs.com",       "Zimmer_Biomet_Careers"),

    # Retail
    ("Home Depot",         "https://homedepot.wd1.myworkdayjobs.com",          "External"),
    ("TJX",                "https://tjx.wd1.myworkdayjobs.com",                "TJX_External_Career_Site"),
    ("Dollar General",     "https://dollargeneral.wd1.myworkdayjobs.com",      "External"),
    ("Dollar Tree",        "https://dollartree.wd1.myworkdayjobs.com",         "External"),
    ("Ross Stores",        "https://rossstores.wd1.myworkdayjobs.com",         "Ross_Stores"),
    ("Tractor Supply",     "https://tractorsupply.wd5.myworkdayjobs.com",      "External"),
    ("AutoZone",           "https://autozone.wd1.myworkdayjobs.com",           "External"),
    ("Lululemon",          "https://lululemon.wd5.myworkdayjobs.com",          "Lululemon_External"),
    ("Nordstrom",          "https://nordstrom.wd5.myworkdayjobs.com",          "Nordstrom"),
    ("Whirlpool",          "https://whirlpool.wd5.myworkdayjobs.com",          "External"),

    # Manufacturing
    ("Illinois Tool Works","https://itw.wd1.myworkdayjobs.com",                "External"),
    ("Parker Hannifin",    "https://parker.wd1.myworkdayjobs.com",             "Parker"),
    ("Textron",            "https://textron.wd1.myworkdayjobs.com",            "Textron_Careers"),
    ("Fortive",            "https://fortive.wd1.myworkdayjobs.com",            "fortive"),
    ("Danaher",            "https://danaher.wd1.myworkdayjobs.com",            "External"),
    ("Deere & Company",    "https://deere.wd5.myworkdayjobs.com",              "External_Career_Site"),
    ("Cummins",            "https://cummins.wd1.myworkdayjobs.com",            "Cummins_Careers_External"),
    ("Roper Technologies", "https://ropertech.wd1.myworkdayjobs.com",          "External"),

    # Tech (era of Intel, HP, Cisco)
    ("Qualcomm",           "https://qualcomm.wd5.myworkdayjobs.com",           "External"),
    ("Texas Instruments",  "https://ti.wd5.myworkdayjobs.com",                 "External"),
    ("Broadcom",           "https://broadcom.wd1.myworkdayjobs.com",           "External_Career_Site"),
    ("Palo Alto Networks", "https://paloaltonetworks.wd1.myworkdayjobs.com",   "PaloAltoNetworks"),
    ("CrowdStrike",        "https://crowdstrike.wd1.myworkdayjobs.com",        "crowdstrikecareers"),
    ("ServiceNow",         "https://servicenow.wd1.myworkdayjobs.com",         "ServiceNow"),
    ("Workday",            "https://workday.wd5.myworkdayjobs.com",            "Workday"),
    ("Splunk",             "https://splunk.wd1.myworkdayjobs.com",             "External"),
    ("NetApp",             "https://netapp.wd1.myworkdayjobs.com",             "NetApp"),

    # Food/Beverage/CPG
    ("Starbucks",          "https://starbucks.wd5.myworkdayjobs.com",          "starbucksjobs"),
    ("McDonald's",         "https://mcdonalds.wd1.myworkdayjobs.com",          "Corporate"),
    ("Chipotle",           "https://chipotle.wd5.myworkdayjobs.com",           "External"),
    ("Yum Brands",         "https://yum.wd5.myworkdayjobs.com",                "Yum_External"),
    ("Constellation Brands","https://cbrands.wd1.myworkdayjobs.com",           "cbrands"),
    ("Hershey",            "https://hershey.wd5.myworkdayjobs.com",            "External"),
    ("McCormick",          "https://mccormick.wd1.myworkdayjobs.com",          "McCormick"),
    ("Church & Dwight",    "https://churchdwight.wd1.myworkdayjobs.com",       "External"),
    ("Domino's",           "https://dominos.wd5.myworkdayjobs.com",            "External"),
    ("Tyson Foods",        "https://tyson.wd1.myworkdayjobs.com",              "External"),

    # Logistics
    ("Werner Enterprises", "https://werner.wd1.myworkdayjobs.com",             "Werner_Enterprises"),
    ("Schneider National", "https://schneider.wd1.myworkdayjobs.com",          "External"),
    ("Knight-Swift",       "https://knight.wd5.myworkdayjobs.com",             "External"),
    ("Ryder",              "https://ryder.wd1.myworkdayjobs.com",              "ryder"),
    ("Saia",               "https://saia.wd1.myworkdayjobs.com",               "Saia_External"),

    # Hospitality
    ("Hyatt",              "https://hyatt.wd5.myworkdayjobs.com",              "globalcareers"),
    ("Wyndham",            "https://wyndham.wd1.myworkdayjobs.com",            "External"),
    ("MGM Resorts",        "https://mgmresorts.wd1.myworkdayjobs.com",         "External"),
    ("Caesars",            "https://caesars.wd1.myworkdayjobs.com",            "External"),
    ("Royal Caribbean",    "https://royalcaribbean.wd5.myworkdayjobs.com",     "External"),
    ("Carnival",           "https://carnival.wd1.myworkdayjobs.com",           "Carnival_External_Career_Site"),

    # Energy/Utilities
    ("Southern Company",   "https://southernco.wd1.myworkdayjobs.com",         "External"),
    ("AES",                "https://aes.wd1.myworkdayjobs.com",                "External"),
    ("Sempra",             "https://sempra.wd1.myworkdayjobs.com",             "Sempra"),
    ("Entergy",            "https://entergy.wd1.myworkdayjobs.com",            "External"),
    ("FirstEnergy",        "https://firstenergy.wd1.myworkdayjobs.com",        "FirstEnergy"),
    ("Consolidated Edison","https://coned.wd5.myworkdayjobs.com",              "External"),
    ("Phillips 66",        "https://phillips66.wd1.myworkdayjobs.com",         "P66External"),
    ("Marathon Petroleum", "https://mpc.wd1.myworkdayjobs.com",                "External"),

    # Real Estate
    ("Prologis",           "https://prologis.wd1.myworkdayjobs.com",           "Prologis"),
    ("Simon Property Group","https://simon.wd1.myworkdayjobs.com",             "Simon"),
    ("AvalonBay",          "https://avalonbay.wd1.myworkdayjobs.com",          "External"),
    ("Equity Residential", "https://eqr.wd5.myworkdayjobs.com",                "External"),
    ("CoStar Group",       "https://costar.wd5.myworkdayjobs.com",             "External"),
    ("Welltower",          "https://welltower.wd5.myworkdayjobs.com",          "External"),
    ("Realty Income",      "https://realtyincome.wd1.myworkdayjobs.com",       "Realty_Income"),

    # Government/Non-profit/Research
    ("World Bank",         "https://worldbankgroup.wd1.myworkdayjobs.com",     "External_Career_Site"),
    ("IMF",                "https://imf.wd1.myworkdayjobs.com",                "Search"),
    ("Brookings",          "https://brookings.wd1.myworkdayjobs.com",          "External"),
    ("Urban Institute",    "https://urban.wd1.myworkdayjobs.com",              "External"),

    # Staffing
    ("Robert Half",        "https://roberthalf.wd1.myworkdayjobs.com",         "External"),
    ("ManpowerGroup",      "https://manpowergroup.wd1.myworkdayjobs.com",      "External"),
    ("Adecco",             "https://adeccogroup.wd1.myworkdayjobs.com",        "External"),
    ("Insight Global",     "https://insightglobal.wd5.myworkdayjobs.com",      "External"),

    # Auto / industrial
    ("Ford Motor",         "https://ford.wd5.myworkdayjobs.com",               "FordCareers"),
    ("General Motors",     "https://gm.wd5.myworkdayjobs.com",                 "GM_Careers"),
    ("Toyota",             "https://toyota.wd5.myworkdayjobs.com",             "External"),
    ("Honda",              "https://honda.wd1.myworkdayjobs.com",              "External"),
    ("Tesla",              "https://tesla.wd1.myworkdayjobs.com",              "External"),

    # More banks/financial
    ("State Street",       "https://statestreet.wd1.myworkdayjobs.com",        "External"),
    ("Northern Trust",     "https://northerntrust.wd1.myworkdayjobs.com",      "External"),
    ("AON",                "https://aon.wd1.myworkdayjobs.com",                "External"),
    ("Marsh McLennan",     "https://mmc.wd1.myworkdayjobs.com",                "External"),
    ("WTW",                "https://wtw.wd1.myworkdayjobs.com",                "WTW"),

    # Other consulting/services
    ("Cognizant",          "https://cognizant.wd1.myworkdayjobs.com",          "External"),
    ("Wipro",              "https://wipro.wd5.myworkdayjobs.com",              "External"),
    ("Infosys",            "https://infosys.wd1.myworkdayjobs.com",            "Infosys"),
    ("Tata Consultancy",   "https://tcs.wd1.myworkdayjobs.com",                "External"),

    # More tech
    ("Snowflake",          "https://snowflake.wd1.myworkdayjobs.com",          "Snowflake_Careers"),
    ("MongoDB",            "https://mongodb.wd1.myworkdayjobs.com",            "External"),
    ("Datadog",            "https://datadog.wd1.myworkdayjobs.com",            "External"),
    ("Atlassian",          "https://atlassian.wd1.myworkdayjobs.com",          "External"),
]


def slug_from_url(u: str) -> str:
    m = re.match(r"https?://([^.]+)\.wd\d+\.myworkdayjobs\.com", u)
    return m.group(1) if m else ""


async def try_one(client, url, board, body):
    slug = slug_from_url(url)
    if not slug:
        return (0, 0)
    endpoint = f"{url}/wday/cxs/{slug}/{board}/jobs"
    try:
        r = await client.post(
            endpoint, json=body, timeout=15,
            headers={"Accept":"application/json","Content-Type":"application/json","User-Agent":"Mozilla/5.0"},
        )
        if r.status_code != 200:
            return (r.status_code, 0)
        try:
            data = r.json()
            postings = data.get("jobPostings") or []
            total = data.get("total")
            return (200, total if total is not None else len(postings))
        except: return (200, 0)
    except: return (-1, 0)


async def verify_one(client, name, url, board, sem):
    async with sem:
        # v1_minimal first (we know this is the dominant working pattern)
        status, count = await try_one(client, url, board, {"searchText": "manager"})
        if status == 200 and count > 0:
            return (name, url, board, count, "OK")
        # Try a couple URL fallbacks for 404s
        if status == 404:
            slug = slug_from_url(url)
            for dc in ["wd1", "wd3", "wd5", "wd12"]:
                for b in ["External", "External_Career_Site", "Careers", "External_Careers", board]:
                    if (dc, b) == (url.split(".wd")[1].split(".")[0] if ".wd" in url else "", board):
                        continue
                    fb_url = f"https://{slug}.{dc}.myworkdayjobs.com"
                    s2, c2 = await try_one(client, fb_url, b, {"searchText": "manager"})
                    if s2 == 200 and c2 > 0:
                        return (name, fb_url, b, c2, f"OK fallback")
        return (name, None, None, 0, f"fail (status={status})")


async def main():
    print(f"Verifying {len(NEW_CANDIDATES)} new candidates...\n")
    async with httpx.AsyncClient() as c:
        sem = asyncio.Semaphore(10)
        results = await asyncio.gather(*[verify_one(c, n, u, b, sem) for n, u, b in NEW_CANDIDATES])
    working = [r for r in results if r[1] is not None]
    failing = [r for r in results if r[1] is None]
    print(f"=== WORKING ({len(working)}) ===")
    for n, u, b, ct, _ in sorted(working, key=lambda x: -x[3]):
        print(f"  ok  [{ct:>5}]  {n}")
    print(f"\n=== FAILING ({len(failing)}) ===")
    for n, _, _, _, note in failing:
        print(f"  fail   {n}: {note}")
    # Emit Python tuples
    out = ["# New verified Workday tenants (auto-generated)"]
    for n, u, b, ct, _ in sorted(working, key=lambda x: -x[3]):
        out.append(f'    ({n!r:30s}, {u!r:60s}, {b!r}),  # {ct} jobs')
    Path('C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/workday_new_verified.txt').write_text("\n".join(out), encoding="utf-8")
    print(f"\nNew verified list -> scripts/workday_new_verified.txt ({len(working)} new tenants)")


if __name__ == "__main__":
    asyncio.run(main())

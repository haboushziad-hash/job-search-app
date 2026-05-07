"""Direct-POST probe of Workday CXS endpoints across many candidate tenants.

Bypasses Playwright entirely. Just tries the standard POST body shape
({"appliedFacets":{},"limit":20,"offset":0,"searchText":"manager"}) against
URLs constructed from common (subdomain, pod, board) patterns.

Faster than Playwright discovery (~1s per URL) and produces a clean list of
tenants that work with the existing WorkdayScraper out of the box.
"""
import asyncio
import json
import time
import httpx


# Format: (display_name, sub, pod, board)
# pod is wdN where N is 1-12. board is the path segment after the host.
# We'll try each (sub, pod, board) combo.
CANDIDATES = [
    # ===== Confirmed by Playwright discovery this run =====
    ("Merck",          "msd",      "wd5", "SearchJobs"),

    # ===== Companies likely on Workday that previously failed =====
    ("KPMG",           "kpmg",     "wd5", "KPMGUS_Careers"),
    ("UnitedHealth",   "uhg",      "wd5", "External"),
    ("Wells Fargo",    "wellsfargo","wd1", "WellsFargoJobs"),
    ("Liberty Mutual", "lmi",      "wd1", "LMI"),
    ("Honeywell",      "honeywell","wd1", "Honeywell"),
    ("Lockheed Martin","lockheedmartin", "wd1", "External"),
    ("Northrop Grumman","ngc",     "wd1", "NGCareers"),
    ("SAIC",           "saic",     "wd5", "External"),
    ("Kraft Heinz",    "kraftheinz","wd1", "Kraft_Heinz_Careers"),
    ("Mondelez",       "mondelez", "wd1", "External"),
    ("Johnson & Johnson", "jnjc",  "wd5", "jnjcareers"),

    # ===== Try variants for each =====
    ("KPMG (alt board)", "kpmg",   "wd5", "KPMG_Careers"),
    ("KPMG (External)",  "kpmg",   "wd5", "External"),
    ("UnitedHealth alt", "uhg",    "wd5", "Careers"),
    ("UnitedHealth alt2","unitedhealthgroup","wd5", "External"),
    ("J&J alt",          "jnj",    "wd5", "jnjcareers"),
    ("J&J alt 2",        "jnj",    "wd1", "jnjcareers"),
    ("J&J alt 3",        "jnjc",   "wd1", "jnjcareers"),
    ("J&J alt 4",        "johnsonandjohnson", "wd1", "jnj"),

    # ===== CPG that may be on Workday =====
    ("Coca-Cola Workday","coca-cola","wd1", "coca-cola"),
    ("Coca-Cola Workday 2","cocacola","wd1", "Careers"),
    ("PepsiCo Workday",    "pepsico","wd1", "PepsiCoJobs"),
    ("PepsiCo alt",        "pepsico","wd5", "External"),
    ("P&G Workday",        "pg",    "wd1", "external_pgcareers"),
    ("Mondelez alt",       "mondelez","wd5", "External"),
    ("Kraft Heinz alt",    "kraftheinz","wd5", "Kraft_Heinz_Careers"),

    # ===== Healthcare / pharma =====
    ("Pfizer (already)",   "pfizer","wd1", "PfizerCareers"),
    ("Bristol Myers",      "bms",   "wd5", "BMS"),
    ("AbbVie",             "abbvie","wd1", "External"),
    ("AstraZeneca WD",     "astrazeneca","wd1","Careers_AZ"),
    ("Bayer WD",           "bayer", "wd5", "Bayer"),
    ("CVS",                "cvshealth","wd1","External"),
    ("CVS alt",            "cvs",    "wd1", "External"),
    ("Walgreens",          "walgreens","wd5","WBA_Careers_External"),
    ("Anthem",             "anthem","wd5", "ANTHEM"),
    ("Centene",            "centene","wd5","CenteneCareers"),

    # ===== Retail =====
    ("Kroger WD",          "kroger","wd5", "Kroger"),
    ("Costco",             "costco","wd5", "Costco"),
    ("Lowes",              "lowes", "wd5", "Lowes"),
    ("Home Depot",         "homedepot","wd1", "homedepot"),

    # ===== Hospitality / travel =====
    ("Marriott",           "marriott","wd5","marriott"),
    ("Hilton",             "hilton","wd5", "Hilton_Careers"),
    ("MGM Resorts",        "mgm",   "wd5", "External"),

    # ===== Industrials =====
    ("ExxonMobil WD",      "exxonmobil","wd1","External"),
    ("ExxonMobil alt",     "exxon", "wd1", "External"),
    ("Chevron",            "chevron","wd1","External"),
    ("Caterpillar",        "caterpillar","wd1","External_Careers"),
    ("Deere",              "deere", "wd1", "JohnDeere"),
    ("Ford",               "ford",  "wd1", "FordCareers"),
    ("GM",                 "generalmotors","wd5","Careers_External"),

    # ===== Tech (the few that are Workday) =====
    ("Oracle",             "oracle","wd5", "OracleCorporation"),
    ("ServiceNow",         "servicenow","wd1","ServiceNow"),
    ("Workday self",       "workday","wd5","Workday"),

    # ===== Financial services =====
    ("US Bank",            "usbank","wd5", "USBank_Careers"),
    ("PNC alt",            "pnc",   "wd5", "PNC"),
    ("Bank of America",    "bofa",  "wd5", "External"),
    ("BoA alt",            "bankofamerica","wd5","External"),
    ("Charles Schwab",     "schwab","wd5", "External"),
    ("Fidelity",           "fidelity","wd1","FMRCareers"),

    # ===== Insurance =====
    ("Progressive",        "progressive","wd1","External"),
    ("State Farm",         "statefarm","wd1","External"),
    ("MetLife",            "metlife","wd5", "External"),
    ("AIG (alt)",          "aig",   "wd5", "aig"),

    # ===== Telecom =====
    ("AT&T",               "att",   "wd5", "ATT"),
    ("Verizon",            "verizon","wd5","careers"),

    # ===== CPG additional =====
    ("Colgate",            "colgate","wd5", "ColgateCareers"),
    ("Kimberly-Clark",     "kimberlyclark","wd1","External"),
    ("General Mills",      "generalmills","wd1","External"),
    ("Kellogg",            "kellogg","wd1","External_Careers"),
    ("Hershey alt",        "hersheys","wd1","External"),
    ("Mondelez alt 2",     "mdlz",  "wd1", "External"),
    ("Coke alt 3",         "ko",    "wd1", "External"),

    # ===== v0.3.5 expansion targets =====
    # Pharma / biotech (industries the curated list missed)
    ("Amgen",              "amgen", "wd1", "Careers"),
    ("Amgen alt",          "amgen", "wd5", "External"),
    ("Eli Lilly",          "lilly", "wd5", "LillyCareers"),
    ("Eli Lilly alt",      "lilly", "wd1", "External"),
    ("Regeneron",          "regeneron", "wd5", "Regeneron_Careers"),
    ("Regeneron alt",      "regeneron","wd1", "External"),
    ("Novartis",           "novartis","wd3", "Novartis_Careers"),
    ("Sanofi",             "sanofi","wd3", "SanofiCareers"),
    ("Genentech",          "roche", "wd3", "Roche_Careers"),
    ("Vertex",             "vrtx",  "wd1", "vertex"),

    # Healthcare / payers
    ("HCA Healthcare",     "hca",   "wd1", "HCAExternal"),
    ("HCA alt",            "hcahealthcare","wd1","HCAExternal"),
    ("Cigna",              "cigna", "wd5", "cigna_careers"),
    ("Cigna alt",          "cigna", "wd1", "External"),
    ("Kaiser Permanente",  "kp",    "wd5", "kaiser"),
    ("Kaiser alt",         "kaiser","wd1", "External"),
    ("UnitedHealth alt 3", "uhg",   "wd1", "External"),
    ("Humana",             "humana","wd5", "External"),

    # Big banks (the v0.3.4 failure list explicitly called these out)
    ("JPMorgan",           "jpmc",  "wd1", "External"),
    ("JPMorgan alt 1",     "jpmc",  "wd5", "JPMC_External"),
    ("JPMorgan alt 2",     "jpmorganchase","wd1", "External"),
    ("Goldman Sachs",      "gs",    "wd1", "GS_External"),
    ("Goldman alt",        "goldmansachs","wd1","External"),
    ("Bank of America",    "bofa",  "wd1", "External"),
    ("BofA alt 1",         "bankofamerica","wd1","External"),
    ("BofA alt 2",         "bankofamerica","wd5","External"),

    # Big retail (v0.3.4 dropped these from the curated list)
    ("Walmart WD",         "walmart","wd5","WalmartExternal"),
    ("Walmart alt",        "walmart","wd1","External"),
    ("Target WD",          "target","wd5", "External"),
    ("Target alt",         "target","wd1", "External"),

    # CPG additions
    ("Unilever",           "unilever","wd3","External"),
    ("Unilever alt",       "unilever","wd5","External_Careers"),
    ("Coca-Cola alt 4",    "coca-cola","wd5","Coca-Cola_External"),
]


async def probe_one(client: httpx.AsyncClient, name: str, sub: str, pod: str, board: str) -> dict:
    base = f"https://{sub}.{pod}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{sub}/{board}/jobs"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "manager"}
    try:
        r = await client.post(
            url, json=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            },
        )
    except Exception as e:
        return {"name": name, "url": url, "status": "ERR", "n": 0, "reason": f"{type(e).__name__}"}

    if r.status_code != 200:
        return {"name": name, "url": url, "status": str(r.status_code), "n": 0, "reason": ""}

    try:
        data = r.json()
    except Exception:
        return {"name": name, "url": url, "status": "200-NOJSON", "n": 0, "reason": ""}

    postings = data.get("jobPostings") or []
    return {
        "name": name, "url": url, "status": "OK", "n": len(postings),
        "sub": sub, "pod": pod, "board": board, "total": data.get("total"),
    }


async def main():
    print(f"=== Direct Workday probe: {len(CANDIDATES)} URLs ===\n")
    sem = asyncio.Semaphore(8)
    started = time.time()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        async def bounded(c):
            async with sem:
                return await probe_one(client, *c)

        results = await asyncio.gather(*[bounded(c) for c in CANDIDATES])

    ok = [r for r in results if r["status"] == "OK"]
    print(f"\nResults ({round(time.time()-started, 1)}s):")
    print(f"{'name':24s}  {'status':>8s}  {'n':>4s}  url")
    print("-" * 100)
    for r in results:
        print(f"{r['name'][:24]:24s}  {r['status']:>8s}  {r['n']:>4d}  {r['url'][:60]}")

    print(f"\n=== {len(ok)} working tenants ===")
    for r in ok:
        print(f'    ("{r["name"]}", "https://{r["sub"]}.{r["pod"]}.myworkdayjobs.com", "{r["board"]}"),')


if __name__ == "__main__":
    asyncio.run(main())

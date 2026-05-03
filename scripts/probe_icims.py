"""Probe many iCIMS subdomain patterns to find real working tenants.

iCIMS hosting follows several patterns; we test each against a small set of
companies known to use iCIMS for their public job board. A working tenant
returns an HTML page containing the iCIMS job table markup.
"""
import asyncio
import httpx
import re


# Known or suspected iCIMS tenants based on public job board references
CANDIDATES = [
    "wegmans", "usfoods", "tractorsupplycompany", "wellsfargo", "burlington",
    "delhaize", "deere", "hess", "schwab", "snapon",
    "hcsc", "wynn", "pacificdentalservices", "uschamber",
    "boston-scientific", "centurylink", "frontier", "sears",
    "rapid7", "publix", "hardesty", "hallmark", "dadeland",
    "hershey", "bayer", "iag", "warbypiper",
    # Larger known iCIMS clients (recent)
    "academy", "dolce", "jpmorganchase", "raytheon", "tesla",
    # Healthcare / pharma common iCIMS users
    "centene", "carbon", "cardinalhealth", "healthnet",
    # Retail
    "advanceautoparts", "homedepot", "michaels", "petsmart",
    "saksoff5th", "dressbarn", "burlingtoncoatfactory",
    "kohlsdept", "kohlsstore", "famousfootwear",
    # CPG candidates
    "smith-nephew", "campbell", "campbells", "smucker",
]

# Reliable iCIMS marker — the page has these in the HTML
ICIMS_MARKERS = [
    "iCIMS_JobsTable", "iCIMS_Jobs", "icims.com", "iCIMS.com",
    "powered by iCIMS", "iCIMS, Inc",
]


async def probe(c: httpx.AsyncClient, sub: str):
    url = f"https://careers-{sub}.icims.com/jobs/search?ss=1&searchKeyword=manager"
    try:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    except Exception as e:
        return sub, None, type(e).__name__, 0, False
    body = r.text or ""
    icims_marker = any(m in body for m in ICIMS_MARKERS)
    return sub, r.status_code, None, len(body), icims_marker


async def main():
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
        sem = asyncio.Semaphore(6)

        async def bounded(s):
            async with sem:
                return await probe(c, s)

        results = await asyncio.gather(*[bounded(s) for s in CANDIDATES])

    print(f"{'tenant':28s}  status   bytes   icims?  err")
    print("-" * 70)
    working = []
    for sub, status, err, n, marker in results:
        flag = "ICIMS" if marker else ("HTML " if status == 200 else "    ")
        print(f"{sub:28s}  {str(status):>6}  {n:6}  {flag}  {err or ''}")
        if marker and status == 200:
            working.append(sub)

    print(f"\nWorking iCIMS tenants ({len(working)}):")
    for w in working:
        print(f"  {w}")


if __name__ == "__main__":
    asyncio.run(main())

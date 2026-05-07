"""Probe candidate iCIMS tenants for working public RSS feeds.

For each (display_name, sub) candidate, GET the canonical RSS URL
(https://careers-<sub>.icims.com/jobs/search/rss?in_iframe=1) and check:
  - HTTP 200
  - Body parses as RSS XML
  - >= 1 <item> element

Print a verified list ready to paste into ICIMS_RSS_TENANTS in
backend/scraper/icims_rss.py.

Run:
    backend/venv/Scripts/python.exe scripts/probe_icims_rss.py
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
import httpx


# Curated candidates — start with the Playwright-verified set + the v0.3.5
# expansion targets called out in the planning doc. Add more as new
# fixtures expose gaps.
CANDIDATES: list[tuple[str, str]] = [
    # Playwright-verified set (RSS confirmed alongside the SPA path)
    ("Liberty Mutual",  "libertymutual"),
    ("Six Flags",       "sixflags"),
    ("Cedar Fair",      "cedarfair"),
    ("Chick-fil-A",     "chickfila"),
    ("Snap-on",         "snapon"),
    # v0.3.5 expansion — retail / CPG / convenience that the curated
    # Workday/Greenhouse lists under-serve.
    ("PetSmart",        "petsmart"),
    ("Dollar Tree",     "dollartree"),
    ("Pep Boys",        "pepboys"),
    ("BJ's Wholesale",  "bjs"),
    # Additional candidates worth checking:
    ("AutoZone",        "autozone"),
    ("Tractor Supply",  "tractorsupply"),
    ("Discount Tire",   "discounttire"),
    ("AAA",             "aaaclub"),
    ("Wawa",            "wawa"),
    ("Sheetz",          "sheetz"),
    ("Big Lots",        "biglots"),
    ("Five Below",      "fivebelow"),
    ("Burlington",      "burlington"),
    ("Ross Stores",     "rossstores"),
    ("Hard Rock",       "hardrock"),
    ("La-Z-Boy",        "la-z-boy"),
    ("Allstate Insurance","allstate"),  # ATS may differ from Workday board
]


def _url(sub: str) -> str:
    return f"https://careers-{sub}.icims.com/jobs/search/rss?in_iframe=1"


async def probe_one(client: httpx.AsyncClient, name: str, sub: str) -> dict:
    url = _url(sub)
    try:
        r = await client.get(
            url,
            headers={
                "Accept": "application/rss+xml, application/xml, text/xml",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0"
                ),
            },
        )
    except Exception as e:
        return {"name": name, "sub": sub, "status": "ERR", "items": 0,
                "reason": f"{type(e).__name__}"}

    if r.status_code != 200:
        return {"name": name, "sub": sub, "status": str(r.status_code),
                "items": 0, "reason": ""}

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return {"name": name, "sub": sub, "status": "200-NOXML",
                "items": 0, "reason": ""}

    channel = root.find("channel")
    if channel is None:
        return {"name": name, "sub": sub, "status": "200-NOCHAN",
                "items": 0, "reason": ""}

    items = channel.findall("item")
    return {
        "name": name, "sub": sub,
        "status": "OK" if items else "200-EMPTY",
        "items": len(items),
        "reason": "",
    }


async def main():
    print(f"=== Probing {len(CANDIDATES)} iCIMS RSS candidates ===\n")
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        async def bounded(c):
            async with sem:
                return await probe_one(client, *c)
        results = await asyncio.gather(*[bounded(c) for c in CANDIDATES])

    print(f"{'name':22s}  {'status':>10s}  {'items':>5s}  url")
    print("-" * 110)
    for r in results:
        print(
            f"{r['name'][:22]:22s}  {r['status']:>10s}  {r['items']:>5d}  "
            f"{_url(r['sub'])[:60]}"
        )

    ok = [r for r in results if r["status"] == "OK"]
    print(f"\n=== {len(ok)} working tenants ===")
    for r in ok:
        print(f'    ("{r["name"]}",  "{r["sub"]}"),')


if __name__ == "__main__":
    asyncio.run(main())

"""Broad iCIMS subdomain probe — runs the live Playwright scraper against
many candidates and reports which actually return roles.

Adds verified tenants to a generated list at the end. Re-run periodically as
new candidates are discovered.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.scraper.client import ScraperClient
from backend.scraper.icims_playwright import ICIMSPlaywrightScraper
from playwright.async_api import async_playwright


# Candidate iCIMS subdomains — broader cast than ICIMS_PW_TENANTS so we can
# find new working ones. A failing entry costs ~10s; the scraper times out
# at 45s/tenant if it hangs.
CANDIDATES = [
    # === Already verified (positive controls) ===
    ("Snap-on",                "snapon"),
    ("PetSmart",               "petsmart"),
    ("Pacific Dental Services","pacificdentalservices"),
    ("Liberty Mutual",         "libertymutual"),

    # === CPG / Zach gap ===
    ("Hershey",                "hersheys"),
    ("Hershey alt",            "hershey"),
    ("Mars",                   "mars"),
    ("Mars alt",               "marsincorporated"),
    ("ConAgra",                "conagra"),
    ("ConAgra alt",            "conagrabrands"),
    ("Mondelez",               "mondelezinternational"),
    ("Estee Lauder",           "esteelauder"),
    ("Constellation Brands",   "cbrands"),
    ("Diageo",                 "diageo"),
    ("Diageo alt",             "diageoplc"),
    ("Anheuser-Busch",         "anheuserbusch"),
    ("Anheuser-Busch alt",     "abinbev"),
    ("Church & Dwight",        "churchdwight"),
    ("Church & Dwight alt",    "churchdwightcareers"),
    ("Clorox",                 "thecloroxcompany"),
    ("Clorox alt",             "clorox"),
    ("Energizer",              "energizer"),
    ("Brown-Forman",           "brownforman"),

    # === Retail ===
    ("Dollar Tree",            "dollartree"),
    ("Dollar Tree alt",        "dollartreestores"),
    ("Big Lots",               "biglots"),
    ("Burlington",             "burlington"),
    ("Macy's",                 "macysjobs"),
    ("Bed Bath & Beyond",      "bedbathandbeyond"),
    ("Tractor Supply",         "tractorsupplycompany"),
    ("AutoZone",               "autozone"),
    ("Sherwin-Williams",       "sherwin"),
    ("Lowe's",                 "lowes"),

    # === Pharma / healthcare ===
    ("Bayer",                  "bayer"),
    ("AstraZeneca",            "astrazeneca"),
    ("Sanofi",                 "sanofi"),
    ("Novartis",               "novartis"),
    ("GSK",                    "gsk"),
    ("Takeda",                 "takeda"),
    ("Allergan",               "allergan"),
    ("Regeneron",              "regeneron"),
    ("Endo",                   "endopharma"),
    ("AmerisourceBergen",      "amerisourcebergen"),
    ("Cardinal Health",        "cardinalhealth"),
    ("Hospira",                "hospira"),

    # === Hospitality / leisure ===
    ("IHG",                    "ihg"),
    ("Hyatt",                  "hyatt"),
    ("Choice Hotels",          "choicehotels"),
    ("Wynn Resorts",           "wynnresorts"),
    ("Six Flags",              "sixflags"),
    ("Cedar Fair",             "cedarfair"),
    ("MGM",                    "mgmresorts"),

    # === Industrial / manufacturing ===
    ("Cummins",                "cummins"),
    ("Eaton",                  "eaton"),
    ("Honeywell",              "honeywell"),
    ("Honeywell alt",          "honeywellintl"),
    ("Emerson",                "emerson"),
    ("Schlumberger",           "schlumberger"),
    ("Halliburton",            "halliburton"),

    # === Insurance / financial ===
    ("Nationwide",             "nationwide"),
    ("Progressive",            "progressive"),
    ("Allstate",               "allstate"),
    ("Travelers",              "travelers"),
    ("Travelers alt",          "thetravelers"),
    ("Cigna",                  "cigna"),

    # === Media / telecom ===
    ("Charter",                "charter"),
    ("Charter alt",            "spectrum"),
    ("Verizon",                "verizoncareers"),

    # === Other ===
    ("Wegmans",                "wegmans"),
    ("US Foods",               "usfoods"),
    ("Sysco",                  "sysco"),
    ("McDonald's",             "mcdonalds"),
    ("Starbucks",              "starbucks"),
    ("Yum",                    "yum"),
    ("Chick-fil-A",            "chickfila"),
]


async def main():
    print(f"Probing {len(CANDIDATES)} iCIMS candidate tenants with live Playwright...")
    print(f"Each tenant has 45s timeout; expect ~{len(CANDIDATES) * 30 / 60:.0f} min total\n")

    async with ScraperClient(timeout_seconds=20) as client:
        scraper = ICIMSPlaywrightScraper(client=client)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                sem = asyncio.Semaphore(4)

                async def probe(display: str, sub: str):
                    async with sem:
                        try:
                            roles = await asyncio.wait_for(
                                scraper._search_tenant_keyword(
                                    browser, display, sub, "manager", 25
                                ),
                                timeout=45.0,
                            )
                        except Exception as e:
                            return display, sub, [], type(e).__name__
                        return display, sub, roles, None

                tasks = [probe(d, s) for d, s in CANDIDATES]
                results = []
                for i, t in enumerate(asyncio.as_completed(tasks), 1):
                    res = await t
                    display, sub, roles, err = res
                    n = len(roles or [])
                    flag = "OK " if n > 0 else ("ERR" if err else "EMPTY")
                    print(f"  [{i:>2}/{len(CANDIDATES)}] {flag}  {display:24s} ({sub})  -> {n} roles  {err or ''}")
                    results.append(res)
            finally:
                await browser.close()

    working = [(d, s, r) for d, s, r, _ in results if r]
    print(f"\n{'='*70}\nWORKING iCIMS TENANTS: {len(working)} / {len(CANDIDATES)}\n{'='*70}")
    total_roles = sum(len(r) for _, _, r in working)
    print(f"Total roles across working tenants: {total_roles}\n")

    print("Updated ICIMS_PW_TENANTS list:")
    print("ICIMS_PW_TENANTS: list[tuple[str, str]] = [")
    for d, s, r in sorted(working, key=lambda x: -len(x[2])):
        print(f'    ("{d}", "{s}"),  # {len(r)} roles for "manager"')
    print("]")


if __name__ == "__main__":
    asyncio.run(main())

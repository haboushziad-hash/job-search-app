"""Phase C1 v2: broader, more targeted non-tech ATS probe.

Focus areas based on user's expected testers:
- Environmental research / clean tech / sustainability
- Hospitality / travel
- Education / EdTech
- Healthcare / clinical
- Banking / wealth management / asset mgmt
- Government / civic / nonprofit
- More CPG / consumer
- Energy / utilities
"""
from __future__ import annotations

import asyncio
import httpx
import time


GREENHOUSE_CANDIDATES = [
    # Environmental / climate / clean tech
    ("Climeworks",           "climeworks"),
    ("Charm Industrial",     "charmindustrial"),
    ("Watershed",            "watershed"),
    ("Pachama",              "pachama"),
    ("Aclima",               "aclima"),
    ("PivotBio",             "pivotbio"),
    ("Indigo Ag",            "indigoag"),
    ("Agtonomy",             "agtonomy"),
    ("Plenty",               "plenty"),
    ("AppHarvest",           "appharvest"),
    ("Nutrien",              "nutrien"),
    ("Beyond Meat",          "beyondmeat"),
    ("Impossible Foods",     "impossiblefoods"),
    ("Apeel Sciences",       "apeelsciences"),
    ("ZeroAvia",             "zeroavia"),
    ("Rivian",               "rivian"),
    ("Lucid Motors",         "lucidmotors"),
    ("Aurora",               "aurora"),
    ("Wayve",                "wayve"),
    ("Form Energy",          "formenergy"),
    ("ESS Tech",             "esstech"),
    ("Stem",                 "stem"),
    ("Octopus Energy",       "octopusenergy"),
    ("Helion Energy",        "helion"),
    ("Commonwealth Fusion",  "commonwealthfusionsystems"),
    ("Sila",                 "silanano"),
    # Education / EdTech
    ("Chegg",                "chegg"),
    ("Quizlet",              "quizlet"),
    ("Course Hero",          "coursehero"),
    ("Master Class",         "masterclass"),
    ("2U",                   "2u"),
    ("Pearson",              "pearson"),
    # Hospitality / travel
    ("Airbnb",               "airbnb"),
    ("Hopper",               "hopper"),
    ("Tripadvisor",          "tripadvisor"),
    ("Mindbody",             "mindbody"),
    ("Toast",                "toast"),
    ("Resy",                 "resy"),
    # Healthcare / clinical / biotech
    ("23andMe",              "23andme"),
    ("Recursion",            "recursionpharmaceuticals"),
    ("Ginkgo Bioworks",      "ginkgobioworks"),
    ("Tempus",               "tempuslabs"),
    ("Flatiron Health",      "flatironhealth"),
    ("Komodo Health",        "komodohealth"),
    ("Maven Clinic",         "mavenclinic"),
    ("Hinge Health",         "hingehealth"),
    ("Carbon Health",        "carbonhealth"),
    # Banking / wealth / fintech
    ("Marqeta",              "marqeta"),
    ("Bill.com",             "billcom"),
    ("Toast Tab",            "toasttab"),
    ("Square",               "square"),
    # CPG additional
    ("Athletic Greens",      "athleticgreens"),
    ("Ritual",               "ritual"),
    ("Manscaped",            "manscaped"),
    ("Harry's",              "harrys"),
    ("Curology",             "curology"),
    ("Function Health",      "functionhealth"),
    ("Athletic Brewing",     "athleticbrewing"),
    # Logistics / mobility
    ("Convoy",               "convoyinc"),
    ("Flexport",             "flexport"),
    ("project44",            "project44"),
    # Real estate / construction
    ("Procore",              "procore"),
    ("Better.com",           "bettermortgage"),
    # Government / civic
    ("Code for America",     "codeforamerica"),
    ("Govern",               "govlimitedgov"),
    # Other consumer
    ("Patagonia",            "patagonia"),
    ("REI",                  "rei"),
    ("Lululemon",            "lululemon"),
    ("Allbirds",             "allbirds"),
    ("Outdoor Research",     "outdoorresearch"),
]

LEVER_CANDIDATES = [
    ("Pinterest",            "pinterest"),
    ("Cruise",               "cruise"),
    ("Postman",              "postman"),
    ("Discord",              "discord"),
    ("Patreon",              "patreon"),
    ("Reddit",               "reddit"),
    ("Etsy",                 "etsy"),
    ("Coinbase",             "coinbase"),
    ("Roblox",               "roblox"),
    ("Wealthsimple",         "wealthsimple"),
    ("Booking.com",          "bookingholdings"),
    ("Allbirds",             "allbirds"),
    ("Bumble",               "bumble"),
    ("Grammarly",            "grammarly"),
    ("Quora",                "quora"),
    ("MasterClass",          "masterclass"),
]

ASHBY_CANDIDATES = [
    ("Vercel",               "vercel"),
    ("Replicate",            "replicate"),
    ("Posthog",              "posthog"),
    ("Decagon",              "decagon"),
    ("Sierra",               "sierra"),
    ("Adept",                "adept"),
    ("Inflection",           "inflection"),
    ("AI21 Labs",            "ai21"),
    ("Mistral",              "mistral"),
    ("Together AI",          "togetherai"),
    ("Modal Labs",           "modal"),
    ("Atomic Industries",    "atomic"),
    ("Spectaire",            "spectaire"),
]


async def probe_greenhouse(c: httpx.AsyncClient, name: str, token: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return ("OK", len(data.get("jobs", [])))
        return (str(r.status_code), 0)
    except Exception as e:
        return ("ERR-" + type(e).__name__[:8], 0)


async def probe_lever(c: httpx.AsyncClient, name: str, token: str):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return ("OK", len(data) if isinstance(data, list) else 0)
        return (str(r.status_code), 0)
    except Exception as e:
        return ("ERR-" + type(e).__name__[:8], 0)


async def probe_ashby(c: httpx.AsyncClient, name: str, slug: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            n = len(data.get("jobs", [])) if isinstance(data, dict) else 0
            return ("OK", n)
        return (str(r.status_code), 0)
    except Exception as e:
        return ("ERR-" + type(e).__name__[:8], 0)


async def main():
    started = time.time()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
        sem = asyncio.Semaphore(10)

        async def b(fn, n, t):
            async with sem:
                return n, t, await fn(c, n, t)

        gh = await asyncio.gather(*[b(probe_greenhouse, n, t) for n, t in GREENHOUSE_CANDIDATES])
        lv = await asyncio.gather(*[b(probe_lever, n, t) for n, t in LEVER_CANDIDATES])
        ab = await asyncio.gather(*[b(probe_ashby, n, t) for n, t in ASHBY_CANDIDATES])

    print(f"=== Probed in {round(time.time() - started, 1)}s ===")

    def report(label, results):
        ok = [(n, t, c) for n, t, (s, c) in results if s == "OK" and c > 0]
        ok.sort(key=lambda x: -x[2])
        print(f"\n=== {label}: {len(ok)} working ({sum(c for *_, c in ok)} total roles) ===")
        for n, t, c in ok:
            print(f'    ("{n}", "{t}"),   # {c} jobs')

    report("GREENHOUSE", gh)
    report("LEVER", lv)
    report("ASHBY", ab)


if __name__ == "__main__":
    asyncio.run(main())

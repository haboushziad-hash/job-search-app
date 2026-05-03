"""Probe candidate company tokens for non-tech employers across
Greenhouse/Lever/Ashby. Outputs the verified working tokens so we can
mass-add them to the scrapers' company lists.

This is the FAST PATH to expanding coverage — no new scrapers needed,
just add data to existing working code.
"""
from __future__ import annotations

import asyncio
import httpx
import time


# ============================================================================
# CANDIDATE TOKENS — researched from public job listings
# ============================================================================

GREENHOUSE_CANDIDATES = [
    # Retail / consumer goods
    ("Warby Parker",         "warbyparker"),
    ("Allbirds",             "allbirds"),
    ("Glossier",             "glossier"),
    ("Casper",               "casper"),
    ("Peloton",              "peloton"),
    ("Sweetgreen",           "sweetgreen"),
    ("Chewy",                "chewy"),
    ("Wayfair",              "wayfair"),
    ("Shopify Plus",         "shopifyplusmerchantsuccess"),
    ("Faire",                "faire"),
    ("Sonder",               "sondercorporation"),
    ("Outdoor Voices",       "outdoorvoices"),
    ("Blue Apron",           "blueapron"),
    ("HelloFresh",           "hellofresh"),
    ("Daily Harvest",        "dailyharvest"),
    ("Olipop",               "olipop"),
    ("Liquid Death",         "liquiddeath"),
    ("Magic Spoon",          "magicspoon"),
    # Health / wellness / consumer health
    ("Headspace",            "headspacehealth"),
    ("Calm",                 "calm"),
    ("Hims & Hers",          "forhims"),
    ("Ro",                   "ro"),
    ("Capsule",              "capsulecareers"),
    ("Oscar Health",         "oscarhealth"),
    ("Cerebral",             "cerebralinc"),
    ("Talkspace",            "talkspace"),
    ("Color Health",         "colorgenomics"),
    ("Cityblock Health",     "cityblock"),
    ("Forward",              "goforward"),
    # Fintech / banking
    ("SoFi",                 "sofi"),
    ("Robinhood",            "robinhoodmarkets"),
    ("Carta",                "carta"),
    ("NerdWallet",           "nerdwallet"),
    ("Wealthfront",          "wealthfront"),
    ("Betterment",           "betterment"),
    ("Affirm",               "affirm"),
    ("Klarna",               "klarna"),
    ("Plaid",                "plaid"),
    # HR tech
    ("Gusto",                "gusto"),
    ("Justworks",            "justworks"),
    ("Rippling",             "rippling"),
    ("Lattice",              "lattice"),
    ("BambooHR",             "bamboohr"),
    # Real estate
    ("Compass",              "urbancompass"),
    ("Opendoor",             "opendoor"),
    ("Better.com",           "bettermortgage"),
    # Media / journalism
    ("The Athletic",         "theathletic"),
    ("Vox Media",            "voxmedia"),
    ("BuzzFeed",             "buzzfeed"),
    # Insurance
    ("Lemonade",             "lemonade"),
    ("Hippo",                "hippo"),
    ("Root Insurance",       "rootinsurance"),
    # Energy / climate
    ("Tesla Energy",         "teslaenergy"),
    ("Span",                 "span"),
    ("Lyra Health",          "lyrahealth"),
    # Logistics / mobility
    ("Lyft",                 "lyft"),
    ("DoorDash",             "doordash"),
    ("Instacart",            "instacart"),
    ("Uber",                 "uber"),
    # Education
    ("Coursera",             "coursera"),
    ("Duolingo",             "duolingo"),
    ("Outschool",            "outschool"),
    ("Khan Academy",         "khanacademy"),
    # B2B SaaS w/ broad roles
    ("HubSpot",              "hubspot"),
    ("Zendesk",              "zendesk"),
    ("Intercom",             "intercom"),
]

LEVER_CANDIDATES = [
    ("Lyft",                 "lyft"),
    ("DoorDash",             "doordash"),
    ("Instacart",            "instacartcareers"),
    ("Eventbrite",           "eventbrite"),
    ("Box",                  "box"),
    ("Grammarly",            "grammarly"),
    ("Quora",                "quora"),
    ("Patreon",              "patreon"),
    ("Reddit",               "reddit"),
    ("Slack",                "slack"),
    ("Twitch",               "twitch"),
    ("Spotify",              "spotify"),
    ("Pinterest",            "pinterest"),
    ("Bumble",               "bumble"),
    ("Etsy",                 "etsy"),
    ("Coinbase",             "coinbase"),
    ("ConsenSys",            "consensys"),
    ("Discord",              "discord"),
    # Non-pure-tech
    ("Sweetgreen",           "sweetgreen"),
    ("Toast",                "toasttab"),
    ("ZipRecruiter",         "ziprecruiter"),
    ("Roblox",               "roblox"),
]

ASHBY_CANDIDATES = [
    ("Linear",               "linear"),
    ("Vercel",               "vercel"),
    ("Notion",               "notion"),
    ("Posthog",              "posthog"),
    ("Mercury",              "mercury"),
    ("Replicate",            "replicate"),
    ("Browserbase",          "browserbase"),
    ("Decagon",              "decagon"),
    ("Sierra",               "sierra"),
    ("Adept",                "adept"),
]


async def probe_greenhouse(c: httpx.AsyncClient, name: str, token: str):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            jobs = data.get("jobs", [])
            return ("OK", len(jobs), "")
        return (str(r.status_code), 0, "")
    except Exception as e:
        return ("ERR", 0, type(e).__name__)


async def probe_lever(c: httpx.AsyncClient, name: str, token: str):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            n = len(data) if isinstance(data, list) else 0
            return ("OK", n, "")
        return (str(r.status_code), 0, "")
    except Exception as e:
        return ("ERR", 0, type(e).__name__)


async def probe_ashby(c: httpx.AsyncClient, name: str, slug: str):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        r = await c.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            n = len(data.get("jobs", [])) if isinstance(data, dict) else 0
            return ("OK", n, "")
        return (str(r.status_code), 0, "")
    except Exception as e:
        return ("ERR", 0, type(e).__name__)


async def main():
    started = time.time()
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
        sem = asyncio.Semaphore(8)

        async def bounded(fn, name, tok):
            async with sem:
                res = await fn(c, name, tok)
                return name, tok, res

        gh_tasks = [bounded(probe_greenhouse, n, t) for n, t in GREENHOUSE_CANDIDATES]
        lever_tasks = [bounded(probe_lever, n, t) for n, t in LEVER_CANDIDATES]
        ashby_tasks = [bounded(probe_ashby, n, t) for n, t in ASHBY_CANDIDATES]

        gh_results = await asyncio.gather(*gh_tasks)
        lever_results = await asyncio.gather(*lever_tasks)
        ashby_results = await asyncio.gather(*ashby_tasks)

    print(f"=== Probed in {round(time.time() - started, 1)}s ===\n")

    def report(name, results):
        ok = [(n, t, status, count) for n, t, (status, count, _) in results if status == "OK" and count > 0]
        ok.sort(key=lambda x: -x[3])
        print(f"\n=== {name}: {len(ok)} working ({sum(c for *_, c in ok)} total roles) ===")
        for n, t, status, count in ok:
            print(f'    ("{n}", "{t}"),   # {count} jobs')

    report("GREENHOUSE", gh_results)
    report("LEVER", lever_results)
    report("ASHBY", ashby_results)


if __name__ == "__main__":
    asyncio.run(main())

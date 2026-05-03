"""Probe additional Lever candidate tokens to find more working employers."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx


# Candidate tokens, both common name and variants
CANDIDATES = [
    "spotify", "whoop", "mistral", "ro", "outreach", "houzz", "highspot",
    "clari", "skillshare",  # known working
    # Try more
    "github", "atlassian", "asana", "monday", "miro", "1password",
    "zoom", "lyft", "uber", "tinder", "pinterest", "twilio", "okta",
    "duckduckgo", "elastic", "splunk", "newrelic", "pagerduty",
    "shopify", "klaviyo", "segment", "amplitude", "mixpanel",
    "zendesk", "intercom", "salesforce", "hubspot",
    "carbonblack", "tenable", "darktrace", "rapid7", "kraken", "binance",
    "cruise", "rivian", "polestar",
    "deepmind", "anthropic", "cohere",
    "instacartcareers", "instacart", "doordash",
    "openai-official", "stripe-careers",
    # Random non-tech / non-AI
    "pelotontech", "warbyparker", "casper", "allbirds",
    "mainstreet", "boxed", "thirdlove",
    "ramp", "brex", "kraken-hq", "forge", "carta",
    "trellisaccounting", "guideline", "pry", "merge",
    "elevenlabs", "luma", "clay-app", "crusoe",
    "wonder", "candle", "pomelo", "pulley",
]


async def main():
    sem = asyncio.Semaphore(10)
    async with httpx.AsyncClient(timeout=10) as c:
        async def probe(t):
            async with sem:
                try:
                    r = await c.get(f"https://api.lever.co/v0/postings/{t}?mode=json")
                    if r.status_code == 200 and isinstance(r.json(), list):
                        return t, len(r.json())
                except Exception:
                    pass
                return t, 0

        results = await asyncio.gather(*[probe(c) for c in CANDIDATES])

    working = sorted([r for r in results if r[1] > 0], key=lambda x: -x[1])
    print(f"\n=== {len(working)} working Lever tokens out of {len(CANDIDATES)} probed ===")
    for t, n in working:
        print(f'    ("{t}", "{t}"),  # {n} jobs')


if __name__ == "__main__":
    asyncio.run(main())

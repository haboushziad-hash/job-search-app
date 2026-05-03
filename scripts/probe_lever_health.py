"""Probe every existing Lever company token to see which ones still work."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from backend.scraper.lever import LEVER_COMPANIES


async def main():
    print(f"Probing {len(LEVER_COMPANIES)} existing Lever companies...")
    sem = asyncio.Semaphore(8)
    results = []

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
        async def probe(name, token):
            async with sem:
                try:
                    r = await c.get(f"https://api.lever.co/v0/postings/{token}?mode=json")
                    n = len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0
                    return name, token, r.status_code, n
                except Exception as e:
                    return name, token, "ERR", 0

        results = await asyncio.gather(*[probe(n, t) for n, t in LEVER_COMPANIES])

    working = [(n, t, c) for n, t, s, c in results if s == 200 and c > 0]
    dead = [(n, t, s) for n, t, s, c in results if s != 200 or c == 0]
    working.sort(key=lambda x: -x[2])

    print(f"\n=== LIVE: {len(working)} / {len(LEVER_COMPANIES)} ===")
    for n, t, c in working:
        print(f'    ("{n}", "{t}"),   # {c} jobs')

    print(f"\n=== DEAD/MIGRATED: {len(dead)} ===")
    print(", ".join(f"{n}({s})" for n, t, s in dead[:30]))


if __name__ == "__main__":
    asyncio.run(main())

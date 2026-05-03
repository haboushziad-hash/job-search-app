"""POST-method probes — 405s above suggest the endpoint exists but wants POST."""
import sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from backend.scraper.client import USER_AGENTS
import random

HEADERS = {
    "User-Agent": random.choice(USER_AGENTS),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://builtin.com",
    "Referer": "https://builtin.com/jobs",
}


async def test():
    async with httpx.AsyncClient(timeout=30) as c:
        # Various plausible POST shapes
        body_variants = [
            {"search": "manager", "page": 1, "perPage": 10},
            {"query": "manager", "page": 1, "limit": 10},
            {"keyword": "manager", "page": 1, "perPage": 10},
            {"q": "manager", "page": 1},
            {"search": {"keyword": "manager"}, "page": 1},
            {"filters": {"search": "manager"}, "pagination": {"page": 1, "perPage": 10}},
            {"text": "manager"},
        ]
        for body in body_variants:
            for url in ['https://api.builtin.com/v1/jobs', 'https://api.builtin.com/jobs/search', 'https://api.builtin.com/search']:
                try:
                    r = await c.post(url, json=body, headers=HEADERS)
                    print(f'  [{r.status_code}] POST {url[-40:]:40s}  body={list(body.keys())}  len={len(r.text)}')
                    if r.status_code == 200:
                        try:
                            data = json.loads(r.text)
                            if isinstance(data, dict):
                                print(f'    KEYS: {list(data.keys())[:10]}')
                                if 'jobs' in data or 'data' in data or 'results' in data or 'items' in data:
                                    print(f'    HIT! Sample: {repr(data)[:400]}')
                                    return
                        except: pass
                    elif r.status_code == 400:
                        print(f'    body resp: {r.text[:200]!r}')
                except Exception as e:
                    pass


asyncio.run(test())

"""Probe what BuiltIn's API does today — the original /api/v1/jobs returns 404."""
import sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.scraper.client import ScraperClient


async def test():
    async with ScraperClient() as c:
        urls = [
            ('OLD /api/v1/jobs', 'https://builtin.com/api/v1/jobs?search=manager&page=1&perPage=5'),
            ('/jobs HTML',       'https://builtin.com/jobs?search=manager'),
            ('/jobs/data',       'https://builtin.com/jobs/data?search=manager'),
            ('builtinla',        'https://www.builtinla.com/api/v1/jobs?search=manager'),
            ('/api/v2/jobs',     'https://builtin.com/api/v2/jobs?search=manager'),
            ('/search',          'https://builtin.com/search?q=manager'),
            ('/jobs simple',     'https://builtin.com/jobs'),
            ('/api jobs.json',   'https://builtin.com/api/jobs.json?search=manager'),
        ]
        for name, u in urls:
            try:
                r = await c.get(u)
                ctype = (r.headers.get('content-type') or '').split(';')[0]
                print(f'  [{r.status_code}]  {name:20s}  ctype={ctype:20s}  len={len(r.text)}')
                if 'json' in ctype:
                    try:
                        data = json.loads(r.text)
                        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                        print(f'         JSON keys: {keys}')
                        if isinstance(data, dict):
                            for k, v in data.items():
                                if isinstance(v, list):
                                    print(f'         {k}: list of {len(v)}')
                                    if v: print(f'           sample[0]: {repr(v[0])[:200]}')
                                else:
                                    print(f'         {k}: {repr(v)[:80]}')
                    except Exception as e:
                        print(f'         JSON parse fail: {e}')
                elif r.status_code == 200 and 'html' in ctype:
                    # Look for embedded job data in __NEXT_DATA__ or apollo state
                    text = r.text
                    if '__NEXT_DATA__' in text:
                        print('         HTML contains __NEXT_DATA__ (Next.js SSR)')
                        # Try to extract
                        m = text.find('__NEXT_DATA__')
                        print(f'         snippet: {text[m:m+200]!r}')
                    elif '__APOLLO_STATE__' in text:
                        print('         HTML contains __APOLLO_STATE__')
                    elif '"jobs"' in text:
                        print('         HTML contains "jobs" — embedded data')
                        m = text.find('"jobs"')
                        print(f'         snippet: {text[m:m+200]!r}')
            except Exception as e:
                print(f'  ERR  {name:20s}  {type(e).__name__}: {str(e)[:100]}')


asyncio.run(test())

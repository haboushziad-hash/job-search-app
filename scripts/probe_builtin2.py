"""Probe api.builtin.com — the new API subdomain."""
import sys, asyncio, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.scraper.client import ScraperClient


async def test():
    async with ScraperClient() as c:
        urls = [
            ('api.builtin.com/',           'https://api.builtin.com/'),
            ('api jobs',                   'https://api.builtin.com/jobs?search=manager'),
            ('api v1 jobs',                'https://api.builtin.com/v1/jobs?search=manager'),
            ('api v1 jobs/search',         'https://api.builtin.com/v1/jobs/search?search=manager'),
            ('api search',                 'https://api.builtin.com/search?q=manager'),
            ('api/jobs/search',            'https://api.builtin.com/jobs/search?search=manager'),
            ('api graphql',                'https://api.builtin.com/graphql'),
            ('api v1',                     'https://api.builtin.com/v1'),
            ('api jobs?keyword=',          'https://api.builtin.com/jobs?keyword=manager'),
            ('api v1 search',              'https://api.builtin.com/v1/search?q=manager'),
        ]
        for name, u in urls:
            try:
                r = await c.get(u)
                ctype = (r.headers.get('content-type') or '').split(';')[0]
                print(f'  [{r.status_code}]  {name:30s}  ctype={ctype:20s}  len={len(r.text)}')
                if 'json' in ctype:
                    try:
                        data = json.loads(r.text)
                        if isinstance(data, dict):
                            keys = list(data.keys())[:8]
                            print(f'         keys: {keys}')
                            for k in keys[:5]:
                                v = data[k]
                                if isinstance(v, list):
                                    print(f'         {k}: list of {len(v)}')
                                    if v and isinstance(v[0], dict):
                                        sub_keys = list(v[0].keys())[:10]
                                        print(f'           [0] keys: {sub_keys}')
                                else:
                                    print(f'         {k}: {repr(v)[:100]}')
                    except Exception as e:
                        print(f'         JSON parse fail: {e}')
                else:
                    snip = r.text[:200].replace('\n', ' ')
                    print(f'         snippet: {snip!r}')
            except Exception as e:
                print(f'  ERR  {name:30s}  {type(e).__name__}: {str(e)[:100]}')


asyncio.run(test())

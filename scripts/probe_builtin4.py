"""Look at the actual HTML to find what API the page uses."""
import sys, asyncio, re, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.scraper.client import ScraperClient


async def test():
    async with ScraperClient() as c:
        r = await c.get('https://builtin.com/jobs?search=ai+strategy+consultant')
        print(f'status={r.status_code} len={len(r.text)}')

        # Try to extract __NEXT_DATA__ JSON blob
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                print('__NEXT_DATA__ TOP-LEVEL KEYS:', list(data.keys()))
                if 'props' in data:
                    pp = data['props'].get('pageProps') if isinstance(data['props'], dict) else None
                    if pp:
                        print('pageProps KEYS:', list(pp.keys())[:15])
                        # Look for jobs array
                        for k in ('jobs', 'initialJobs', 'searchResults', 'data', 'items'):
                            if k in pp:
                                v = pp[k]
                                if isinstance(v, list):
                                    print(f'  pageProps.{k}: list of {len(v)}')
                                    if v: print(f'    [0] keys: {list(v[0].keys())[:15] if isinstance(v[0], dict) else type(v[0]).__name__}')
                                elif isinstance(v, dict):
                                    print(f'  pageProps.{k}: dict keys {list(v.keys())[:10]}')
                            # Save the full JSON for offline study
                Path('C:/Users/habou/OneDrive/Desktop/Job Search App/scripts/builtin_next_data.json').write_text(
                    json.dumps(data, indent=2)[:1_000_000], encoding='utf-8')
                print('\nSaved to scripts/builtin_next_data.json (truncated to 1MB)')
            except Exception as e:
                print(f'Parse fail: {e}')
        else:
            print('No __NEXT_DATA__ found')

        # Look for explicit API URLs hardcoded in the HTML
        # E.g. /api/something/jobs
        api_paths = set(re.findall(r'(?:/api|/v1|api\.builtin\.com)/[a-zA-Z0-9_/\-]+', r.text))
        print(f'\nAPI-like paths found in HTML ({len(api_paths)}):')
        for p in sorted(api_paths)[:20]:
            print(f'  {p}')


asyncio.run(test())

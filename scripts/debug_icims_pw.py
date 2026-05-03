"""Debug iCIMS Playwright — render snapon page and dump structure."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
        )
        page = await ctx.new_page()
        for sub in ("snapon", "petsmart", "pacificdentalservices"):
            url = f"https://careers-{sub}.icims.com/jobs/search?ss=1&searchKeyword=manager"
            print(f"\n=== {sub}: {url} ===")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(5000)  # extra time
                # Dump <title> and count of links matching iCIMS pattern
                title = await page.title()
                jobs_count = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('a'))
                            .filter(a => /\\/jobs\\/\\d+\\//.test(a.getAttribute('href') || ''))
                            .length
                """)
                # Count iframes
                iframe_count = await page.evaluate("() => document.querySelectorAll('iframe').length")
                # Count of total links
                link_count = await page.evaluate("() => document.querySelectorAll('a').length")
                # Look for any text that says "results" or "found"
                results_text = await page.evaluate("""
                    () => {
                        const body = document.body ? document.body.innerText : '';
                        const m = body.match(/\\d+\\s+(results|jobs|positions|matches)/i);
                        return m ? m[0] : null;
                    }
                """)
                print(f"  page title: {title!r}")
                print(f"  link count: {link_count}, iframe count: {iframe_count}")
                print(f"  /jobs/N/* link count: {jobs_count}")
                print(f"  results text: {results_text!r}")
                # Also try Pattern 4: data-jobid attr
                data_jobid = await page.evaluate("() => document.querySelectorAll('[data-jobid]').length")
                print(f"  data-jobid count: {data_jobid}")
                # Sample some links to see what's there
                samples = await page.evaluate("""
                    () => Array.from(document.querySelectorAll('a'))
                              .map(a => ({href: a.getAttribute('href'), text: (a.innerText || '').trim().slice(0, 40)}))
                              .filter(x => x.href && x.href.length > 0)
                              .slice(0, 8)
                """)
                print("  link samples:")
                for s in samples:
                    print(f"    {s['text'][:40]:40s}  {s['href'][:80]}")

                # If there are iframes, check them
                if iframe_count > 0:
                    iframes = await page.query_selector_all("iframe")
                    for i, ih in enumerate(iframes[:3]):
                        try:
                            src = await ih.get_attribute("src")
                            print(f"  iframe[{i}] src: {src}")
                            f = await ih.content_frame()
                            if f:
                                fjobs = await f.evaluate("""
                                    () => Array.from(document.querySelectorAll('a'))
                                              .filter(a => /\\/jobs\\/\\d+\\//.test(a.getAttribute('href') || ''))
                                              .length
                                """)
                                print(f"    iframe content links: {fjobs}")
                        except Exception as e:
                            print(f"    iframe error: {e}")
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())

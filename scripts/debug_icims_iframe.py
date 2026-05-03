"""Probe iCIMS iframe content directly."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    # Hit the iframe URL directly
    url = "https://careers-snapon.icims.com/jobs/search?ss=1&searchKeyword=manager&in_iframe=1"
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(4000)

            # Dump structure
            stats = await page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const tables = document.querySelectorAll('table');
                    const rows = document.querySelectorAll('tr');
                    return {
                        link_count: links.length,
                        table_count: tables.length,
                        row_count: rows.length,
                        sample_links: links.slice(0, 12).map(a => ({
                            href: a.getAttribute('href') || '',
                            text: (a.innerText || a.textContent || '').trim().slice(0, 80),
                            classes: a.className,
                        })),
                    };
                }
            """)
            print(f"Stats: {stats['link_count']} links, {stats['table_count']} tables, {stats['row_count']} rows")
            print("Sample links:")
            for s in stats["sample_links"]:
                print(f"  {s['text'][:50]:50s}  {s['href'][:60]}")
                print(f"    classes: {s['classes']}")

            # Try multiple selectors for iCIMS job rows
            for sel in (
                'a[href*="/jobs/"]',
                'a[href^="/jobs/"]',
                '.iCIMS_JobsTable a',
                'div[class*="Job"] a',
                'span[class*="title"]',
                'tr[class*="job"] a',
            ):
                try:
                    els = await page.query_selector_all(sel)
                    print(f"\nSelector {sel!r}: {len(els)} matches")
                    for el in els[:3]:
                        try:
                            txt = (await el.inner_text())[:60]
                            href = await el.get_attribute("href")
                            print(f"  {txt:50s}  {href[:60] if href else 'no-href'}")
                        except Exception:
                            pass
                except Exception as e:
                    print(f"  ERR {type(e).__name__}: {e}")

            # Save full HTML for forensic inspection
            html = await page.content()
            import os
            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icims_snapon_iframe.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"\nSaved {len(html)} bytes to {out_path}")
            # Look for nested iframes / job content patterns
            patterns_found = await page.evaluate("""
                () => {
                    const results = {};
                    results.nested_iframes = document.querySelectorAll('iframe').length;
                    results.frame_srcs = Array.from(document.querySelectorAll('iframe')).map(f => f.src);
                    return results;
                }
            """)
            print(f"\nNested iframe info: {patterns_found}")
        finally:
            await b.close()


if __name__ == "__main__":
    asyncio.run(main())

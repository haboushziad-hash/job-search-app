"""Re-debug: list ALL frame.evaluate links, no regex — verify JS sees the job links."""
import asyncio
from playwright.async_api import async_playwright


async def main():
    url = "https://careers-snapon.icims.com/jobs/search?ss=1&searchKeyword=manager"
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(8000)

            iframe_handle = await page.query_selector("#icims_content_iframe")
            if not iframe_handle:
                print("No #icims_content_iframe found on outer page")
                return
            frame = await iframe_handle.content_frame()
            if not frame:
                print("frame is None")
                return

            # Just list ALL link hrefs
            all_hrefs = await frame.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                          .map(a => a.getAttribute('href') || '')
                          .filter(h => h.length > 0)
            """)
            print(f"All {len(all_hrefs)} link hrefs:")
            for h in all_hrefs[:30]:
                print(f"  {h[:120]}")

            # Look for /jobs/ in any href
            job_hrefs = [h for h in all_hrefs if "/jobs/" in h and "/job" in h.split("/jobs/")[-1]]
            print(f"\n/jobs/ + /job/ hrefs: {len(job_hrefs)}")
            for h in job_hrefs[:5]:
                print(f"  {h[:150]}")

            # Try the SIMPLEST regex from JS
            job_data = await frame.evaluate(r"""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    return links.map(a => ({
                        href: a.getAttribute('href') || '',
                        text: (a.innerText || a.textContent || '').trim().slice(0, 80),
                    })).filter(l => l.href.includes('/jobs/') && l.href.includes('/job'));
                }
            """)
            print(f"\nFiltered by includes(): {len(job_data)} job links")
            for j in job_data[:5]:
                print(f"  text={j['text'][:60]!r}")
                print(f"  href={j['href'][:120]}")
        finally:
            await b.close()


if __name__ == "__main__":
    asyncio.run(main())

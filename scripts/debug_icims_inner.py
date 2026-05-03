"""Inspect the FULLY rendered inner iCIMS iframe (after JS executes)."""
import asyncio
import os
from playwright.async_api import async_playwright


async def main():
    # Hit the OUTER page, wait, then drill into the icims_content_iframe
    url = "https://careers-snapon.icims.com/jobs/search?ss=1&searchKeyword=manager"
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(7000)  # ample time for nested SPA mount

            # Find the icims_content_iframe specifically
            iframe_handle = await page.query_selector("#icims_content_iframe")
            if not iframe_handle:
                print("No icims_content_iframe found")
                return
            frame = await iframe_handle.content_frame()
            if not frame:
                print("Couldn't access iframe content")
                return

            # Inside the iframe, find all links and their patterns
            data = await frame.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const out = [];
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        const text = (a.innerText || a.textContent || '').trim();
                        if (text.length < 4 || text.length > 200) continue;
                        if (!href) continue;
                        out.push({
                            href: href.slice(0, 200),
                            text: text.slice(0, 100),
                            classes: a.className,
                        });
                    }
                    // Also look at all elements with text containing job-like words
                    const all_text = document.body ? document.body.innerText : '';
                    return {
                        link_count: links.length,
                        all_links: out.slice(0, 30),
                        body_excerpt: all_text.slice(0, 500),
                    };
                }
            """)
            print(f"Inner iframe link count: {data['link_count']}")
            print(f"\nFirst 30 links:")
            for l in data["all_links"]:
                print(f"  {l['text'][:70]:70s}")
                print(f"     -> {l['href'][:100]}")
                print(f"     classes: {l['classes']}")

            print(f"\nBody excerpt:")
            print(data["body_excerpt"])

            # Save inner iframe HTML
            inner_html = await frame.content()
            out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icims_snapon_inner.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(inner_html)
            print(f"\nSaved {len(inner_html)} bytes to {out_path}")
        finally:
            await b.close()


if __name__ == "__main__":
    asyncio.run(main())

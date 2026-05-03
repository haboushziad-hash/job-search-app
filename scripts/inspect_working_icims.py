"""Look at the working iCIMS tenant HTML to see if jobs are parseable."""
import asyncio
import httpx
import re


async def main():
    for sub in ("snapon", "pacificdentalservices", "petsmart"):
        url = f"https://careers-{sub}.icims.com/jobs/search?ss=1&searchKeyword=manager"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        html = r.text
        print(f"\n=== {sub} ({r.status_code}, {len(html)} bytes) ===")

        # Try multiple iCIMS-specific patterns
        patterns = {
            "/jobs/N/title link":  r'href="(/jobs/\d+/[^"#?]+)"',
            "data-jobid":          r'data-jobid="(\d+)"',
            "iCIMS_JobsTable row": r'class="[^"]*iCIMS_JobsTable[^"]*"',
            "a class=jobtitle":    r'<a[^>]*class="[^"]*(?:title|jobtitle)[^"]*"[^>]*>\s*([^<]+)\s*</a>',
            "h3 inside a":         r'<a[^>]*href="([^"]+)"[^>]*>\s*<h3[^>]*>([^<]+)</h3>',
            "div.title":           r'<div[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</div>',
        }
        for name, pat in patterns.items():
            matches = re.findall(pat, html, re.IGNORECASE)
            print(f"  {name:25s}: {len(matches)} matches")
            if matches:
                print(f"    {matches[:3]}")

        # Show short snippet of body where jobs probably are
        bidx = html.find("Job Title")
        if bidx == -1:
            bidx = html.find("job-search-results")
        if bidx == -1:
            bidx = html.find("results-found")
        print(f"\n  searched markers, best position: {bidx}")
        if bidx > -1:
            print(f"  snippet: {html[bidx:bidx+500]!r}")


if __name__ == "__main__":
    asyncio.run(main())

"""Look at the Liberty Mutual iCIMS HTML to find a parseable structure."""
import asyncio
import httpx
import re


async def main():
    url = "https://careers-libertymutual.icims.com/jobs/search?ss=1&searchKeyword=manager&hashed=-1"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
    html = r.text
    print(f"Status: {r.status_code}, body len: {len(html)}")

    # Look for common patterns in iCIMS HTML
    print("\n=== Looking for job link patterns ===")
    # Pattern 1: /jobs/####/job-title
    m1 = re.findall(r'href="(/jobs/\d+/[^"]+)"', html)
    print(f"Pattern 1 (/jobs/N/title): {len(m1)} matches")
    if m1:
        print("  Samples:", m1[:5])

    # Pattern 2: data-iframe-src with job IDs
    m2 = re.findall(r'data-job-id="(\d+)"', html)
    print(f"Pattern 2 (data-job-id): {len(m2)} matches")
    if m2:
        print("  Samples:", m2[:5])

    # Pattern 3: iCIMS_JobsTable rows
    m3 = re.findall(r'class="iCIMS_JobsTable[^"]*"', html)
    print(f"Pattern 3 (iCIMS_JobsTable): {len(m3)} matches")

    # Pattern 4: JSON data embedded
    m4 = re.findall(r'JOBS\s*=\s*(\[[\s\S]*?\]);', html)
    print(f"Pattern 4 (JOBS = ...): {len(m4)} matches")
    if m4:
        print("  First 200 chars:", m4[0][:200])

    # Pattern 5: data-jobtitle
    m5 = re.findall(r'data-jobtitle="([^"]+)"', html)
    print(f"Pattern 5 (data-jobtitle): {len(m5)} matches")
    if m5:
        print("  Samples:", m5[:5])

    # Pattern 6: Look for any "title" mentions inside markup
    m6 = re.findall(r'<a[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</a>', html, re.IGNORECASE)
    print(f"Pattern 6 (anchor with title class): {len(m6)} matches")
    if m6:
        print("  Samples:", m6[:5])

    # Save first 5K chars for manual inspection
    print("\n=== First 3000 chars of HTML body ===")
    # Find the body section
    bidx = html.lower().find("<body")
    print(html[bidx:bidx+3000])


if __name__ == "__main__":
    asyncio.run(main())

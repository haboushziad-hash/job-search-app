"""Debug iCIMS endpoint directly to see what URLs work."""
import asyncio
import httpx


CANDIDATES = [
    # Various subdomain patterns to try for each tenant
    "https://careers-hersheys.icims.com/jobs/search.json?searchKeyword=manager",
    "https://careers-hersheys.icims.com/jobs/search?searchKeyword=manager&format=rss",
    "https://careers-hersheys.icims.com/jobs/search?searchKeyword=manager",
    "https://hersheys.icims.com/jobs/search?searchKeyword=manager",
    "https://careers.thehersheycompany.com/jobs/search?searchKeyword=manager",
    "https://careers-mars.icims.com/jobs/search?searchKeyword=manager",
    "https://jobs.mars.com/jobs/search?searchKeyword=manager",
    "https://careers.mars.com/jobs/search?searchKeyword=manager",
    "https://careers-diageo.icims.com/jobs/search?searchKeyword=manager",
    "https://diageo-careers.icims.com/jobs/search?searchKeyword=manager",
    "https://careers-bayer.icims.com/jobs/search?searchKeyword=manager",
    "https://career.bayer.us/en/job-search-results",
    "https://careers-hyatt.icims.com/jobs/search?searchKeyword=manager",
    "https://hyatt.icims.com/jobs/search?searchKeyword=manager",
    # Try generic fallbacks
    "https://careers-ups.icims.com/jobs/search?searchKeyword=manager",
    "https://careers-honeywell.icims.com/jobs/search?searchKeyword=manager",
    "https://careers-libertymutual.icims.com/jobs/search?searchKeyword=manager",
    "https://careers-dollartree.icims.com/jobs/search?searchKeyword=manager",
]


async def main():
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        for url in CANDIDATES:
            try:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                body_len = len(r.text or "")
                snippet = (r.text[:100] if r.text else "").replace("\n", " ")
                print(f"  {r.status_code}  {body_len:>6}  {url[:70]}")
                if r.status_code == 200 and body_len > 200:
                    print(f"        {snippet[:80]}")
            except Exception as e:
                print(f"  ERR  {type(e).__name__}  {url[:70]}")


if __name__ == "__main__":
    asyncio.run(main())

"""iCIMS scraper.

iCIMS powers ~15-20% of mid-market and Fortune 500 careers pages, especially
in CPG (Hershey, Mars, ConAgra, Diageo, Estée Lauder), retail (Dollar Tree,
Big Lots, Kohl's), pharma (Bayer, AstraZeneca, Sanofi), and hospitality
(IHG, Hyatt, Choice Hotels). Coverage in industries that Workday/Greenhouse
under-serve — exactly the gap surfaced by the Zach run audit.

Strategy:
  iCIMS exposes per-tenant public job boards at predictable subdomains:
    https://careers-{tenant}.icims.com/jobs/search
  Some tenants also use vanity domains (e.g. jobs.{company}.com) backed by
  iCIMS — those are not auto-discoverable.

  iCIMS provides multiple feed formats; we use the most stable two:
    1. JSON: ?searchKeyword={kw}&format=json    (newer tenants)
    2. RSS: ?searchKeyword={kw}&format=rss      (universally supported,
       returns last-N postings as XML)

  We try JSON first, fall back to RSS, and skip the tenant on both failing.
  No HTML parsing — too fragile across themes.

Per-tenant timeout: 30s. Concurrency: 12 in-flight (matches Workday).
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
from xml.etree import ElementTree as ET

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


# ============================================================================
# CURATED ICIMS TENANTS — DEFERRED to Phase 1.5
# ============================================================================
# Live verification (scripts/probe_icims.py, 2026-05-03) confirmed:
#   - careers-{tenant}.icims.com pattern is NOT predictable; subdomains are
#     tenant-specific and require per-tenant discovery
#   - Tenants that DO respond serve SPA-rendered HTML (jobs loaded via JS
#     after page load), requiring Playwright to scrape
#   - Both .json and ?format=rss endpoints return 404 for most tenants
#
# Verified WORKING subdomains (3/49 tested): snapon, pacificdentalservices,
# petsmart — but their HTML doesn't contain job markup at request time.
#
# Phase 1.5 deliverable: Playwright-based iCIMS discovery + scraping. Each
# verified tenant needs its own page-render config. Effort: ~6-8 hr for the
# Playwright harness, 5-10 min per tenant added.
#
# For now, the tenant list is empty so the scraper is a no-op — preserving
# the integration point without producing junk data for testers.
ICIMS_TENANTS: list[tuple[str, str]] = []


def _parse_icims_date(raw: str) -> Optional[str]:
    """iCIMS uses RFC822 in RSS or ISO-like in JSON. Best-effort parse to ISO."""
    if not raw:
        return None
    raw = str(raw).strip()
    # RFC822: "Tue, 21 Apr 2026 14:23:00 GMT"
    try:
        dt = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %Z")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S GMT")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except (ValueError, TypeError):
        pass
    # ISO-ish
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        pass
    return None


class ICIMSScraper(BaseScraper):
    source_name = "iCIMS"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        """Search every iCIMS tenant for every keyword in parallel.

        Each (tenant, keyword) request runs with a 30s timeout under a
        Semaphore(12) cap so a single slow tenant can't stall the run.
        Failing tenants/keywords return [] silently (logged but not raised).
        """
        sem = asyncio.Semaphore(12)

        async def bounded(display: str, sub: str, kw: str) -> list[Role]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        self._search_tenant_keyword(display, sub, kw, limit_per_keyword),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    return []
                except Exception:
                    return []

        tasks = [
            bounded(display, sub, kw)
            for display, sub in ICIMS_TENANTS
            for kw in keywords
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten + dedupe by URL then by (company, title)
        seen_urls: set[str] = set()
        seen_pairs: set[tuple[str, str]] = set()
        out: list[Role] = []
        for r in results:
            if isinstance(r, BaseException):
                continue
            for role in r or []:
                if role.job_url and role.job_url in seen_urls:
                    continue
                pair = ((role.company or "").lower(), (role.job_title or "").lower())
                if pair in seen_pairs:
                    continue
                if role.job_url:
                    seen_urls.add(role.job_url)
                seen_pairs.add(pair)
                out.append(role)
        return out

    async def _search_tenant_keyword(
        self,
        display: str,
        sub: str,
        keyword: str,
        limit: int,
    ) -> list[Role]:
        """Try JSON first, fall back to RSS. Returns [] on any failure."""
        # JSON path (newer tenants)
        json_roles = await self._try_json(display, sub, keyword, limit)
        if json_roles:
            return json_roles
        # RSS fallback (universally supported)
        return await self._try_rss(display, sub, keyword, limit)

    async def _try_json(
        self, display: str, sub: str, keyword: str, limit: int,
    ) -> list[Role]:
        url = f"https://careers-{sub}.icims.com/jobs/search.json"
        params = {
            "ss": "1",
            "searchKeyword": keyword,
            "in_iframe": "1",
        }
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                url, params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []

        # iCIMS JSON shape varies by tenant. Common fields: data.results or
        # results or jobs. Each item has title, jobUrl/url, location, postingDate.
        items = []
        if isinstance(data, dict):
            for k in ("data", "results", "jobs", "jobPostings"):
                v = data.get(k)
                if isinstance(v, list):
                    items = v
                    break
                if isinstance(v, dict):
                    inner = v.get("results") or v.get("jobs")
                    if isinstance(inner, list):
                        items = inner
                        break
        elif isinstance(data, list):
            items = data
        if not items:
            return []

        out: list[Role] = []
        for j in items[:limit]:
            if not isinstance(j, dict):
                continue
            title = (j.get("title") or j.get("jobTitle") or "").strip()
            url = (
                j.get("jobUrl") or j.get("url") or j.get("link")
                or j.get("applyUrl") or ""
            )
            if not title or not url:
                continue
            location = (j.get("location") or j.get("locationsText")
                        or j.get("city") or "") or ""
            location_type, _ = self._classify_location(location)
            posted = _parse_icims_date(
                j.get("postingDate") or j.get("postedOn") or j.get("date") or ""
            )
            out.append(Role(
                job_title=title,
                company=self._normalize_company(display),
                job_url=str(url),
                location=location or None,
                location_type=location_type,
                job_description_full="",
                jd_completeness="Missing",
                salary_text=j.get("salary") or j.get("compensation") or None,
                posted_date=posted,
                primary_source=self.source_name,
                date_first_seen=datetime.now(timezone.utc).date().isoformat(),
            ))
        return out

    async def _try_rss(
        self, display: str, sub: str, keyword: str, limit: int,
    ) -> list[Role]:
        """RSS feed: universally supported on iCIMS public boards.

        URL: https://careers-{sub}.icims.com/jobs/search?searchKeyword={kw}&format=rss
        Each <item> has <title>, <link>, <description>, <pubDate>.
        """
        url = f"https://careers-{sub}.icims.com/jobs/search"
        params = {
            "ss": "1",
            "searchKeyword": keyword,
            "format": "rss",
        }
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                url, params=params,
                headers={
                    "Accept": "application/rss+xml,application/xml,text/xml",
                    "User-Agent": "Mozilla/5.0",
                },
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        body = resp.text or ""
        if not body or "<item" not in body.lower():
            return []

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []

        # Find <item> elements (RSS namespace varies; use local-name match)
        items = [
            el for el in root.iter()
            if el.tag.split("}", 1)[-1].lower() == "item"
        ]
        if not items:
            return []

        out: list[Role] = []
        for item in items[:limit]:
            title = ""
            link = ""
            desc = ""
            pub = ""
            for child in item:
                tag = child.tag.split("}", 1)[-1].lower()
                if tag == "title":
                    title = (child.text or "").strip()
                elif tag == "link":
                    link = (child.text or "").strip()
                elif tag == "description":
                    desc = (child.text or "").strip()
                elif tag == "pubdate":
                    pub = (child.text or "").strip()
            if not title or not link:
                continue
            # iCIMS RSS titles are sometimes "Job Title - Location"
            location = ""
            if " - " in title:
                # heuristic: split last " - " — many tenants use it as the
                # location separator. Conservative: only split if last
                # segment looks like a location (has a comma or city words)
                head, sep, tail = title.rpartition(" - ")
                if "," in tail or any(w in tail.lower() for w in
                                      ("remote", "hybrid", "office")):
                    title = head
                    location = tail
            location_type, _ = self._classify_location(location)
            jd_text = _strip_html(desc) if desc else ""
            out.append(Role(
                job_title=title,
                company=self._normalize_company(display),
                job_url=link,
                location=location or None,
                location_type=location_type,
                job_description_full=jd_text[:8000],
                jd_completeness="Full" if len(jd_text) > 500 else "Partial",
                salary_text=_extract_salary_from_text(jd_text),
                posted_date=_parse_icims_date(pub),
                primary_source=self.source_name,
                date_first_seen=datetime.now(timezone.utc).date().isoformat(),
            ))
        return out

    async def fetch_jd(self, role: Role) -> str:
        """Fetch JD body. RSS path already populated this; HTML fallback for JSON path."""
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            resp = await self.client.get(role.job_url)
            return _strip_html(resp.text)
        except Exception:
            return ""


def _extract_salary_from_text(text: str) -> Optional[str]:
    """Quick regex pull of salary band from RSS description text."""
    if not text:
        return None
    # $130,000 - $165,000  /  $130K - $165K  /  $130,000-$165,000
    m = re.search(
        r"\$\s?([\d,]{3,7})(?:K)?\s*[\-–to]+\s*\$?\s?([\d,]{3,7})(?:K)?",
        text,
    )
    if m:
        return m.group(0)
    return None

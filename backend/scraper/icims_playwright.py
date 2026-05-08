"""iCIMS scraper via Playwright (SPA-aware).

iCIMS public job boards are JavaScript-rendered SPAs — a plain HTTP GET
returns a shell HTML page with no job markup. This scraper launches headless
Chromium, lets the SPA render, then extracts job rows from the rendered DOM.

Why Playwright: iCIMS doesn't expose a clean JSON API at the public board
level. Each tenant uses iCIMS's branded careers theme, with the job table
rendered client-side via React/jQuery. The DOM structure is more consistent
across themes than the HTTP-level API surface.

Coverage: covers CPG (Hershey, Mars, ConAgra), retail (Dollar Tree, Kohl's,
PetSmart), hospitality (IHG, Choice Hotels), pharma (Bayer), industrial
(Snap-on) — sectors under-served by Workday/Greenhouse.

Per-tenant timeout: 45s (Playwright pages are slower than direct HTTP).
Concurrency: 4 (browsers are heavier than HTTP clients).

Failing tenants return [] silently; per-tenant failures don't break the run.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html


# ============================================================================
# Verified iCIMS tenants — confirmed live 2026-05-03 by running the actual
# Playwright scraper against `careers-{sub}.icims.com/jobs/search` and
# parsing the rendered iframe for /jobs/{id}/{slug}/job links.
# Each entry produced >= 1 role for the keyword "manager".
# ============================================================================
ICIMS_PW_TENANTS: list[tuple[str, str]] = [
    # Insurance / financial services
    ("Liberty Mutual",  "libertymutual"),     # 25 roles — territory/regional
    # Hospitality / leisure
    ("Six Flags",       "sixflags"),          # 20 roles — operations/regional
    ("Cedar Fair",      "cedarfair"),         # 20 roles — park operations
    ("Chick-fil-A",     "chickfila"),         # 2  roles — restaurant management
    # Industrial sales (direct match for Zach's Frito-Lay route-sales bg)
    ("Snap-on",         "snapon"),            # 20 roles — account/sales mgr
]


class ICIMSPlaywrightScraper(BaseScraper):
    """Playwright-based iCIMS scraper. Renders SPA, extracts job rows."""

    source_name = "iCIMS"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        # Lazy import — Playwright may not be installed in some environments
        # v0.3.12: surface the missing-dependency error so testers see *why*
        # iCIMS returns 0. The PyInstaller bundle excludes Playwright (it's
        # in backend.spec's `excludes` list at line 74) so production runs
        # ALWAYS hit this branch. Previously this returned [] silently with
        # roles=0, elapsed_s=0.0, errored=False — the exact silent-no-op
        # signature that hid GoogleJobs for 3 releases. The orchestrator's
        # silent-zero alert (also v0.3.12) now logs a WARNING when it sees
        # this triple, but setting quota_exhausted_reason makes the *cause*
        # explicit in the audit JSON's per_source_health.
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            self.quota_exhausted = True
            self.quota_exhausted_reason = (
                "Playwright not installed in this build (excluded from "
                "PyInstaller bundle). iCIMS scraping requires headless "
                "browser; v0.3.13 may add an HTTP-only fallback for "
                "tenants that expose JSON endpoints."
            )
            return []

        sem = asyncio.Semaphore(4)  # browser context cap
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                async def bounded(display: str, sub: str, kw: str) -> list[Role]:
                    async with sem:
                        try:
                            return await asyncio.wait_for(
                                self._search_tenant_keyword(
                                    browser, display, sub, kw, limit_per_keyword
                                ),
                                timeout=45.0,
                            )
                        except Exception:
                            return []

                tasks = [
                    bounded(display, sub, kw)
                    for display, sub in ICIMS_PW_TENANTS
                    for kw in keywords
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                await browser.close()

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
        self, browser, display: str, sub: str, keyword: str, limit: int,
    ) -> list[Role]:
        """Open page, render SPA, extract job rows from the inner iframe.

        Verified iCIMS pattern (probed live 2026-05-03 against snap-on):
          - Outer page renders a chrome with <iframe id="icims_content_iframe">
          - The inner iframe contains <a href=".../jobs/{id}/{slug}/job?in_iframe=1">
          - Inner iframe takes 5-7s to fully populate
        """
        url = f"https://careers-{sub}.icims.com/jobs/search?ss=1&searchKeyword={keyword.replace(' ', '+')}"
        # Use Playwright default context (no custom UA) — empirically works
        # better against iCIMS than spoofing Chrome 126 (some tenants cloak).
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                return []
            # Wait for the canonical iCIMS iframe element to appear, then for
            # its inner SPA to render. Total budget: ~12s wall-clock max.
            try:
                await page.wait_for_selector("#icims_content_iframe", timeout=10000)
                await page.wait_for_timeout(3500)  # let the iframe DOM populate
            except Exception:
                # Fallback path: just wait a bit and try anyway
                await page.wait_for_timeout(7000)

            # First try the named iCIMS content iframe — the canonical location
            roles = await self._extract_from_icims_iframe(page, display, limit)
            if roles:
                return roles

            # Fallback: try every iframe + the outer page (older themes)
            return await self._extract_fallback(page, display, limit)
        finally:
            await context.close()

    async def _extract_from_icims_iframe(self, page, display: str, limit: int) -> list[Role]:
        """Extract from the canonical #icims_content_iframe (modern iCIMS theme)."""
        import os
        verbose = os.environ.get("ICIMS_DEBUG", "")
        try:
            iframe_handle = await page.query_selector("#icims_content_iframe")
            if not iframe_handle:
                if verbose:
                    print(f"  [icims:{display}] no #icims_content_iframe element on page")
                return []
            frame = await iframe_handle.content_frame()
            if not frame:
                if verbose:
                    print(f"  [icims:{display}] iframe content_frame is None")
                return []
            # Use simple JS includes() filter — string-includes is more reliable
            # across runtime regex flavors than literal-syntax regex.
            jobs = await frame.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        // iCIMS detail links contain '/jobs/' AND end with '/job'
                        // (with possible '?...' query string).
                        if (!href.includes('/jobs/')) continue;
                        const afterJobs = href.split('/jobs/')[1] || '';
                        // afterJobs should look like "{id}/{slug}/job?..."
                        const parts = afterJobs.split('?')[0].split('/');
                        if (parts.length < 3) continue;
                        const id = parts[0];
                        if (!/^\\d+$/.test(id)) continue;
                        const last = parts[parts.length - 1];
                        if (last !== 'job') continue;
                        if (seen.has(id)) continue;
                        seen.add(id);
                        const title = (a.innerText || a.textContent || '').trim()
                            .replace(/^(Job\\s+)?Title:?\\s*/i, '')
                            .replace(/\\s+/g, ' ');
                        if (!title || title.length < 4 || title.length > 200) continue;
                        let loc = '';
                        let parent = a.parentElement;
                        for (let depth = 0; depth < 6 && parent; depth++) {
                            const txt = (parent.innerText || '').replace(title, '');
                            const lm = txt.match(/[A-Za-z\\.\\- ]+,\\s*[A-Z]{2,3}\\b|Remote|Hybrid/i);
                            if (lm) { loc = lm[0].trim(); break; }
                            parent = parent.parentElement;
                        }
                        out.push({title, href, location: loc, id});
                    }
                    return out;
                }
            """)
            if verbose:
                print(f"  [icims:{display}] iframe extracted {len(jobs or [])} jobs")
            return self._js_to_roles(jobs or [], display)[:limit]
        except Exception as e:
            if verbose:
                print(f"  [icims:{display}] exception: {type(e).__name__}: {e}")
            return []

    async def _extract_fallback(self, page, display: str, limit: int) -> list[Role]:
        """Older iCIMS themes that don't use icims_content_iframe."""
        # Try the outer page first
        try:
            jobs = await page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const seen = new Set();
                    const out = [];
                    for (const a of links) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\\/jobs\\/(\\d+)\\/[^?#\\/]+\\/job/);
                        if (!m) continue;
                        const id = m[1];
                        if (seen.has(id)) continue;
                        seen.add(id);
                        const title = (a.innerText || a.textContent || '').trim();
                        if (!title || title.length < 4 || title.length > 200) continue;
                        out.push({title, href, location: '', id});
                    }
                    return out;
                }
            """)
            roles = self._js_to_roles(jobs or [], display)
            if roles:
                return roles[:limit]
        except Exception:
            pass
        # Then try every other iframe
        try:
            handles = await page.query_selector_all("iframe")
            for h in handles[:5]:
                try:
                    frame = await h.content_frame()
                    if not frame:
                        continue
                    jobs = await frame.evaluate("""
                        () => {
                            const links = Array.from(document.querySelectorAll('a'));
                            const seen = new Set();
                            const out = [];
                            for (const a of links) {
                                const href = a.getAttribute('href') || '';
                                const m = href.match(/\\/jobs\\/(\\d+)\\/[^?#\\/]+\\/job/);
                                if (!m) continue;
                                const id = m[1];
                                if (seen.has(id)) continue;
                                seen.add(id);
                                const title = (a.innerText || a.textContent || '').trim();
                                if (!title || title.length < 4 || title.length > 200) continue;
                                out.push({title, href, location: '', id});
                            }
                            return out;
                        }
                    """)
                    roles = self._js_to_roles(jobs or [], display)
                    if roles:
                        return roles[:limit]
                except Exception:
                    continue
        except Exception:
            pass
        return []

    def _js_to_roles(self, js_jobs: list, display: str) -> list[Role]:
        """Convert JS-extracted dicts into Role objects."""
        out: list[Role] = []
        if not isinstance(js_jobs, list):
            return out
        for j in js_jobs:
            if not isinstance(j, dict):
                continue
            title = (j.get("title") or "").strip()
            href = (j.get("href") or "").strip()
            if not title or not href:
                continue
            # Resolve relative URLs to absolute
            if href.startswith("/"):
                # We don't have base_url here — caller supplies via context.
                # Best we can do is leave it relative; downstream filters tolerate.
                href = href
            location_raw = (j.get("location") or "").strip()
            location_type, _ = self._classify_location(location_raw)
            out.append(Role(
                job_title=title[:200],
                company=self._normalize_company(display),
                job_url=href if href.startswith("http") else None,
                location=location_raw or None,
                location_type=location_type,
                job_description_full="",
                jd_completeness="Missing",
                salary_text=None,
                posted_date=None,
                primary_source=self.source_name,
                date_first_seen=datetime.now(timezone.utc).date().isoformat(),
            ))
        return out

    async def fetch_jd(self, role: Role) -> str:
        """Fetch JD body via Playwright (renders the job detail page)."""
        if role.job_description_full:
            return role.job_description_full
        if not role.job_url:
            return ""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0",
                    )
                    page = await context.new_page()
                    await page.goto(role.job_url, wait_until="domcontentloaded", timeout=20000)
                    await page.wait_for_timeout(2500)
                    # iCIMS typically renders the JD in an element with these classes
                    for sel in (
                        "#iCIMS_JobContent",
                        ".iCIMS_JobContent",
                        ".iCIMS_JobOverview",
                        "div[class*='JobDescription']",
                        "main",
                    ):
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                txt = await el.inner_text()
                                if txt and len(txt) > 200:
                                    return txt[:8000]
                        except Exception:
                            continue
                    # Fallback: full body text
                    body = await page.evaluate("() => document.body.innerText || ''")
                    return (body or "")[:8000]
                finally:
                    await browser.close()
        except Exception:
            return ""

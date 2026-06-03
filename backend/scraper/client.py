"""Shared HTTP client for scrapers.

Centralized so every board scraper benefits from the same:
  - User-agent rotation (looks like a real browser, not a bot)
  - Per-domain rate limiting (stay polite, avoid bans)
  - Exponential-backoff retry on 429/5xx
  - Configurable timeouts
  - Automatic cookie jar per scrape session
  - Structured logging (so the admin dashboard can spot scraper issues)
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry, stop_after_attempt, wait_exponential, retry_if_exception,
)


# Realistic browser User-Agents — rotated per request to avoid pattern detection.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
]


@dataclass
class DomainPacing:
    """Per-domain rate limit state. One instance per registered domain."""
    min_delay_seconds: float = 1.5      # minimum gap between requests
    max_delay_seconds: float = 3.5      # upper bound for jitter
    last_request_time: float = 0.0      # monotonic timestamp of last request
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))


# Per-domain pacing config. Override per board if you know the site's tolerance.
DOMAIN_OVERRIDES: dict[str, dict[str, float]] = {
    # Job board APIs are generally tolerant of moderate rates
    "boards-api.greenhouse.io":  {"min_delay_seconds": 0.5,  "max_delay_seconds": 1.2},
    "api.lever.co":              {"min_delay_seconds": 0.5,  "max_delay_seconds": 1.2},
    "api.ashbyhq.com":           {"min_delay_seconds": 0.5,  "max_delay_seconds": 1.2},
    "jobs.ashbyhq.com":          {"min_delay_seconds": 0.8,  "max_delay_seconds": 2.0},
    # HTML-scraped boards need to look more human
    # Wave-2B Phase 2 (FIX 9): loosened 2.0/5.0 → 0.8/2.0 (avg ~1.4s gap).
    # The aggressive prior pacing combined with Semaphore(2)+timeout=25s in
    # builtin.py was killing 60-75% of keywords (each keyword took ~140s
    # wall-clock vs 25s timeout). Together with FIX 5 (timeout 25 → 90)
    # this restores BuiltIn raw counts. Pre-Wave-2B raw-httpx fired 80
    # requests within seconds with no soft-ban, so 1.4s avg gap is safe.
    "builtin.com":               {"min_delay_seconds": 0.8,  "max_delay_seconds": 2.0},
    "www.builtin.com":           {"min_delay_seconds": 0.8,  "max_delay_seconds": 2.0},
    "wellfound.com":             {"min_delay_seconds": 2.5,  "max_delay_seconds": 6.0},
    "indeed.com":                {"min_delay_seconds": 3.0,  "max_delay_seconds": 7.0},
    "www.indeed.com":            {"min_delay_seconds": 3.0,  "max_delay_seconds": 7.0},
    # HigherEdJobs uses DataDome — soft-IP-ban triggers when 10 categories
    # hit in <2s. Conservative pacing keeps sequential category fetches safe.
    "www.higheredjobs.com":      {"min_delay_seconds": 4.0,  "max_delay_seconds": 8.0},
}


def _is_retryable(exc: BaseException) -> bool:
    """Retry on rate limits, server errors, transient network issues."""
    s = str(exc)
    if any(c in s for c in ("429", "500", "502", "503", "504", "ConnectError", "ReadTimeout", "RemoteProtocolError")):
        return True
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    return False


class ScraperClient:
    """Shared HTTP client for all scrapers.

    Use as:
        async with ScraperClient() as client:
            response = await client.get(url)
            data = await client.get_json(url)
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        default_headers: Optional[dict[str, str]] = None,
    ):
        self._timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        self._default_headers = default_headers or {}
        self._client: Optional[httpx.AsyncClient] = None
        self._domain_pacing: dict[str, DomainPacing] = {}

    async def __aenter__(self) -> "ScraperClient":
        # v0.1.4: when running in Worker proxy mode, every Worker-bound
        # request needs the X-Tester-UUID header. Inject it as a default
        # so every scraper proxy call (Adzuna/USAJOBS/Findwork/JSearch)
        # passes the Worker's auth gate. Headers added here also propagate
        # to direct (non-proxy) requests, but those endpoints ignore the
        # header so it's harmless.
        from backend.config import config
        merged_headers = dict(self._default_headers)
        tester_uuid = (config.TESTER_UUID or "").strip()
        if tester_uuid:
            merged_headers.setdefault("X-Tester-UUID", tester_uuid)
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            http2=False,  # http2 occasionally breaks with stricter sites
            headers=merged_headers if merged_headers else None,
            # v0.3.30: raise the pool above httpx's default max_connections=100
            # so Workday's high tenant-fanout concurrency (Semaphore 80) isn't
            # bottlenecked by the connection pool. Per-domain pacing still guards
            # shared-domain sources; this mainly lets independent-domain fan-out
            # (Workday/Greenhouse/etc.) actually run in parallel.
            limits=httpx.Limits(max_connections=150, max_keepalive_connections=40),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Pacing
    # ------------------------------------------------------------------

    def _pacing_for(self, url: str) -> DomainPacing:
        domain = urlparse(url).netloc.lower()
        if domain not in self._domain_pacing:
            override = DOMAIN_OVERRIDES.get(domain)
            if override is None:
                # Suffix overrides for ATS hosts where every tenant is its own
                # subdomain (e.g. tenant.wd5.myworkdayjobs.com), so an exact-match
                # override can't catch them. The 1.5-3.5s default made the paced
                # liveness HEAD phase the dominant search cost (~546s/749 roles).
                # Workday tolerates faster pacing; 0.6-1.2s is ~3x quicker and
                # gentle for the ~4-5 HEADs/tenant liveness does (no 429s — and a
                # rate-limited HEAD only lowers confidence, never drops a role).
                if domain.endswith("myworkdayjobs.com"):
                    override = {"min_delay_seconds": 0.6, "max_delay_seconds": 1.2}
            override = override or {}
            self._domain_pacing[domain] = DomainPacing(
                min_delay_seconds=override.get("min_delay_seconds", 1.5),
                max_delay_seconds=override.get("max_delay_seconds", 3.5),
            )
        return self._domain_pacing[domain]

    async def _wait_for_pacing(self, pacing: DomainPacing) -> None:
        elapsed = time.monotonic() - pacing.last_request_time
        target_delay = random.uniform(pacing.min_delay_seconds, pacing.max_delay_seconds)
        if elapsed < target_delay:
            await asyncio.sleep(target_delay - elapsed)
        pacing.last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # Headers
    # ------------------------------------------------------------------

    def _build_headers(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            # Brotli ("br") intentionally omitted — httpx only auto-decompresses
            # gzip/deflate. Advertising "br" causes Workday/CDN responses to come
            # back as raw brotli bytes which the JD parser then sees as binary
            # garbage — ruined every Workday JD until 2026-05-02.
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        headers.update(self._default_headers)
        if extra:
            headers.update(extra)
        return headers

    # ------------------------------------------------------------------
    # Core request methods
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def get(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("Use `async with ScraperClient() as client:`")

        pacing = self._pacing_for(url)
        async with pacing.semaphore:
            await self._wait_for_pacing(pacing)
            response = await self._client.get(
                url,
                params=params,
                headers=self._build_headers(headers),
            )
            response.raise_for_status()
            return response

    async def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        merged_headers = {"Accept": "application/json"}
        if headers:
            merged_headers.update(headers)
        response = await self.get(url, params=params, headers=merged_headers)
        return response.json()

    async def head(
        self,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = True,
    ) -> httpx.Response:
        """HEAD request — used by liveness checker, doesn't fetch body."""
        if self._client is None:
            raise RuntimeError("Use `async with ScraperClient() as client:`")
        pacing = self._pacing_for(url)
        async with pacing.semaphore:
            await self._wait_for_pacing(pacing)
            return await self._client.head(
                url,
                headers=self._build_headers(headers),
                follow_redirects=follow_redirects,
            )

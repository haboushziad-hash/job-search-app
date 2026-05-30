"""Abstract Scraper base class — every job board implementation extends this.

Defines the common contract:
  - search(keywords, limit) -> list[Role]
  - fetch_jd(role) -> str  (called when JD body wasn't included in search results)
  - source_name -> str

Every board scraper handles its own quirks (auth, pagination, anti-bot)
behind this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from backend.models import Role
from backend.scraper.client import ScraperClient


class BaseScraper(ABC):
    """Abstract base for board-specific scrapers."""

    source_name: str = "abstract"   # e.g. "Greenhouse", "Lever", "Indeed"

    def __init__(self, client: Optional[ScraperClient] = None):
        self._client = client
        self._owns_client = client is None
        # v0.1.4: surfaces upstream API quota state to the orchestrator.
        # When a scraper detects a 429 (rate limit) or 403 (often quota
        # exceeded) from its upstream API, it sets quota_exhausted=True
        # so the orchestrator can surface "Adzuna hit monthly cap — your
        # other sources still ran" in the audit JSON instead of silently
        # showing 0 roles. Tester transparency.
        self.quota_exhausted: bool = False
        self.quota_exhausted_reason: str = ""
        # v0.3.4: per-keyword raw-count tracking. Scraper subclasses populate
        # this dict {keyword: raw_role_count} before applying their own dedup.
        # Surfaced in the audit JSON so we can diagnose:
        #   - Which keywords pull the most (potential over-pull, low-quality)
        #   - Which keywords return 0 (typo, banned by upstream, no matches)
        #   - Whether num_pages tuning actually translates to more raw roles
        # Lives on the scraper instance for the lifetime of one search call.
        self.per_keyword_raw_counts: dict[str, int] = {}
        # v0.3.5: optional per-user upstream filters. The orchestrator sets
        # this before calling search() so scrapers that support upstream
        # narrowing (JSearch, Adzuna, GoogleJobs) can translate user prefs
        # into API-side filters and reduce noise pre-pipeline. Scrapers
        # that don't support filters ignore the field. Shape:
        #   {
        #       "location_text": "Washington DC" | None,
        #       "salary_minimum": 95000 | None,
        #       "remote_only": True | False | None,
        #   }
        # All keys optional. None values = no filter.
        self._user_filters: Optional[dict] = None
        # v0.3.12: per-source paid-API cost in USD. Scrapers that hit a
        # paid upstream (DataForSEO, Serper, RapidAPI) accumulate the
        # estimated dollar cost here so the orchestrator can roll it into
        # the audit JSON's cost_breakdown. Free / public scrapers leave it
        # at 0.0. v0.3.9 GoogleJobs originally tried to write this as
        # `cost_estimate` directly but the attribute didn't exist on
        # BaseScraper, raising AttributeError silently and zeroing out the
        # whole scraper run. v0.3.10 patched with `_dfseo_cost_estimate`
        # local attr; v0.3.12 promotes it to the proper interface.
        self.cost_estimate: float = 0.0

    async def __aenter__(self):
        if self._client is None:
            self._client = ScraperClient()
            await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._owns_client and self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
            self._client = None

    @property
    def client(self) -> ScraperClient:
        if self._client is None:
            raise RuntimeError(
                f"{self.__class__.__name__} needs ScraperClient — use async with"
            )
        return self._client

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------

    @abstractmethod
    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        """Search the board for roles matching any of the keywords.

        Returns deduplicated roles with as much metadata as the board exposes:
        title, company, URL, location, salary, posted_date, JD (if included).

        Boards that require fetching JDs separately should leave job_description_full
        empty and rely on `fetch_jd` to enrich after search.
        """
        ...

    @abstractmethod
    async def fetch_jd(self, role: Role) -> str:
        """Fetch the full JD body for a role. Returns the JD text (may be HTML-stripped)."""
        ...

    # ------------------------------------------------------------------
    # Helpers usable by all subclasses
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_company(company: str) -> str:
        """Strip extra whitespace, drop trailing 'Inc.'/'LLC' for dedup matching."""
        if not company:
            return ""
        c = " ".join(company.split())
        for suffix in (", Inc.", ", LLC", ", Ltd.", " Inc.", " LLC", " Ltd."):
            if c.endswith(suffix):
                c = c[: -len(suffix)].rstrip(",").strip()
        return c

    @staticmethod
    def _classify_location(location_text: str) -> tuple[str, str | None]:
        """Heuristic location classification → (location_type, normalized_location).

        Returns location_type from {"Remote", "Hybrid", "On-site"} when
        inferrable. Wave-2B Phase 2 (FIX 17, 2026-05-30): expanded remote
        detection beyond the bare "remote" substring. Several scrapers
        surface remote roles using vocabulary that the old heuristic
        misclassified as On-site:
          - "Anywhere" — common across Lever, Ashby, RemoteOK, Jobicy
          - "Worldwide" / "Global" — frequently used by international boards
          - "Distributed" — used by some YC-style boards
          - Bare country/nation strings ("United States", "USA") which
            Google Jobs (and increasingly Workday postings) use as a
            distributed-team tag rather than "headquartered there".
          - "Work from home" / "WFH" — direct synonyms for Remote
        Misclassified remote roles were getting filtered OUT by
        hard_filter.location for users who set acceptable_locations to a
        specific city — even though the role would actually accept them.
        Cross-scraper impact: applies to every scraper that calls
        self._classify_location() (i.e., everything except GoogleJobs
        which has its own classifier with parallel logic).
        """
        if not location_text:
            return ("On-site", None)
        loc = location_text.strip()
        low = loc.lower()
        # Explicit remote-style markers (any-substring match)
        for kw in ("remote", "anywhere", "worldwide", "work from home", "wfh"):
            if kw in low:
                return ("Remote", loc)
        # "Distributed" is a legit remote marker but can also describe
        # distributed-systems roles (engineering). Require exact match.
        if low in ("distributed", "fully distributed"):
            return ("Remote", loc)
        # Bare country/nation strings (no city component) → likely remote.
        # Require exact match to avoid false positives like "Boston, USA".
        if low in (
            "united states", "usa", "us", "u.s.", "u.s.a.",
            "global", "international",
        ):
            return ("Remote", loc)
        if "hybrid" in low:
            return ("Hybrid", loc)
        return ("On-site", loc)

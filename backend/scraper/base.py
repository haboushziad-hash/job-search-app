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

        Returns location_type from {"Remote", "Hybrid", "On-site"} when inferrable.
        """
        if not location_text:
            return ("On-site", None)
        loc = location_text.strip()
        low = loc.lower()
        if "remote" in low:
            return ("Remote", loc)
        if "hybrid" in low:
            return ("Hybrid", loc)
        return ("On-site", loc)

"""Hacker News 'Ask HN: Who is hiring?' monthly thread scraper.

Every month, HN runs a "Who is hiring?" thread where companies post
direct hiring posts as comments. Pattern is:
  ``CompanyName | Role | Location | Remote/Hybrid/Onsite | URL``

Free, no auth, no anti-bot. Uses Algolia HN API to find the latest
thread and fetch all comments. Each top-level comment is one job posting.

Coverage: skews YC-portfolio + early-stage tech, but surfaces companies
invisible to ATS scrapers (often startup CTOs posting directly).

Endpoints:
  - List threads:   https://hn.algolia.com/api/v1/search?query=Who is hiring&tags=story
  - Get thread:     https://hn.algolia.com/api/v1/items/{thread_id}
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from html import unescape
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper import _keyword_match as _kw_match


HN_SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_ITEM_API = "https://hn.algolia.com/api/v1/items"

# Match current monthly thread: "Ask HN: Who is hiring? (Month YYYY)"
_HIRING_THREAD_RE = re.compile(
    r"Who is hiring\?\s*\((January|February|March|April|May|June|"
    r"July|August|September|October|November|December)\s+\d{4}\)",
    re.IGNORECASE,
)


class HNHiringScraper(BaseScraper):
    source_name = "HN-WhoIsHiring"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        thread_id = await self._find_latest_thread()
        if not thread_id:
            return []
        comments = await self._fetch_thread_comments(thread_id)
        if not comments:
            return []

        keywords_lower = [k.lower() for k in keywords]
        seen: set[str] = set()
        out: list[Role] = []
        for c in comments:
            try:
                role = self._comment_to_role(c)
            except Exception:
                continue
            if not role:
                continue
            # Token-overlap match (shared via _keyword_match.py).
            if not _kw_match.matches_any_keyword(
                role.job_title or "",
                role.job_description_full or "",
                keywords_lower,
            ):
                continue
            key = (role.company or "") + "|" + (role.job_title or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(role)
            if len(out) >= limit_per_keyword * len(keywords):
                break
        return out

    async def _find_latest_thread(self) -> Optional[int]:
        """Find the most recent 'Ask HN: Who is hiring?' story by user 'whoishiring'.

        Uses /search_by_date (chronological) instead of /search (Algolia
        relevance-default) to avoid latching onto old high-relevance threads
        (e.g. the 2020 pandemic-era thread). Filters titles against a regex
        matching the canonical "Who is hiring? (Month YYYY)" pattern so we
        only accept the actual current monthly thread.
        """
        # whoishiring is the official account that posts every month
        params = {
            "tags": "story,author_whoishiring",
            "hitsPerPage": 5,
        }
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                HN_SEARCH_API, params=params,
                headers={"Accept": "application/json"},
            )
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        for hit in data.get("hits") or []:
            title = hit.get("title") or ""
            if _HIRING_THREAD_RE.search(title):
                obj_id = hit.get("objectID")
                if obj_id:
                    try:
                        return int(obj_id)
                    except (ValueError, TypeError):
                        pass
        return None

    async def _fetch_thread_comments(self, thread_id: int) -> list[dict]:
        """Pull all top-level comments from the given thread."""
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                f"{HN_ITEM_API}/{thread_id}",
                headers={"Accept": "application/json"},
                timeout=20.0,
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        return data.get("children") or []

    def _comment_to_role(self, comment: dict) -> Optional[Role]:
        """Parse a single HN job-post comment into a Role.

        Expected pattern (varies by poster):
          'CompanyName | Role | Location | Remote/Hybrid | $rate | URL'

        Reality: chaotic. We extract title (first non-empty short line),
        URL (first http link), and treat the rest as JD body.
        """
        text = comment.get("text") or ""
        if not text or len(text) < 30:
            return None

        # Strip HTML, decode entities
        plain = re.sub(r"<[^>]+>", " ", text)
        plain = unescape(plain).strip()
        plain = re.sub(r"\s+", " ", plain)

        # Find first URL
        url_m = re.search(r"https?://[^\s<>\"]+", plain)
        url = url_m.group(0).rstrip(".,)") if url_m else ""
        if not url:
            # Fallback: use the HN permalink so the role isn't dropped.
            comment_id = comment.get("id")
            if comment_id:
                url = f"https://news.ycombinator.com/item?id={comment_id}"
            else:
                return None

        # Title heuristic: first ~120 chars of the comment (often "Company | Role | ...")
        first_chunk = plain[:200]
        # Try to split on " | " or " - " and take the first segment as company,
        # second as title. Many HN posts follow this pattern.
        company = ""
        title = ""
        if " | " in first_chunk:
            parts = [p.strip() for p in first_chunk.split(" | ") if p.strip()]
            if len(parts) >= 2:
                company = parts[0][:80]
                title = parts[1][:120]
        if not title:
            # Fallback: try " - " separator
            if " - " in first_chunk:
                parts = [p.strip() for p in first_chunk.split(" - ") if p.strip()]
                if len(parts) >= 2:
                    company = parts[0][:80]
                    title = parts[1][:120]
        if not title:
            # Last resort: use first sentence as title
            sentence = re.split(r"[.!?]\s+", first_chunk, maxsplit=1)[0]
            title = sentence[:120].strip()
            if not title:
                return None

        # Location heuristic: look for 'REMOTE', 'ONSITE', city patterns
        plain_lower = plain.lower()
        if "remote" in plain_lower[:300]:
            location_type = "Remote"
        elif "hybrid" in plain_lower[:300]:
            location_type = "Hybrid"
        else:
            location_type = "On-site"

        # Use the comment text (truncated) as the JD body
        return Role(
            job_title=title.strip()[:200],
            company=self._normalize_company(company.strip() or "HN-poster"),
            job_url=url,
            location=None,  # HN posts vary too much to reliably parse
            location_type=location_type,
            job_description_full=plain[:5000],
            jd_completeness="Full" if len(plain) > 500 else "Partial",
            posted_date=None,
            primary_source=self.source_name,
            date_first_seen=datetime.now(timezone.utc).date().isoformat(),
        )

    async def fetch_jd(self, role: Role) -> str:
        return role.job_description_full or ""

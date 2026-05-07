"""NoDesk scraper — free RSS feed of curated remote roles.

NoDesk is a hand-curated remote-jobs board with a strong design / engineering /
product mix (relatively light on sales/CS compared to RemoteOK). Free RSS feed,
no auth.

Endpoint: GET https://nodesk.co/remote-jobs/index.xml
  Returns: RSS 2.0 XML.

Quirk: NoDesk's RSS feed contains undefined HTML entities (e.g. raw `&`
inside descriptions instead of `&amp;`). Python's stdlib xml.etree
ParseError-rejects these strictly. We use lxml's recover-mode parser if
available, else a regex pre-clean to escape stray ampersands. Either path
yields a parseable tree without losing items.

RSS item shape (after recovery):
  <item>
    <title>Job Title at Company</title>
    <link>https://nodesk.co/remote-jobs/...</link>
    <description><![CDATA[ HTML body ]]></description>
    <pubDate>...</pubDate>
    <category>Engineering</category>
    ...
  </item>

Title parsing: NoDesk titles are "Job Title at Company" — split on " at " to
recover both fields when present.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from backend.models import Role
from backend.scraper.base import BaseScraper
from backend.scraper.greenhouse import _strip_html
from backend.scraper import _keyword_match as _kw_match


NODESK_RSS_URL = "https://nodesk.co/remote-jobs/index.xml"

# Match standalone `&` not already followed by entity-name-or-#-digit;-pattern.
# Used to escape stray ampersands so stdlib XML parser doesn't reject the feed.
_STRAY_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")


def _parse_pubdate(raw: str) -> Optional[str]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _split_title_company(rss_title: str) -> tuple[str, str]:
    """NoDesk titles are 'Job Title at Company'. Split on ' at ' if present.
    Falls back to whole-string title + empty company when no separator."""
    if " at " in rss_title:
        title, company = rss_title.rsplit(" at ", 1)
        return title.strip(), company.strip()
    return rss_title.strip(), ""


def _parse_feed_lenient(xml_bytes: bytes):
    """Parse XML in recover mode. Tries lxml first (best handling of
    malformed feeds); falls back to stdlib ET with regex pre-clean."""
    try:
        from lxml import etree as lxml_etree  # type: ignore
        parser = lxml_etree.XMLParser(recover=True, encoding="utf-8")
        return lxml_etree.fromstring(xml_bytes, parser=parser)
    except ImportError:
        pass
    # Fallback: stdlib ET with stray-amp pre-clean
    import xml.etree.ElementTree as ET
    text = xml_bytes.decode("utf-8", errors="replace")
    cleaned = _STRAY_AMP.sub("&amp;", text)
    return ET.fromstring(cleaned)


class NoDeskScraper(BaseScraper):
    """Free RSS feed at nodesk.co/remote-jobs/index.xml. Curated remote roles
    leaning design / engineering / product."""

    source_name = "NoDesk"
    attribution: str = "Powered by NoDesk — https://nodesk.co"

    async def search(
        self,
        *,
        keywords: list[str],
        limit_per_keyword: int = 50,
        posted_within_days: Optional[int] = 30,
    ) -> list[Role]:
        try:
            resp = await self.client._client.get(  # type: ignore[union-attr]
                NODESK_RSS_URL,
                headers={
                    "Accept": "application/rss+xml, application/xml, text/xml",
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; FindMeSomeDamnJobz/0.3.5; "
                        "+https://findmesomedamnjobz.com)"
                    ),
                },
                timeout=20.0,
            )
        except Exception:
            self.quota_exhausted = True
            self.quota_exhausted_reason = "NoDesk fetch error (transient)"
            return []
        if resp.status_code != 200:
            if resp.status_code in (403, 429):
                self.quota_exhausted = True
                self.quota_exhausted_reason = (
                    f"NoDesk HTTP {resp.status_code} (rate limit or block)"
                )
            return []

        try:
            root = _parse_feed_lenient(resp.content)
        except Exception:
            return []

        # Find all <item> elements regardless of root shape (lxml tree vs ET tree).
        items = root.findall(".//item") if root is not None else []

        keywords_lower = [k.lower() for k in keywords]
        per_kw_count: dict[str, int] = {kw: 0 for kw in keywords}

        cutoff_iso = None
        if posted_within_days:
            from datetime import timedelta
            cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=posted_within_days)).isoformat()

        roles: list[Role] = []
        seen_urls: set[str] = set()

        for item in items:
            def _t(tag: str) -> str:
                el = item.find(tag)
                txt = el.text if el is not None else None
                return (txt or "").strip()

            raw_title = _t("title")
            link = _t("link") or _t("guid")
            description = _t("description")
            pubdate_raw = _t("pubDate")
            category = _t("category")

            if not raw_title or not link:
                continue
            if link in seen_urls:
                continue

            posted_iso = _parse_pubdate(pubdate_raw)
            if cutoff_iso and posted_iso and posted_iso < cutoff_iso:
                continue

            title, company = _split_title_company(raw_title)
            jd_clean = _strip_html(description)

            if not _kw_match.matches_any_keyword(raw_title, jd_clean, keywords_lower):
                continue
            matched_kw: Optional[str] = None
            for kw_raw, kw_low in zip(keywords, keywords_lower):
                if not kw_low:
                    continue
                if _kw_match.matches_any_keyword(raw_title, jd_clean, [kw_low]):
                    matched_kw = kw_raw
                    per_kw_count[kw_raw] += 1
                    break

            seen_urls.add(link)
            roles.append(Role(
                job_title=title[:200],
                company=(self._normalize_company(company) or "(unknown)")[:120],
                source=self.source_name,
                primary_source=self.source_name,
                job_url=link,
                # NoDesk is remote-only by design
                location_type="Remote",
                job_description_full=jd_clean[:8000] if jd_clean else "",
                jd_completeness="Full" if len(jd_clean) > 500 else "Partial",
                posted_date=posted_iso,
                matched_keyword=matched_kw,
                # Their <category> often = function bucket; surface it as
                # tag for the tester's eyeballing convenience.
                tags=[category[:30]] if category else None,
            ))

        self.per_keyword_raw_counts = per_kw_count
        return roles

    async def fetch_jd(self, role: Role) -> str:
        """JD already inline in RSS — no separate fetch."""
        return role.job_description_full or ""

"""Shared keyword-matching logic across scrapers.

Centralizes the token-overlap matcher originally proven in Greenhouse's
scraper. All other scrapers use this module so the matcher behaves
identically across sources — no more "Lever uses substring, Greenhouse
uses tokens, Ashby uses substring" inconsistency.

Match rule (matches greenhouse._matches_any_keyword exactly):
  - 1-2 token keywords (e.g. "AI strategy"): ALL content tokens must
    appear in title+JD. Missing one inverts the meaning.
  - 3+ token keywords (e.g. "AI Enablement Services Lead"): need 60%+
    overlap in title OR 75%+ in title+JD.

Empty-JD handling: when a role's JD body is empty (Workday/iCIMS lazy-fetch
during scrape, JD comes later), only the title is matched. Threshold drops
to 50% for 3+ token keywords to account for the smaller match surface.

Used by:
  - greenhouse.py     — boolean filter at scrape time (existing behavior)
  - lever.py          — replace substring filter (PHASE 3 fix)
  - ashby.py          — replace substring filter
  - arbeitnow.py      — replace substring filter
  - hn_hiring.py      — replace substring filter
  - smartrecruiters.py — replace title-only substring filter
  - workday.py        — post-fetch sanity filter (was unfiltered)
  - icims.py          — post-fetch sanity filter
  - themuse.py        — post-fetch sanity filter (worst offender)
  - adzuna.py         — post-fetch sanity filter
  - builtin.py        — post-fetch sanity filter
  - remotive.py       — post-fetch sanity filter
  - climatebase.py    — post-fetch sanity filter
  - usajobs.py        — post-fetch sanity filter
  - findwork.py       — post-fetch sanity filter
  - runner._tag_matched_keywords — UI "via X" tag (already migrated)
"""
from __future__ import annotations

import re
from typing import Iterable, Optional


# Stopwords that carry no signal in a job-title context. Kept tight so
# domain words like "ai" / "lead" remain meaningful tokens.
STOPWORDS = frozenset({
    "a", "an", "and", "or", "for", "of", "to", "the", "in", "on",
    "with", "at", "by", "as", "&",
})


def tokenize(text: str) -> list[str]:
    """Lowercase + split on non-word chars. Filters stopwords + 1-char tokens."""
    return [
        t for t in re.split(r"[^a-z0-9]+", (text or "").lower())
        if t and len(t) > 1 and t not in STOPWORDS
    ]


def matches_any_keyword(
    title: str,
    jd: str,
    keywords: Iterable[str],
) -> bool:
    """Token-overlap match: does ANY keyword overlap enough with the role?

    title: role.job_title (string, may be empty)
    jd: role.job_description_full or job_description_essence (may be empty)
    keywords: iterable of keyword strings (case-insensitive)

    Behavior:
      - Pre-tokenizes the role once
      - Iterates keywords; first match short-circuits
      - For 1-2 token keywords: requires ALL tokens
      - For 3+ token keywords with JD present: 60% in title OR 75% in title+JD
      - For 3+ token keywords with JD empty: 50% in title (lower threshold
        because we only have the title to match against)

    Returns True if the role matches any keyword.
    """
    title_tokens = set(tokenize(title))
    jd_tokens = set(tokenize(jd[:2000])) if jd else set()
    full_tokens = title_tokens | jd_tokens
    jd_present = bool(jd_tokens)

    for kw in keywords:
        kw_tokens = tokenize(kw)
        if not kw_tokens:
            continue

        # Short keywords (1-2 tokens) — strict, all tokens required
        if len(kw_tokens) <= 2:
            if all(t in full_tokens for t in kw_tokens):
                return True
            continue

        # Long keywords (3+ tokens) — overlap rule
        kw_size = len(kw_tokens)
        title_overlap = sum(1 for t in kw_tokens if t in title_tokens) / kw_size
        if jd_present:
            full_overlap = sum(1 for t in kw_tokens if t in full_tokens) / kw_size
            if title_overlap >= 0.60 or full_overlap >= 0.75:
                return True
        else:
            # JD missing — title-only mode, looser threshold
            if title_overlap >= 0.50:
                return True

    return False


def best_keyword_match(
    title: str,
    jd: str,
    keywords_by_tier: dict[int, list[tuple[str, list[str]]]],
) -> Optional[str]:
    """Find the single best keyword that matches a role. Used by UI tagger.

    keywords_by_tier: pre-tokenized {1: [(text, tokens)], 2: [...]} sorted
                     longest-first within each tier.

    Priority: Tier 1 in title > Tier 2 in title > Tier 1 in JD > Tier 2 in JD.
    Within each priority slot, longest keyword wins.

    Returns the keyword text (original casing) or None.
    """
    title_tokens = set(tokenize(title))
    jd_tokens = set(tokenize(jd[:2000])) if jd else set()
    full_tokens = title_tokens | jd_tokens

    def _matches(role_tokens: set, kw_tokens: list[str]) -> bool:
        if not kw_tokens:
            return False
        if len(kw_tokens) <= 2:
            return all(t in role_tokens for t in kw_tokens)
        return sum(1 for t in kw_tokens if t in role_tokens) / len(kw_tokens) >= 0.60

    # Pass 1 — title-only (preferred)
    for tier in (1, 2):
        for kw_text, kw_tokens in keywords_by_tier.get(tier, []):
            if _matches(title_tokens, kw_tokens):
                return kw_text
    # Pass 2 — title + JD
    for tier in (1, 2):
        for kw_text, kw_tokens in keywords_by_tier.get(tier, []):
            if _matches(full_tokens, kw_tokens):
                return kw_text
    return None

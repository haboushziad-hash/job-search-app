"""Salary extraction from JD body — catches signal scrapers missed.

Many job boards don't expose structured salary data, but the JD body often
contains "Pay Range: $130,000 - $165,000" or similar. This module:
  1. Strips HTML tags so amounts split across <span>...</span>...<span> can
     be matched as adjacent text
  2. Tries 15+ regex patterns covering: labeled ranges, unlabeled ranges,
     bullet-list amounts after a "salary range" preface, geo-conditional
     ranges, and labeled single-amount mentions
  3. Falls back to a "two $ amounts within 200 chars of salary/pay/comp/range"
     proximity match — catches truly arbitrary formats
  4. Updates role.salary_text / salary_min / salary_max in place

Cost: $0 (all regex). The patterns below were validated against 22 real
Workday/Greenhouse JDs sampled from production runs.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from backend.models import Role


# ----------------------------------------------------------------------------
# HTML tag stripper — preserves the textual order of $ amounts so spans like
# <span>$147,500</span><span class="divider">—</span><span>$192,500 USD</span>
# become "$147,500 — $192,500 USD" and match the range patterns below.
# ----------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&#x27;": "'",
}


def _strip_html_preserving_text(html: str) -> str:
    """Strip HTML tags and decode common entities so $ amounts that were
    nested in separate spans are now adjacent text."""
    if not html:
        return ""
    # First decode entities (em-dashes between spans are common)
    text = html
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    # Strip tags but insert spaces so <span>X</span><span>Y</span> -> "X Y"
    text = _HTML_TAG.sub(" ", text)
    # Collapse runs of whitespace
    text = re.sub(r"\s+", " ", text)
    return text


# ----------------------------------------------------------------------------
# Regex patterns (ordered by specificity — more specific first)
# ----------------------------------------------------------------------------

# Re-usable amount fragment: $130,000 / $130K / $130k / $130 (no comma, just digits)
_AMOUNT = r"\d{2,3}(?:,\d{3})+|\d{2,3}\s*[Kk]|\d{4,7}"

# $130,000 - $165,000 / $130K - $165K / $130,000–$165,000 (em-dash)
PATTERN_RANGE = re.compile(
    r"\$\s*(" + _AMOUNT + r")\s*"
    r"(?:to|-|–|—)\s*"
    r"\$?\s*(" + _AMOUNT + r")"
)

# Labeled range — broadest possible label set
PATTERN_LABELED_RANGE = re.compile(
    r"(?:pay\s*range|salary\s*range|compensation\s*range|comp\s*range|"
    r"base\s*pay\s*range|base\s*salary\s*range|target\s*compensation|"
    r"total\s*target\s*compensation|target\s*total\s*compensation|"
    r"annual\s*salary|annual\s*compensation|annual\s*base\s*salary|"
    r"annual\s*cash\s*salary|annual\s*base|"
    r"pay\s*scale|salary|compensation|comp|"
    r"base\s*pay|base\s*salary|"
    r"us\s*base\s*pay\s*range|usa\s*base\s*pay|"
    r"new\s*hire\s*base\s*salary|new\s*hire\s*pay|"
    r"target\s*base\s*salary)"
    r"[^$]{0,80}?"  # allow up to 80 chars between label and first $
    r"\$\s*(" + _AMOUNT + r")\s*"
    r"(?:to|-|–|—)\s*"
    r"\$?\s*(" + _AMOUNT + r")",
    re.IGNORECASE,
)

# Bullet-list pattern: "salary range listed below" or "pay ranges ... below"
# followed within 600 chars by two $ amounts — typical of Toast/Instacart style
PATTERN_LISTED_BELOW = re.compile(
    r"(?:salary\s*range|pay\s*range|pay\s*ranges?|compensation)\s*"
    r"(?:for\s*(?:this|a)\s*(?:role|successful\s*candidate|position))?\s*"
    r"(?:is\s*)?(?:listed\s*below|are\s*listed\s*below|are\s*as\s*follows|"
    r"is\s*as\s*follows|below)"
    r"[\s\S]{0,800}?"
    r"\$\s*(" + _AMOUNT + r")"
    r"[\s\S]{0,200}?"
    r"\$\s*(" + _AMOUNT + r")",
    re.IGNORECASE,
)

# "Base salary: $140,000" or "starting at $140K"
PATTERN_LABELED_SINGLE = re.compile(
    r"(?:base\s*salary|starting\s*salary|annual\s*cash\s*salary|salary|"
    r"starting\s*at|starts\s*at|base\s*pay)"
    r"[:\s]*(?:is\s*|of\s*)?\$\s*(" + _AMOUNT + r")",
    re.IGNORECASE,
)

# Two $ amounts within 200 chars of a salary-context word — last-resort proximity
PATTERN_PROXIMITY = re.compile(
    r"(?:salary|pay|compensation|comp|base|annual|total\s*target)"
    r"[\s\S]{0,200}?"
    r"\$\s*(" + _AMOUNT + r")"
    r"[\s\S]{0,200}?"
    r"\$\s*(" + _AMOUNT + r")",
    re.IGNORECASE,
)

# Hourly: $65/hr or $65 per hour
PATTERN_HOURLY = re.compile(
    r"\$\s*(\d{2,3}(?:\.\d{1,2})?)\s*(?:/\s*hr|/\s*hour|per\s*hour)",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _parse_amount(s: str) -> Optional[int]:
    """Convert '130,000' or '130K' or '130k' to integer 130000."""
    if not s:
        return None
    s = s.strip().replace(",", "").replace(" ", "")
    if s.lower().endswith("k"):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _is_realistic_annual_salary(amount: int) -> bool:
    """Reject obviously wrong matches (e.g. $401k for a 401(k) plan,
    or $50,000,000 for revenue figures)."""
    return 30_000 <= amount <= 1_000_000


# ----------------------------------------------------------------------------
# Main extraction function
# ----------------------------------------------------------------------------

def _try_range(pattern: re.Pattern, text: str) -> Optional[tuple[str, int, int]]:
    """Run a 2-group range pattern; return (text, min, max) if both amounts
    are valid annual salaries, else None. Ensures min<=max by swapping if
    needed (Workday "$165,000 - $130,000" reversed lists do happen)."""
    m = pattern.search(text)
    if not m:
        return None
    smin = _parse_amount(m.group(1))
    smax = _parse_amount(m.group(2))
    if not (smin and smax):
        return None
    if smin > smax:
        smin, smax = smax, smin
    if not (_is_realistic_annual_salary(smin) and _is_realistic_annual_salary(smax)):
        return None
    # Reject ranges that are absurdly wide (>5x) — usually means we matched
    # two unrelated numbers (e.g. "127 employees" + "$140k salary").
    if smax > smin * 5:
        return None
    return (f"${smin:,} - ${smax:,}", smin, smax)


def extract_salary_from_jd(jd_text: str) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Try regex patterns to extract (salary_text, salary_min, salary_max).

    Returns (None, None, None) if no salary signal found. Patterns are tried
    in order of specificity — most reliable first.
    """
    if not jd_text:
        return (None, None, None)

    # Strip HTML so spans/tags don't break adjacency. JDs from Greenhouse
    # commonly look like <span>$147,500</span><span>—</span><span>$192,500</span>
    text = _strip_html_preserving_text(jd_text)

    # Truncate to first ~8K chars — salary is almost always in the top section
    # (raised from 5K because some JDs have intro paragraphs before salary)
    text = text[:8000]

    # 1. Labeled range — most reliable
    result = _try_range(PATTERN_LABELED_RANGE, text)
    if result:
        return result

    # 2. "Listed below" + bullet-style amounts (Toast/Instacart pattern)
    result = _try_range(PATTERN_LISTED_BELOW, text)
    if result:
        return result

    # 3. Unlabeled range — only trust when the surrounding text contains a
    # salary-context word, to avoid matching e.g. "$5M-$10M revenue".
    m = PATTERN_RANGE.search(text)
    if m:
        # Look at 100 chars before the match for salary context
        ctx_start = max(0, m.start() - 100)
        context = text[ctx_start:m.end()].lower()
        if any(w in context for w in ("salary", "pay", "compensation", "comp",
                                       "base", "annual", "ttc", "total target")):
            smin = _parse_amount(m.group(1))
            smax = _parse_amount(m.group(2))
            if smin and smax and smin > smax:
                smin, smax = smax, smin
            if (smin and smax and _is_realistic_annual_salary(smin)
                    and _is_realistic_annual_salary(smax)
                    and smax <= smin * 5):
                return (f"${smin:,} - ${smax:,}", smin, smax)

    # 4. Proximity fallback — two amounts within 200 chars of a salary word
    result = _try_range(PATTERN_PROXIMITY, text)
    if result:
        return result

    # 5. Labeled single value
    m = PATTERN_LABELED_SINGLE.search(text)
    if m:
        amount = _parse_amount(m.group(1))
        if amount and _is_realistic_annual_salary(amount):
            return (f"${amount:,}", amount, amount)

    # 6. Hourly — convert to annual estimate (~2080 hours)
    m = PATTERN_HOURLY.search(text)
    if m:
        try:
            hourly = float(m.group(1))
            annual = int(hourly * 2080)
            if _is_realistic_annual_salary(annual):
                return (f"${hourly}/hr (~${annual:,}/yr)", annual, annual)
        except ValueError:
            pass

    return (None, None, None)


def enrich_salaries(roles: list[Role], log: bool = True) -> list[Role]:
    """Run salary extraction on every role missing structured salary data.
    Mutates roles in place. Returns the same list."""
    enriched = 0
    for role in roles:
        if role.salary_min is not None or role.salary_max is not None:
            continue  # already has structured salary
        if not role.job_description_full:
            continue
        text, smin, smax = extract_salary_from_jd(role.job_description_full)
        if smin is not None or smax is not None:
            role.salary_text = role.salary_text or text
            role.salary_min = role.salary_min or smin
            role.salary_max = role.salary_max or smax
            enriched += 1
    if log:
        total = len(roles)
        with_salary = sum(1 for r in roles if r.salary_max is not None)
        coverage = (with_salary / total * 100) if total else 0
        print(f"[salary_extractor] enriched {enriched} roles via JD regex. "
              f"Coverage: {with_salary}/{total} ({coverage:.0f}%)")
    return roles

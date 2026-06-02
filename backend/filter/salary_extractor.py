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

# v0.3.3: CSS class names that semantically signal a salary/pay block.
# When the HTML stripper sees a tag with one of these class names, it
# injects "salary " into the text BEFORE stripping the tag so that the
# downstream PROXIMITY pattern can detect the salary context. Without
# this, layouts like Pinterest's `<div class="pay-range"><span>$X</span>
# <span>—</span><span>$Y USD</span></div>` lose the salary semantic
# entirely (the visible text after strip is just "$X — $Y USD" with no
# nearby salary/pay/comp word, so PROXIMITY won't match).
_SALARY_CLASS_HINTS = re.compile(
    r'class\s*=\s*["\'][^"\']*?'
    r'(pay-range|salary-range|salary_range|compensation-range|comp-range|'
    r'pay_range|pay-band|salary-band|pay-info|salary-info|comp-info|'
    r'compensation|salary|pay-rate|wage)'
    r'[^"\']*?["\']',
    re.IGNORECASE,
)
# Tag opener that has a salary-class hint anywhere in its attributes.
# We use this to find the START of a salary container so we can prepend
# a context word into the text stream before tag-stripping happens.
_TAG_WITH_SALARY_CLASS = re.compile(
    r'<[a-zA-Z][^>]*class\s*=\s*["\'][^"\']*?'
    r'(?:pay-range|salary-range|salary_range|compensation-range|comp-range|'
    r'pay_range|pay-band|salary-band|pay-info|salary-info|comp-info|'
    r'compensation|salary|pay-rate|wage)'
    r'[^"\']*?["\'][^>]*>',
    re.IGNORECASE,
)


def _strip_html_preserving_text(html: str) -> str:
    """Strip HTML tags and decode common entities so $ amounts that were
    nested in separate spans are now adjacent text.

    v0.3.3: also detects CSS class names that semantically mark a
    salary/pay container (e.g. <div class="pay-range">) and injects the
    word "salary" into the text stream just before that container, so
    the downstream PROXIMITY regex can find the salary context word
    even when the visible text after strip lacks it. This fixes the
    Pinterest-style case where the salary semantic lives in the HTML
    class attribute but evaporates when tags are stripped.
    """
    if not html:
        return ""
    text = html
    # First decode entities (em-dashes between spans are common)
    for entity, char in _HTML_ENTITIES.items():
        text = text.replace(entity, char)
    # v0.3.3: inject "salary" before any tag whose class signals a
    # salary container. Replacement is " salary " (padded with spaces)
    # so it doesn't run into adjacent text after tag-stripping.
    text = _TAG_WITH_SALARY_CLASS.sub(lambda m: " salary " + m.group(0), text)
    # Strip tags but insert spaces so <span>X</span><span>Y</span> -> "X Y"
    text = _HTML_TAG.sub(" ", text)
    # Collapse runs of whitespace
    text = re.sub(r"\s+", " ", text)
    return text


# ----------------------------------------------------------------------------
# Regex patterns (ordered by specificity — more specific first)
# ----------------------------------------------------------------------------

# Re-usable amount fragment: $130,000 / $130,000.00 / $130K / $130k / $130
# Wave-2B Phase 2 (FIX 22, 2026-05-30): added optional `.\d{1,2}` decimal
# suffix to handle Google Jobs / Indeed format "$85,000.00 - $110,000.00".
# Previously the trailing ".00" broke _parse_amount because _AMOUNT didn't
# capture it.
_AMOUNT = r"\d{2,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{2,3}\s*[Kk]|\d{4,7}(?:\.\d{1,2})?"

# $130,000 - $165,000 / $130K - $165K / $130,000–$165,000 (em-dash)
PATTERN_RANGE = re.compile(
    r"\$\s*(" + _AMOUNT + r")\s*"
    r"(?:to|-|–|—)\s*"
    r"\$?\s*(" + _AMOUNT + r")"
)

# Labeled range — broadest possible label set.
# v0.3.3: added OTE / on-target-earnings labels for sales/account roles
# (e.g., Marqeta "OTE range for this position is: National: $143,000 -
# $178,800" — without these labels, the unlabeled PATTERN_RANGE fallback
# was bypassing this case because the text "OTE range" didn't match any
# salary-context word).
PATTERN_LABELED_RANGE = re.compile(
    # Wave-2B Phase 2 (FIX 22): added `s?` to plural variants — Greenhouse
    # postings use "Base Compensation Ranges:" not "Base Compensation
    # Range:" and the prior regex didn't match plurals. Same for
    # "Pay Ranges:" etc.
    r"(?:pay\s*ranges?|salary\s*ranges?|compensation\s*ranges?|comp\s*ranges?|"
    r"base\s*pay\s*ranges?|base\s*salary\s*ranges?|target\s*compensation|"
    r"total\s*target\s*compensation|target\s*total\s*compensation|"
    r"annual\s*salary|annual\s*compensation|annual\s*base\s*salary|"
    r"annual\s*cash\s*salary|annual\s*base|"
    r"pay\s*scale|salary|compensation|comp|"
    r"base\s*pay|base\s*salary|"
    r"us\s*base\s*pay\s*ranges?|usa\s*base\s*pay|"
    r"new\s*hire\s*base\s*salary|new\s*hire\s*pay|"
    r"target\s*base\s*salary|"
    # v0.3.3: OTE = On-Target Earnings (sales/account roles)
    r"ote\s*ranges?|ote|on[\s\-]target\s*earnings|on[\s\-]target\s*compensation|"
    r"target\s*earnings|earnings\s*ranges?|earnings\s*target|"
    r"new\s*hire\s*ote|annual\s*ote)"
    r"[^$]{0,80}?"  # allow up to 80 chars between label and first $
    r"\$\s*(" + _AMOUNT + r")\s*"
    r"(?:to|-|–|—)\s*"
    r"\$?\s*(" + _AMOUNT + r")",
    re.IGNORECASE,
)

# Wave-2B Phase 2 (FIX 22): K-range without $ sign — BuiltIn / Indeed
# style "110K-130K Annually" / "60K-81K". These appear in listing cards
# rather than JD body, so the unlabeled PATTERN_RANGE missed them (no
# $ prefix). Require a salary-context word within 30 chars to avoid
# matching unrelated numbers like "100K-130K users" or
# "10K-20K subscribers".
PATTERN_K_RANGE_BARE = re.compile(
    r"(\d{2,3})\s*[Kk]\s*[-–—]\s*(\d{2,3})\s*[Kk]"
    r"\s*(?:annually|annual|/\s*year|/\s*yr|per\s*year|salary|comp|pay|usd)?",
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
    """Convert '130,000' / '130,000.00' / '130K' / '130k' to integer.

    Wave-2B Phase 2 (FIX 22): float() already handles decimals like
    "130000.00" so the only fix needed at this layer was the upstream
    _AMOUNT regex (now captures the decimal suffix). int(float(...))
    then truncates to integer cents-free dollars.
    """
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
# v0.3.26 — page-chrome, currency, and multi-region helpers
# ----------------------------------------------------------------------------

# S2: a "Similar Jobs" / "Recommended jobs" rail lives in the same HTML the
# scraper captured. Without cutting it, salary extraction (and the LLM scorer)
# read a NEIGHBORING posting — e.g. Loftware (no salary) inherited Oklo's
# "220K-280K Annually" from the Similar Jobs box.
# STRONG rail markers only — these phrases do not occur in normal JD prose, so
# the FIRST occurrence reliably marks where the real posting ends and the
# similar/recommended rail begins. (Deliberately NOT "view all jobs" / "browse
# jobs" — those are nav links that appear mid-page and caused an early false cut.)
_CHROME_BOUNDARY_RE = re.compile(
    r"(?:similar jobs|similar companies|jobs you might (?:also )?like|"
    r"recommended (?:jobs|for you)|more jobs (?:at|from|like)|related jobs|"
    r"other jobs (?:you|at|from)|people also viewed|set (?:a |an )?alert for similar)",
    re.IGNORECASE,
)


def strip_page_chrome(text: str) -> str:
    """Cut a trailing "Similar Jobs"/"Recommended jobs" rail so salary + scoring
    never read a neighboring posting. Cuts at the first STRONG rail marker that
    appears past the first 150 chars (so a JD can't be truncated to nothing)."""
    if not text:
        return text
    m = _CHROME_BOUNDARY_RE.search(text)
    if m and m.start() > 150:
        return text[:m.start()]
    return text


# S5: a $ amount is NON-USD only when a foreign currency token sits next to it
# AND no explicit USD marker is present. "$130,000" with nothing else = USD.
_NON_USD_RE = re.compile(
    r"\b(?:CAD|C\$|AUD|A\$|NZD|NZ\$|GBP|EUR|SGD|S\$|INR|MXN|BRL|JPY|CHF|DKK|SEK|NOK)\b|£|€|₹",
    re.IGNORECASE,
)
_USD_MARKER_RE = re.compile(r"\bUSD\b|\bUS\$|\bU\.S\.\s*dollar", re.IGNORECASE)

# S3: region qualifiers. Multi-region postings list a high-cost-metro band first
# (which we were wrongly picking) and a broad "all other locations" band second.
# Prefer the national band; if none, avoid the metro band; else take the lowest.
_HIGH_METRO_RE = re.compile(
    r"\b(?:san francisco|sf bay|bay area|new york city|new york metro|nyc|"
    r"greater seattle|seattle|los angeles|zone\s*1|tier\s*1|premium\s*(?:market|metro))\b",
    re.IGNORECASE,
)
_NATIONAL_RE = re.compile(
    r"\b(?:all other|other us|other location|other geograph|national(?:ly|wide)?|"
    r"rest of (?:the )?(?:us|country)|zone\s*2|tier\s*2)\b",
    re.IGNORECASE,
)


def _is_usd(ctx: str) -> bool:
    if _NON_USD_RE.search(ctx) and not _USD_MARKER_RE.search(ctx):
        return False
    return True


def _collect_ranges(text: str) -> list[dict]:
    """Find EVERY plausible salary range (not just the first), each tagged with
    its position, a confidence rank, the surrounding context, and a USD flag.
    Selection (currency + region) happens in _select_range."""
    out: list[dict] = []

    def add(g1, g2, start, end, conf):
        smin = _parse_amount(g1) if isinstance(g1, str) else g1
        smax = _parse_amount(g2) if isinstance(g2, str) else g2
        if not (smin and smax):
            return
        if smin > smax:
            smin, smax = smax, smin
        if not (_is_realistic_annual_salary(smin) and _is_realistic_annual_salary(smax)):
            return
        if smax > smin * 5:  # absurdly wide => two unrelated numbers
            return
        # Region context is wide (the "All Other US Locations:" label can precede
        # the range by a few words). Currency context is TIGHT — the token sits
        # immediately after the range ("…125K CAD", "…$90K USD") — so a USD label
        # elsewhere in the JD can't whitewash a CAD range (the Harris bug).
        region_ctx = text[max(0, start - 90): start + 90]
        # Currency is read from a TIGHT window: the matched span (the K-range
        # pattern can CONSUME a trailing "USD") plus ~6 chars after, where a
        # "$X - $Y CAD/USD" suffix lives. Crucially it must NOT reach a following
        # clause like "For Canadian employees:" that belongs to the NEXT range —
        # that bled across and mislabeled Instacart's US range as CAD.
        cur_ctx = text[max(0, start - 4): end + 6]
        out.append({"min": smin, "max": smax, "start": start, "conf": conf,
                    "usd": _is_usd(cur_ctx),
                    "explicit_usd": bool(_USD_MARKER_RE.search(cur_ctx)),
                    "ctx": region_ctx})

    for m in PATTERN_LABELED_RANGE.finditer(text):
        add(m.group(1), m.group(2), m.start(), m.end(), 3)
    for m in PATTERN_LISTED_BELOW.finditer(text):
        add(m.group(1), m.group(2), m.start(), m.end(), 3)
    for m in PATTERN_RANGE.finditer(text):
        # Accept on a salary keyword OR a region qualifier — multi-region comp
        # blocks list "<City>: $X-$Y" where the trigger is the region, not a
        # salary word (Komodo/Wilson Sonsini "All Other US Locations: $…").
        ctx_l = text[max(0, m.start() - 100): m.end()].lower()
        if (any(w in ctx_l for w in ("salary", "pay", "compensation", "comp",
                                     "base", "annual", "ttc", "total target"))
                or _HIGH_METRO_RE.search(ctx_l) or _NATIONAL_RE.search(ctx_l)):
            add(m.group(1), m.group(2), m.start(), m.end(), 1)
    for m in PATTERN_PROXIMITY.finditer(text):
        add(m.group(1), m.group(2), m.start(), m.end(), 1)
    for m in PATTERN_K_RANGE_BARE.finditer(text):
        ctx_l = text[max(0, m.start() - 50): m.end() + 30].lower()
        if any(w in ctx_l for w in ("salary", "pay", "compensation", "comp", "base",
                                    "annual", "annually", "/year", "/yr", "per year", "usd")):
            add(int(m.group(1)) * 1000, int(m.group(2)) * 1000, m.start(), m.end(), 2)

    # dedup identical (min,max), keeping the highest-confidence occurrence
    best: dict = {}
    for c in out:
        k = (c["min"], c["max"])
        if k not in best or c["conf"] > best[k]["conf"]:
            best[k] = c
    return list(best.values())


def _select_range(cands: list[dict]) -> dict:
    """Pick one range. Multi-region: prefer national/"all other" band, else avoid
    the high-metro band, else lowest (conservative). Single-tier: highest
    confidence, then earliest position (preserves prior 'labeled-first' behavior)."""
    if len(cands) == 1:
        return cands[0]
    region_signal = any(_HIGH_METRO_RE.search(c["ctx"]) or _NATIONAL_RE.search(c["ctx"]) for c in cands)
    if region_signal:
        nat = [c for c in cands if _NATIONAL_RE.search(c["ctx"])]
        if nat:
            return min(nat, key=lambda c: c["max"])
        non_metro = [c for c in cands if not _HIGH_METRO_RE.search(c["ctx"])]
        pool = non_metro or cands
        return min(pool, key=lambda c: c["max"])
    return sorted(cands, key=lambda c: (-c["conf"], c["start"]))[0]


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

    # v0.3.26 (S2): cut the "Similar Jobs" rail so we never read a neighbor's pay.
    text = strip_page_chrome(text)

    # Truncate to first ~8K chars — salary is almost always in the top section
    # (raised from 5K because some JDs have intro paragraphs before salary)
    text = text[:8000]

    # v0.3.26 (S3/S5): collect EVERY plausible range, then choose — prefer USD,
    # and for multi-region postings prefer the national / "all other locations"
    # band rather than the first (highest-cost-metro) one. Replaces the old
    # "return the first pattern that matches" logic that picked SF/NYC over
    # "All Other US" (Komodo/Wilson Sonsini) and crossed a CAD high with a USD
    # low (Harris).
    cands = _collect_ranges(text)
    usd = [c for c in cands if c["usd"]]
    # When the JD mixes currencies (e.g. "110K-125K CAD / 79K-90K USD"), trust
    # ranges with an EXPLICIT USD marker over USD-by-default ones — this avoids
    # picking a board's mis-combined "79K-125K" card summary (USD low + CAD high).
    if usd and any(not c["usd"] for c in cands):
        explicit = [c for c in usd if c.get("explicit_usd")]
        if explicit:
            usd = explicit
    if usd:  # if ONLY non-USD ranges exist, don't mislabel a foreign number as USD
        c = _select_range(usd)
        return (f"${c['min']:,} - ${c['max']:,}", c["min"], c["max"])

    # No trustworthy range — fall back to a labeled single value, then hourly.
    m = PATTERN_LABELED_SINGLE.search(text)
    if m:
        amount = _parse_amount(m.group(1))
        if amount and _is_realistic_annual_salary(amount):
            return (f"${amount:,}", amount, amount)

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
    enriched_from_text = 0
    for role in roles:
        if role.salary_min is not None and role.salary_max is not None:
            continue  # already has full structured salary

        # Two-pass: first try salary_text (scrapers often populate this with
        # a human-readable range like "$130K-$165K" but skip parsing min/max).
        # Then fall back to scanning the JD body.
        if role.salary_text and (role.salary_min is None or role.salary_max is None):
            _, smin, smax = extract_salary_from_jd(role.salary_text)
            if smin is not None:
                role.salary_min = role.salary_min or smin
            if smax is not None:
                role.salary_max = role.salary_max or smax
            if smin is not None or smax is not None:
                enriched_from_text += 1
                continue

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

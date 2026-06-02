"""Hard pre-filters — applied before the LLM cascade.

These are deterministic rules. If a role fails any of them, it's dropped
without spending API tokens. Order is from cheapest check to most expensive.

Why this matters: every role killed here is ~$0.001 saved AND it never
clutters the apply list. Hard filters are higher-leverage than prompt tuning.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from backend.models import CandidateProfile, Role
from backend.filter.geocoding import is_within_radius


# ----------------------------------------------------------------------------
# Salary floor (soft — see hard_floor parameter)
# ----------------------------------------------------------------------------

def passes_salary_floor(
    role: Role,
    *,
    minimum: int,
    soft_band_pct: float = 0.10,
    hard_floor: bool = False,
) -> bool:
    """Soft salary filter — keep roles within band of minimum.

    If `hard_floor=True`: drop anything below minimum.
    If `hard_floor=False` (default soft): drop only if salary_max is clearly
        below the soft band (minimum * (1 - soft_band_pct)).
    Roles with no salary listed always pass (don't punish missing data).
    """
    smax = role.salary_max
    if smax is None:
        # No salary data — always include, let LLM judge
        return True
    if hard_floor:
        return smax >= minimum
    soft_floor = int(minimum * (1.0 - soft_band_pct))
    return smax >= soft_floor


# ----------------------------------------------------------------------------
# Location filter
# ----------------------------------------------------------------------------

# Non-US country/region tokens that indicate a remote role is NOT US-eligible.
# Used to gate "Remote India" / "Remote (UK)" type listings out of US searches.
# Word-boundary matched so "indianapolis" doesn't trigger "india".
_NON_US_REMOTE_TOKENS = (
    "india", "pakistan", "philippines", "bangladesh", "sri lanka", "vietnam",
    "indonesia", "malaysia", "singapore", "thailand", "japan", "china", "hong kong",
    "taiwan", "korea", "australia", "new zealand",
    "uk", "united kingdom", "england", "scotland", "wales", "ireland",
    "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland",
    "poland", "czech", "romania", "ukraine", "russia", "turkey",
    "brazil", "argentina", "mexico", "colombia", "chile", "peru",
    "south africa", "egypt", "nigeria", "kenya", "morocco",
    "uae", "saudi arabia", "israel", "jordan",
    "emea", "apac", "latam", "anz",  # regional shorthand
)
_NON_US_REMOTE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _NON_US_REMOTE_TOKENS) + r")\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------
# Dead-listing detection — regex fast-path. Catches the obvious "no longer
# hiring" boilerplate before we spend embedding+LLM budget on the role.
# Stage 2 has a stronger LLM-based check that fires on borderline cases.
# ----------------------------------------------------------------------------

_DEAD_LISTING_PATTERNS = [
    re.compile(p, re.I) for p in (
        # Original patterns (v0.1.4 era)
        r"no longer accepting applications",
        r"this (?:position|role|job)\s+(?:has been|is)\s+filled",
        r"we are no longer hiring for this (?:role|position)",
        r"this requisition (?:has been|is)\s+(?:closed|filled)",
        r"applications? (?:are\s+)?(?:no longer|currently not) (?:being )?accepted",
        r"this listing has expired",
        r"job posting (?:has been|is) closed",
        r"we have filled this position",
        # v0.1.9 expansion: dead-listing patterns observed in real tester
        # data weren't matching just the original 8. Closure banners on
        # Greenhouse, Workday, Indeed, Lever each phrase it differently;
        # we were undercounting stale roles by ~5-10 per run. Patterns
        # below stay specific (require closure-context phrasing) to
        # avoid false-positives on legitimate JDs that mention deadlines
        # or use words like "closed" / "expired" in non-closure context.
        r"this (?:position|role|job|opening|posting) (?:has been|is) closed",
        r"this (?:job|role|position|opening|posting) is no longer (?:open|available|active)",
        r"this (?:job|position|role) has expired",
        r"sorry,?\s+(?:this|that) (?:job|position|role|opening) (?:is\s+)?(?:closed|no longer)",
        r"we[''’]?ve\s+(?:found|filled) (?:our|the) (?:match|candidate|ideal candidate|right person)",
        r"thank you for your interest,?\s+but\s+(?:this|the) (?:position|role|job|opening)",
        r"hiring (?:has\s+)?(?:closed|paused|on hold)",
        r"closed to (?:new\s+)?applications",
        r"application (?:deadline|period) has (?:passed|ended|expired|closed)",
        r"this (?:opening|position|role) (?:has been|is) (?:filled|withdrawn|removed)",
        r"\bstatus:?\s+(?:closed|filled|expired|inactive)\b",
        # v0.3.26 (L2): board-specific removal banners observed in live testing.
        # BuiltIn/builtinchicago: "Sorry, this job was removed at 02:13 a.m. …".
        # talent.com: "No longer accepting applications". HHMI: "no longer available".
        r"this (?:job|posting|listing) (?:was|has been) removed",
        r"\bjob was removed (?:at|on)\b",
        r"no longer accepting applications",
        r"(?:job|position|role|posting|listing) is no longer available",
        # v0.3.26: "Job Post Expired" expired-page (BuiltIn/builtinchicago) — the
        # JD gets replaced by an expired notice + a rail of other postings.
        r"job post(?:ing)? (?:has )?expired",
        r"\bjob post expired\b",
        r"this (?:job|posting) (?:is|has) (?:now )?(?:expired|been removed)",
    )
]


def is_dead_listing(jd_text: str) -> bool:
    """Return True if the JD contains obvious 'no longer hiring' boilerplate."""
    if not jd_text or len(jd_text) < 50:
        return False
    sample = jd_text[:5000]  # only check the first 5K chars
    return any(p.search(sample) for p in _DEAD_LISTING_PATTERNS)


# ----------------------------------------------------------------------------
# Hybrid reclassification (v0.2.0)
# ----------------------------------------------------------------------------
#
# Scrapers default location_type to "On-site" when the JD doesn't contain
# the literal word "hybrid". But many roles ARE functionally hybrid:
#  - Multi-city listings ("San Francisco | New York | Washington DC" — a
#    role with 3+ offices is almost always hybrid in practice; on-site
#    means a single specific office).
#  - JD body uses "X days in office" / "flexible work" language without
#    saying "hybrid" verbatim.
#
# The audit (May 4, 2026 run) found 0 hybrid roles in qualifying out of
# 178, while ~25-40 roles in the dataset were obviously hybrid (Anthropic
# 3-office, Scale AI 4-office, Waymo 4-office, etc.). Without this
# reclassification, the Hybrid filter chip on Dashboard shows 0 results
# for users with otherwise hybrid-friendly profiles — looks broken.

_HYBRID_JD_SIGNALS = [
    re.compile(p, re.I) for p in (
        r"\bhybrid\s+(?:work|schedule|arrangement|model|role|position|policy)",
        r"\b\d+\s*-?\s*\d*\s*days?\s+(?:per\s+week\s+)?(?:in\s+(?:the\s+)?office|on[- ]site)",
        r"\bin[- ]office\s+\d+\s*-?\s*\d*\s*days?",
        r"\bflexible\s+work\s+(?:arrangement|schedule|environment|policy)",
        r"\bwork\s+(?:from\s+home|remote)\s+\d+\s*-?\s*\d*\s*days?",
        r"\bhybrid\s+(?:in[- ]office|remote)",
        r"\b\d+\s+days?\s+(?:remote|in[- ]office),?\s+\d+\s+days?",
        r"\b(?:two|three|four)\s+days?\s+(?:per\s+week\s+)?in\s+(?:the\s+)?office",
        r"\bremote[- ]friendly\s+(?:with|but)\s+",
    )
]


def _count_distinct_cities(location_text: str) -> int:
    """Count distinct cities in a location string.

    Handles common multi-city formats:
      "San Francisco, CA | New York, NY | Washington, DC"  → 3
      "Boston, MA; Cambridge, MA; Waltham, MA"             → 3 (but same metro)
      "Atlanta, GA / Charlotte, NC"                         → 2
      "San Francisco"                                       → 1
      ""                                                    → 0
    """
    if not location_text:
        return 0
    # Split on common multi-city separators
    parts = re.split(r"\s*[|;/]\s*", location_text)
    cities: set[str] = set()
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        # Take everything before the first comma as the city portion
        city = p.split(",")[0].strip()
        if city and len(city) > 1:
            cities.add(city)
    return len(cities)


# Strong remote-only signals — JD claims that imply true remote, not
# a hybrid role with remote-flex. Conservative phrasing to avoid catching
# JDs that just MENTION remote in passing ("we offer remote work for
# some teams, but this role is in-office").
_REMOTE_STRONG_SIGNALS = [
    re.compile(p, re.I) for p in (
        r"\bfully\s+remote\b",
        r"\b100%\s*remote\b",
        r"\bremote[- ]first\b",
        r"\bwork\s+from\s+anywhere\b",
        r"\bthis\s+(?:role|position)\s+is\s+(?:fully\s+)?remote\b",
        r"\b(?:role|position)\s+(?:can\s+be|is)\s+(?:fully\s+)?remote\b",
    )
]


def reclassify_hybrid_roles(roles: list[Role], log: bool = False) -> int:
    """In-place reclassification of arrangement classification.

    Three buckets of behavior:

      A) On-site roles (already classified): try to upgrade to Hybrid via
           1. 3+ distinct cities in location text (multi-office roles are
              functionally hybrid; nobody is on-site in 3 cities at once).
           2. JD body matches any _HYBRID_JD_SIGNALS regex (e.g., "3 days
              in office", "flexible work arrangement").

      B) Empty/None location_type (scraper bug, e.g. JSearch returns null
           when both is_remote=false AND city/state are null): try to
           classify based on JD body content. Priority order:
             1. Strong remote claims → Remote
             2. Hybrid signals → Hybrid
             3. Just mentions "remote" → Hybrid (safer default than Remote
                — many JDs mention remote-friendly while being office-based)
             4. Otherwise leave as None (truly unclassifiable)

      C) Already Remote/Hybrid: skip (already classified).

    The fix is centralized here so any scraper that produces empty
    location_type gets the same recovery treatment.
    """
    reclassified = 0
    for r in roles:
        loc_type = (r.location_type or "").strip().lower()

        # Bucket C: already definitive — skip
        if loc_type in ("remote", "hybrid"):
            continue

        jd = (r.job_description_full or "")[:5000]

        # Bucket A: On-site → Hybrid heuristics (existing logic)
        if loc_type == "on-site":
            if _count_distinct_cities(r.location or "") >= 3:
                r.location_type = "Hybrid"
                reclassified += 1
                continue
            if jd and any(p.search(jd) for p in _HYBRID_JD_SIGNALS):
                r.location_type = "Hybrid"
                reclassified += 1
            continue

        # Bucket B: Empty/None — try to classify via JD body
        if not jd:
            continue  # nothing to scan, leave as None

        # Strong remote signal → Remote
        if any(p.search(jd) for p in _REMOTE_STRONG_SIGNALS):
            r.location_type = "Remote"
            reclassified += 1
            continue
        # Multi-city in location field → Hybrid
        if _count_distinct_cities(r.location or "") >= 3:
            r.location_type = "Hybrid"
            reclassified += 1
            continue
        # Hybrid JD signals → Hybrid
        if any(p.search(jd) for p in _HYBRID_JD_SIGNALS):
            r.location_type = "Hybrid"
            reclassified += 1
            continue
        # Bare "remote" mention in JD → Hybrid (conservative default —
        # many JDs say "remote-friendly" but actually want office presence)
        if re.search(r"\bremote\b", jd, re.I):
            r.location_type = "Hybrid"
            reclassified += 1
            continue
        # Bare "on-site" or "in office" mention → On-site
        if re.search(r"\b(?:on[- ]site|in[- ]office)\b", jd, re.I):
            r.location_type = "On-site"
            reclassified += 1
            continue
        # Truly unclassifiable — leave as None for genuine "we don't know"

    if log and reclassified > 0:
        # ASCII-only print — Windows cp1252 console can't encode Unicode
        # arrows, and the bundled backend.exe runs under whatever locale
        # the user's system uses. Don't risk it.
        print(f"[reclassify] reclassified {reclassified} roles (on-site -> hybrid + empty -> remote/hybrid/on-site)")
    return reclassified


def _is_non_us_remote(haystack: str) -> bool:
    """Return True if a 'remote' location string mentions a non-US locale.

    Conservative — only fires on explicit non-US tokens. 'Remote' alone or
    'US Remote' or 'Remote — anywhere' all return False (i.e., admitted).
    """
    if not haystack:
        return False
    return bool(_NON_US_REMOTE_PATTERN.search(haystack))


# ----------------------------------------------------------------------------
# Title-keyword exclusion filter (DYNAMIC per-candidate)
# ----------------------------------------------------------------------------
# Drops roles whose title contains any candidate-specific exclusion token.
# The token list comes from TWO sources:
#   1. profile.excluded_title_patterns — LLM-generated by the profile builder
#      based on resume + targets ("if title contains 'engineer' for a non-eng
#      candidate, drop"). This is the primary mechanism — fully dynamic.
#   2. profile.negative_signals — derived from freeform-context "avoid X"
#      statements. Phrase-level — match if ALL meaningful tokens appear.
#
# Match style:
#   - Whole-word, case-insensitive, against role.job_title only (not JD).
#   - "engineer" pattern drops "Senior Engineer" but NOT "Engagement Manager"
#     or "Engineering Manager" (the title pattern is just the noun form).
#   - Multi-word patterns require all words present in title (any order).

# Stopwords: ignored when extracting matchable tokens from a phrase.
_PHRASE_STOPWORDS = frozenset({
    "the", "and", "or", "of", "for", "to", "from", "with", "that", "than",
    "but", "not", "i'm", "im", "any", "all", "no", "a", "an", "in", "on",
    "at", "by", "as", "is", "are", "be", "been", "do", "does", "did",
    "have", "has", "had", "want", "wants", "wanted", "avoid", "avoiding",
    "moving", "away", "out", "leave", "leaving", "tired", "sick", "hate",
    "dislike", "prefer", "preferred", "rather", "instead",
})


def _match_phrase_in_title(phrase: str, title_lower: str) -> bool:
    """Return True if ALL meaningful tokens of `phrase` appear as whole words
    in `title_lower`. Single-word pattern "engineer" matches only one token.
    Multi-word "ML engineer" needs both "ml" AND "engineer" present (any order).

    Tokens are matched with an optional 's' or 'ing' suffix so the pattern
    "engineer" also catches "engineers" and "engineering" — addresses the
    audit case where "Engineering Manager" should have been caught by an
    "engineer" exclusion but wasn't (whole-word boundary mismatch). The
    suffix expansion is conservative: 's' (plural) and 'ing' (gerund) cover
    the vast majority of cases without introducing false positives.
    """
    tokens = re.findall(r"[a-z][a-z+/\-]{1,}", phrase.lower())
    meaningful = [t for t in tokens if t not in _PHRASE_STOPWORDS and len(t) > 1]
    if not meaningful:
        return False
    return all(
        re.search(r"\b" + re.escape(t) + r"(?:s|ing)?\b", title_lower)
        for t in meaningful
    )


def passes_title_function_match(
    role: Role,
    *,
    profile: CandidateProfile,
) -> tuple[bool, Optional[str]]:
    """Hard-filter check: does this role's title represent a function the
    candidate wants? Returns (passes, reason_if_dropped).

    Drops the role if:
      1. Title matches any pattern in profile.excluded_title_patterns
         (LLM-generated, per-candidate, replaces hardcoded technical patterns).
      2. Title matches any phrase in profile.negative_signals (freeform-derived).
    """
    title = role.job_title or ""
    if not title.strip():
        return True, None

    title_lower = title.lower()

    # Per-candidate excluded title patterns (the dynamic list)
    for pattern in profile.excluded_title_patterns:
        if not pattern or not pattern.strip():
            continue
        if _match_phrase_in_title(pattern, title_lower):
            return False, f"excluded_title: {pattern[:40]}"

    # Negative-signal title match (freeform-derived)
    for sig in profile.negative_signals:
        if not sig or not sig.strip():
            continue
        if _match_phrase_in_title(sig, title_lower):
            return False, f"negative_signal: {sig[:60]}"

    return True, None

def passes_location(
    role: Role,
    *,
    work_arrangements: list[str],
    acceptable_locations: list[str],
    excluded_locations: list[str],
    acceptable_location_radii: Optional[list[int]] = None,
) -> bool:
    """Match role's location against profile preferences.

    Philosophy: BE GENEROUS. When in doubt, include. Better to score a few
    extra non-matches and let the LLM judge than to silently drop real
    matches because of a string mismatch.

    Logic:
      - Always include Remote roles (location-agnostic)
      - For hybrid/on-site, match by:
          1. Direct substring (Richmond, VA in "Richmond, VA")
          2. State overlap (acceptable "Richmond VA" → role "Henrico, VA")
          3. Metro alias (Richmond → Henrico, Chesterfield, Glen Allen, ...)
      - Only EXCLUDE if location matches the excluded_locations list
      - Roles with unclear/missing location → INCLUDE (let LLM judge)
    """
    arrangements = {a.lower() for a in work_arrangements}
    accepts = [l.lower().strip() for l in acceptable_locations]
    excludes = [l.lower().strip() for l in excluded_locations]

    loc_type = (role.location_type or "").lower()
    loc_text = (role.location or "").lower()
    city = (role.city or "").lower()
    state = (role.state or "").lower()
    haystack = f"{loc_text} {city} {state}".strip()
    # v0.2.0: strip periods from haystack so "D.C." matches the same
    # checks that handle "DC". Without this, locations like
    # "Washington, D.C." failed the ", dc" / " dc" suffix tests due
    # to the period inside the abbreviation. Period-stripping is safe
    # for location strings — "St. Louis" still works because we don't
    # use "St" alone as a search token (it's always part of a larger
    # haystack scan).
    haystack = haystack.replace(".", "")

    # Non-US guard runs FIRST (before any early returns) against a title-
    # augmented haystack so role titles like "Strategic AI adoption lead (UK)"
    # are caught even when location data is missing. Several scrapers
    # (Ashby/Workday) leave location=None when the country is only embedded
    # in the title parenthetical — those roles previously slipped through
    # via the empty-haystack early return below.
    # We use a SEPARATE haystack here (only for this non-US check) to avoid
    # false-positive city/state matches in the inclusion logic below
    # ("VP, New York Sales" would otherwise match the NY metro).
    title_lower = (role.job_title or "").lower()
    non_us_haystack = f"{haystack} {title_lower}".strip()
    if _is_non_us_remote(non_us_haystack):
        return False

    if not haystack.strip():
        # Missing location data — be stricter for arrangements that require
        # physical presence. Hybrid and on-site roles need a verifiable
        # metro match; without one we'd be passing through roles the user
        # can't actually take. Remote/unknown still passes through to let
        # the LLM judge based on JD content.
        #
        # v0.1.8: rejected hybrid/on-site with empty haystack outright.
        # v0.2.0 softening: before rejecting, scan the JD body for the
        # user's accepted locations. Some scrapers correctly tag
        # location_type but leave structured location data empty; the
        # city often appears in the JD prose. If we find a Richmond/DC
        # mention in the JD, accept the role.
        if "hybrid" in loc_type or "on-site" in loc_type or "onsite" in loc_type:
            jd_text = (role.job_description_full or "").lower()
            if jd_text and accepts:
                jd_sample = jd_text[:8000]  # cap scan to first 8K chars
                # Direct accept match
                for accept in accepts:
                    if accept in jd_sample:
                        return True
                # Metro alias expansion (Richmond → Henrico, DC → Arlington, etc.)
                for term in _expand_to_metro_terms(accepts):
                    if term in jd_sample:
                        return True
                # State name match (broader)
                for code in _extract_states(accepts):
                    state_name = _STATE_NAMES.get(code, "")
                    if state_name and state_name in jd_sample:
                        return True
            return False
        return True

    # Excluded locations — only this can hard-reject
    if excludes:
        for ex in excludes:
            if ex and ex in haystack:
                return False

    # Remote roles pass when user accepts remote.
    # The non-US country guard above already ran; if we reached here, the
    # role is not flagged as a non-US remote.
    if "remote" in loc_type or "remote" in loc_text:
        if not ("remote" in arrangements or not arrangements):
            return False
        return True

    # If user didn't specify any acceptable locations, accept everything
    if not accepts:
        # Just respect arrangement preference if any
        if "hybrid" in loc_type and "hybrid" not in arrangements and arrangements:
            return False
        return True

    # 1. Real geocoded distance check — most accurate when both locations geocode.
    #    User added "Richmond VA, 50mi" + role at "Hanover, VA" = ~16mi → match.
    radii = acceptable_location_radii or []
    for idx, accept in enumerate(accepts):
        radius = radii[idx] if idx < len(radii) else 50
        within = is_within_radius(haystack, accept, float(radius))
        if within is True:
            return True
        # within is False → keep checking other acceptable locations
        # within is None → couldn't geocode, fall through to substring/state logic

    # 2. Direct substring (handles cases where role's location text matches exactly
    #    even if geocoding failed)
    for accept in accepts:
        if accept in haystack:
            return True

    # 3. Metro alias expansion (suburbs map to their metro)
    accept_metro_terms = _expand_to_metro_terms(accepts)
    for term in accept_metro_terms:
        if term in haystack:
            return True

    # 4. State overlap — if role's state matches any acceptable state, include
    accept_states = _extract_states(accepts)
    for st_code in accept_states:
        if (
            f", {st_code}" in haystack or
            f" {st_code} " in haystack or
            haystack.endswith(f" {st_code}") or
            haystack.endswith(f", {st_code}") or
            _STATE_NAMES.get(st_code, "") in haystack
        ):
            return True

    return False


# ----------------------------------------------------------------------------
# Metro / state lookups (small, hand-curated for v1)
# ----------------------------------------------------------------------------

# Common US state name → 2-letter code for haystack matching
_STATE_NAMES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}

# Metro area expansions — when user adds "Richmond VA", we also accept these
# city names that fall in the same metro. Generous on purpose.
_METRO_ALIASES: dict[str, list[str]] = {
    "richmond": ["henrico", "chesterfield", "glen allen", "midlothian", "mechanicsville"],
    "washington": ["arlington", "alexandria", "tysons", "mclean", "fairfax", "reston",
                   "bethesda", "rockville", "silver spring", "dc metro", "northern virginia"],
    "new york": ["jersey city", "newark", "hoboken", "stamford", "white plains", "manhattan",
                 "brooklyn", "queens", "bronx", "long island", "yonkers"],
    "san francisco": ["oakland", "berkeley", "san jose", "palo alto", "mountain view",
                      "sunnyvale", "santa clara", "san mateo", "redwood city", "south bay",
                      "bay area"],
    "los angeles": ["santa monica", "pasadena", "irvine", "long beach", "burbank",
                    "culver city", "venice"],
    "boston": ["cambridge", "waltham", "somerville", "brookline", "newton"],
    "seattle": ["bellevue", "redmond", "kirkland", "tacoma"],
    "chicago": ["evanston", "schaumburg", "naperville", "oak park"],
    "austin": ["round rock", "cedar park", "georgetown"],
    "atlanta": ["alpharetta", "marietta", "decatur", "sandy springs"],
    "dallas": ["plano", "frisco", "irving", "fort worth", "arlington"],
    "denver": ["boulder", "aurora", "lakewood"],
    "miami": ["fort lauderdale", "coral gables", "doral"],
    "philadelphia": ["king of prussia", "conshohocken"],
}


def _extract_states(accepts: list[str]) -> set[str]:
    """Pull 2-letter state codes from acceptable_locations.

    v0.2.0 fix: previously, "washington dc" was extracted as the WA
    state (Washington) because the function checked `name in a`
    indiscriminately, and dict iteration order happens to hit "wa" :
    "washington" before "dc" : "district of columbia". Result: every
    Bellevue/Seattle/Spokane WA role passed the state overlap check
    for users targeting DC.

    Fix: two-pass extraction. First check for explicit 2-letter state
    suffix ("richmond va" → va, "washington dc" → dc, "Spokane WA" →
    wa). Only if NO suffix match found, fall back to full-state-name
    matching (e.g., "Olympia" → wa via the name "washington"). This
    prioritizes the more specific signal (suffix) over the looser one
    (name substring) and avoids the "washington dc" → wa misclass.
    """
    states: set[str] = set()
    for a in accepts:
        a = a.lower().strip()
        # First pass: explicit 2-letter code at the end of the string
        # (most reliable — users overwhelmingly use this format).
        matched = False
        for code in _STATE_NAMES.keys():
            if (
                a.endswith(f" {code}") or
                a.endswith(f", {code}") or
                a == code
            ):
                states.add(code)
                matched = True
                break
        if matched:
            continue
        # Second pass: state name substring match (handles "Olympia,
        # Washington" → wa). Only runs if no suffix match found, so
        # "washington dc" already exited the loop with dc above.
        for code, name in _STATE_NAMES.items():
            if name in a:
                states.add(code)
                break
    return states


def _expand_to_metro_terms(accepts: list[str]) -> set[str]:
    """For each acceptable city, return the full set of metro-included terms.

    v0.2.0 fix: when the metro_city name is ambiguous with a US state
    name (e.g., "washington" — both DC the city AND WA the state),
    we do NOT add the bare metro_city to the terms set. Adding it
    leaked Washington-STATE roles ("Bellevue, Washington",
    "Tukwila, Washington", "Redmond, Washington") into a user's
    qualifying list when their accept was "Washington DC", because the
    haystack contains the spelled-out state name. The direct-accept
    substring check in passes_location step 2 still handles exact
    "Washington DC" matches; the state-overlap check in step 4 handles
    "Washington, D.C." style strings via the 'district of columbia'
    state-name lookup. So we don't lose any legitimate matches by
    skipping the bare-name add — we just stop the leak.
    """
    # State names that overlap with metro keys — never add as a bare term.
    AMBIGUOUS_STATE_NAMES = {"washington"}
    terms: set[str] = set()
    for a in accepts:
        a_lower = a.lower()
        for metro_city, suburbs in _METRO_ALIASES.items():
            if metro_city in a_lower:
                terms.update(suburbs)
                if metro_city not in AMBIGUOUS_STATE_NAMES:
                    terms.add(metro_city)
    return terms


# ----------------------------------------------------------------------------
# Posted date freshness
# ----------------------------------------------------------------------------

def passes_posted_date(
    role: Role,
    *,
    max_age_days: int = 30,
) -> bool:
    """Drop roles older than `max_age_days`. Roles with no date pass."""
    if not role.posted_date:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    posted = _parse_date_lenient(role.posted_date)
    if posted is None:
        return True
    return posted >= cutoff


def _parse_date_lenient(raw: str) -> Optional[datetime]:
    """Try several common date formats."""
    if not raw:
        return None
    raw = str(raw).strip()
    # Unix timestamp (ms) — Lever
    if raw.isdigit():
        try:
            ts = int(raw)
            if ts > 10_000_000_000:   # ms
                return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    # ISO 8601 — assume UTC if no tz info (avoids naive/aware comparison errors)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        pass
    # Common YYYY-MM-DD
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


# ----------------------------------------------------------------------------
# Already-applied filter
# ----------------------------------------------------------------------------

def passes_not_applied(
    role: Role,
    *,
    applied_keys: set[tuple[str, str]],
) -> bool:
    """applied_keys is a set of (company.lower(), title.lower()) tuples.
    Returns True if NOT in the set."""
    key = (
        (role.company or "").strip().lower(),
        (role.job_title or "").strip().lower(),
    )
    return key not in applied_keys


# ----------------------------------------------------------------------------
# Combined runner
# ----------------------------------------------------------------------------

def apply_hard_filters(
    roles: Iterable[Role],
    *,
    profile: CandidateProfile,
    max_age_days: int = 30,
    applied_keys: Optional[set[tuple[str, str]]] = None,
    hard_salary_floor: bool = False,
    per_company_cap: int = 0,  # 0 = disabled, all roles enter scoring
    log: bool = True,
) -> list[Role]:
    """Run all hard filters in sequence. Returns surviving roles + a brief log.

    Reads filter parameters from the candidate profile:
      - salary_minimum
      - work_arrangements
      - acceptable_locations
      - excluded_locations

    Per-company cap (default 0 = DISABLED): historically capped roles
    per-company before scoring to prevent monopolizers (Snorkel, Cresta)
    from dominating the API budget. Empirically this dropped real STRONG
    matches: Phase A regression with cap=8 lost half of Ziad's STRONG+GOOD;
    cap=15 still lost Snorkel's 16th+ relevant role even when it was a
    legitimate STRONG match.

    New strategy (2026-05-03): score EVERY hard-filter survivor regardless
    of company. Apply diversity caps at the DASHBOARD/EXPORT layer (where
    we already have a 3-per-company display cap behind "show more"). Yes,
    this scores 10-20% more roles per run (~$0.02-0.05 extra), but it
    captures real signal we'd otherwise lose.

    Set per_company_cap > 0 to re-enable hard-filter capping (not recommended
    given current employer-pool size).
    """
    applied_keys = applied_keys or set()
    surviving: list[Role] = []
    counts = {
        "input": 0,
        "dropped_incomplete": 0,
        "dropped_title_function": 0,
        "dropped_salary": 0,
        "dropped_location": 0,
        "dropped_posted_date": 0,
        "dropped_already_applied": 0,
        "dropped_company_cap": 0,
        "kept": 0,
    }
    title_function_examples: list[str] = []

    for role in roles:
        counts["input"] += 1

        # Incomplete-entry exclusion (title or URL missing)
        if not (role.job_title or "").strip() or not (role.job_url or "").strip():
            counts["dropped_incomplete"] += 1
            continue

        # Title-function match (drops roles whose function doesn't match the
        # candidate's profile — e.g. "Solutions Architect" for a non-engineer).
        passes_func, drop_reason = passes_title_function_match(role, profile=profile)
        if not passes_func:
            counts["dropped_title_function"] += 1
            if log and len(title_function_examples) < 3:
                title_function_examples.append(
                    f"{role.company} — {role.job_title} ({drop_reason})"
                )
            continue

        # Salary
        if profile.salary_minimum:
            if not passes_salary_floor(
                role, minimum=profile.salary_minimum, hard_floor=hard_salary_floor
            ):
                counts["dropped_salary"] += 1
                continue

        # Location
        if profile.work_arrangements or profile.acceptable_locations:
            if not passes_location(
                role,
                work_arrangements=profile.work_arrangements or [],
                acceptable_locations=profile.acceptable_locations or [],
                acceptable_location_radii=profile.acceptable_location_radii or [],
                excluded_locations=profile.excluded_locations or [],
            ):
                counts["dropped_location"] += 1
                continue

        # Posted date
        if not passes_posted_date(role, max_age_days=max_age_days):
            counts["dropped_posted_date"] += 1
            continue

        # Already-applied
        if not passes_not_applied(role, applied_keys=applied_keys):
            counts["dropped_already_applied"] += 1
            continue

        counts["kept"] += 1
        surviving.append(role)

    # Per-company cap — applied after all other filters. Sort each company's
    # roles by posted_date desc (freshest first), then keep the top N.
    if per_company_cap and per_company_cap > 0 and surviving:
        from collections import defaultdict

        def _date_key(r: Role) -> str:
            # Sort newest first; missing dates sort last.
            return r.posted_date or ""

        by_company: dict[str, list[Role]] = defaultdict(list)
        for r in surviving:
            key = (r.company or "").strip().lower()
            by_company[key].append(r)

        capped: list[Role] = []
        for key, group in by_company.items():
            group_sorted = sorted(group, key=_date_key, reverse=True)
            kept = group_sorted[:per_company_cap]
            dropped = len(group_sorted) - len(kept)
            if dropped > 0:
                counts["dropped_company_cap"] += dropped
            capped.extend(kept)
        surviving = capped
        counts["kept"] = len(surviving)

    if log:
        print(f"[hard_filters] {counts['input']} input >> {counts['kept']} kept "
              f"(incomplete={counts['dropped_incomplete']}, "
              f"title_function={counts['dropped_title_function']}, "
              f"salary={counts['dropped_salary']}, "
              f"location={counts['dropped_location']}, "
              f"stale={counts['dropped_posted_date']}, "
              f"already_applied={counts['dropped_already_applied']}, "
              f"company_cap={counts['dropped_company_cap']})")
        if title_function_examples:
            print("[hard_filters] title_function drops (sample): " +
                  " | ".join(title_function_examples))

    return surviving

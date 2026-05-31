"""Scoring guardrails (v0.3.24, FIX 29) — anti-inflation safeguards.

The 2026-05-30 adversarial scoring audit found a systematic, one-directional
over-scoring bias (56.7% tier calibration, ALL 14 disagreements "too high").
Three reproducible mechanisms, addressed here + in stage3_deep_eval.py:

  P0-1  Stage 3 hallucinated confident rationales on roles whose JD body was
        actually an SPA-redirect / "enable JavaScript" stub (zero real
        content), boosting scores +28 to +44. → jd_is_capturable() detects
        stubs so the caller can cap the tier instead of scoring fiction.

  P0-2  Large Stage2→Stage3 positive jumps with no JD evidence are a
        hallucination signal. → the caller clamps the delta when the JD is
        not capturable.

  P0-3  Excluded work (hands-on engineering, sales/GTM) sails through the
        TITLE filter because "Consultant"/"Lead"/"Enablement Manager" titles
        evade the pattern list — but the JD BODY is the excluded work. →
        scan_excluded_body_signals() flags body-level excluded duties.

These are pure, deterministic, unit-tested helpers (no LLM, no network) so
they can run inline in the scoring path with zero added cost/latency.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# P0-1: JD capturability — detect stub / redirect / bot-wall JD bodies
# ---------------------------------------------------------------------------

# Markers that indicate the "JD" is actually a JS shell, redirect, or block
# page rather than real posting content. Case-insensitive substring match.
_STUB_MARKERS = (
    "enable javascript",
    "please enable",
    "you need to enable",
    "javascript is required",
    "javascript to run",
    "redirecting",
    "please wait while",
    "loading…",
    "access denied",
    "are you a robot",
    "verify you are human",
    "captcha",
    "page not found",
    "404 not found",
    "this page isn't working",
    "temporarily unavailable",
    "enable cookies",
    "turn on javascript",
)

# Minimum real-content length. Below this, a JD can't support a confident
# deep-eval score — there isn't enough substance to judge fit. 250 chars is
# ~40 words; real JDs run 1,500-8,000 chars. Tuned to catch stubs without
# tripping on the occasional terse-but-real listing (those land 250-500).
_MIN_JD_CHARS = 250


def jd_is_capturable(jd_text: Optional[str]) -> bool:
    """Return True if jd_text looks like a REAL job description body.

    False when it is empty, a stub/redirect/bot-wall, or too short to
    support a deep-eval score. Callers should cap the tier (e.g. at MAYBE)
    and flag for re-scrape rather than let Stage 3 hallucinate a rationale.
    """
    if not jd_text:
        return False
    text = jd_text.strip()
    if len(text) < _MIN_JD_CHARS:
        return False
    low = text.lower()
    # If a stub marker dominates a short-ish body, it's a shell page.
    for marker in _STUB_MARKERS:
        if marker in low:
            # A real 5,000-char JD that merely mentions "captcha" in passing
            # shouldn't be killed — only treat the marker as fatal when the
            # body is short (stub pages are short AND contain the marker).
            if len(text) < 1200:
                return False
    return True


# ---------------------------------------------------------------------------
# P0-3: JD-body excluded-work scan (title-filter blind spot)
# ---------------------------------------------------------------------------

# Hands-on engineering / build work the candidate's profile excludes.
# Split into HIGH-confidence (a single hit is decisive — these phrases only
# appear in real build roles) and GENERIC (need >=2, since one could be a
# passing mention in an advisory role).
_ENG_HIGH = (
    r"\bsdlc\b",
    r"full[\-\s]?stack",
    r"\b(?:c#|asp\.net|\.net core|react|node\.js|angular|java\b|golang|c\+\+|typescript)\b",
    r"\bback[\-\s]?end\b|\bfront[\-\s]?end\b",
    r"writing production code",
    r"hands[\-\s]?on (?:senior |lead |principal )?(?:software )?(?:architect|engineer|builder|developer)",
    r"\byears? (?:of )?(?:software|hands[\-\s]?on) (?:development|engineering|coding)\b",
    r"\bproficien\w+ (?:in|with) (?:python|java|c#|react|sql|azure|aws)\b",
)
_ENG_GENERIC = (
    r"hands[\-\s]?on (?:software|engineering|development|coding|ml|machine learning|expertise|experience)",
    r"\b(?:design|build|develop|implement|deliver) (?:and \w+ )?(?:ai/ml |ml |software |the )?(?:solutions|components|features|services|pipelines|models|applications)\b",
    r"\bsoftware development\b",
    r"engineering (?:practices|best practices|standards|excellence)",
    r"with minimal oversight",
    r"\bmodern (?:frameworks|tools|engineering)\b",
)

# Sales / GTM work the candidate excludes. The "enablement" false-friend:
# the title says enablement but the duties are enabling SELLERS.
_SALES_HIGH = (
    # Decisive: these only appear when the role's substance is selling.
    # NOTE: bare "sales/gtm enablement" is intentionally NOT here — it's the
    # exact false-friend word that collides with the candidate's legit "AI
    # Enablement" target, so it requires corroboration (moved to GENERIC).
    r"\b(?:quota|book of business|pipeline generation|deal[\-\s]?quality|revenue target|net new revenue)\b",
    r"\b(?:pre[\-\s]?sales|solutions? engineering|sales engineering)\b",
    r"\bobjection handling\b",
)
_SALES_GENERIC = (
    r"\b(?:sales|gtm|go[\-\s]?to[\-\s]?market) enablement\b",
    r"\benable (?:the |our )?(?:sales|sellers|reps|account executives|ae'?s|gtm|field)\b",
    r"\bcoach (?:reps|sellers|the sales|account)\b",
    r"\bclose deals\b|\bclosing deals\b",
    r"\bdemo(?:s|ing)? to prospects\b",
    r"\b(?:seller|rep) (?:productivity|onboarding|ramp)\b",
)

_ENG_HIGH_RE = [re.compile(p, re.IGNORECASE) for p in _ENG_HIGH]
_ENG_GEN_RE = [re.compile(p, re.IGNORECASE) for p in _ENG_GENERIC]
_SALES_HIGH_RE = [re.compile(p, re.IGNORECASE) for p in _SALES_HIGH]
_SALES_GEN_RE = [re.compile(p, re.IGNORECASE) for p in _SALES_GENERIC]


def scan_excluded_body_signals(jd_text: Optional[str]) -> dict:
    """Scan a JD body for excluded-work signals the title filter misses.

    Returns {"engineering": [hits], "sales": [hits], "strong": bool}.
    `strong` (the down-tier trigger) is True when EITHER:
      - any HIGH-confidence signal fires (decisive phrases only seen in real
        build / sales roles, e.g. "C#/ASP.NET", "SDLC", "sales enablement",
        "quota"), OR
      - >=2 distinct GENERIC signals of one category fire.
    Scans the first ~4500 chars where core-duty language lives. One stray
    generic phrase ("we partner with engineers") will NOT down-tier.
    """
    out = {"engineering": [], "sales": [], "strong": False}
    if not jd_text:
        return out
    head = jd_text[:4500]

    def hits(regexes):
        found = []
        for rx in regexes:
            m = rx.search(head)
            if m:
                found.append(m.group(0)[:60])
        return sorted(set(found))

    eng_high = hits(_ENG_HIGH_RE)
    eng_gen = hits(_ENG_GEN_RE)
    sales_high = hits(_SALES_HIGH_RE)
    sales_gen = hits(_SALES_GEN_RE)

    out["engineering"] = sorted(set(eng_high + eng_gen))
    out["sales"] = sorted(set(sales_high + sales_gen))
    eng_strong = bool(eng_high) or len(eng_gen) >= 2
    sales_strong = bool(sales_high) or len(sales_gen) >= 2
    out["strong"] = eng_strong or sales_strong
    return out

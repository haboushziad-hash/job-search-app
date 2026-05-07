"""Code-level title-headline overlap floor (v0.3.4).

Belt-and-suspenders enforcement of the prompt-level title floor (PRINCIPLE 8 in
Stage 2 + Stage 3 system prompts). The prompt rule asks the LLM to apply a
graduated floor based on how many CONTENT words from the role title also
appear in the candidate's headline + target_functions:

    3+ content words match → score FLOORS at 70
    2 content words match  → score FLOORS at 65
    1 content word match (and it's a CORE function noun) → FLOORS at 55

The LLM mostly respects this, but audit data shows occasional violations —
e.g., a role like "Marketing Operations Manager" for an Operations-headlined
candidate landing at 47 (below the 55 floor) when the LLM gets distracted by
domain concerns. Code-level enforcement raises any score that falls below
the applicable floor and tags the role's reasoning so the adjustment is
auditable.

Why graduated (matching prompt): the prior binary 3-word floor failed on
1-2 word overlaps that ARE legitimate strong matches ("Operations Manager"
for Operations-headlined candidate is 1 content word but a strong match —
"Manager" doesn't count, "Operations" is core).
"""
from __future__ import annotations

import re
from typing import Iterable


# Words that don't count as "content" for overlap purposes. Connectives,
# articles, prepositions, plus pure seniority/level markers and generic
# role nouns that almost every title uses.
_FILLER_WORDS: frozenset[str] = frozenset({
    # Connectives / articles / prepositions
    "and", "or", "of", "the", "for", "a", "an", "to", "with", "in", "on",
    "at", "by", "as", "from", "into",
    # Seniority / level markers
    "manager", "lead", "senior", "junior", "associate", "principal",
    "staff", "head", "chief", "director", "vp", "evp", "svp",
    # Roman numerals + level suffixes
    "i", "ii", "iii", "iv", "v",
    # Punctuation-survivors
    "amp",  # from & in titles
})


def content_words(text: str | None) -> set[str]:
    """Extract substantive content words. Lowercases, strips punctuation,
    filters filler words. Returns a set of unique tokens with length >= 2."""
    if not text:
        return set()
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    return {t for t in tokens if t not in _FILLER_WORDS and len(t) >= 2}


def compute_title_floor(
    *,
    role_title: str | None,
    candidate_headline: str | None,
    candidate_target_functions: Iterable[str] | None,
) -> tuple[int, int, list[str]]:
    """Compute the applicable title-headline overlap floor.

    Returns: (floor_score, overlap_count, overlap_words)
      floor_score: minimum allowed score, 0 if no floor applies
      overlap_count: number of content words shared
      overlap_words: the actual overlap (for reasoning text)

    Rules (matching PRINCIPLE 8 in scoring prompts):
      3+ content words shared → 70
      2 content words shared  → 65
      1 content word shared AND it's a CORE function noun (i.e., appears
        in candidate's headline OR is the first content word of any
        target_function) → 55
      otherwise → 0 (no floor — function/JD signals dominate)
    """
    role_words = content_words(role_title)
    if not role_words:
        return 0, 0, []

    head_words = content_words(candidate_headline)
    func_text = " ".join(candidate_target_functions or [])
    func_words = content_words(func_text)

    candidate_words = head_words | func_words
    if not candidate_words:
        return 0, 0, []

    overlap = role_words & candidate_words
    n = len(overlap)

    if n >= 3:
        return 70, n, sorted(overlap)
    if n == 2:
        return 65, n, sorted(overlap)
    if n == 1:
        # Single-word case: it must be a CORE function noun.
        # Heuristic: appears in headline directly, OR is the first content
        # word of any target_function (the lead noun of "Marketing Operations"
        # is "Marketing", lead noun of "Quality Assurance" is "Quality").
        core_words: set[str] = set(head_words)
        for tf in (candidate_target_functions or []):
            tf_words = content_words(tf)
            if tf_words:
                # First content word of the target_function string
                first = re.findall(r"[a-zA-Z]+", tf.lower())
                for w in first:
                    if w in tf_words:
                        core_words.add(w)
                        break
        if overlap & core_words:
            return 55, n, sorted(overlap)

    return 0, n, sorted(overlap)


def apply_floor_to_score(
    *,
    score: int,
    role_title: str | None,
    candidate_headline: str | None,
    candidate_target_functions: Iterable[str] | None,
) -> tuple[int, str | None]:
    """Apply title floor to a score. Returns (adjusted_score, reason_tag).

    `reason_tag` is None when no adjustment was made, or a short string like
    "[title-floor:55,overlap=operations]" when the floor was applied. The tag
    is intended to be prepended to the role's reasoning text so the
    adjustment is auditable in the dashboard.
    """
    floor, n, overlap = compute_title_floor(
        role_title=role_title,
        candidate_headline=candidate_headline,
        candidate_target_functions=candidate_target_functions,
    )
    if floor > 0 and score < floor:
        overlap_str = ",".join(overlap[:3])
        return floor, f"[title-floor:{floor},overlap={overlap_str}]"
    return score, None

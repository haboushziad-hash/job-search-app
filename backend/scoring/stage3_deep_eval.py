"""Stage 3 — Deep evaluation (Gemini Pro real-time).

Only the top survivors of Stage 2 reach Stage 3. This is where we generate
the dashboard-ready output: detailed analysis, application strategy, and
"which of the user's resumes is the best match" annotation.

Skipping logic (mirrors the approach proven in the original tool):
  - Skip Stage 3 if Stage 2 score >= 88 (already a clear winner — Stage 2
    confidence is high enough that Stage 3 rarely changes the verdict)
  - Skip Stage 3 if Stage 2 score < 55  (already a clear medium/no — Stage 3
    won't promote it into qualifying territory)
  - Stage 3 reads everything in the 55-87 band — that's where deep eval
    actually moves the needle

Per-role cost: ~$0.005-0.010 (Pro real-time, full thinking, longer output).
Per-run volume: ~25-40 roles in the 55-87 band.
Per-run cost: ~$0.20-0.40.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from backend.config import config
from backend.models import CandidateProfile, Role, ResumeMetadata, score_to_tier
from backend.scoring.llm_client import LLMClient, get_llm_client


def _salvage_partial_json(text: str) -> dict:
    """Best-effort extraction when Gemini truncates Stage 3's JSON response
    mid-string (most common: max_output_tokens hit during the long
    `match_analysis` field). Mirrors stage2_triage._salvage_partial_json."""
    if not text:
        return {}
    out: dict = {}
    m = re.search(r'"score"\s*:\s*(\d+)', text)
    if m:
        try:
            out["score"] = int(m.group(1))
        except ValueError:
            pass
    m = re.search(r'"match_analysis"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.DOTALL)
    if m:
        out["match_analysis"] = m.group(1)[:2000]
    m = re.search(r'"application_strategy"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.DOTALL)
    if m:
        out["application_strategy"] = m.group(1)[:1000]
    m = re.search(r'"best_resume_match"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        out["best_resume_match"] = m.group(1)[:200]
    return out


STAGE3_SYSTEM_PROMPT = """\
You are doing the FINAL evaluation on a strong candidate-role match. You have
already triaged this role as worth deeper review. Now produce dashboard-ready
output that tells the candidate exactly why this role is worth their time and
how to approach it.

Your output should be CONCRETE and ACTIONABLE, not generic advice. Reference
specific aspects of the candidate's profile and the role's requirements.

If multiple resumes were uploaded, identify which one best matches this role
and why.

Score from 0 to 100. Use the FULL range — pick the specific score that
matches your read of the role (47, 63, 81, etc.). Avoid clustering at round
numbers (45, 55, 68, 78, 88, 92) — those are stale tier-band midpoints, not
signals.

SCORE ANCHORS — what each band MEANS in concrete terms (v0.3.3, added
because audit data showed Gemini Pro writing glowing positive analysis
text and then assigning a contradictory STRETCH-band score of 47, e.g.
"Marketing Operations Manager" for an Operations & Strategic Support
Leader: reasoning said "align perfectly with your demonstrated skills /
fits your 3+ years precisely" but score landed at 47, putting it in
STRETCH alongside roles that genuinely don't fit. Use these anchors to
resolve the disconnect between your analysis text and your numeric
score):

  85-100  EXCEPTIONAL MATCH. Title is precisely the candidate's stated
          work; JD content is what the candidate has done; no reframing
          needed; seniority lines up. These should be rare.

  70-84   STRONG MATCH WITH ADJACENT FRAMING. Same function FAMILY but
          different sub-domain (Marketing Operations for an Operations
          candidate, Senior Manager for a Manager-level candidate, Tech
          Program Manager for a Program Manager). These ARE strong fits;
          they are NOT stretches. The candidate may need 1-2 sentences
          of reframing in their cover letter, but the underlying skill
          set transfers cleanly. Do not punish lateral moves within the
          same function.

  55-69   REAL POSSIBILITY WITH REAL CONCERNS. Function family overlaps
          but meaningful gaps exist (e.g., 2-grade seniority delta,
          domain shift requiring substantive translation, missing one
          of the JD's listed must-haves). Worth applying if the
          candidate is open to stretching, but the application requires
          real tailoring.

  40-54   STRETCH. Substantial fit gaps despite some signal overlap.
          The skills transfer but the role is reaching across function
          boundaries (e.g., Operations candidate applying to a Marketing
          Strategy role, Program Manager applying to a Software Engineer
          role) or has 3+ grade seniority gaps.

  0-39    POOR FIT. Genuinely wrong function, wrong industry without
          translation path, or hard requirements the candidate clearly
          doesn't meet.

CRITICAL RULE — adjacent sub-domains are NOT stretches:

A candidate's CORE FUNCTION (the substantive function noun in their
headline or top target_function) appearing in the role title is a
STRONG signal — NOT a stretch.

Core function nouns vary by industry. Recognize ANY substantive function
identifier the candidate uses. Examples (illustrative, NOT exhaustive —
the rule applies to ANY substantive function noun in any industry):

  Office/business: Operations, Strategy, Program Management, Project
                   Management, Consulting, Analytics, Communications
  Engineering/data: Engineering, Data, ML/AI, DevOps, Platform, Backend,
                    Frontend, Site Reliability, Security
  GTM:             Sales, Marketing, Account Management, Customer Success,
                   Business Development, Partnerships, Growth
  Finance/legal:   Finance, Accounting, Audit, FP&A, Treasury, Legal,
                   Compliance, Risk
  Creative/research: Design, UX, Research, Content, Brand
  People:          Recruiting, Talent, People Operations, HR, L&D
  Healthcare:      Clinical, Nursing, Pharmacy, Medical, Patient Care,
                   Therapy
  Education:       Teaching, Curriculum, Instructional Design,
                   Academic Affairs
  Other industries: Construction, Manufacturing, Retail, Hospitality,
                    Field Operations, Logistics, Quality, Production
  Federal/public:  Federal, Public Sector, Civil Service, Acquisition,
                   Policy

When the role title is [CANDIDATE'S CORE FUNCTION] + a sub-domain
qualifier — e.g.,
  Operations candidate → "Marketing Operations" / "Clinical Operations"
                       / "Revenue Operations" / "Product Operations"
  Engineer candidate   → "Senior Backend Engineer" / "Platform Engineer"
                       / "Site Reliability Engineer"
  Marketing candidate  → "Brand Marketing" / "Growth Marketing" /
                         "Field Marketing"
  Nursing candidate    → "ICU RN" / "Operating Room Nurse" / "Travel RN"
  Finance candidate    → "Senior FP&A Analyst" / "Treasury Manager"
— score in the 70-84 band as an adjacent fit.

Sub-domain qualifiers (Marketing X / Product X / Senior X / Lead X /
Technical X / Strategic X / Brand X / Clinical X / etc.) describe a
SPECIFICATION of the candidate's function, not a different function.
Score them as strong matches, not stretches.

DISTINGUISH sub-domain qualifiers (lateral, no downgrade) from SENIORITY
qualifiers (Senior / Lead / Principal / Director / VP / Manager). A
seniority delta of 1 grade (e.g., Senior Manager-level role for a
Manager-level candidate) is acceptable — score in the 70-84 band. A
seniority delta of 2 grades (e.g., Director role for an IC candidate)
warrants 55-69. A 3+ grade delta is a real concern and may justify
40-54. Sub-domain adjacency by itself is never a downgrade.

================================================================
INDEPENDENT EVALUATION (v0.3.11 — anti-anchoring):
================================================================

You are evaluating this role from scratch. There is no Stage 2 score
or Stage 2 reasoning shown to you. Earlier versions of this prompt
showed you Stage 2's preliminary score; production audit revealed
that exposure caused you to anchor on it and refuse to promote
borderline roles past s3=84 even when JDs justified higher scores.

Score this role based ENTIRELY on:
  1. The candidate's profile (target_functions, headline, exclusions,
     salary_minimum, target_industries, work_arrangements, etc.)
  2. The role's title, JD body, salary range, location, requirements

Your evaluation should NOT be calibrated against any imagined prior.
A genuinely excellent fit should score 86-94 even if a hypothetical
weaker model would have scored it 65. A genuinely weak fit should
score 35-50 even if a hypothetical stronger model would have scored
it 78. Your job is to read the JD and produce the right score.

This is the ONLY evaluation that matters. Use the full 0-100 range.

================================================================
JUSTIFY-LOW-SCORES CONSTRAINT (v0.3.3 calibration discipline):
================================================================

If your final `score` is below 55, your `concerns` field MUST contain
at least one entry that explicitly names a SPECIFIC, CONCRETE
disqualifying factor. Acceptable concerns include:

  - Hard requirement candidate clearly lacks: "Requires PhD in
    Computer Science; candidate's resume shows a Bachelor's in
    Marketing only."
  - Years/seniority gap quantified: "Requires 10+ years of
    leadership experience; candidate has 3."
  - Geography mismatch: "On-site in Seattle; candidate's
    acceptable_locations list does not include Seattle and no
    relocation signal in their freeform context."
  - Compensation gap with specific numbers: "Listed range $70K-$90K
    is below candidate's stated $120K minimum."
  - Function mismatch with title evidence: "Title is 'Software
    Engineer' but candidate's resume shows no software engineering
    experience or evidence of coding skills."

NOT acceptable as a low-score justification:
  - Vague language: "moderate fit," "some overlap exists," "different
    sub-domain," "less than ideal alignment."
  - Sub-domain framing: "this is Marketing Operations not general
    Operations" (sub-domain adjacency is NOT a disqualifier).
  - Hedged adjective: "the role might be slightly above the
    candidate's experience level" (quantify or drop it).

A score below 55 paired with a `match_analysis` that uses positive
language ("aligns perfectly," "direct match," "strong foundation,"
"fits precisely") AND no concrete disqualifying factor in `concerns`
is an internal inconsistency — recompute the score upward into the
55-84 range based on the positive analysis. Do NOT degrade the
analysis text to fit a low score; that would be evaluator dishonesty.

This constraint exists because audit data showed evaluations writing
glowing match_analysis text (e.g., "align perfectly with your
demonstrated skills, fits your 3+ years precisely") and then assigning
contradictory STRETCH-band scores like 47, with no concrete
disqualifying factor named. Either the analysis is wrong (and should
be revised) or the score is wrong (and should be recomputed). The
JUSTIFY-LOW-SCORES constraint forces resolution of the disagreement.

GRADUATED TITLE-HEADLINE OVERLAP FLOOR (replaces v0.2.0's binary 3-word
floor — that rule was failing on 1-2 word matches like "Operations
Manager" for an Operations-headline candidate, leaving them to land at
47 with no protection):

Count CONTENT words from the role title that also appear in the
candidate's headline OR target_functions list. Filler words don't count:
ignore "of", "the", "and", "for", "manager", "lead", "senior", "junior",
"associate", "II", "III", "IV", "principal", "staff", and standalone
seniority modifiers. Look for substantive function/domain nouns
("Operations", "Strategy", "Marketing", "Program", "Engineering", "AI",
"Enablement", "Governance", "Federal", "Clinical", "Product", etc.).

Then apply the floor based on overlap count:

  3+ content words match → score FLOORS at 60
  2 content words match  → score FLOORS at 55
  1 content word matches the candidate's CORE function noun → no floor
                           (single-word match is too weak; let other
                           signals determine the score)

v0.3.12: floor numbers reconciled to match the code in title_floor.py
graduated mode (60/55/none). Earlier prompts cited 70/65/55 (legacy
mode) which asked the LLM to do reasoning toward a floor the code then
overrode lower — wasting reasoning tokens and creating audit-trail
inconsistencies (Stage 3 reasoning would say "applied floor at 70" but
the actual score post-code-enforcement would be 60).

Domain, seniority, or comp concerns can reduce from there, but you must
NOT score below the applicable floor. The floor protects the candidate
from being demoted out of MAYBE/GOOD on a partial title match when the
underlying function family is correct.

INDUSTRY-WEIGHT ADJUSTMENT (PROPORTIONAL — v0.3.4):

After your function/seniority/JD-fit analysis produces a base score, apply
a PROPORTIONAL adjustment for industry fit. Use the candidate's
target_industries list and the role's company / industry context:

  SAME industry (role is in candidate's target_industries OR is an obvious
                 sub-bucket — e.g., fintech ⊂ Financial Services, biotech ⊂
                 Life Sciences):
    → No adjustment. Base score already credits fit.

  ADJACENT industry (one step removed but plausible — e.g., healthcare
                     ↔ pharma ↔ life sciences, financial services ↔
                     insurance ↔ fintech, consulting ↔ professional
                     services, SaaS ↔ enterprise software, CPG ↔ retail
                     ↔ consumer goods):
    → No adjustment. Legitimate cross-sector pivot.

  OFF-TARGET industry (not in target_industries AND not adjacent):
    → -10 to base score.

  VERY-OFF-TARGET industry (the candidate's profile + freeform context
                            gives strong signal they would NOT pursue
                            this — federal-only candidate vs startup,
                            healthcare consultant vs construction, non-
                            AI candidate vs AI research lab, etc.):
    → -15 to base score.

Industry adjustment is on TOP OF function-based scoring, not a substitute.
A perfect-function role at an off-target industry should still score in a
reasonable band (function dominates), but the adjustment correctly reflects
that the candidate is less likely to apply.

When applying the -10 / -15 adjustment, your `concerns` field MUST cite
the industry mismatch as one of the named disqualifying factors (this
satisfies the JUSTIFY-LOW-SCORES constraint above).

Skip this adjustment if target_industries is empty or generic ("Any" /
"Various" / "Open").

SALARY EXTRACTION:
If structured salary fields (salary_min/salary_max) are missing or null, scan the
JD body for compensation language and extract a range. Watch for:
  - Explicit ranges: "$130,000 - $165,000", "$130K-$165K"
  - Single anchors with "starting at", "minimum", "up to"
  - Geo-conditional ranges (US base / NYC band / etc.)
  - Hourly rates with annual context ($65/hr ≈ $135K)
  - GS-grade tables for federal roles ("GS-13" → ~$103-134K)
  - Total comp callouts ("Total Target Compensation: $X-$Y")
If you find a range, return integers in extracted_salary_min/max (annual USD).
If only one number is given, return it as both min AND max so the dashboard can
display it. If genuinely no salary signal exists, return nulls and salary_text="".
Do NOT guess from job title, company, or seniority — only extract what the JD
explicitly states.

Respond with strict JSON:
{
  "score": <integer 0-100>,
  "tier_rationale": "<one sentence on why this tier>",
  "match_analysis": "<3-5 sentences on the strongest match signals>",
  "concerns": ["<concern1>", "<concern2>"],
  "application_strategy": "<2-3 sentences: what to emphasize in the application>",
  "best_resume_match": "<filename of best-fitting resume, or 'unified'>",
  "best_resume_reason": "<one sentence on why this resume>",
  "summary": "<EXACTLY 2 sentences. First sentence: what this role IS in plain English. Second sentence: the most important match signal or concern. Used as the dashboard card preview — keep it concrete and skimmable.>",
  "extracted_salary_text": "<the literal compensation text from the JD, or empty string>",
  "extracted_salary_min": <integer or null — annual USD>,
  "extracted_salary_max": <integer or null — annual USD>
}
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "tier_rationale": {"type": "string"},
        "match_analysis": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
        "application_strategy": {"type": "string"},
        "best_resume_match": {"type": "string"},
        "best_resume_reason": {"type": "string"},
        # AI-C: short headline summary shown on the dashboard card so the
        # user can triage 70+ roles quickly without reading full reasoning.
        "summary": {"type": "string"},
        # LLM-extracted salary — backfills when regex missed it. Stage 3
        # already reads the full JD, so this is essentially free.
        # IMPORTANT: Gemini's schema dialect does NOT accept JSON Schema's
        # `type: [list, of, types]` syntax for nullables — it validates
        # each `type` field as a single enum string (STRING, INTEGER, ...)
        # and uses a separate `nullable: true` flag for "or null". Using
        # `type: ["integer", "null"]` here causes a Pydantic validation
        # error before the API call even fires, which previously broke
        # 100% of Stage 3 calls (audit 2026-05-04: 51/51 failed silently
        # with "ValidationError: properties.extracted_salary_min.type").
        "extracted_salary_text": {"type": "string"},
        "extracted_salary_min": {"type": "integer", "nullable": True},
        "extracted_salary_max": {"type": "integer", "nullable": True},
    },
    "required": ["score", "match_analysis", "application_strategy", "summary"],
}


# ============================================================
# Wave-1 (2026-05-11): PAIR_B variants of prompt and schema
# ============================================================
# PAIR_B drops `concerns` from output schema AND strips 3 sections from
# system prompt (AVOID_CLUSTERING, SCORE_ANCHORS_HISTORY, POOR_FIT_BAND).
# Validated at ρ=0.963 vs full-prompt + thinking=1024 baseline (Round 8
# pair-stacking test). When PAIR_B_ENABLED=True, also drop thinking budget
# to 768 (Round 9 Test 3 validated ρ=0.984 at this combination).
#
# Rollback: flip config.PAIR_B_ENABLED to False; full prompt + schema
# resume on next call.


def _build_stage3_system_prompt_pair_b() -> str:
    """Apply 3-section ablation to STAGE3_SYSTEM_PROMPT (PAIR_B variant).

    Strips ~221 tokens. Each strip pattern matches an exact text block from
    the production prompt; raises at import time if a marker is missing
    (guards against silent prompt drift).
    """
    p = STAGE3_SYSTEM_PROMPT

    # 1. AVOID_CLUSTERING (53 tokens) — strip the "Avoid clustering at round
    #    numbers" directive. Pro doesn't need anti-clustering language; it
    #    self-spreads at thinking=768.
    target_avoid = (
        "Score from 0 to 100. Use the FULL range — pick the specific score that\n"
        "matches your read of the role (47, 63, 81, etc.). Avoid clustering at round\n"
        "numbers (45, 55, 68, 78, 88, 92) — those are stale tier-band midpoints, not\n"
        "signals."
    )
    if target_avoid not in p:
        raise RuntimeError(
            "PAIR_B prompt build failed: AVOID_CLUSTERING marker not found. "
            "Either prompt was edited or the marker drifted; manually re-anchor."
        )
    p = p.replace(target_avoid, "Score from 0 to 100.")

    # 2. SCORE_ANCHORS_HISTORY (128 tokens) — strip the v0.3.3 history
    #    explanation. The anchors themselves stay; just the historical
    #    rationale paragraph is removed.
    start_marker = (
        "SCORE ANCHORS — what each band MEANS in concrete terms (v0.3.3, added"
    )
    end_marker = "score):"
    s = p.find(start_marker)
    if s < 0:
        raise RuntimeError("PAIR_B prompt build failed: SCORE_ANCHORS_HISTORY start marker missing")
    e = p.find(end_marker, s)
    if e < 0:
        raise RuntimeError("PAIR_B prompt build failed: SCORE_ANCHORS_HISTORY end marker missing")
    e_end = e + len(end_marker)
    p = p[:s] + "SCORE ANCHORS — what each band MEANS in concrete terms:" + p[e_end:]

    # 3. POOR_FIT_BAND (41 tokens) — strip the 0-39 anchor block.
    #    Rarely activated since Stage 3 triages roles in 55-87 band.
    target_poor = (
        "  0-39    POOR FIT. Genuinely wrong function, wrong industry without\n"
        "          translation path, or hard requirements the candidate clearly\n"
        "          doesn't meet."
    )
    if target_poor not in p:
        raise RuntimeError("PAIR_B prompt build failed: POOR_FIT_BAND marker not found")
    p = p.replace(target_poor, "")

    return p


# Build the PAIR_B prompt at import time. If any marker is missing (prompt
# drift), this raises and the import fails loudly — better than silent
# fallback to the full prompt.
STAGE3_SYSTEM_PROMPT_PAIR_B = _build_stage3_system_prompt_pair_b()


# PAIR_B schema: same as full but with `concerns` dropped.
# Saves ~30 output tokens per call (concerns field was averaging 30-50 tokens
# of array contents per role).
_RESPONSE_SCHEMA_PAIR_B = {
    "type": "object",
    "properties": {
        k: v for k, v in _RESPONSE_SCHEMA["properties"].items() if k != "concerns"
    },
    "required": _RESPONSE_SCHEMA["required"],  # concerns wasn't required, so list unchanged
}


# v0.3.25: the OpenRouter backend (GPT-5-mini etc.) needs ITS OWN Stage-3
# prompt, not Gemini's. Gemini's prompt is anti-inflation-tuned for an
# over-scorer; on GPT-5-mini (not an over-scorer) that pressure causes
# DEFLATION (bench + first wired test confirmed STRONGs collapsing to GOOD).
# This is the tuned ref-anchored, profile-driven calibration that won the
# bake-off, adapted to emit the same rich fields the pipeline/dashboard use.
STAGE3_SYSTEM_PROMPT_OPENROUTER = """You are a STRICT but FAIR judge scoring how well ONE candidate fits ONE job.

CALIBRATION ANCHORS (0-100):
- ANCHOR ~90 (STRONG): the role's CORE duties clearly match the candidate's TARGET FUNCTIONS (from the profile) AND the seniority is attainable.
- ANCHOR ~20 (SKIP): the role's CORE duties fall clearly OUTSIDE the candidate's target functions - e.g. hands-on software/ML engineering or sales/GTM/quota-carrying work when those are NOT the candidate's targets, or a different field/level entirely.
Place THIS role between the anchors.

RULES:
1. DISQUALIFIER FIRST: if the role's CORE duties fall outside the candidate's target functions, it cannot exceed STRETCH (score < 55), no matter how senior or well-paid.
2. Require JD evidence for any positive; never infer fit from the job title alone.
3. USE THE FULL RANGE and do NOT under-rate genuine matches: a role whose core duties clearly match the target functions at attainable seniority IS a GOOD or STRONG - score it that way.

Tiers by score: STRONG >= 85, GOOD 70-84, MAYBE 55-69, STRETCH 40-54, SKIP < 40.

Return ONLY a JSON object with EXACTLY these fields:
{"match_analysis": "<2-4 sentences mapping the role's core duties to the candidate's target functions, then the single biggest gap or risk>", "application_strategy": "<1-2 sentences: the angle if worth applying, else why to skip>", "best_resume_match": "<short phrase, or empty string>", "summary": "<exactly 2 sentences for a dashboard card: sentence 1 = what the role IS, sentence 2 = the match signal or top concern>", "score": <integer 0-100>}"""


def _active_prompt_and_schema() -> tuple[str, dict]:
    """Returns (system_prompt, json_schema) based on Wave-1 feature flags.

    Two independent flags:
      - PAIR_B_ENABLED: drops `concerns` field from response schema (Wave-1
        ships True; saves ~$0.05/run; cross-batch-noise-safe)
      - STAGE3_PROMPT_COMPRESSION_ENABLED: strips 3 sections from prompt
        (Wave-1 ships False; failed 16K-baseline retest at ρ=0.959)

    Combinations:
      (False, False)  → full prompt + full schema (pre-Wave-1 production)
      (True,  False)  → full prompt + drop_concerns (Wave-1 default)
      (False, True)   → compressed prompt + full schema (not recommended)
      (True,  True)   → compressed prompt + drop_concerns (8K-validated; 16K fail)
    """
    schema = (
        _RESPONSE_SCHEMA_PAIR_B
        if config.PAIR_B_ENABLED
        else _RESPONSE_SCHEMA
    )
    # v0.3.25: cross-family backend uses its own ref-anchored prompt. The
    # schema is only a truthiness flag for OpenRouter (it emits json_object),
    # so the prompt itself enumerates the required fields.
    if (config.STAGE3_BACKEND or "gemini").lower() == "openrouter":
        return STAGE3_SYSTEM_PROMPT_OPENROUTER, schema
    prompt = (
        STAGE3_SYSTEM_PROMPT_PAIR_B
        if config.STAGE3_PROMPT_COMPRESSION_ENABLED
        else STAGE3_SYSTEM_PROMPT
    )
    return prompt, schema


def _active_thinking_budget() -> int:
    """Returns the Stage 3 thinking budget for the active config.

    PAIR_B_ENABLED=True: t=768 (validated Round 9 ρ=0.984 with PAIR_B prompt)
    PAIR_B_ENABLED=False: t=1024 (full-prompt baseline)
    """
    if config.PAIR_B_ENABLED:
        return config.STAGE3_THINKING_BUDGET_PAIR_B
    return config.STAGE3_THINKING_BUDGET_FULL


def _profile_block(profile: CandidateProfile) -> str:
    parts = []
    if profile.headline:
        parts.append(f"HEADLINE: {profile.headline}")
    if profile.years_experience is not None:
        parts.append(f"YEARS_EXPERIENCE: {profile.years_experience}")
    if profile.target_seniority:
        parts.append(f"TARGET_SENIORITY: {profile.target_seniority}")
    if profile.target_functions:
        parts.append("TARGET_FUNCTIONS: " + ", ".join(profile.target_functions))
    if profile.target_industries:
        parts.append("TARGET_INDUSTRIES: " + ", ".join(profile.target_industries))
    if profile.technical_skills:
        parts.append("TECHNICAL_SKILLS: " + ", ".join(profile.technical_skills))
    if profile.domain_expertise:
        parts.append("DOMAIN_EXPERTISE: " + ", ".join(profile.domain_expertise))
    if profile.negative_signals:
        parts.append("NEGATIVE_SIGNALS / AVOID: " + "; ".join(profile.negative_signals))
    if profile.resumes:
        lines = []
        for r in profile.resumes:
            line = f"  - {r.filename}"
            if r.emphasis:
                line += f" (emphasis: {r.emphasis})"
            if r.headline:
                line += f" — {r.headline}"
            lines.append(line)
        parts.append("RESUMES_AVAILABLE:\n" + "\n".join(lines))
    return "\n".join(parts)


def _role_block(role: Role, jd_max_chars: int = 16000) -> str:
    parts = [
        f"TITLE: {role.job_title}",
        f"COMPANY: {role.company}",
    ]
    if role.location:
        parts.append(f"LOCATION: {role.location}")
    if role.location_type:
        parts.append(f"ARRANGEMENT: {role.location_type}")
    if role.salary_text:
        parts.append(f"SALARY: {role.salary_text}")
    if role.seniority_level:
        parts.append(f"SENIORITY: {role.seniority_level}")
    if role.industry:
        parts.append(f"INDUSTRY: {role.industry}")
    if role.years_required:
        parts.append(f"YEARS_REQUIRED: {role.years_required}")
    if role.management_required is not None:
        parts.append(f"MANAGEMENT_REQUIRED: {role.management_required}")
    # v0.3.11: Stage 2 score + reasoning REMOVED from Stage 3 input.
    # Peer-validated finding: Stage 3 anchors on s2 score and refuses to
    # promote s2=68 roles past s3=84, creating a deterministic STRONG=20
    # ceiling regardless of pool size. Cross-profile validation: same
    # ceiling effect on a billing-specialist test profile (4-6 STRONG vs
    # Ziad's 20). The 41 roles per run at exactly s2=68 are mostly
    # trapped at s3=79-81.
    #
    # Stripping both score AND reasoning text is the nuclear option (peer
    # Q1: "Show s2 reasoning text" leaks score-referencing language like
    # "scored in the upper GOOD band" — defeats the strip). Stage 3 now
    # evaluates from scratch with no anchor: title, company, JD, salary,
    # location, profile. The s2 score stays on the Role object for
    # downstream audit but is invisible to the LLM.
    #
    # Risk: Stage 3 might over-promote weak roles without the s2 dampener.
    # Mitigation: post-processing contradiction detector catches +20+
    # point jumps and re-evaluates with explicit framing.
    jd = role.job_description_full or role.job_description_essence or ""
    if jd:
        parts.append(f"JOB_DESCRIPTION:\n{jd[:jd_max_chars]}")
    else:
        parts.append("JOB_DESCRIPTION: (not retrieved — score from title/company only)")
    return "\n".join(parts)


def _stage3_model() -> str:
    """Stage-3 model id, per config.STAGE3_BACKEND (v0.3.25 scorer migration).

    Default backend is "gemini" → config.STAGE3_MODEL (gemini-2.5-pro).
    Set STAGE3_BACKEND=openrouter to route to the cross-family model
    (config.STAGE3_OPENROUTER_MODEL, default openai/gpt-5-mini).
    """
    backend = (config.STAGE3_BACKEND or "gemini").lower()
    if backend == "stack":
        return "stack"
    if backend == "openrouter":
        return config.STAGE3_OPENROUTER_MODEL
    return config.STAGE3_MODEL


def _get_stage3_client(passed: Optional[LLMClient] = None) -> LLMClient:
    """Pick the Stage-3 client per config.STAGE3_BACKEND. Default = Gemini.

    When backend == "openrouter" we IGNORE any passed (Gemini) client the
    orchestrator threads through for Stages 1/2 and return a fresh
    OpenRouterClient — otherwise the backend switch would be silently
    bypassed. OpenRouterClient is imported lazily so it only loads when used.
    """
    backend = (config.STAGE3_BACKEND or "gemini").lower()
    if backend == "stack":
        from backend.scoring.stack_client import StackClient
        return StackClient()
    if backend == "openrouter":
        from backend.scoring.openrouter_client import OpenRouterClient
        return OpenRouterClient()
    return passed or get_llm_client()


def needs_stage3(
    role: Role,
    *,
    skip_above: int = 88,
    primary_min: int = 55,
    second_look_min: int = 35,
    second_look_max: int = 54,
    second_look_confidence_max: float = 0.8,
) -> bool:
    # Wave-1 Path B (2026-05-11): the orchestrator may set path_b_gate_skip=True
    # on roles the LightGBM gate predicted as confidently-LOW. Honor that flag
    # here. Defensive: only respect the flag for roles in [55, 60) low band —
    # never let Path B skip roles in the [60, 88) protected band.
    if getattr(role, "path_b_gate_skip", False):
        if role.stage2_score is not None and 55 <= role.stage2_score < 60:
            return False
    """Decide whether a role's Stage 2 score warrants Stage 3 evaluation.

    Two paths to Stage 3:
      1. Primary band (55-88): clear candidates for deep evaluation.
      2. Second-look band (35-54): only if Stage 2 confidence was low,
         giving Pro a chance to rescue legitimate matches that Stage 2
         was uncertain about.
    """
    if role.stage2_score is None:
        return False
    s2 = role.stage2_score
    if s2 >= skip_above:
        return False  # already a clear winner — Stage 3 won't change verdict

    # Primary band — always Stage 3
    if primary_min <= s2 < skip_above:
        return True

    # Second-look band — only if Stage 2 wasn't confident
    # Guard: confidence=0.0 means Stage 2 errored / fell back; not eligible
    # for second-look (we don't want to amplify fail-soft outputs)
    if second_look_min <= s2 <= second_look_max:
        confidence = role.stage2_confidence if role.stage2_confidence is not None else 0.5
        if 0.05 < confidence < second_look_confidence_max:
            return True

    return False


async def _score_one(
    role: Role,
    profile_text: str,
    client: LLMClient,
    semaphore: asyncio.Semaphore,
    thinking_budget: Optional[int] = 1024,
    *,
    system_prompt: Optional[str] = None,
    response_schema: Optional[dict] = None,
    cached_handle: Any = None,
) -> Role:
    # Wave-1 (2026-05-11): support feature-flagged PAIR_B variant + context cache.
    # When cached_handle is non-None, system=None and cached_content=cached_handle —
    # Gemini SDK reads the system prompt from the cache (90% input discount).
    active_prompt = system_prompt if system_prompt is not None else STAGE3_SYSTEM_PROMPT
    active_schema = response_schema if response_schema is not None else _RESPONSE_SCHEMA
    async with semaphore:
        prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE:\n{_role_block(role)}\n"
        )
        try:
            response = await client.complete(
                model=_stage3_model(),
                system=None if cached_handle else active_prompt,
                cached_content=cached_handle,
                user=prompt,
                # Stage 3 emits a 7-field JSON with a multi-sentence
                # match_analysis + application_strategy. 1024 was hitting the
                # token ceiling mid-string and corrupting the entire response
                # for ~90% of roles. 4096 leaves ample headroom.
                max_output_tokens=4096,
                # Score must be reproducible across runs. Audit observed a
                # 35-point swing on the same role at temp 0.3 — the variance
                # is noise, not insight. Structured JSON output doesn't
                # benefit from creative sampling.
                temperature=0.0,
                json_schema=active_schema,
                thinking_budget=thinking_budget,
            )
            data = response.parsed_json
            if not isinstance(data, dict):
                # Try a strict parse first, then fall back to partial salvage
                # so a truncated response still yields a usable score.
                try:
                    data = json.loads(response.text) if response.text else {}
                except json.JSONDecodeError:
                    data = _salvage_partial_json(response.text or "")

            if not data or "score" not in data:
                # No usable score recovered — leave Stage 3 fields empty so
                # _finalize_score falls back to Stage 2 cleanly. Don't masquerade
                # the failure as a result.
                role.stage3_score = None
                role.stage3_tier = None
                role.stage3_analysis = ""
                role.stage3_application_strategy = ""
                role.stage3_best_resume_match = ""
            else:
                score = int(data.get("score", role.stage2_score or 50))
                role.stage3_score = score
                role.stage3_tier = score_to_tier(score)
                role.stage3_analysis = str(data.get("match_analysis", ""))[:2000]
                role.stage3_application_strategy = str(data.get("application_strategy", ""))[:1000]
                role.stage3_best_resume_match = str(data.get("best_resume_match", ""))[:200]
                # AI-C: short summary for dashboard
                summary_str = str(data.get("summary", ""))[:400]
                if summary_str:
                    role.summary = summary_str

                # LLM-extracted salary backfill — only fills fields that
                # were missing. Never overwrites scraper-provided structured
                # data (which is more reliable than free-text extraction).
                ext_text = data.get("extracted_salary_text") or ""
                ext_min = data.get("extracted_salary_min")
                ext_max = data.get("extracted_salary_max")
                if (ext_min is not None or ext_max is not None) and isinstance(ext_min, (int, type(None))) and isinstance(ext_max, (int, type(None))):
                    if role.salary_min is None and ext_min is not None:
                        role.salary_min = int(ext_min)
                    if role.salary_max is None and ext_max is not None:
                        role.salary_max = int(ext_max)
                    if not role.salary_text and ext_text:
                        role.salary_text = str(ext_text)[:200]
        except Exception as e:
            # Real errors (network, auth, etc.) — null out Stage 3 fields so
            # _finalize_score uses Stage 2 cleanly. Stash the error in
            # application_strategy for operator visibility without polluting
            # the analysis field.
            role.stage3_score = None
            role.stage3_tier = None
            role.stage3_analysis = ""
            role.stage3_application_strategy = f"[stage3_error: {type(e).__name__}: {str(e)[:200]}]"
            role.stage3_best_resume_match = ""
    return role


async def stage3_deep_eval(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    concurrency: int = int(os.environ.get("STAGE3_CONCURRENCY", "3")),
    skip_above: int = 88,
    skip_below: int = 55,
    run_id: Optional[str] = None,
    thinking_budget: Optional[int] = 1024,
) -> list[Role]:
    """Run Stage 3 only on roles in the 55-87 score band.

    Roles outside that band are returned unchanged (their Stage 2 score
    becomes the final score — set in the orchestrator).
    """
    if not roles:
        return []

    client = _get_stage3_client(client)
    if hasattr(client, "current_run_id"):
        client.current_run_id = run_id  # type: ignore[attr-defined]
        client.current_stage = "stage3"  # type: ignore[attr-defined]

    profile_text = _profile_block(profile)
    semaphore = asyncio.Semaphore(concurrency)

    targets = [
        r for r in roles
        if needs_stage3(r, skip_above=skip_above, primary_min=skip_below)
    ]
    if not targets:
        return roles

    # Wave-1 (2026-05-11): pick active prompt/schema/thinking-budget per
    # PAIR_B feature flag, AND create context cache once per run so all
    # subsequent Stage 3 calls get 90% input discount on system prompt.
    active_prompt, active_schema = _active_prompt_and_schema()
    active_budget = thinking_budget if thinking_budget is not None else _active_thinking_budget()
    cached_handle: Any = None
    if config.PRO_CONTEXT_CACHING_ENABLED:
        try:
            cached_handle = await client.create_cache(
                model=_stage3_model(),
                system=active_prompt,
                ttl_seconds=config.PRO_CONTEXT_CACHE_TTL_SECONDS,
            )
            if cached_handle is None:
                print("[stage3] context cache unavailable; falling back to inline system prompt")
            else:
                print(f"[stage3] context cache active; {len(targets)} Stage 3 calls will share cache")
        except Exception as e:
            print(f"[stage3] cache create failed ({type(e).__name__}): falling back to inline prompt")
            cached_handle = None

    tasks = [
        _score_one(
            r, profile_text, client, semaphore,
            thinking_budget=active_budget,
            system_prompt=active_prompt,
            response_schema=active_schema,
            cached_handle=cached_handle,
        )
        for r in targets
    ]
    await asyncio.gather(*tasks)

    # v0.3.9: POST-PROCESSING CONTRADICTION DETECTOR
    #
    # Replaces the v0.3.8 prompt-level RESOLVE-MAYBE-CONTRADICTIONS
    # constraint that didn't work. Per peer review: autoregressive
    # generation moves forward only — the model can't retroactively check
    # its own analysis against its own score mid-generation. v0.3.8 added
    # +$0.18/run cost and INCREASED contradictions from 12 → 24.
    #
    # The reliable fix is post-processing: after Stage 3 completes,
    # scan each role's analysis text for positive-fit phrases. If a role
    # has positive analysis AND a 40-69 score AND no specific concrete
    # concern named, fire a SECOND Stage 3 call with explicit "you scored
    # this 57 but wrote 'aligns perfectly' — re-evaluate" framing. The
    # second call has access to the first call's contradictory output
    # and is forced to resolve it.
    #
    # Cost: ~$0.16/run for ~20 contradicted roles re-evaluated. Net cost
    # vs v0.3.8 prompt approach: -$0.02/run (saves $0.18 from rolled-back
    # prompt, costs $0.16 for re-evals). And contradictions actually get
    # resolved this time.
    #
    # Universality: trigger pattern is universal positive-fit language
    # (regex matches "[adjective] [match-noun]" combos), not AI-strategy-
    # specific phrases. Works for nurse / engineer / lawyer / analyst.
    contradictions_resolved = await _resolve_stage3_contradictions(
        targets=targets,
        client=client,
        profile_text=profile_text,
        semaphore=semaphore,
        thinking_budget=thinking_budget,
    )
    if contradictions_resolved:
        print(f"[stage3] post-processing detector: re-evaluated "
              f"{contradictions_resolved} contradictory roles")

    # Code-level title floor enforcement (v0.3.4) — same belt-and-suspenders
    # as Stage 2. v0.3.9: graduated mode (matches Stage 2's mode change).
    from backend.scoring.title_floor import apply_floor_to_score
    for role in targets:
        if role.stage3_score is None:
            continue
        new_score, tag = apply_floor_to_score(
            score=role.stage3_score,
            role_title=role.job_title,
            candidate_headline=profile.headline,
            candidate_target_functions=profile.target_functions,
            mode="graduated",
        )
        if tag is not None:
            role.stage3_score = new_score
            role.stage3_analysis = (
                f"{tag} {role.stage3_analysis or ''}".strip()[:2000]
            )

    return roles


# ===========================================================================
# v0.3.9 POST-PROCESSING CONTRADICTION DETECTOR
# ===========================================================================

# Universal positive-fit pattern (NOT AI-strategy-specific).
# Catches "perfect/exceptional/strong/outstanding/ideal/excellent +
# fit/match/alignment/candidate" combinations across all professions.
# Examples it catches:
#   - "aligns perfectly with your AI background"  (Ziad-style)
#   - "ideal candidate for clinical work"  (nurse-style)
#   - "strong alignment with engineering"  (engineer-style)
#   - "exceptional match for sales operations"  (sales-style)
#   - "outstanding fit"  (generic)
import re

UNIVERSAL_FIT_PATTERN = re.compile(
    r"(?:"
    # Pattern A: "[adjective] [noun]" — "perfect fit", "ideal candidate",
    # "exceptional match", "strong alignment", "outstanding fit"
    r"(?:perfect|exceptional|outstanding|ideal|excellent|strong|"
    r"near[\s\-]?perfect)"
    r"\s+"
    r"(?:fit|fits|match|matches|alignment|alignment\s+with|"
    r"candidate|candidates|suit|suits|suited)"
    r"|"
    # Pattern B: "[verb] [adverb]" — "aligns perfectly", "matches exactly"
    r"(?:aligns?|maps?|matches|fits|suited?)"
    r"\s+"
    r"(?:perfectly|exceptionally|exactly|precisely|directly|seamlessly|"
    r"closely)"
    r"|"
    # Pattern C: "[adverb] [verb]" — "directly maps", "perfectly aligns"
    r"(?:directly|perfectly|exceptionally|exactly|precisely|seamlessly|"
    r"closely)"
    r"\s+"
    r"(?:aligns?|maps?|matches|fits|suited?|reflects?|mirrors?)"
    r")",
    re.IGNORECASE,
)


def _has_contradiction(role) -> bool:
    """A role is in contradiction if score is 40-69 AND analysis text
    contains universal positive-fit language."""
    s3 = role.stage3_score
    if s3 is None or s3 < 40 or s3 >= 70:
        return False
    text = (role.stage3_analysis or "") + " " + (role.stage3_application_strategy or "")
    if not text.strip():
        return False
    return bool(UNIVERSAL_FIT_PATTERN.search(text))


async def _resolve_stage3_contradictions(
    *,
    targets,
    client,
    profile_text: str,
    semaphore,
    thinking_budget,
) -> int:
    """Find roles with analysis-score contradictions and force a second
    Stage 3 call that explicitly references the contradiction.

    Returns count of roles re-evaluated.
    """
    contradicted = [r for r in targets if _has_contradiction(r)]
    if not contradicted:
        return 0

    async def _resolve_one(role):
        # Build a directly-confrontational prompt that quotes the contradiction
        original_analysis = role.stage3_analysis or ""
        original_score = role.stage3_score
        # Find the offending phrase to quote back
        m = UNIVERSAL_FIT_PATTERN.search(original_analysis + " " + (role.stage3_application_strategy or ""))
        offending = m.group(0) if m else "[positive-fit language]"

        retry_prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE TO RE-EVALUATE:\n"
            f"TITLE: {role.job_title}\n"
            f"COMPANY: {role.company}\n"
            f"LOCATION: {role.location or '(not specified)'}\n"
            f"JOB_DESCRIPTION:\n{(role.job_description_full or '')[:12000]}\n\n"
            f"YOUR PRIOR EVALUATION:\n"
            f"  Score: {original_score} (MAYBE/STRETCH tier)\n"
            f"  Analysis included: \"{offending}\"\n\n"
            f"CONTRADICTION DETECTED:\n"
            f"You wrote {offending!r} but scored only {original_score}. "
            f"This is internally inconsistent — your analysis says the role "
            f"is excellent, but your score says it's only borderline.\n\n"
            f"RE-EVALUATE and pick ONE:\n"
            f"  Option A: If the role really IS exceptional, score it 70-85 "
            f"to match your analysis.\n"
            f"  Option B: If the score is correct, REVISE the analysis to "
            f"name a SPECIFIC concrete concern (seniority gap, salary gap, "
            f"location mismatch, JD red flag, missing requirement). Replace "
            f"all enthusiastic phrasing with calibrated assessment.\n\n"
            f"Return JSON with the same schema as before. The new analysis "
            f"text and new score MUST be internally consistent."
        )

        try:
            from backend.config import config
            response = await client.complete(
                model=_stage3_model(),
                system=STAGE3_SYSTEM_PROMPT,
                user=retry_prompt,
                max_output_tokens=4096,
                temperature=0.0,
                json_schema=_RESPONSE_SCHEMA,
                thinking_budget=thinking_budget,
            )
            data = response.parsed_json
            if isinstance(data, dict):
                # Update role with the resolved evaluation
                new_score = data.get("score")
                if isinstance(new_score, int) and 0 <= new_score <= 100:
                    role.stage3_score = new_score
                role.stage3_analysis = (
                    str(data.get("match_analysis") or role.stage3_analysis)[:2000]
                )
                role.stage3_application_strategy = (
                    str(data.get("application_strategy") or role.stage3_application_strategy or "")[:1500]
                )
                # Tag for audit trail
                tag = "[contradiction-resolved]"
                role.stage3_analysis = f"{tag} {role.stage3_analysis}"[:2000]
        except Exception as e:
            # On error, keep original — don't make things worse
            print(f"[stage3 contradiction-resolver] {role.job_title[:50]}: {e}")

    async def _bounded(role):
        async with semaphore:
            await _resolve_one(role)

    await asyncio.gather(*[_bounded(r) for r in contradicted])
    return len(contradicted)

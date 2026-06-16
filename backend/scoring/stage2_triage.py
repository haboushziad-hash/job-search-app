"""Stage 2 — Triage scoring (Gemini Pro).

Real per-role evaluation. Scores 0-100, assigns provisional tier, gives
short reasoning. This is the workhorse — most roles end their journey here.

Cost optimization:
  - Gemini Pro with batch=True (50% off — though for now we run real-time
    while the official batch API matures; see TODO)
  - Context cache for the system prompt (75% off cached input tokens)
  - JD truncated to 2K tokens (saves ~30% on input)
  - Strict JSON schema (eliminates prose padding)
  - Concurrency limited to respect rate limits

Per-role cost: ~$0.001-0.002.
Per-run volume: ~280 roles surviving embedding + Stage 1.
Per-run cost: ~$0.30-0.55.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from backend.config import config
from backend.models import CandidateProfile, Role, Tier, score_to_tier
from backend.scoring.llm_client import LLMClient, get_llm_client
from backend.scoring.title_floor import apply_floor_to_score


STAGE2_SYSTEM_PROMPT = """\
You are a hiring-fit scorer for a job search tool. You see a candidate profile
and a single role. Score how strong a fit this role is for the candidate.

Score from 0 to 100, where 0 means "absolutely not a fit" and 100 means
"perfect, apply today." Use the FULL range.

================================================================
ANTI-CLUSTERING (mandatory)
================================================================

Score-clustering at exact anchor values is a known LLM scoring failure
mode. Without explicit calibration, models tend to round to "round"
multiples of 5 (like 55, 65, 75) and then bucket roles at those anchors
rather than spreading them across the full range. This destroys the
downstream tier-determination signal: if 30%+ of roles share the same
score, the tier-assigner can't differentiate between them.

DETECTION RULE: If you would emit the same score for 15% or more of
roles in a typical run, your scoring is collapsed and you must spread
it. The healthy distribution shape across N roles should be roughly
uniform within each tier band, not piled at anchor values.

FORBIDDEN values: 58, 68, 78. These are the historical clustering
anchors observed in past audits. If you find yourself rounding to one
of these, force a 1-3 point shift based on a specific concern severity
differential. Examples:

  Role: borderline GOOD with one concern about seniority
        -> DO NOT score 68. Score 65, 67, 69, 71, 73, or 74.
        -> Pick based on how severe the seniority concern is.

  Role: clearly strong fit with one minor concern about industry
        -> DO NOT score 78. Score 75, 77, 79, or 80.
        -> Pick based on how related the industry is.

  Role: weak-fit STRETCH with promising title
        -> DO NOT score 58. Score 55, 57, 59, 61, 63.
        -> Pick based on how close the title is to target.

If you produce a score of exactly 58, 68, or 78, you have failed this
constraint. Step back, identify the SINGLE most important concern,
score the role 1-4 points off the anchor based on that concern's
severity, and emit THAT score.

Also avoid clustering at: 40, 42, 45, 47, 50, 55, 88. Pick SPECIFIC
scores: 31, 38, 43, 47, 51, 63, 81, etc.

WEAK-FIT calibration (sub-50 range) — STRETCH-tier roles should be
scored across the full 25-49 range based on how close they are to a
fit, NOT clustered at 42 or 47. A role you're 10% confident about
scores ~30. A role you're 40% confident about scores ~46. A role at
the borderline of qualifying scores ~50. SPREAD them.

This is the SINGLE most important calibration discipline for v0.3.11.
A spread distribution gives the downstream Stage 3 scorer the signal
it needs to evaluate independently. A clustered distribution traps the
pipeline at deterministic ceilings.

Tier labels (STRONG/GOOD/MAYBE/STRETCH) are assigned downstream from your
raw score — you do not need to think about them.

When scoring, consider:
  1. Function alignment — is this the kind of work the candidate wants?
  2. Seniority alignment — too junior, too senior, just right?
  3. Hard requirements — does the candidate clearly meet them?
  4. Soft requirements — does the candidate likely meet them?
  5. Geography & arrangement — does it match candidate preferences?
  6. Salary — does it meet/approach the candidate's minimum?
  7. Industry/domain — is this a sector the candidate targets?

============================================================
ANTI-PATTERNS — score 25-35, NEVER above 50
============================================================
- "Submit your resume for future hiring" / "Join our talent pool" /
  "General candidate application" — these are not real roles, they're
  resume collectors. Even at fantastic companies, score 25-35.
- Generic landing pages like "Business & Industry Consultants Fluent in..."
  — if the posting reads like a category page rather than a specific opening,
  score 30-40.
- Titles that match perfectly but the JD body is empty/missing/generic
  marketing copy with no actual responsibilities or requirements —
  score 35-45 with low confidence.

============================================================
TITLE-vs-JD ALIGNMENT — read the JD before scoring
============================================================
Titles can mislead. Always read the JD before scoring. Apply these principles:

PRINCIPLE 1: TITLE MATCHES CANDIDATE TARGETS, JD DISAGREES
  When the title aligns with the candidate's target_functions but the JD
  describes a meaningfully different role, lower the score:
    - Title fits, JD fits -> score 70-90
    - Title fits, JD partially fits -> score 50-65
    - Title fits, JD reveals different work -> score 30-45

  Example pattern: "Senior X Consultant" but the JD requires hands-on
  technical work the candidate's resume doesn't show.

PRINCIPLE 2: TECHNICAL-vs-NONTECHNICAL MISMATCH
  Candidates roughly fall into "hands-on technical" or "advisory/strategy"
  archetypes. Reading the candidate profile, judge which archetype they fit:

    - If candidate is non-technical (consulting, strategy, business, ops)
      and JD requires hands-on coding, ML model training, infrastructure
      build, or data pipelines -> score 25-40 even if title sounds right.
    - If candidate is technical (engineering, data, ML) and JD is pure
      advisory with no build component -> similar penalty in reverse.

  This applies across ALL fields: an accountant looking at "Audit Manager"
  shouldn't get high score on roles that turn out to be IT audit (technical).

PRINCIPLE 3: DOMAIN ALIGNMENT
  Score higher when JD's domain (industry, sub-function) matches the
  candidate's stated target_industries or domain_expertise. Score lower
  when domain is unrelated even if title looks similar.

PRINCIPLE 4: SENIORITY-vs-RESPONSIBILITIES
  If a "Director" or "VP" title requires 10+ years of hands-on operational
  work the candidate doesn't have, that's a leadership role with an IC
  workload. Score 30-50 unless the candidate explicitly targets this kind
  of mixed role.

PRINCIPLE 5: AVOID HARSH AUTO-REJECT ON TITLE ALONE
  Titles vary wildly across companies (one company's "Manager" is another's
  "Lead"). Read the JD before scoring below 35. Use the candidate's stated
  target_functions and freeform context as the primary signal of intent.

PRINCIPLE 6: NEGATIVE SIGNALS (from candidate profile)
  If the candidate's negative_signals or freeform context names something
  to AVOID (e.g., "moving out of federal," "no customer success," "not
  interested in pure technical"), apply a -15 to -25 penalty when the role
  matches that signal — even if the title would otherwise score well.

PRINCIPLE 6.6: PURE-SALES TITLES (v0.3.8 — universal anti-pattern):
  For roles whose title is unambiguously a quota-carrying sales position:
    - "Account Executive", "Senior Account Executive", "Enterprise AE"
    - "Sales Development Representative", "SDR"
    - "Business Development Representative", "BDR"
    - "Inside Sales", "Outside Sales"
    - "Sales Engineer" (despite "Engineer" — quota-carrying revenue role)
    - "Pre-sales Engineer" / "Presales Engineer"

  Apply this conditional logic:

  IF the candidate's TARGET_FUNCTIONS or HEADLINE explicitly indicates
     they WANT sales work (contains "sales", "account executive",
     "business development", "quota", "revenue", "pipeline", "closing
     deals", or similar sales-specific signals):
     -> Score normally (60-90 if company/JD fit). Sales-targeting candidate.

  ELSE (candidate's profile has NO sales-intent signals):
     -> Score 20-35. These roles carry individual quota responsibility
       that is fundamentally different work from non-sales functions.
       The title-floor (Principle 8) does NOT apply — even a strong
       title-headline overlap on words like "manager" or "consultant"
       does not rescue a pure-sales role for a non-sales candidate.

  GTM-adjacent titles ("GTM Strategy", "GTM Operations", "Go-to-Market",
  "Solutions Consultant", "Customer Success Manager") are SOFTER:
     - If candidate targets GTM/customer success -> score normally
     - Otherwise -> apply -10 to score (not a hard cap; just a tilt)
     These titles can be legitimate enablement/operations work depending
     on JD; the soft penalty respects that ambiguity.

  Why this rule exists: v0.3.6 audit had Snowflake "Account Executive,
  Public Sector" qualifying at 55 MAYBE because the title-floor caught
  the word "federal" in overlap. Stage 2 reasoning had explicitly said
  "significant mismatch, sales role" — but the floor overrode it. This
  rule + the v0.3.8 title-floor strict mode together kill that pattern:
  sales titles for non-sales candidates score 20-35 and stay there.

PRINCIPLE 7: ROLE FUNCTION OVER COMPANY AFFINITY
  A role's FUNCTION (what the person actually does day-to-day) matters more
  than the COMPANY (employer brand, domain). A role at a perfect-fit company
  doing the wrong work is NOT a strong match. Concrete cases:

  - "Applied AI Architect" at a top AI lab requires production ML deployment.
    Candidate without engineering background -> score 30-45 even if company
    is a perfect match. The title contains "Architect" — that's a function
    word, not a domain word.
  - "Customer Success Manager" at a target company is CSM work, not
    enablement work. Strategy candidates get 50-65, not 80+.
  - "Product Manager" requires PM background. Strategy/consulting candidates
    without PM experience: 35-55, not 75+.

  When the title contains a function word (Engineer, Architect, Developer,
  Data Scientist, Product Manager, Designer, etc.) and the candidate has
  no resume evidence of that function, cap the score at 55 regardless of
  how strong the company/domain alignment looks.

PRINCIPLE 8: THE TITLE IS A WEAK SIGNAL — IT NEVER SETS A SCORE FLOOR (v0.3.35)
  A role's TITLE matching words in the candidate's headline or target_functions
  earns the role NO minimum score. Title overlap tells you where to LOOK, not
  whether the role fits. ALWAYS grade by the actual DUTIES and REQUIREMENTS in
  the JD versus the candidate's resume (Principles 1-7, 9, 10).

  A strong title match may RAISE YOUR CONFIDENCE that you've identified the
  role correctly — but if the duties/requirements are off-target, the wrong
  seniority, technical beyond the candidate, or otherwise a poor fit, score it
  LOW no matter how well the title matches. Never let a title keyword ("AI",
  "Strategy", "Consultant", "Manager", "Program", "Governance", etc.) pull a
  score UP on its own.

  (History: a code + prompt "title-floor" used to raise such scores to 55-60 on
  keyword overlap. A 2026 audit found it was the single biggest source of
  false-positives — off-target roles surfaced as GOOD purely because their
  title shared a word with the candidate's. It has been removed in BOTH the
  prompt and the code. Grade the work, not the title.)

PRINCIPLE 9: TITLE PATTERNS REQUIRING CAREFUL JD READING
  Some title patterns reliably mislead. Read the JD carefully before scoring
  these high or low — don't auto-reject. Examples below assume the candidate
  has the relevant background; if the candidate's profile doesn't include
  the relevant function/domain experience, score normally.

  - "AI Consultant" / "Senior AI Consultant" / "AI Solutions Consultant":
    Score 55-75 if JD emphasizes strategy, readiness assessment, adoption
    workshops, change management, or client advisory WITHOUT requiring
    SQL/ML/cloud engineering. Score 25-40 if JD requires hands-on technical
    build (RAG, ML models, data pipelines). Most AI-native startups,
    consulting firms with named AI practices, and Big 4 firms default to
    strategy/advisory framing unless the JD explicitly requires hands-on
    engineering — read the JD content rather than the company name.
  - "Forward Deployed Strategist" / "Forward Deployed Consultant" / "Advisory
    AI Strategist": Score 55-70 if client-facing strategy/enablement without
    software engineering requirements. "Forward Deployed Engineer" with
    coding requirements is a different track — score 25-35 unless the
    candidate's profile includes hands-on engineering.
  - "Engagement Manager" with AI context: Score 55-70 if managing AI
    delivery engagements, client relationships, or portfolios. Bump for
    senior consulting candidates UNLESS JD explicitly says "must carry
    individual sales quota."
  - "AI Strategy Consultant" / "AI Strategist" / "Senior AI Strategist":
    Score 55-75 at any company. Do NOT reject for "requires MBB background"
    unless JD explicitly says "MBB experience required." "Strategy
    consulting background preferred" is different from "MBB required."
  - "Federal AI Strategy" / "Public Sector AI Consultant" / "AI Modernization
    Consultant": Score 60-80 for federal-experienced candidates. These are
    target matches when the candidate's profile includes federal/public
    sector work.

PRINCIPLE 10: INDUSTRY-WEIGHT ADJUSTMENT (PROPORTIONAL — v0.3.4)
  After computing a base score from function/seniority/JD-fit signals, apply
  a PROPORTIONAL industry-fit adjustment. Use the candidate's target_industries
  list and the role's company / industry context to classify:

    SAME industry (role's industry is in candidate's target_industries
                   OR is an obvious sub-bucket of one — e.g., "fintech" ⊂
                   "Financial Services", "biotech" ⊂ "Life Sciences"):
      -> Apply NO adjustment (the base score already credits fit).

    ADJACENT industry (one step removed but plausible — e.g.,
                       healthcare ↔ pharma ↔ life sciences,
                       financial services ↔ insurance ↔ fintech,
                       consulting ↔ professional services,
                       SaaS ↔ enterprise software,
                       CPG ↔ retail ↔ consumer goods):
      -> Apply 0 adjustment (no penalty, no bonus). These are legitimate
        cross-sector pivots.

    OFF-TARGET industry (not in target_industries AND not adjacent):
      -> Apply -10 to the base score.

    VERY-OFF-TARGET industry (the candidate's profile + freeform context
                              gives strong signal they would NOT pursue
                              this — e.g., a federal-only candidate vs
                              a startup, a healthcare consultant vs
                              construction, a non-AI candidate vs an AI
                              research lab):
      -> Apply -15 to the base score.

  IMPORTANT: This is industry FIT, not function FIT. Function mismatch is
  already covered by other principles. Industry adjustment is on top of
  function-based scoring, not a substitute. A perfect-function role at an
  off-target industry should still score reasonably (function dominates),
  but the industry adjustment correctly reflects that the candidate is
  less likely to apply.

  ALSO IMPORTANT: This rule does NOT apply when target_industries is empty
  or generic ("Any" / "Various" / "Open"). Skip the adjustment in that case.

============================================================
GEOGRAPHIC MISMATCH HANDLING
============================================================
A geographic mismatch is a -15 penalty, NOT an auto-reject:
- If role is on-site in a non-acceptable location: -15 from base score
- If role is hybrid in a non-acceptable location: -10 from base score
- If role is remote-anywhere: 0 penalty (always acceptable)
- Remote-but-restricted-to-state-X: only acceptable if X is in candidate's list

Do NOT score below 25 just for geography. The fit signal still matters.

============================================================
HANDLING THIN / MISSING / UNUSABLE JDS
============================================================
Do NOT be optimistic about a JD you cannot actually read — a matching title is
not evidence of fit, and we must not surface a role we can't evaluate.
- If the JD is MISSING, empty, "(not retrieved)", a login wall, a redirect or
  routing stub (e.g. JSON like {"widget":"redirect",...}), an empty JavaScript
  shell, site navigation/footer chrome, or otherwise has NO real
  responsibilities or requirements to evaluate:
    -> score 0-15 with low confidence. Do NOT score on the title alone.
      (The pipeline will SKIP it or attempt a re-fetch from a better source.)
- If the JD is < 200 chars, or is generic marketing copy with no
  responsibilities/requirements: score 25-40 with LOW confidence (0.2-0.4).
- Never score above 50 without substantive JD evidence of the actual duties
  and requirements.

============================================================
OFF-TARGET FUNCTION FLAG (v0.3.27) — a GATE, separate from the score
============================================================
Independently of the score, classify whether this role's CORE day-to-day
FUNCTION sits clearly OUTSIDE the candidate's target_functions. This is a
yes/no gate on the KIND of job, NOT a score nudge — a strong-fit fit-score and
an off-target function can BOTH be true (e.g. a great-sounding "AI Strategy"
title whose actual job is customer success).

Set `off_target_function` to the matching bucket ONLY when the core duties
clearly are NOT one of the candidate's target functions:
  - "customer-success" — driving an EXTERNAL customer's success/renewal/adoption
                         of a product (CSM, account management, customer
                         engagement/experience). This is about OUTSIDE customers,
                         NOT about enabling the company's own teams.
  - "product-mgmt"     — owning a product roadmap/backlog (vs strategy/enablement)
  - "policy-analyst"   — policy / intelligence / research analyst, advisory non-AI
  - "sales-gtm"        — quota-carrying, BD, pre-sales, revenue
  - "generic-pm"       — program/project management with NO AI/data substance
                         (plain defense/IT/ops PM that merely mentions AI)
  - "engineering"      — hands-on software/ML/data build
  - "other"            — clearly off-target but none of the above

CRITICAL — "on-target vs off-target" is decided ONLY against THIS candidate's
own target_functions (listed in the profile, and repeated as an explicit
on-target whitelist below if present). The SAME role can be on-target for one
candidate and off-target for another — do NOT carry a fixed notion of which
functions are "good". Whatever this candidate targets (e.g. AI strategy, nursing,
skilled trades, sales, software engineering, finance, operations), a role whose
CORE duties match those targets is "none" (on-target), even when the title
carries "Manager", "Lead", "Success", "Specialist", "Partner", "Analyst", or
"Consultant".

General disambiguation (apply RELATIVE to this candidate's targets — NOT as a
universal whitelist):
  - A role that serves the company's OWN INTERNAL teams (internal enablement,
    adoption, change management, transformation, governance, or program/project
    management that carries real domain substance) is NOT "customer-success" —
    that bucket is for serving EXTERNAL customers — and is NOT "generic-pm" when
    it carries real substance. It is "none" ONLY when that work matches THIS
    candidate's target_functions; otherwise pick the bucket that fits.
  - An engineering role is "none" for a candidate who targets hands-on
    engineering, but "engineering" for a candidate who does not.

Set it to the string "none" (on-target) when the core function IS one of THIS
candidate's target_functions — EVEN IF seniority, domain, industry, years, or
salary are a stretch (those are score deductions, not off-target). Set the
matching off-target bucket when the core duties clearly fall OUTSIDE this
candidate's target_functions. When genuinely unsure, use "none": this gate is
for CLEAR function mismatches only. Always judge against the candidate's stated
target_functions, not the title's keywords.

============================================================
SCORE <-> REASONING CONSISTENCY (mandatory)
============================================================
Your numeric score MUST agree with the verdict in your reasoning. If your
reasoning names a significant or disqualifying problem — off-target core
function, a hard requirement the candidate clearly lacks, the wrong seniority,
a salary entirely below the candidate's floor, or a missing/unusable JD — then
the score CANNOT land in the GOOD/STRONG range; lower it to match. Likewise, do
NOT write glowing reasoning and then emit a low score. A reader comparing your
score to your reasoning must never find them contradictory.

Respond with strict JSON:
{
  "score": <integer 0-100>,
  "reasoning": "<2-3 sentences explaining the score>",
  "confidence": <float 0.0-1.0>,
  "key_match_signals": ["<signal1>", "<signal2>"],
  "key_concerns": ["<concern1>", "<concern2>"],
  "industry": "<one-word industry tag: Tech, Healthcare, Finance, CPG, Retail, Manufacturing, Energy, Government, Consulting, Education, Media, RealEstate, Logistics, Hospitality, Pharma, Insurance, Telecom, Other>",
  "off_target_function": <"none" if the core function is one of the candidate's targets; else one of "customer-success","product-mgmt","policy-analyst","sales-gtm","generic-pm","engineering","other">,
  "is_dead_listing": <true if JD says 'no longer accepting applications', 'this position has been filled', or 'we are no longer hiring' — false otherwise>
}
"""

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "key_match_signals": {"type": "array", "items": {"type": "string"}},
        "key_concerns": {"type": "array", "items": {"type": "string"}},
        "industry": {"type": "string"},
        "off_target_function": {
            "type": "string",
            "enum": ["none", "customer-success", "product-mgmt", "policy-analyst",
                     "sales-gtm", "generic-pm", "engineering", "other"],
        },
        "is_dead_listing": {"type": "boolean"},
    },
    "required": ["score", "reasoning", "confidence"],
}

# v0.3.27 off-target FUNCTION gate buckets. Stage 2 emits one of these (or
# "none") in `off_target_function`; the orchestrator caps any flagged role at
# STRETCH. Kept in sync with the OFF-TARGET FUNCTION FLAG prompt section above.
_OFF_TARGET_BUCKETS = {
    "customer-success", "product-mgmt", "policy-analyst",
    "sales-gtm", "generic-pm", "engineering", "other",
}


def _profile_block(profile: CandidateProfile) -> str:
    parts = [f"HEADLINE: {profile.headline or '(none)'}"]
    if profile.years_experience is not None:
        parts.append(f"YEARS_EXPERIENCE: {profile.years_experience}")
    if profile.target_seniority:
        parts.append(f"TARGET_SENIORITY: {profile.target_seniority}")
    if profile.target_functions:
        parts.append("TARGET_FUNCTIONS: " + ", ".join(profile.target_functions))
    if profile.target_industries:
        parts.append("TARGET_INDUSTRIES: " + ", ".join(profile.target_industries))
    if profile.technical_skills:
        parts.append("TECHNICAL_SKILLS: " + ", ".join(profile.technical_skills[:25]))
    if profile.domain_expertise:
        parts.append("DOMAIN_EXPERTISE: " + ", ".join(profile.domain_expertise[:15]))
    if profile.salary_minimum:
        parts.append(f"SALARY_MINIMUM: ${profile.salary_minimum:,}")
    if profile.work_arrangements:
        parts.append("WORK_ARRANGEMENTS: " + ", ".join(profile.work_arrangements))
    if profile.acceptable_locations:
        parts.append("ACCEPTABLE_LOCATIONS: " + ", ".join(profile.acceptable_locations))
    if profile.excluded_locations:
        parts.append("EXCLUDED_LOCATIONS: " + ", ".join(profile.excluded_locations))
    if profile.negative_signals:
        # Surfaced prominently — Principle 6 in the system prompt requires the
        # model to apply -15 to -25 when a role matches one of these.
        parts.append("NEGATIVE_SIGNALS / AVOID: " + "; ".join(profile.negative_signals))
    return "\n".join(parts)


# Wave-1 Section 14.4 (2026-05-11): JD section-aware regex compression.
# Strips boilerplate sections (EEO statement, benefits enumeration, "About
# us"-style company paragraphs, perks) while preserving Requirements,
# Qualifications, Responsibilities, and salary text. Saves $0.02-0.07/run
# on Stage 2 input tokens per Section 14.4 measurement.

_JD_BOILERPLATE_PATTERNS = [
    # EEO / Equal Opportunity statements — common opening phrases.
    # Strips from the EEO opener to the next blank line or end-of-text.
    (
        r"(?is)\b(we\s+are\s+an\s+equal\s+opportunity"
        r"|equal\s+opportunity\s+(employer|workplace|statement)"
        r"|all\s+qualified\s+applicants\s+will\s+receive\s+consideration"
        r"|EEO\s+statement)"
        r".*?(?=\n\s*\n|\Z)",
        "[EEO]",
    ),
    # Benefits enumeration — section header on its own line, content until blank line
    (
        r"(?is)\n\s*\n\s*(BENEFITS|OUR\s+BENEFITS|WHAT\s+WE\s+OFFER|PERKS"
        r"|PERKS\s+AND\s+BENEFITS|COMPENSATION\s+AND\s+BENEFITS"
        r"|TOTAL\s+REWARDS|WHY\s+JOIN\s+US)\s*\n"
        r".*?(?=\n\s*\n|\Z)",
        "[BENEFITS]",
    ),
    # "About the company" / marketing intro section headers
    (
        r"(?is)\n\s*\n\s*(ABOUT\s+US|ABOUT\s+THE\s+COMPANY"
        r"|WHO\s+WE\s+ARE|OUR\s+STORY|OUR\s+MISSION"
        r"|OUR\s+COMPANY|COMPANY\s+OVERVIEW)\s*\n"
        r".*?(?=\n\s*\n|\Z)",
        "[ABOUT]",
    ),
    # Application instructions / recruiter accommodation language
    (
        r"(?is)\b(if\s+you\s+have\s+a\s+disability"
        r"|to\s+apply\s+for\s+this\s+(role|position)"
        r"|please\s+submit\s+your\s+application"
        r"|how\s+to\s+apply"
        r"|reasonable\s+accommodation)"
        r".*?(?=\n\s*\n|\Z)",
        "[APPLY]",
    ),
]
_JD_BOILERPLATE_COMPILED = [
    (__import__("re").compile(pat), tag) for pat, tag in _JD_BOILERPLATE_PATTERNS
]


def _compress_jd_for_stage2(jd: str) -> str:
    """Strip boilerplate sections from a JD body, preserving substantive content.

    Returns compressed JD body. If compression would reduce content below a
    safety threshold (200 chars or 70% of original), returns the original
    JD to guard against over-stripping. Section 14.4 spec, Wave-1.
    """
    if not jd or len(jd) < 400:
        return jd  # too short to bother
    compressed = jd
    for pattern, tag in _JD_BOILERPLATE_COMPILED:
        compressed = pattern.sub(f"\n{tag}\n", compressed)
    # Safety check: if compression removed >70% of content or below 200 chars,
    # something is wrong with the regex — fall back to original.
    if len(compressed) < 200 or len(compressed) < len(jd) * 0.30:
        return jd
    return compressed


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
    jd = role.job_description_essence or role.job_description_full or ""
    if jd:
        # Wave-1 Section 14.4: regex-strip boilerplate before truncation.
        # When STAGE2_JD_COMPRESSION_ENABLED=False, the function is a no-op
        # via the conditional below (defensive — _compress_jd_for_stage2 is
        # also safe to call on any input).
        if config.STAGE2_JD_COMPRESSION_ENABLED:
            jd = _compress_jd_for_stage2(jd)
        parts.append(f"JOB_DESCRIPTION:\n{jd[:jd_max_chars]}")
    else:
        parts.append("JOB_DESCRIPTION: (not retrieved)")
    return "\n".join(parts)


def _is_cache_lookup_error(err: Exception) -> bool:
    """Detect Gemini cache-lookup failures (propagation lag, project mismatch).

    The most common production failure (v0.3.5 audit, 71% of qualifying roles)
    is `403 PERMISSION_DENIED — CachedContent not found`. The error wording is
    misleading: the API key DOES have permission, but Gemini's serving node
    handling this particular request hasn't yet seen the cache resource that
    was created moments earlier on a different node.

    Other shapes we treat as cache-lookup failures:
      - "Cached content not found"
      - "404 NOT_FOUND" referencing a cachedContents resource
      - Cross-project access (cache created on Key A, call routed to Key B
        after `_rotate_to_next_key` fired)

    We DO NOT treat generic 403/PERMISSION_DENIED as cache errors — only
    when the message also references CachedContent / cached_content.
    """
    s = str(err)
    if "CachedContent not found" in s or "Cached content not found" in s:
        return True
    if "cachedContents/" in s and ("404" in s or "NOT_FOUND" in s):
        return True
    if "PERMISSION_DENIED" in s and ("CachedContent" in s or "cachedContents" in s):
        return True
    return False


def _salvage_partial_json(text: str) -> dict:
    """Best-effort extraction of score/reasoning from a truncated JSON response.

    When max_output_tokens is hit mid-string, the response looks like:
        {"score": 75, "reasoning": "This role is a good fit because the can
    No closing quote, no closing brace. We salvage the score and partial reasoning.
    """
    if not text:
        return {}
    out: dict = {}
    # Score: integer after "score":
    m = re.search(r'"score"\s*:\s*(\d+)', text)
    if m:
        try:
            out["score"] = int(m.group(1))
        except ValueError:
            pass
    # Confidence: float after "confidence":
    m = re.search(r'"confidence"\s*:\s*([0-9.]+)', text)
    if m:
        try:
            out["confidence"] = float(m.group(1))
        except ValueError:
            pass
    # Reasoning: capture as much string content as we can
    m = re.search(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)', text, re.DOTALL)
    if m:
        out["reasoning"] = m.group(1)[:500]
    return out


async def _complete_with_cache_fallback(
    *,
    client: LLMClient,
    system_prompt: str,
    prompt: str,
    cached_content: Optional[object],
    is_batch: bool,
    thinking_budget: Optional[int],
):
    """Run a Stage 2 completion, falling back to inline system prompt on
    cache-lookup failure.

    v0.3.8 takes `system_prompt` as a parameter (instead of hardcoded
    STAGE2_SYSTEM_PROMPT) so the per-run dynamic prompt with user-specific
    hard exclusions injected at the top is used in BOTH the cached and
    fallback paths. Without this, a cache 403 retry would lose the user's
    excluded_title_patterns and negative_signals injection.

    Why this exists (v0.3.5.1): production audit showed 71% of parallel
    Stage 2 calls hit `CachedContent not found` due to Gemini cache
    propagation lag. When that happens, this helper retries the SAME call
    without the cache — preserving the correct LLM-scored result and only
    sacrificing the 75% input discount on the affected call.

    Order of attempts:
      1. With cache (cheap path; works for ~30-95% of calls depending on
         how propagated Gemini's cache is at burst time).
      2. On cache-lookup error: retry without cache, using the inline
         system prompt. Retry happens at most ONCE — non-cache errors
         propagate up to the outer try/except in _score_one.
    """
    try:
        return await client.complete(
            model=config.STAGE2_MODEL,
            system=None if cached_content else system_prompt,
            user=prompt,
            max_output_tokens=2048,
            temperature=0.0,
            json_schema=_RESPONSE_SCHEMA,
            cached_content=cached_content,
            is_batch=is_batch,
            thinking_budget=thinking_budget,
        )
    except Exception as e:
        if cached_content is not None and _is_cache_lookup_error(e):
            # Self-heal: retry without cache, using the SAME dynamic system prompt
            return await client.complete(
                model=config.STAGE2_MODEL,
                system=system_prompt,
                user=prompt,
                max_output_tokens=2048,
                temperature=0.0,
                json_schema=_RESPONSE_SCHEMA,
                cached_content=None,
                is_batch=is_batch,
                thinking_budget=thinking_budget,
            )
        raise


# W70 resilience: tell apart an INFRASTRUCTURE failure (the AI service is down,
# all keys are exhausted, the daily/global budget cap was hit, the network
# dropped) from a benign per-role parse hiccup. JSON-parse problems are already
# salvaged inside _score_one and never reach its except block, so almost
# everything that DOES reach it is service/network class. We err deliberately
# toward "infra" here: a genuine code bug is rare and per-role, so it can never
# reach the systemic-failure threshold, whereas an outage hits ~every role and
# MUST fail the run honestly instead of papering it over with neutral 50s.
_INFRA_ERROR_NEEDLES = (
    "429", "resource_exhausted", "quota", "rate limit", "ratelimit",
    "503", "502", "504", "500", "unavailable", "deadline", "timeout",
    "timed out", "connect", "connection", "read error", "readerror",
    "dns", "getaddrinfo", "ssl", "all keys exhausted", "no available key",
    "daily_cap_exceeded", "daily cap", "global_budget_exhausted", "proxy",
)


def _is_infra_error(exc: BaseException) -> bool:
    s = str(exc).lower()
    tn = type(exc).__name__.lower()
    if any(n in s for n in _INFRA_ERROR_NEEDLES):
        return True
    return any(n in tn for n in (
        "timeout", "connect", "transport", "httperror", "clienterror",
        "oserror", "ssl", "networkerror",
    ))


async def _score_one(
    role: Role,
    profile_text: str,
    client: LLMClient,
    semaphore: asyncio.Semaphore,
    cached_content: Optional[object] = None,
    is_batch: bool = False,
    thinking_budget: Optional[int] = None,
    system_prompt: str = STAGE2_SYSTEM_PROMPT,
) -> Role:
    async with semaphore:
        prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE TO SCORE:\n{_role_block(role)}\n"
        )
        try:
            response = await _complete_with_cache_fallback(
                client=client,
                system_prompt=system_prompt,
                prompt=prompt,
                cached_content=cached_content,
                is_batch=is_batch,
                thinking_budget=thinking_budget,
            )
            data = response.parsed_json
            if not isinstance(data, dict):
                # Try strict JSON parse first
                try:
                    data = json.loads(response.text) if response.text else {}
                except json.JSONDecodeError:
                    # Tolerant fallback: extract score + reasoning via regex
                    # from a truncated response
                    data = _salvage_partial_json(response.text or "")

            score = int(data.get("score", 0))
            role.stage2_score = score
            role.stage2_tier = score_to_tier(score)
            role.stage2_reasoning = str(data.get("reasoning", ""))[:1000]
            role.stage2_confidence = float(data.get("confidence", 0.5))
            # AI-A: industry detection (piggybacks Stage 2 with no extra LLM cost)
            ind = data.get("industry")
            if ind and isinstance(ind, str):
                role.industry = ind.strip()[:30]
            # v0.3.27: off-target FUNCTION gate, recorded separately from the
            # fit score. Stage 2 classifies whether the role's CORE function is
            # outside the candidate's target_functions; the orchestrator caps
            # those at STRETCH (the deterministic catch for CS/product/policy
            # "false-friends" a title regex misses). "none"/null/unknown =>
            # on-target, no gate.
            otf = data.get("off_target_function")
            if isinstance(otf, str) and otf.strip().lower() in _OFF_TARGET_BUCKETS:
                role.off_target_function = otf.strip().lower()
            # AI-B: dead-listing flag — if Stage 2 detected the JD says
            # "no longer accepting applications" / "filled", drop the score
            # and flag inactive so the role doesn't show in qualifying.
            if data.get("is_dead_listing") is True:
                role.stage2_score = 0
                role.stage2_tier = Tier.SKIP
                role.stage2_reasoning = (
                    "[dead-listing] " + (role.stage2_reasoning or "")
                )[:1000]
        except Exception as e:
            if _is_infra_error(e):
                # INFRASTRUCTURE failure (service down / keys exhausted / budget
                # cap / network). Do NOT fabricate a neutral 50 — that would
                # surface an unscored role as a real STRETCH match. Leave the
                # score None and tag it so stage2_triage() can decide, once the
                # whole batch is in, whether the failure is systemic enough to
                # fail the run honestly. A handful of transient blips below the
                # threshold simply drop out (None never qualifies downstream).
                role.stage2_score = None
                role.stage2_tier = None
                role.stage2_reasoning = f"stage2_infra_error: {type(e).__name__}: {str(e)[:200]}"
                role.stage2_confidence = 0.0
            else:
                # Benign per-role hiccup: assign neutral score with low
                # confidence so the role can still be considered downstream.
                role.stage2_score = 50
                role.stage2_tier = Tier.STRETCH
                role.stage2_reasoning = f"stage2_error: {type(e).__name__}: {str(e)[:200]}"
                role.stage2_confidence = 0.0
    return role


def build_dynamic_stage2_prompt(profile: CandidateProfile) -> str:
    """Build the per-run Stage 2 system prompt by injecting user-specific
    hard rules at the top of the static base prompt.

    v0.3.8 — addresses the audit finding that the user's freetext exclusions
    (negative_signals + excluded_title_patterns) were too soft when only
    delivered as profile DATA in the user message. The model would interpret
    them inconsistently — sometimes as hard rules, sometimes as soft hints
    that other principles could override.

    By injecting the same exclusions at the TOP of the SYSTEM prompt as
    explicit `[HARD]` rules with score caps, the model treats them as
    architectural constraints rather than data-to-interpret. The clearance
    rescore test confirmed: switching from soft "Avoids clearance roles"
    profile data to explicit "[HARD] cap at 30" prompt rule moved 6 of
    15 clearance-mentioning roles from STRONG/MAYBE down 11-29 points.

    Universality: works for ANY user. The candidate's own
    excluded_title_patterns (LLM-generated by the profile builder from
    their resume + freetext) drive the title cap. The candidate's own
    negative_signals drive the JD-content cap. A tax accountant's list
    looks different from an AI strategy candidate's list, but the same
    prompt logic applies to both.
    """
    base = STAGE2_SYSTEM_PROMPT

    # Excluded title patterns — title-level cap at 35
    if profile.excluded_title_patterns:
        base += "\n\n" + "=" * 60 + "\n"
        base += "USER-SPECIFIED EXCLUDED TITLE PATTERNS (HARD CAP AT 35)\n"
        base += "=" * 60 + "\n"
        base += (
            "If the role title contains ANY of these patterns "
            "(case-insensitive substring match), cap the score at 35. "
            "The candidate has explicitly indicated they do NOT want "
            "titles matching these patterns:\n\n"
        )
        for pat in profile.excluded_title_patterns:
            base += f"  - {pat}\n"
        base += (
            "\nThis cap OVERRIDES Principle 8 (title-headline overlap floor) "
            "and any company-affinity boost. Even if the company is a "
            "perfect fit, the title exclusion takes precedence — the "
            "candidate has already told us they don't want this category.\n"
        )

    # Negative signals — content-level cap at 30
    if profile.negative_signals:
        base += "\n\n" + "=" * 60 + "\n"
        base += "USER-SPECIFIED HARD EXCLUSIONS (CAP AT 30)\n"
        base += "=" * 60 + "\n"
        base += (
            "Apply these BEFORE any other principle. When a role triggers "
            "any of these conditions AS A REQUIREMENT (not as 'nice to have' "
            "or incidental mention), cap the final score at 30.\n\n"
        )
        for sig in profile.negative_signals:
            base += f"  - {sig}\n"
        base += (
            "\nIMPORTANT — DO NOT over-apply. The cap fires only when the "
            "JD or title makes the excluded condition a REQUIREMENT. Examples:\n"
            "  TRIGGER (cap at 30):  'Active TS/SCI clearance required'\n"
            "  TRIGGER (cap at 30):  'Must hold Secret clearance'\n"
            "  DO NOT trigger:       'Cleared facilities benefits available'\n"
            "  DO NOT trigger:       'Some team members may need clearance'\n"
            "  DO NOT trigger:       'Federal contracting experience helpful'\n"
            "\nWhen capped, your reasoning MUST cite the specific user "
            "exclusion that triggered it (e.g., \"matches negative_signal: "
            "TS/SCI clearance required for this role\").\n"
        )

    # W71r (2026-06-16): off_target_function GATE rewritten as a PRECISION
    # instrument. The prior v0.3.28 whitelist was a keyword-amnesty ("treat close
    # synonyms / adjacent sub-functions as on-target, even with Manager/Success/
    # Analyst titles"), so any AI-saturated JD passed as "none" regardless of its
    # real core duties -- leaking data-science / research / account-management /
    # proposal-ops roles into GOOD. This version forces a DUTY-BULK test, judges
    # FUNCTION ONLY (seniority is handled separately by SC1), honors the full
    # target_functions list (AI program/product roles stay on-target), and is
    # strongly biased toward "none". Blind-validated vs the Opus ground-truth set:
    # ~70% catch of content-level leaks at 2% false-reject across 151 on-target
    # roles; combined with the title guard, 75% catch. Profile-gated +
    # generalizable: the same JD is on-target for one candidate and off-target for
    # another; nothing about which functions are "good" is hardcoded.
    if profile.target_functions:
        _target_functions_list = "".join(
            f"  - {fn}\n" for fn in profile.target_functions
        ).rstrip("\n")
        _off_target_function_gate = """\
============================================================
THIS CANDIDATE'S TARGET FUNCTIONS — off-target-FUNCTION gate
============================================================
SCOPE OF THIS GATE: Judge FUNCTION ONLY — the discipline of the day-to-day
labor. You do NOT judge seniority, title altitude, years, salary, clearance,
industry, domain, company, or remote/onsite here. A separate seniority check
(SC1) already handles "too senior / too junior" as a soft tier demotion, so
this gate must NEVER flag a role for being Director / Sr-Director / VP / AVP /
Head / Executive / Lead, or for being too junior. There is no seniority bucket
in this gate. If your only reason to flag is "this is above/below the
candidate's target seniority," return "none".

This gate exists to catch the SMALL number of roles whose actual day-to-day
labor is PLAINLY a different profession from anything the candidate targets.
It is a precision instrument, not a quality filter: weak fits, stretch fits,
adjacent fits, and thin JDs are NOT its job — the numeric score and other
checks handle those. When in any doubt, return "none". Burying a genuine match
is far worse than letting a borderline off-target role sit in the GOOD tier.

THIS CANDIDATE TARGETS (the on-target functions):
{TARGET_FUNCTIONS_LIST}

Read that list literally and in full. Every function on it is on-target, and
its natural role-title variants are on-target too. In particular, if the list
includes a "program/product/project management" flavor of the target topic
(e.g. "AI program management"), then AI Product Owner, AI Product Manager, AI
Program Manager, AI Project Manager, AI Product Strategist, and "AI strategy &
transformation" product roles are ON-TARGET — do NOT flag them as product-mgmt
or generic-pm. The product/program management OF the target topic IS a target
function for this candidate.

------------------------------------------------------------
STEP 1 — Identify the CORE function (the majority of duties).
------------------------------------------------------------
Read the responsibilities / "what you'll do" section and mentally tally where
the bulk of the day-to-day labor sits. Weight the listed DUTIES, not the
title, the mission paragraph, the company, or the product the company sells.
If the duties split, pick the discipline that owns the LARGEST share of the
actual work.

------------------------------------------------------------
STEP 2 — On-target ("none") — the DEFAULT.
------------------------------------------------------------
Return "none" if the bulk of the duties IS one of the candidate's target
functions, OR if you cannot say with confidence that the bulk is some OTHER,
clearly-named profession. Strongly biased toward "none": a role is on-target
when the duties are the target work itself — for this profile, things like
defining/standing-up AI strategy, AI enablement, AI adoption, AI governance /
responsible-AI / AI risk-and-policy frameworks, AI program/product management,
training/change-management for AI, or federal/technology consulting that
delivers any of those. Note these are all ON-TARGET and must NOT be flagged:
  - Designing or owning a governance / responsible-AI / risk / policy /
    standards-and-controls framework, even at enterprise scale, even titled
    Director/Sr-Director/VP, and even when it names "controls," "assurance,"
    "monitoring," or "risk appetite." Governance IS a target function.
  - A FEDERAL / agency / IC AI POLICY analyst whose output is policy, doctrine,
    frameworks, and guidance for a government client — this is AI governance /
    policy advisory, NOT analyst-firm research. On-target.
  - An "AI Specialist," "Applied AI Analyst," "AI Advisor," or similar whose
    duties are advisory / enablement / adoption / safe-deployment / translating
    AI for non-experts — even if it mentions prototyping or "hands-on" — unless
    the BULK is genuinely writing production code / training models / data
    engineering. Advisory-with-some-prototyping is on-target.
  - An AI-enablement, AI-adoption, or knowledge-management-FOR-AI program/PM
    role (e.g. M365/Copilot knowledge enablement, BPM redesign for AI adoption,
    AI transformation delivery with change management) — the substance is
    AI enablement/adoption, so on-target.
  - An internal AI delivery / engagement / practice role whose labor is
    deploying, implementing, governing, training, and driving adoption of an AI
    capability for the org or its delivery clients — on-target even if titled
    "Engagement Manager," "Success," "Advocate," or "Adoption."

------------------------------------------------------------
STEP 3 — Off-target — only for UNAMBIGUOUS, clearly-different professions.
------------------------------------------------------------
Flag off-target ONLY when a reasonable reader would say "this is plainly a <X>
job, not <target> work" — i.e. the BULK of the duties is one of the narrow
patterns below AND there is NO substantial AI-strategy/enablement/adoption/
governance/program labor in the role. Map to the closest bucket; use "other"
only if a clearly-different profession fits none. Each bucket is deliberately
narrow:

  - research-analyst — ONLY when the MAIN OUTPUT is published research,
    advisory publications, or thought-leadership produced for an analyst /
    research firm or research institute (e.g. Gartner-style market research,
    syndicated reports). A government/agency policy analyst producing policy
    and frameworks is NOT this — that is governance/policy (on-target).
    Monitoring vendors/market to inform an EMPLOYER's own AI strategy/roadmap
    is strategy work, not analyst-firm research -> "none".

  - research-science — ONLY when the bulk is original scientific/academic
    research as the deliverable (novel methods, papers, experiments). A role
    that builds governance/risk/assurance FRAMEWORKS and standards, even at a
    safety/policy institute, is governance (on-target) -> "none".

  - engineering / data-science — ONLY hands-on building as the bulk: writing
    production code, training/deploying models, ML engineering, data
    engineering/pipelines, architecture-as-implementation. Advisory,
    enablement, governance, evaluation, requirements, or "translate AI for the
    business" work is NOT engineering even when the topic is AI and even if a
    minority of duties is technical -> "none".

  - product-mgmt — ONLY when the role owns a product roadmap/backlog for a
    NON-target product line as the job AND the target list does NOT include an
    AI program/product-management function. If the target list includes "AI
    program management" (or similar), AI product/program/strategy roles are
    ON-TARGET -> "none". Never put an AI product/program/strategy role here for
    this candidate.

  - generic-pm — ONLY when the program's real SUBSTANCE is a clearly NON-target
    domain — IT, telecom, infrastructure, ERP rollout, asset management,
    security-program ops, generic business operations — that merely name-drops
    AI or uses AI tools. A program/project whose substance IS AI/data
    enablement, adoption, transformation, or governance (even with budget
    tracking, status reporting, or change-management duties) is ON-TARGET ->
    "none".

  - customer-account / sales-gtm — ONLY external account ownership for the
    VENDOR's revenue: renewals, expansion, upsell, quota, pipeline, QBRs,
    book-of-business, retention targets, or enabling sellers / pre-sales /
    capture / proposal-bid operations. An internal or delivery-side role whose
    labor is AI deployment, implementation, enablement, adoption, or engagement
    DELIVERY (not revenue ownership) is on-target -> "none". If the role mixes
    account relationship with real AI-adoption delivery and revenue ownership
    is not clearly the bulk, return "none".

------------------------------------------------------------
STEP 4 — TIE-BREAKER (strongly weighted toward "none").
------------------------------------------------------------
Return "none" whenever ANY of these is true:
  - The bulk of duties is not CLEARLY a single different profession.
  - The JD is thin, generic, or boilerplate (you cannot read a real
    duty-by-duty majority).
  - The role plausibly contains substantial target labor (AI strategy /
    enablement / adoption / governance / program-management / change /
    consulting) even alongside other duties.
  - Off-target TOPIC but target LABOR (e.g. building an AI-adoption program at
    a non-AI company) -> "none".
Only flag when target topic is present but the LABOR is unmistakably a
different discipline (e.g. owning customer renewals/quota for an AI product ->
customer-account), or when neither topic nor labor is target at all.

Always judge against THIS candidate's target functions above — the same role
is on-target for one candidate and off-target for another. Never carry a fixed
notion of which functions are "good," and never flag for seniority.
"""
        base += "\n\n" + _off_target_function_gate.strip("\n").replace(
            "{TARGET_FUNCTIONS_LIST}", _target_functions_list
        ) + "\n"

    return base


async def stage2_triage(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    # v0.3.13: env-var override for benchmarking. Flash has very high TPM
    # ceiling (~1000 RPM/key × 3 keys = 3000 RPM), so concurrency=8 was
    # historically very conservative. Bumping to 16-30 should be safe.
    # Set STAGE2_CONCURRENCY env var to override.
    concurrency: int = int(__import__("os").environ.get("STAGE2_CONCURRENCY", "16")),  # v0.3.38: 8->16 (comment above: 16-30 safe; round-robin 3-key Worker; ~480 RPM << 3000)
    # v0.3.5.2: cache RE-ENABLED with two-layer safety net.
    #
    # History: v0.3.5 hit 71% cache failures (`403 PERMISSION_DENIED —
    # CachedContent not found`). Root cause: Gemini cache propagation lag —
    # the cache resource is created on one serving node, but parallel calls
    # within seconds get routed to OTHER nodes that haven't yet seen the
    # replication. The error wording is misleading; the API key has full
    # permission, the cache just isn't visible yet on that node.
    #
    # v0.3.5.2 fix:
    #   1. PRE-WARM: after create_cache() returns, fire one sequential
    #      completion using the cache. This validates the cache is reachable
    #      AND lets propagation flow before the parallel burst kicks off.
    #      If pre-warm fails, we disable cache for the run (use inline
    #      system prompt) — failing safely.
    #   2. PER-CALL FALLBACK: each Stage 2 call goes through
    #      _complete_with_cache_fallback, which retries WITHOUT cache on
    #      `CachedContent not found` errors. Stragglers that miss
    #      propagation still get a correct LLM score — they just don't
    #      benefit from the 75% input discount.
    #
    # Net effect: cache works for the majority that propagated; the rest
    # self-heal. Score quality is preserved (no more title-floor garbage
    # for cache-failed roles). The $0.05-0.21/run savings comes back.
    use_cache: bool = True,
    run_id: Optional[str] = None,
    # Flash supports thinking_budget=0 for cheapest+fastest. Pro requires >=1.
    # The explicit anti-pattern + scoring rubric in the prompt does most of the
    # work; thinking adds little for this kind of structured scoring.
    thinking_budget: Optional[int] = 0,
) -> list[Role]:
    """Score every role with Gemini Pro Stage 2.

    use_cache: try Gemini context caching for the system prompt (75% off).
               Falls back gracefully if cache creation fails (system prompt
               is too short for caching threshold).

    thinking_budget: how many thought tokens Pro can use per role. 256 is
                     a small budget that meaningfully helps reasoning without
                     blowing up cost. Set to 0 for absolute cheapest.
    """
    if not roles:
        return []

    client = client or get_llm_client()
    if hasattr(client, "current_run_id"):
        client.current_run_id = run_id  # type: ignore[attr-defined]
        client.current_stage = "stage2"  # type: ignore[attr-defined]

    profile_text = _profile_block(profile)

    # v0.3.8: build the per-run dynamic system prompt with user-specific
    # hard exclusions injected at the top. See build_dynamic_stage2_prompt
    # docstring. The cache is keyed on this prompt, so different profiles
    # get different cache entries — but cache is per-run anyway (fresh
    # CachedContent created per call to this function), so cache hit rate
    # is unaffected.
    system_prompt = build_dynamic_stage2_prompt(profile)

    cached_content = None
    if use_cache:
        # Try to create a context cache; fall back to inline system prompt.
        #
        # v0.3.15 (P1.9): TTL CUT BACK from 86400s (24h) -> 3600s (1h).
        # The 24h TTL was added in v0.3.9 to enable same-day rerun cache
        # hits, but the audit found the storage cost ($1/MTok-hour for
        # Flash-family) was paying for 23 hours we didn't use — a typical
        # search completes in ~20 minutes. At ~14K cached prompt tokens,
        # 24h TTL costs ~$0.34/run in storage; 1h TTL costs ~$0.014.
        # Within-run cache hits are unaffected (run finishes well under 1h).
        # Same-day reruns lose the cache benefit but profile-build cache
        # (P1.12) and JD score cache (P1.6) cover the cross-run reuse case
        # at the role level, which is more directly tied to actual savings.
        cached_content = await client.create_cache(
            model=config.STAGE2_MODEL,
            system=system_prompt,
            ttl_seconds=3600,
        )

        # PRE-WARM (v0.3.5.2): force Gemini to validate + propagate the cache
        # before the parallel burst hits. Without this, 71% of parallel calls
        # at production scale failed with "CachedContent not found" because
        # the cache hadn't propagated to all serving nodes. A single
        # sequential call up front:
        #   - Confirms the cache is actually usable on this account
        #   - Gives Gemini's cache infrastructure a few seconds of "real
        #     world" propagation time before 280 parallel calls fire
        #   - If pre-warm itself fails, we know cache is broken for THIS
        #     run and switch to inline system prompt for safety
        if cached_content is not None:
            try:
                await client.complete(
                    model=config.STAGE2_MODEL,
                    system=None,  # cache supplies system instruction
                    user="ping",
                    max_output_tokens=8,
                    temperature=0.0,
                    cached_content=cached_content,
                    is_batch=False,
                    thinking_budget=0,
                )
            except Exception:
                # Pre-warm failed — cache is broken or propagation is way
                # off. Disable cache for this run; per-call fallback would
                # still work, but better to skip the whole song-and-dance
                # and use the inline path that we know works.
                cached_content = None

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _score_one(
            r, profile_text, client, semaphore,
            cached_content=cached_content,
            is_batch=False,
            thinking_budget=thinking_budget,
            system_prompt=system_prompt,  # v0.3.8 dynamic prompt
        )
        for r in roles
    ]
    scored = await asyncio.gather(*tasks)

    # W70 honesty gate: if an AI-service outage / key exhaustion / budget cap /
    # network loss corrupted at least half the batch, refuse to present a
    # dashboard of fabricated scores. Fail the run so the user retries instead
    # of acting on garbage. A minority of transient failures (below threshold)
    # just drop out as unscored — never fabricated. This is the single most
    # important "never lie about results" safeguard in the scoring path.
    infra_failures = sum(
        1 for r in scored
        if (r.stage2_reasoning or "").startswith("stage2_infra_error:")
    )
    if infra_failures and infra_failures >= max(5, (len(scored) + 1) // 2):
        raise RuntimeError(
            f"Stage 2 scoring failed for {infra_failures}/{len(scored)} roles due "
            f"to an AI-service outage or a daily/budget cap. The run was stopped "
            f"so you are never shown fabricated matches — please try again later."
        )
    if infra_failures:
        print(f"[stage2] {infra_failures}/{len(scored)} roles dropped on transient "
              f"infra errors (below fail threshold; left unscored, not fabricated)")

    # Title floor enforcement — v0.3.9 GRADUATED RELAXATION.
    #
    # History:
    #   v0.3.4: code-level floor added (3+ word -> 70, 2 -> 65, 1 -> 55).
    #           Worked but over-applied (28% of qualifying roles in v3.6
    #           were floor-determined — way too high for a "safety net").
    #   v0.3.8: STRICT mode — floor only fires on Stage 2 failures. Result:
    #           dropped 41 qualifying roles, lost 7 STRONGs, regressed
    #           overall (-22% qualifying vs v3.7).
    #   v0.3.9: GRADUATED RELAXATION (peer review consensus). Apply floor
    #           always, but with REDUCED levels:
    #             3+ word overlap -> floor 60 (was 70, was strict-off in v3.8)
    #             2-word          -> floor 55 (was 65, was strict-off in v3.8)
    #             1-word          -> no floor (was 55)
    #           Preserves protection for strong 2-3 word title matches
    #           (real STRONGs in adjacency cases) while eliminating the
    #           1-word over-inflation that produced 28 of 41 floor-
    #           determined roles in v3.6.
    #
    # The graduated approach trusts Stage 2's calibration for weak (1-word)
    # overlaps but preserves the safety net for strong title matches that
    # Stage 2 might under-score on domain hesitation.
    floors_applied = 0
    floors_skipped = 0
    for role in scored:
        if role.stage2_score is None:
            continue

        new_score, tag = apply_floor_to_score(
            score=role.stage2_score,
            role_title=role.job_title,
            candidate_headline=profile.headline,
            candidate_target_functions=profile.target_functions,
            mode="off",  # v0.3.35: title-floor DISABLED (Principle 8) — a title never floors a score
        )
        if tag is not None:
            role.stage2_score = new_score
            role.stage2_tier = score_to_tier(new_score)
            role.stage2_reasoning = f"{tag} {role.stage2_reasoning or ''}".strip()[:1000]
            floors_applied += 1
        else:
            floors_skipped += 1

    print(
        f"[stage2] title-floor DISABLED (mode=off, Principle 8); "
        f"floored={floors_applied} (expected 0)"
    )
    return scored


def _stage2_succeeded(role: Role) -> bool:
    """Determine whether Stage 2 produced a usable evaluation.

    Strict-mode floor (v0.3.8) only fires when this returns False.

    A 'successful' Stage 2 has:
      - stage2_score is not None
      - stage2_reasoning is non-empty and >= 30 chars
      - stage2_reasoning doesn't start with 'stage2_error' (the cache-
        fallback error tag from the per-call retry code)
    """
    if role.stage2_score is None:
        return False
    reasoning = role.stage2_reasoning or ""
    if not reasoning or len(reasoning) < 30:
        return False
    if reasoning.startswith("stage2_error"):
        return False
    return True

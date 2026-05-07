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
v0.3.11 — ANTI-CLUSTERING (mandatory)
================================================================

Production audit of v0.3.6-v0.3.9 revealed your scores cluster heavily
at exact anchor values. In a typical 280-role run:

  s2 = 78:  18 roles (clustered)
  s2 = 68:  41 roles (HUGE CLUSTER)
  s2 = 58:  40 roles (HUGE CLUSTER)
  s2 = 55:  20 roles
  s2 = 43:  24 roles
  s2 = 30:  17 roles

That's 160 of 280 roles at exactly 6 anchor values. You're using these
as buckets. STOP IT.

FORBIDDEN values for v0.3.11: 58, 68, 78. If you find yourself rounding
to one of these, force a 1-3 point shift based on a specific concern
severity differential. Examples:

  Role: borderline GOOD with one concern about seniority
        → DO NOT score 68. Score 65, 67, 69, 71, 73, or 74.
        → Pick based on how severe the seniority concern is.

  Role: clearly strong fit with one minor concern about industry
        → DO NOT score 78. Score 75, 77, 79, or 80.
        → Pick based on how related the industry is.

  Role: weak-fit STRETCH with promising title
        → DO NOT score 58. Score 55, 57, 59, 61, 63.
        → Pick based on how close the title is to target.

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
    - Title fits, JD fits → score 70-90
    - Title fits, JD partially fits → score 50-65
    - Title fits, JD reveals different work → score 30-45

  Example pattern: "Senior X Consultant" but the JD requires hands-on
  technical work the candidate's resume doesn't show.

PRINCIPLE 2: TECHNICAL-vs-NONTECHNICAL MISMATCH
  Candidates roughly fall into "hands-on technical" or "advisory/strategy"
  archetypes. Reading the candidate profile, judge which archetype they fit:

    - If candidate is non-technical (consulting, strategy, business, ops)
      and JD requires hands-on coding, ML model training, infrastructure
      build, or data pipelines → score 25-40 even if title sounds right.
    - If candidate is technical (engineering, data, ML) and JD is pure
      advisory with no build component → similar penalty in reverse.

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
     → Score normally (60-90 if company/JD fit). Sales-targeting candidate.

  ELSE (candidate's profile has NO sales-intent signals):
     → Score 20-35. These roles carry individual quota responsibility
       that is fundamentally different work from non-sales functions.
       The title-floor (Principle 8) does NOT apply — even a strong
       title-headline overlap on words like "manager" or "consultant"
       does not rescue a pure-sales role for a non-sales candidate.

  GTM-adjacent titles ("GTM Strategy", "GTM Operations", "Go-to-Market",
  "Solutions Consultant", "Customer Success Manager") are SOFTER:
     - If candidate targets GTM/customer success → score normally
     - Otherwise → apply -10 to score (not a hard cap; just a tilt)
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
    Candidate without engineering background → score 30-45 even if company
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

PRINCIPLE 8: TITLE-HEADLINE OVERLAP FLOOR (graduated, v0.3.3)
  Count CONTENT words from the role title that also appear in the candidate's
  headline OR target_functions list. Filler words don't count: ignore
  "and", "or", "of", "the", "for", "a", "an", "to", "with", "in", "on",
  "at", "by", "as", "manager", "lead", "senior", "junior", "associate",
  "II", "III", "IV", "principal", "staff" and other standalone seniority
  modifiers. Look for substantive function/domain nouns ("Operations",
  "Strategy", "Marketing", "Program", "Engineering", "AI", "Enablement",
  "Governance", "Federal", "Clinical", "Product", "Design", "Sales",
  "Finance", etc.).

  Apply the floor based on overlap count:

    3+ content words match → base score FLOORS at 70 (was 70, unchanged)
    2 content words match  → base score FLOORS at 65 (NEW in v0.3.3)
    1 content word matches the candidate's CORE function noun (the
                             function explicitly named in their headline
                             or top target_function) → FLOORS at 55
                             (NEW in v0.3.3)

  Why graduated (v0.3.3): the prior binary 3-word floor failed on legitimate
  1-2 word overlaps like "Operations Manager" (1 content word after stopword
  removal) for an Operations-headline candidate. Audit data showed roles
  like "Marketing Operations Manager" landing at 47-58 because the binary
  floor didn't trigger. The graduated floor protects partial title matches
  proportional to overlap strength.

  Domain or seniority concerns can reduce by 5-10 points from the floor,
  but the floor itself holds. This rule still respects PRINCIPLE 1 (title
  fits, JD disagrees) — if the JD describes meaningfully different work,
  the floor doesn't apply. But DOMAIN concerns alone (industry mismatch,
  sector preference) shouldn't push a strong title-headline match below
  the applicable floor.

  Example: candidate headline "AI Strategy and Enablement Consultant" vs
  role "AI Strategy Consultant" shares 3+ content words → floor at 70.
  Candidate headline "Operations and Strategic Support Leader" vs role
  "Marketing Operations Manager" shares 1 core-function word ("Operations")
  → floor at 55, even though "Marketing" is a sub-domain modifier the
  candidate doesn't share.

PRINCIPLE 9: TITLE PATTERNS REQUIRING CAREFUL JD READING
  Some title patterns reliably mislead. Read the JD carefully before scoring
  these high or low — don't auto-reject:

  - "AI Consultant" / "Senior AI Consultant" / "AI Solutions Consultant":
    Score 55-75 if JD emphasizes strategy, readiness assessment, adoption
    workshops, change management, or client advisory WITHOUT requiring SQL/ML
    /cloud engineering. Score 25-40 if JD requires hands-on technical build
    (RAG, ML models, data pipelines). At AI-native firms (Anthropic, Snorkel,
    Launchpad, OneStream, Quandri), Microsoft partners (Quisitive, Interlink,
    Ahead), and Big 4 (Deloitte, PwC, EY, Accenture) — assume strategy/
    advisory unless JD explicitly proves otherwise.
  - "Forward Deployed Strategist" / "Forward Deployed Consultant" / "Advisory
    AI Strategist": Score 55-70 if client-facing strategy/enablement without
    software engineering requirements. "Forward Deployed Engineer" with
    coding requirements is still a 25-35.
  - "Engagement Manager" with AI context: Score 55-70 if managing AI delivery
    engagements, client relationships, or portfolios. Bump for senior
    consulting candidates UNLESS JD explicitly says "must carry individual
    sales quota."
  - "AI Strategy Consultant" / "AI Strategist" / "Senior AI Strategist":
    Score 55-75 at any company. Do NOT reject for "requires MBB background"
    unless JD explicitly says "MBB experience required." "Strategy consulting
    background preferred" is different from "MBB required."
  - "Federal AI Strategy" / "Public Sector AI Consultant" / "AI Modernization
    Consultant": Score 60-80 for federal-experienced candidates. These are
    target matches.

PRINCIPLE 10: INDUSTRY-WEIGHT ADJUSTMENT (PROPORTIONAL — v0.3.4)
  After computing a base score from function/seniority/JD-fit signals, apply
  a PROPORTIONAL industry-fit adjustment. Use the candidate's target_industries
  list and the role's company / industry context to classify:

    SAME industry (role's industry is in candidate's target_industries
                   OR is an obvious sub-bucket of one — e.g., "fintech" ⊂
                   "Financial Services", "biotech" ⊂ "Life Sciences"):
      → Apply NO adjustment (the base score already credits fit).

    ADJACENT industry (one step removed but plausible — e.g.,
                       healthcare ↔ pharma ↔ life sciences,
                       financial services ↔ insurance ↔ fintech,
                       consulting ↔ professional services,
                       SaaS ↔ enterprise software,
                       CPG ↔ retail ↔ consumer goods):
      → Apply 0 adjustment (no penalty, no bonus). These are legitimate
        cross-sector pivots.

    OFF-TARGET industry (not in target_industries AND not adjacent):
      → Apply -10 to the base score.

    VERY-OFF-TARGET industry (the candidate's profile + freeform context
                              gives strong signal they would NOT pursue
                              this — e.g., a federal-only candidate vs
                              a startup, a healthcare consultant vs
                              construction, a non-AI candidate vs an AI
                              research lab):
      → Apply -15 to the base score.

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
HANDLING THIN/MISSING JDS
============================================================
- If JD is missing entirely: score using title + company + seniority signals.
  Be moderately optimistic — flag confidence as 0.4-0.5.
- If JD is < 200 chars: same approach.
- If JD is generic marketing copy (no responsibilities/requirements):
  score 35-45 with low confidence.
- Never score 80+ without substantive JD evidence.

Respond with strict JSON:
{
  "score": <integer 0-100>,
  "reasoning": "<2-3 sentences explaining the score>",
  "confidence": <float 0.0-1.0>,
  "key_match_signals": ["<signal1>", "<signal2>"],
  "key_concerns": ["<concern1>", "<concern2>"],
  "industry": "<one-word industry tag: Tech, Healthcare, Finance, CPG, Retail, Manufacturing, Energy, Government, Consulting, Education, Media, RealEstate, Logistics, Hospitality, Pharma, Insurance, Telecom, Other>",
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
        "is_dead_listing": {"type": "boolean"},
    },
    "required": ["score", "reasoning", "confidence"],
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
            # Fail-soft: assign neutral score with low confidence so the
            # role can still be considered downstream
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

    return base


async def stage2_triage(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    concurrency: int = 8,
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
        # Try to create a context cache; fall back to inline system prompt
        # v0.3.9: TTL bumped 3600s (1h) → 86400s (24h) per peer review.
        # Same-day re-runs reuse the cache without paying creation cost
        # again. Saves $0.03-0.05 per subsequent same-day run. The cache
        # is keyed on profile (which rarely changes intra-day), so this
        # is safe.
        cached_content = await client.create_cache(
            model=config.STAGE2_MODEL,
            system=system_prompt,
            ttl_seconds=86400,
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

    # Title floor enforcement — v0.3.9 GRADUATED RELAXATION.
    #
    # History:
    #   v0.3.4: code-level floor added (3+ word → 70, 2 → 65, 1 → 55).
    #           Worked but over-applied (28% of qualifying roles in v3.6
    #           were floor-determined — way too high for a "safety net").
    #   v0.3.8: STRICT mode — floor only fires on Stage 2 failures. Result:
    #           dropped 41 qualifying roles, lost 7 STRONGs, regressed
    #           overall (-22% qualifying vs v3.7).
    #   v0.3.9: GRADUATED RELAXATION (peer review consensus). Apply floor
    #           always, but with REDUCED levels:
    #             3+ word overlap → floor 60 (was 70, was strict-off in v3.8)
    #             2-word          → floor 55 (was 65, was strict-off in v3.8)
    #             1-word          → no floor (was 55)
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
            mode="graduated",
        )
        if tag is not None:
            role.stage2_score = new_score
            role.stage2_tier = score_to_tier(new_score)
            role.stage2_reasoning = f"{tag} {role.stage2_reasoning or ''}".strip()[:1000]
            floors_applied += 1
        else:
            floors_skipped += 1

    print(
        f"[stage2] floor: applied={floors_applied}, "
        f"skipped={floors_skipped} (mode=graduated)"
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

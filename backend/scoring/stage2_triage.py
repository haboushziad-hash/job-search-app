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
"perfect, apply today." Use the FULL range. Avoid clustering at round numbers
(40, 42, 45, 47, 50, 55, 68, 78, 88) — those are stale tier-band midpoints
or floor anchors, not signals. Pick a SPECIFIC score that matches your read
of the role: 31, 38, 43, 47, 51, 63, 81, etc.

CRITICAL on the low end: weak-fit and STRETCH-tier roles should be scored
across the full 25-49 range based on how close they are to a fit, NOT
clustered at 42 or 47. A role you're 10% confident about scores ~30. A
role you're 40% confident about scores ~46. A role at the borderline of
qualifying scores ~50. SPREAD them.

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


async def _score_one(
    role: Role,
    profile_text: str,
    client: LLMClient,
    semaphore: asyncio.Semaphore,
    cached_content: Optional[object] = None,
    is_batch: bool = False,
    thinking_budget: Optional[int] = None,
) -> Role:
    async with semaphore:
        prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE TO SCORE:\n{_role_block(role)}\n"
        )
        try:
            response = await client.complete(
                model=config.STAGE2_MODEL,
                system=None if cached_content else STAGE2_SYSTEM_PROMPT,
                user=prompt,
                max_output_tokens=2048,  # generous — JSON + thinking share this budget
                # Determinism: same role + same profile must produce same score.
                # Variance is noise on a structured numeric output.
                temperature=0.0,
                json_schema=_RESPONSE_SCHEMA,
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


async def stage2_triage(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    concurrency: int = 8,
    use_cache: bool = False,  # Flash is cheap enough that cache savings aren't worth complexity
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

    cached_content = None
    if use_cache:
        # Try to create a context cache; fall back to inline system prompt
        cached_content = await client.create_cache(
            model=config.STAGE2_MODEL,
            system=STAGE2_SYSTEM_PROMPT,
            ttl_seconds=3600,
        )

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [
        _score_one(
            r, profile_text, client, semaphore,
            cached_content=cached_content,
            is_batch=False,
            thinking_budget=thinking_budget,
        )
        for r in roles
    ]
    scored = await asyncio.gather(*tasks)

    # Code-level title floor enforcement (v0.3.4) — belt-and-suspenders for
    # PRINCIPLE 8 in the prompt. If the LLM scored a role below the floor
    # despite a strong title-headline overlap, raise the score to the floor
    # and tag the reasoning. This catches occasional violations the prompt
    # rule alone misses (audit data showed 1-content-word overlaps like
    # "Marketing Operations Manager" for an Operations candidate landing
    # at 47, below the 55 floor). The floor only RAISES; never lowers.
    for role in scored:
        if role.stage2_score is None:
            continue
        new_score, tag = apply_floor_to_score(
            score=role.stage2_score,
            role_title=role.job_title,
            candidate_headline=profile.headline,
            candidate_target_functions=profile.target_functions,
        )
        if tag is not None:
            role.stage2_score = new_score
            role.stage2_tier = score_to_tier(new_score)
            role.stage2_reasoning = f"{tag} {role.stage2_reasoning or ''}".strip()[:1000]

    return scored

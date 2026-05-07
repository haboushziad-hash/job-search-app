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

  3+ content words match → score FLOORS at 70 (was 55)
  2 content words match  → score FLOORS at 65
  1 content word matches the candidate's CORE function noun (the
                           function explicitly named in their headline
                           or top target_function) → score FLOORS at 55

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
    if role.stage2_score is not None:
        parts.append(f"STAGE2_PRELIM_SCORE: {role.stage2_score}")
    if role.stage2_reasoning:
        parts.append(f"STAGE2_REASONING: {role.stage2_reasoning}")
    jd = role.job_description_full or role.job_description_essence or ""
    if jd:
        parts.append(f"JOB_DESCRIPTION:\n{jd[:jd_max_chars]}")
    else:
        parts.append("JOB_DESCRIPTION: (not retrieved — score from title/company only)")
    return "\n".join(parts)


def needs_stage3(
    role: Role,
    *,
    skip_above: int = 101,
    primary_min: int = 55,
    second_look_min: int = 35,
    second_look_max: int = 54,
    second_look_confidence_max: float = 0.8,
) -> bool:
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
) -> Role:
    async with semaphore:
        prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE:\n{_role_block(role)}\n"
        )
        try:
            response = await client.complete(
                model=config.STAGE3_MODEL,
                system=STAGE3_SYSTEM_PROMPT,
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
                json_schema=_RESPONSE_SCHEMA,
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
    concurrency: int = 6,
    skip_above: int = 101,
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

    client = client or get_llm_client()
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

    tasks = [
        _score_one(r, profile_text, client, semaphore, thinking_budget=thinking_budget)
        for r in targets
    ]
    await asyncio.gather(*tasks)

    # Code-level title floor enforcement (v0.3.4) — same belt-and-suspenders
    # as Stage 2. The Stage 3 prompt has the GRADUATED TITLE-HEADLINE OVERLAP
    # FLOOR rule already; this is the post-LLM safety net.
    from backend.scoring.title_floor import apply_floor_to_score
    for role in targets:
        if role.stage3_score is None:
            continue
        new_score, tag = apply_floor_to_score(
            score=role.stage3_score,
            role_title=role.job_title,
            candidate_headline=profile.headline,
            candidate_target_functions=profile.target_functions,
        )
        if tag is not None:
            role.stage3_score = new_score
            role.stage3_analysis = (
                f"{tag} {role.stage3_analysis or ''}".strip()[:2000]
            )

    return roles

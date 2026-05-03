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

Score from 0 to 100 using the same rubric as Stage 2:
  85-100 STRONG / 70-84 GOOD / 55-69 MAYBE / 40-54 STRETCH / 0-39 SKIP

Respond with strict JSON:
{
  "score": <integer 0-100>,
  "tier_rationale": "<one sentence on why this tier>",
  "match_analysis": "<3-5 sentences on the strongest match signals>",
  "concerns": ["<concern1>", "<concern2>"],
  "application_strategy": "<2-3 sentences: what to emphasize in the application>",
  "best_resume_match": "<filename of best-fitting resume, or 'unified'>",
  "best_resume_reason": "<one sentence on why this resume>",
  "summary": "<EXACTLY 2 sentences. First sentence: what this role IS in plain English. Second sentence: the most important match signal or concern. Used as the dashboard card preview — keep it concrete and skimmable.>"
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


def _role_block(role: Role, jd_max_chars: int = 12000) -> str:
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
    skip_above: int = 88,
    primary_min: int = 55,
    second_look_min: int = 35,
    second_look_max: int = 54,
    second_look_confidence_max: float = 0.7,
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
    concurrency: int = 3,
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
    return roles

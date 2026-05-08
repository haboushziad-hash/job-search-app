"""Stage 1 — LLM pre-filter (Gemini Flash, no thinking).

This is the cheapest possible LLM-touch. After embedding pre-filter, we
still want one quick semantic check to kill obvious mismatches that
embeddings might let through (e.g., a role title that lexically matches
but is fundamentally for a different audience).

Per-role cost: ~$0.00002 (basically free).
Volume: typically all roles surviving embedding pre-filter (~40%).
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from backend.config import config
from backend.models import CandidateProfile, Role
from backend.scoring.llm_client import LLMClient, get_llm_client


STAGE1_SYSTEM_PROMPT = """\
You are an anti-pattern detector for a job search tool. Your ONLY job is to
filter out clearly invalid postings — NOT to judge fit. A downstream evaluator
handles fit assessment.

REJECT only if the posting matches a hard anti-pattern:
- "Submit your resume for future hiring" / "General candidate application" / "Join our talent pool" — these are not real roles
- Wrong industry entirely (e.g. plumbing when candidate is white-collar)
- Extreme seniority mismatch (internship when senior)
- Foreign-language posting when candidate is English-only
- "This position is no longer accepting applications" / "filled"

KEEP everything else, including marginal fits, mixed technical/strategy roles,
and roles with thin JDs. Default to KEEP.

Respond with strict JSON:
{
  "keep": true | false,
  "reason": "<one short sentence>"
}
"""


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["keep", "reason"],
}


def _profile_summary(profile: CandidateProfile) -> str:
    """One-paragraph candidate summary for inclusion in every Stage 1 prompt."""
    parts = []
    if profile.headline:
        parts.append(profile.headline)
    if profile.years_experience:
        parts.append(f"Experience: {profile.years_experience} years")
    if profile.target_seniority:
        parts.append(f"Seniority: {profile.target_seniority}")
    if profile.target_functions:
        parts.append("Targets: " + ", ".join(profile.target_functions[:6]))
    if profile.work_arrangements:
        parts.append("Arrangement: " + ", ".join(profile.work_arrangements))
    if profile.salary_minimum:
        parts.append(f"Min salary: ${profile.salary_minimum:,}")
    return ". ".join(parts)


def _role_summary(role: Role) -> str:
    """Compact role description for Stage 1 — title, company, first 800 chars of JD."""
    parts = [f"Title: {role.job_title}", f"Company: {role.company}"]
    if role.location:
        parts.append(f"Location: {role.location}")
    if role.location_type:
        parts.append(f"Arrangement: {role.location_type}")
    if role.salary_text:
        parts.append(f"Salary: {role.salary_text}")
    if role.seniority_level:
        parts.append(f"Seniority: {role.seniority_level}")
    jd = role.job_description_essence or role.job_description_full or ""
    if jd:
        parts.append(f"JD excerpt: {jd[:800]}")
    return "\n".join(parts)


async def _filter_one(
    role: Role,
    profile_text: str,
    client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> Role:
    """Decide keep/reject for a single role."""
    async with semaphore:
        prompt = (
            f"CANDIDATE PROFILE:\n{profile_text}\n\n"
            f"ROLE:\n{_role_summary(role)}\n\n"
            f"Decide: keep or reject?"
        )
        try:
            response = await client.complete(
                model=config.STAGE1_MODEL,
                system=STAGE1_SYSTEM_PROMPT,
                user=prompt,
                max_output_tokens=128,
                temperature=0.0,
                json_schema=_RESPONSE_SCHEMA,
                thinking_budget=0,  # Disable thinking — Stage 1 is supposed to be cheap
            )
            data = response.parsed_json
            if not isinstance(data, dict):
                # Fallback: parse manually
                data = json.loads(response.text) if response.text else {}
            role.stage1_keep = bool(data.get("keep", True))  # fail-open
            role.stage1_reason = str(data.get("reason", ""))[:200]
        except Exception as e:
            # Fail-open on any error: assume keep, log reason
            role.stage1_keep = True
            role.stage1_reason = f"stage1_error: {type(e).__name__}"
    return role


async def stage1_prefilter(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    # v0.3.13: env-var override for benchmarking. Stage 1 calls are tiny
    # (128 max output tokens, 0 thinking) so we can push concurrency
    # aggressively. STAGE1_CONCURRENCY env var allows runtime tuning.
    concurrency: int = int(__import__("os").environ.get("STAGE1_CONCURRENCY", "10")),
    run_id: Optional[str] = None,
) -> list[Role]:
    """Run Stage 1 pre-filter across roles. Returns only roles flagged keep=True.

    Mutates each input role's `stage1_keep` and `stage1_reason` fields.
    """
    if not roles:
        return []

    client = client or get_llm_client()
    if hasattr(client, "current_run_id"):
        client.current_run_id = run_id  # type: ignore[attr-defined]
        client.current_stage = "stage1"  # type: ignore[attr-defined]

    profile_text = _profile_summary(profile)
    semaphore = asyncio.Semaphore(concurrency)

    tasks = [_filter_one(r, profile_text, client, semaphore) for r in roles]
    processed = await asyncio.gather(*tasks)

    return [r for r in processed if r.stage1_keep]

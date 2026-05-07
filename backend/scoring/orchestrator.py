"""Scoring orchestrator — runs the full Gemini cascade end-to-end.

Pipeline:
  1. Embedding pre-filter      (Gemini embedding-001)   — kills bottom 60%
  2. Stage 1 LLM pre-filter    (Gemini Flash, no think) — kills obvious nos
  3. Stage 2 triage            (Gemini Pro + cache)     — full score
  4. Stage 3 deep eval         (Gemini Pro + thinking)  — 55-87 band only
  5. Final score assembly      (in-memory)              — picks best of {S2, S3}

Returns a list of fully-scored Role objects + a RunSummary with cost data.
"""
from __future__ import annotations

import time
import uuid
from typing import Callable, Optional

from backend.models import CandidateProfile, Role, RunSummary, score_to_tier, Tier
from backend.scoring import cost_tracker
from backend.scoring.embedding_filter import filter_roles_by_embedding
from backend.scoring.llm_client import LLMClient, get_llm_client
from backend.scoring.stage1_prefilter import stage1_prefilter
from backend.scoring.stage2_triage import stage2_triage
from backend.scoring.stage3_deep_eval import stage3_deep_eval


def _apply_salary_realism_penalty(
    role: Role, profile_min: Optional[int]
) -> None:
    """Penalize bottom-heavy salary ranges where the actual offer is
    statistically likely to land below the candidate's minimum.

    v0.3.7 — added in response to Hitachi Vantara case from v0.3.5/v0.3.6:
    role scored 93 STRONG with salary_min $75K against profile minimum
    $130K. The salary range was $75K-$138K — the bottom is 42% below
    minimum and the top barely clears it. Hiring manager negotiations
    typically anchor on the midpoint, which puts the likely offer at
    $90-110K — well below the user's threshold. Score 93 is misleading.

    Trigger conditions (BOTH must be true):
        salary_min < profile_min * 0.70    (bottom is well below floor)
        salary_max < profile_min * 1.50    (top doesn't compensate)

    Why both — this avoids penalizing roles like $90K-$200K (max above
    floor + 50% suggests genuine top-end potential) while catching
    Hitachi-style bottom-heavy ranges.

    Penalty: -15 to the final score, capped at floor 0. Tagged in
    stage3_analysis (or stage2_reasoning if no Stage 3) for audit.

    Universal: thresholds are RELATIVE percentages, scaling correctly to
    any salary_minimum from $50K nurses to $300K execs.
    """
    if not profile_min:
        return
    if role.salary_min is None or role.salary_max is None:
        return
    if role.final_score is None or role.final_score <= 0:
        return

    min_threshold = profile_min * 0.70
    max_threshold = profile_min * 1.50
    if not (role.salary_min < min_threshold and role.salary_max < max_threshold):
        return

    # Trigger penalty
    old_score = role.final_score
    new_score = max(old_score - 15, 0)
    tag = (
        f"[salary-penalty:-15 — ${role.salary_min // 1000}K-"
        f"${role.salary_max // 1000}K vs ${profile_min // 1000}K floor]"
    )
    role.final_score = new_score
    role.final_tier = score_to_tier(new_score)

    # Tag in whichever reasoning field is populated, prefer stage3 since
    # it's the user-visible application_strategy text downstream
    if role.stage3_analysis:
        role.stage3_analysis = f"{tag} {role.stage3_analysis}"[:2000]
    if role.stage2_reasoning:
        role.stage2_reasoning = f"{tag} {role.stage2_reasoning}"[:1000]


def _finalize_score(role: Role, profile: Optional[CandidateProfile] = None) -> None:
    """Pick the final score + tier for a role.

    Stage 3 wins if it ran; otherwise Stage 2 stands.

    v0.3.7: after picking the base final score, apply the salary realism
    penalty if the candidate's profile_min exists and the role's range
    is bottom-heavy.
    """
    if role.stage3_score is not None:
        role.final_score = role.stage3_score
        role.final_tier = role.stage3_tier or score_to_tier(role.stage3_score)
    elif role.stage2_score is not None:
        role.final_score = role.stage2_score
        role.final_tier = role.stage2_tier or score_to_tier(role.stage2_score)
    else:
        role.final_score = 0
        role.final_tier = Tier.SKIP

    # Salary realism penalty (v0.3.7)
    if profile is not None:
        _apply_salary_realism_penalty(role, profile.salary_minimum)


def _backpop_salary_from_reasoning(role: Role) -> None:
    """When the structured salary fields are empty but Stage 2/3 reasoning
    mentions a salary range (the LLM read it from buried JD prose), extract
    and back-populate the salary fields.

    Example: Komodo Health JDs bury "$125,000 to $185,000 (excluding SF/NYC)"
    in a paragraph that the regex extractor doesn't recognize as labeled,
    but the Opus reasoning often quotes it directly: "the role pays
    $125-185K depending on location."
    """
    if role.salary_min is not None or role.salary_max is not None:
        return
    text = (role.stage3_analysis or "") + " " + (role.stage2_reasoning or "")
    if not text.strip():
        return
    # Lazy import to avoid circular
    from backend.filter.salary_extractor import extract_salary_from_jd
    salary_text, smin, smax = extract_salary_from_jd(text)
    if smin is not None or smax is not None:
        role.salary_text = role.salary_text or salary_text
        role.salary_min = role.salary_min or smin
        role.salary_max = role.salary_max or smax


def _tier_breakdown(roles: list[Role]) -> dict[str, int]:
    counts = {"STRONG": 0, "GOOD": 0, "MAYBE": 0, "STRETCH": 0, "SKIP": 0}
    for r in roles:
        if r.final_tier:
            counts[r.final_tier.value] = counts.get(r.final_tier.value, 0) + 1
    return counts


async def score_roles(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    client: Optional[LLMClient] = None,
    license_key: Optional[str] = None,
    embed_keep_fraction: float = 0.40,
    embed_max_roles: int = 500,
    stage3_skip_above: int = 101,
    stage3_skip_below: int = 55,
    stage3_max_roles: int = 100,
    log: bool = True,
    progress: Optional[Callable[[int, str, int], None]] = None,
) -> tuple[list[Role], RunSummary]:
    """Run the full scoring cascade. Returns (scored_roles, run_summary)."""
    run_id = str(uuid.uuid4())
    cost_tracker.start_run(run_id=run_id, license_key=license_key)
    started_at = time.perf_counter()

    client = client or get_llm_client()
    # Tag every call with the run id so the cost log is queryable per-run
    if hasattr(client, "current_run_id"):
        client.current_run_id = run_id            # type: ignore[attr-defined]
        client.current_license_key = license_key  # type: ignore[attr-defined]

    initial_count = len(roles)
    if log:
        print(f"[orchestrator] run {run_id[:8]} starting with {initial_count} roles")

    # Local progress emitter — best-effort 4-arg call, swallows errors so
    # progress callback bugs can't abort the actual scoring run.
    def _emit(pct: int, stage: str, step_index: int, detail: str = "") -> None:
        if progress is None:
            return
        try:
            try:
                progress(pct, stage, step_index, detail)
            except TypeError:
                progress(pct, stage, step_index)
        except Exception as e:
            if log:
                print(f"[progress] callback raised (ignored): {e}")

    # ---- 1. Embedding pre-filter ----
    _emit(50, "Embedding pre-filter", 4, f"Comparing {initial_count:,} roles against your profile semantically...")
    if hasattr(client, "current_stage"):
        client.current_stage = "embedding"  # type: ignore[attr-defined]
    after_embed = await filter_roles_by_embedding(
        profile=profile,
        roles=roles,
        keep_fraction=embed_keep_fraction,
        max_roles=embed_max_roles,
        client=client,
    )
    if log:
        print(f"[orchestrator] after embedding pre-filter: {len(after_embed)} / {initial_count}")

    # ---- 2. Stage 1 LLM pre-filter ----
    _emit(60, "Scoring with AI cascade", 5, f"Stage 1 anti-pattern check on {len(after_embed)} roles (Flash)...")
    after_stage1 = await stage1_prefilter(
        profile=profile,
        roles=after_embed,
        client=client,
        run_id=run_id,
    )
    if log:
        print(f"[orchestrator] after Stage 1: {len(after_stage1)}")

    # ---- 3. Stage 2 triage ----
    _emit(70, "Scoring with AI cascade", 5, f"Stage 2 triage scoring {len(after_stage1)} roles (Flash, ~2s each)...")
    scored = await stage2_triage(
        profile=profile,
        roles=after_stage1,
        client=client,
        run_id=run_id,
    )
    if log:
        s2_qualifying = [r for r in scored if (r.stage2_score or 0) >= 55]
        print(f"[orchestrator] after Stage 2: {len(scored)} scored, {len(s2_qualifying)} qualifying (>=55)")

    # ---- 4. Stage 3 deep eval (in-band roles only) ----
    # v0.3.7: skip_below reverted 58 → 55. v0.3.5 had bumped this to 58
    # for cost savings ($0.03-0.05/run), but production audit of the v0.3.6
    # run revealed 21 roles stuck at exactly 55 because Stage 2's score +
    # title-floor put them at 55, and the 58 cutoff prevented Stage 3 from
    # promoting any of them. Stage 3's average uplift is +8.6 points; a
    # role at 55 typically lands at ~63-64 with Pro evaluation, occasionally
    # higher for genuinely strong matches. The five roles we lost most
    # visibly to the 58 cutoff (NVIDIA AI Strategist, Leidos AI Lead,
    # VirtualVocations AI Transformation, monō ai Pod Lead) are exactly
    # the kind of roles Stage 3 should evaluate.
    #
    # Cost trade: ~+$0.05-0.10/run (15-25 more roles entering Pro). Worth
    # it for recovering legit GOOD/STRONG candidates wrongly capped at 55.
    #
    # max_roles=100 caps the worst-case Stage 3 spend on big-pool runs.
    #
    # Note: s2_in_band is the subset entering Pro deep-eval (stage2_score
    # in [skip_below, skip_above)). It is NOT the final qualifying count —
    # STRETCH roles in [40, skip_below) qualify too via Stage 2 alone. We
    # word the message accordingly so the user doesn't see "Stage 3
    # reviewing N qualifying" and then "Final: M qualifying" with M > N.
    in_band_roles = [
        r for r in scored
        if r.stage2_score is not None
        and stage3_skip_below <= r.stage2_score < stage3_skip_above
    ]
    in_band_roles.sort(key=lambda r: r.stage2_score or 0, reverse=True)
    if stage3_max_roles is not None and len(in_band_roles) > stage3_max_roles:
        # Truncate to top-N and mark losers as Stage 3 skipped so their
        # stage2 score becomes their final score in _finalize_score below.
        # We do this by raising the *effective* skip_above for the cut-off
        # tail — set their stage3 to None (already the default) and let
        # the orchestrator's finalize step pick stage2 as the final.
        cutoff = in_band_roles[stage3_max_roles].stage2_score or stage3_skip_below
        # cutoff is the highest score still EXCLUDED from Stage 3.
        # Bumping skip_below for THIS run to one above cutoff effectively
        # truncates the tail. Use max() so we never lower the threshold.
        effective_skip_below = max(stage3_skip_below, cutoff + 1)
    else:
        effective_skip_below = stage3_skip_below
    s2_in_band = sum(
        1 for r in scored
        if r.stage2_score is not None
        and effective_skip_below <= r.stage2_score < stage3_skip_above
    )
    _emit(80, "Scoring with AI cascade", 5, f"Stage 3 deep evaluation on {s2_in_band} top-tier roles (Pro, ~5-10s each)...")
    scored = await stage3_deep_eval(
        profile=profile,
        roles=scored,
        client=client,
        skip_above=stage3_skip_above,
        skip_below=effective_skip_below,
        run_id=run_id,
    )
    s3_count = sum(1 for r in scored if r.stage3_score is not None)
    if log:
        print(f"[orchestrator] after Stage 3: {s3_count} deep-evaluated")

    # ---- 5. Finalize scores + backfill salary + apply realism penalty ----
    # We backpop salary FIRST so the realism penalty in _finalize_score sees
    # any LLM-extracted salary that wasn't in the structured fields.
    for r in scored:
        _backpop_salary_from_reasoning(r)
        _finalize_score(r, profile=profile)

    qualifying = [r for r in scored if (r.final_score or 0) >= 40]
    qualifying.sort(key=lambda r: r.final_score or 0, reverse=True)

    # ---- Build run summary ----
    duration = int(time.perf_counter() - started_at)
    cost_tracker.finish_run(
        run_id=run_id,
        roles_scraped=initial_count,
        roles_qualifying=len(qualifying),
        duration_seconds=duration,
        status="completed",
    )

    cost_breakdown = cost_tracker.cost_by_run(run_id)
    breakdown = _tier_breakdown(scored)
    summary = RunSummary(
        run_id=run_id,
        license_key=license_key,
        roles_scraped=initial_count,
        roles_after_filter=len(after_embed),
        roles_after_stage1=len(after_stage1),
        roles_after_stage2=sum(1 for r in scored if r.stage2_score is not None),
        roles_qualifying=len(qualifying),
        tier_strong=breakdown.get("STRONG", 0),
        tier_good=breakdown.get("GOOD", 0),
        tier_maybe=breakdown.get("MAYBE", 0),
        tier_stretch=breakdown.get("STRETCH", 0),
        cost_stage1_usd=float(cost_breakdown.get("stage1", {}).get("cost", 0) or 0),
        cost_stage2_usd=float(cost_breakdown.get("stage2", {}).get("cost", 0) or 0),
        cost_stage3_usd=float(cost_breakdown.get("stage3", {}).get("cost", 0) or 0),
        cost_embeddings_usd=float(cost_breakdown.get("embedding", {}).get("cost", 0) or 0),
        cost_misc_usd=float(cost_breakdown.get("misc", {}).get("cost", 0) or 0),
        cost_total_usd=sum(
            float(v.get("cost", 0) or 0) for v in cost_breakdown.values()
        ),
        duration_seconds=duration,
        status="completed",
    )

    if log:
        print(f"[orchestrator] DONE — duration {duration}s, total cost ${summary.cost_total_usd:.4f}")
        print(f"  embed:    ${summary.cost_embeddings_usd:.4f}")
        print(f"  stage1:   ${summary.cost_stage1_usd:.4f}")
        print(f"  stage2:   ${summary.cost_stage2_usd:.4f}")
        print(f"  stage3:   ${summary.cost_stage3_usd:.4f}")
        print(f"  qualifying: {summary.roles_qualifying} "
              f"(STRONG={summary.tier_strong}, GOOD={summary.tier_good}, "
              f"MAYBE={summary.tier_maybe}, STRETCH={summary.tier_stretch})")

    return scored, summary

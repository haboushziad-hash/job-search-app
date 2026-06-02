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

from backend.config import config
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
    # v0.3.26 (UI1): idempotency guard. The JD score cache is model-agnostic and
    # hydrates across runs (even on force_refresh); a hydrated role already carries
    # the "[salary-penalty:…]" tag in its reasoning AND already had -15 baked into
    # its cached final_score. Re-finalizing it would subtract another 15 (score
    # corruption) and prepend a SECOND tag (the doubled "[salary-penalty] [salary-
    # penalty]" users saw). Skip if the penalty was already applied.
    if "[salary-penalty" in (role.stage3_analysis or "") or "[salary-penalty" in (role.stage2_reasoning or ""):
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


def _apply_scoring_guards(role: Role, profile: Optional[CandidateProfile] = None) -> None:
    """v0.3.24 (FIX 29) — anti-inflation guardrails from the 2026-05-30
    adversarial scoring audit (systematic one-directional over-scoring).

    Two deterministic, validated caps applied AFTER the base final score:

      P0-1  JD not capturable (SPA-redirect / JS-error / bot-wall / <250
            chars): Stage 3 hallucinated confident rationales on these stubs
            and boosted scores +28 to +44 (Avanade 63→91, Ramp 35→79). We
            can't trust ANY score derived from a stub, so cap the tier at
            MAYBE and flag for re-scrape.

      P0-3  Excluded work (hands-on engineering / sales-GTM) that evaded the
            TITLE filter because "Consultant"/"Lead"/"Enablement" titles
            dodge the pattern list — but the JD BODY is the excluded work
            (3Cloud, SoftServe, Saviance, Lattice, LaunchDarkly, Cresta).
            Cap at STRETCH so it can't surface as an apply-now match.

    Validated against the audit's 30-role sample: catches 8/8 flagged
    false-positives, leaves 5/5 confirmed-correct roles untouched. The
    standalone Stage2→Stage3 delta-clamp (P0-2) was intentionally NOT
    implemented — it would have wrongly demoted the legitimately-promoted
    "AI Enablement Lead 81→91"; P0-1 + P0-3 cover every audited hallucination.
    """
    if role.final_score is None or role.final_score <= 0:
        return
    from backend.scoring._scoring_guards import (
        jd_is_capturable, scan_excluded_body_signals,
        profile_targets_engineering, profile_targets_sales,
        title_off_target_for_profile, experience_seniority_gate, offtarget_engagement,
    )

    # P0-4 (v0.3.25): off-target TITLE → SKIP, PER-CATEGORY profile-gated. Flags a
    # legal/BD/revenue-ops/pre-sales/recruiting/marketing/finance title ONLY when
    # the candidate does NOT target that category — so a lawyer keeps "Counsel"
    # roles, a recruiter keeps "Recruiter" roles, a seller keeps "Account Exec"
    # roles, etc. (Engineering stays handled by the JD-body scan below.)
    off_cat = title_off_target_for_profile(role.job_title, profile)
    if off_cat:
        role.final_score = 30
        role.final_tier = Tier.SKIP
        flag = f"[off-target title ({off_cat}) — not in candidate's targets — capped at SKIP]"
        role.stage3_analysis = f"{flag} {role.stage3_analysis or ''}"[:2000]
        return

    # SC2 (v0.3.26): non-standard engagement (SkillBridge / internship / fellowship
    # / apprenticeship / volunteer) → SKIP. Not a target full-time role.
    eng = offtarget_engagement(role)
    if eng:
        role.final_score = 30
        role.final_tier = Tier.SKIP
        flag = f"[off-target engagement ({eng}) — not a target full-time role — capped at SKIP]"
        role.stage3_analysis = f"{flag} {role.stage3_analysis or ''}"[:2000]
        return

    # SC1 (v0.3.26): HARD experience / seniority cut-off (JD demands far more years
    # than the candidate has, or an exec-level title above their IC/Manager target)
    # → cap at STRETCH so it can't surface as an apply-now match. Profile-gated.
    gate = experience_seniority_gate(role, profile)
    if gate and (role.final_score or 0) > 46:
        role.final_score = 46
        role.final_tier = score_to_tier(46)
        flag = f"[seniority-gate ({gate}) — capped at STRETCH]"
        if role.stage3_analysis:
            role.stage3_analysis = f"{flag} {role.stage3_analysis}"[:2000]
        else:
            role.stage3_application_strategy = f"{flag} {role.stage3_application_strategy or ''}"[:1000]

    jd = role.job_description_full or role.job_description_essence or ""

    # P0-1: stub / uncapturable JD → cap at MAYBE (55) + re-scrape flag.
    if not jd_is_capturable(jd):
        if role.final_score > 55:
            role.final_score = 55
            role.final_tier = score_to_tier(55)
        flag = "[jd-not-captured: score capped at MAYBE — re-scrape recommended]"
        role.stage3_application_strategy = (
            f"{flag} {role.stage3_application_strategy or ''}"
        )[:1000]
        return  # a stub can't also be reliably body-scanned

    # P0-3: excluded body work evading the title screen → cap at STRETCH (46).
    # Only treat eng/sales as EXCLUDED for candidates who don't TARGET it (v0.3.25).
    sig = scan_excluded_body_signals(
        jd,
        check_eng=not profile_targets_engineering(profile),
        check_sales=not profile_targets_sales(profile),
    )
    if sig["strong"] and role.final_score > 46:
        kind = "engineering" if sig["engineering"] else "sales"
        hits = (sig["engineering"] or sig["sales"])[:3]
        role.final_score = 46
        role.final_tier = score_to_tier(46)
        flag = f"[excluded-{kind}-work in JD body ({', '.join(hits)}) — capped at STRETCH]"
        if role.stage3_analysis:
            role.stage3_analysis = f"{flag} {role.stage3_analysis}"[:2000]
        else:
            role.stage3_application_strategy = (
                f"{flag} {role.stage3_application_strategy or ''}"
            )[:1000]


# v0.3.25: tier score-bands (must match score_to_tier in models.py). Used to
# reconcile a displayed score with its final tier when a floor/resolver/guard
# moved one without the other.
_TIER_BANDS = {
    Tier.STRONG: (85, 100), Tier.GOOD: (70, 84), Tier.MAYBE: (55, 69),
    Tier.STRETCH: (40, 54), Tier.SKIP: (0, 39),
}

# v0.3.25 PERCENTILE TIERING — the FINAL tier authority. The cheap stack's scores
# compress/cluster, so fixed score-band cutoffs are fragile run-to-run and squeeze
# the bands. Instead we assign the final tier by RANK within each run's qualifying
# pool, using shares from the 4-run Opus consensus, with score FLOORS so a weak
# search can't manufacture fake STRONG/GOOD. Validated vs consensus: 50% exact /
# 91% within-1, reproduces the ~12/27/28/34% shape, adapts per run. (score_to_tier
# still gives each role its absolute per-stage tier; this overrides final_tier.)
_PCT_CUM = ((0.115, Tier.STRONG), (0.38, Tier.GOOD), (0.657, Tier.MAYBE))  # cumulative rank shares
_PCT_FLOOR = ((72, Tier.STRONG), (55, Tier.GOOD), (47, Tier.MAYBE))        # min score per tier
# ^ v0.3.26: STRONG floor 82→72. The cheap stack's scores compress into discrete
# clusters (a run's top band is ~76-81, then 63, 57…). At 82 the floor sat ABOVE
# the entire top cluster, so EVERY top-ranked role got floor-capped to GOOD →
# STRONG=0. 72 lets the real top cluster qualify; the rank share (top ~11.5%)
# still caps how many become STRONG, so a weak search can't manufacture fakes.
_TIER_ORD = {Tier.STRONG: 4, Tier.GOOD: 3, Tier.MAYBE: 2, Tier.STRETCH: 1, Tier.SKIP: 0}


def assign_percentile_tiers(qualifying: list) -> None:
    """Override final_tier by rank within the (score-desc sorted) qualifying pool,
    capped by score floors — the more conservative of rank-tier vs floor-tier. The
    displayed final_score is untouched; the tier becomes a per-run relative rank."""
    n = len(qualifying)
    if n == 0:
        return
    for rank, role in enumerate(qualifying):
        frac = rank / n
        rank_tier = Tier.STRETCH
        for cum, t in _PCT_CUM:
            if frac < cum:
                rank_tier = t
                break
        s = role.final_score or 0
        floor_tier = Tier.STRETCH
        for fl, t in _PCT_FLOOR:
            if s >= fl:
                floor_tier = t
                break
        # take the lower (more conservative) of rank vs floor; never re-promote SKIP
        if role.final_tier != Tier.SKIP:
            role.final_tier = rank_tier if _TIER_ORD[rank_tier] <= _TIER_ORD[floor_tier] else floor_tier


def _finalize_score(role: Role, profile: Optional[CandidateProfile] = None) -> None:
    """Pick the final score + tier for a role.

    Stage 3 wins if it ran; otherwise Stage 2 stands.

    v0.3.7: after picking the base final score, apply the salary realism
    penalty if the candidate's profile_min exists and the role's range
    is bottom-heavy.
    v0.3.24: then apply anti-inflation scoring guards (stub-JD + excluded-body).
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

    # Anti-inflation guards (v0.3.24 FIX 29) — applied last so they cap the
    # final tier regardless of which stage produced the base score.
    _apply_scoring_guards(role, profile)

    # v0.3.25: reconcile score <-> tier so the dashboard never shows a number
    # that disagrees with the tier. Drift sources: the Stage-2 title-floor raises
    # the score without re-tiering; the contradiction-resolver lowers the tier
    # without re-scoring. The TIER is the authoritative judgment, so clamp the
    # displayed score into the tier's band (minimal move to the nearest edge).
    if role.final_score is not None and role.final_tier is not None:
        band = _TIER_BANDS.get(role.final_tier)
        if band and not (band[0] <= role.final_score <= band[1]):
            role.final_score = band[1] if role.final_score > band[1] else band[0]


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
    # v0.3.9: bumped 0.40 → 0.55 per peer review. The 0.40 fraction was
    # cutting 60% of hard-filter survivors before any LLM evaluation. At
    # current scale (499 hard-filter passing → 199 kept), that's losing
    # ~3-5 legitimate STRONGs per run to embedding's blunt instrument.
    # 0.55 keeps ~275 of 499. Cost impact: +$0.04-0.06/run on Stage 2 Flash
    # (cheap). At v0.3.9 grinder scale (800-1200 hard-filter passing), the
    # 500-max cap will fire to keep cost bounded.
    embed_keep_fraction: float = 0.55,
    embed_max_roles: int = 500,
    # Wave-1 (2026-05-11): lowered 101 → 88 per Section 14.11.4 empirical
    # validation: 41/41 saved STRONGs in the 18-audit corpus were preserved
    # at threshold 88 (0 quality loss). Roles with stage2 ≥ 88 are clear
    # winners and don't need Stage 3 to confirm. Saves ~2-3 Stage 3 calls
    # per run at $0.014/call = ~$0.03-0.04/run.
    stage3_skip_above: int = 88,
    stage3_skip_below: int = 55,
    # v0.3.9: bumped 100 → 200 per peer review. v3.7 audit showed 154
    # roles in the [55, 100] Stage 2 band, but only 80 got Stage 3
    # evaluated — cap of 100 plus tie-boundary cuts at score=65. Result:
    # 75 in-band roles never got Pro evaluation, losing ~1-3 STRONGs and
    # 5-10 GOODs per run that would have been promoted by Stage 3's
    # +8.6 average uplift. Cap=200 covers the full band even at v3.9
    # grinder scale. Cost: +$0.10-0.20/run.
    stage3_max_roles: int = 200,
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

    # ---- 0. Cross-run JD score cache check (v0.3.9) ----
    # Before any scoring stages run, check the persistent cache for roles
    # we've already scored against this exact profile within the last 7
    # days. Cache hits skip Stage 2 + Stage 3 entirely.
    from backend.scoring import jd_score_cache
    profile_dump_for_hash = (
        profile.model_dump(mode="json") if hasattr(profile, "model_dump") else {}
    )
    prof_hash = jd_score_cache.profile_hash(profile_dump_for_hash)
    cache_hits: list[Role] = []
    cache_misses: list[Role] = []
    for r in roles:
        cached = jd_score_cache.lookup(prof_hash, r)
        if cached:
            jd_score_cache.apply_to_role(r, cached)
            cache_hits.append(r)
        else:
            cache_misses.append(r)
    if log:
        print(f"[orchestrator] JD score cache: {len(cache_hits)} hits, "
              f"{len(cache_misses)} misses (saving ${len(cache_hits)*0.006:.3f})")

    # ---- 1. Embedding pre-filter (only on cache misses) ----
    _emit(50, "Embedding pre-filter", 4, f"Comparing {len(cache_misses):,} new roles against your profile semantically (skipped {len(cache_hits)} cached)...")
    if hasattr(client, "current_stage"):
        client.current_stage = "embedding"  # type: ignore[attr-defined]
    after_embed = await filter_roles_by_embedding(
        profile=profile,
        roles=cache_misses,
        keep_fraction=embed_keep_fraction,
        max_roles=embed_max_roles,
        client=client,
    )
    if log:
        print(f"[orchestrator] after embedding pre-filter: {len(after_embed)} / {len(cache_misses)}")

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
    # Wave-1 Path B (2026-05-11): pre-Stage-3 LightGBM gate.
    # When PATH_B_GATE_ENABLED=True, the gate predicts likely Stage 3 score
    # for low-band roles (s2 in [55, 60)) and skips them if the model is
    # confidently LOW. ZERO STRONG loss on 18-audit corpus. Ships disabled
    # by default for Wave-1 launch; enable per-tester after Phase 1.5.
    path_b_skipped = 0
    if getattr(config, "PATH_B_GATE_ENABLED", False):
        try:
            from backend.scoring.path_b_gate import load_default_or_none
            _gate = load_default_or_none()
        except Exception as e:
            print(f"[path_b] load failed at runtime ({type(e).__name__}); gate disabled this run")
            _gate = None
        if _gate is not None:
            for r in scored:
                if r.stage2_score is None:
                    continue
                if not (effective_skip_below <= r.stage2_score < stage3_skip_above):
                    continue
                try:
                    decision = _gate.decide(r, profile)
                except Exception as e:
                    print(f"[path_b] decide failed for {r.job_title[:40]} ({type(e).__name__}); keeping role")
                    continue
                # Attach telemetry — these fields are best-effort, only
                # written if the Role model accepts them (Pydantic ignores
                # extras when extra='allow')
                try:
                    r.path_b_prediction = decision.predicted_score
                    r.path_b_confidence = decision.confidence
                    r.path_b_skipped = decision.skip
                    r.path_b_reason = decision.reason
                except Exception:
                    pass
                if decision.skip and decision.reason not in ("confident_strong_band",):
                    # confident_strong_band roles are already gated by
                    # stage3_skip_above (88). Only gate the LOW band here.
                    # We mark the role as having stage2 as final by setting
                    # stage3 fields to None and letting effective_skip_below
                    # ignore it. Simpler: just count and let the in-band
                    # filter inside stage3_deep_eval skip these via stage2.
                    # But the cleanest implementation is to bump their
                    # stage2_score temporarily out of band. We don't want
                    # to mutate scores; instead, set a transient flag and
                    # the needs_stage3 helper can read it.
                    r.path_b_gate_skip = True
                    path_b_skipped += 1
            if path_b_skipped:
                print(f"[path_b] gated {path_b_skipped} low-band roles out of Stage 3")

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

    # ---- Reunite cache hits with newly-scored roles ----
    # The cache hits we identified at Phase 0 need to be merged back into
    # the scored pool. They already have full final_score / final_tier from
    # cache, so they bypass the finalize step.
    scored = scored + cache_hits

    # ---- 5. Finalize scores + backfill salary + apply realism penalty ----
    # We backpop salary FIRST so the realism penalty in _finalize_score sees
    # any LLM-extracted salary that wasn't in the structured fields.
    # Cache-hit roles already have final_score; finalize is idempotent for them.
    for r in scored:
        _backpop_salary_from_reasoning(r)
        _finalize_score(r, profile=profile)

    qualifying = [r for r in scored if (r.final_score or 0) >= 40]
    qualifying.sort(key=lambda r: r.final_score or 0, reverse=True)

    # v0.3.25: percentile tiering is the FINAL tier authority — rank-based + floors
    # over the whole qualifying pool (replaces fragile fixed score-band cutoffs).
    assign_percentile_tiers(qualifying)

    # ---- 5b. Persist newly-scored roles to JD score cache ----
    # v0.3.15 (P1.7): cache ALL scored roles, not just qualifying. Prior
    # behavior cached only `final_score >= 40` — meaning rejects (SKIP/
    # STRETCH below the floor) got re-evaluated through embedding + Stage 1
    # + Stage 2 on every rerun. At ~420 rejected roles/run × $0.001/role
    # of pipeline cost, that's ~$0.42/run wasted on duplicate-rejection
    # processing for any user who reruns. Now: cache hit on a known-reject
    # short-circuits the cascade.
    #
    # Skip cache hits (already in cache, hydrated at Phase 0).
    # Skip roles with final_score=None (unscored due to errors — incomplete records).
    cache_hit_keys = {id(r) for r in cache_hits}
    cache_stored = 0
    cache_skipped_unscored = 0
    for r in scored:
        if id(r) in cache_hit_keys:
            continue
        if r.final_score is None:
            cache_skipped_unscored += 1
            continue
        try:
            jd_score_cache.store(prof_hash, r)
            cache_stored += 1
        except Exception:
            pass  # cache failures shouldn't break the run
    if log and cache_stored:
        print(
            f"[orchestrator] cached {cache_stored} role scores for cross-run reuse "
            f"(qualifying + rejects; skipped {cache_skipped_unscored} unscored)"
        )

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

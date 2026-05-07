"""Top-level production runner — the function the desktop app calls.

Pipeline:
  1. Scrape (multiple boards in parallel)
  2. Salary extraction from JD bodies (regex)
  3. Hard filters (salary, location, posted_date, already_applied)
  4. Liveness verification (HEAD requests)
  5. Embedding pre-filter
  6. Stage 1 LLM pre-filter (Flash, anti-pattern only)
  7. Stage 2 triage scoring (Flash)
  8. Stage 3 deep eval (Pro on 55-87 band + second-look on 35-54)
  9. Final scoring + tier assignment
  10. Final liveness re-check on top qualifying roles
  11. Return scored roles + RunSummary
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Callable, Optional

from backend.config import config
from backend.filter.hard_filters import apply_hard_filters
from backend.filter.salary_extractor import enrich_salaries
from backend.models import CandidateProfile, Role, RunSummary
from backend.scoring.orchestrator import score_roles
from backend.scraper.liveness import verify_liveness
from backend.scraper.orchestrator import scrape_all


def _maybe_archive():
    """Return the active Archive instance if initialized, else None.
    All archive writes are wrapped in this so the runner works in environments
    where no audit folder is configured (e.g. unit tests, ad-hoc scripts)."""
    try:
        from backend.storage import get_archive
        return get_archive()
    except Exception:
        return None


async def run_search(
    *,
    profile: CandidateProfile,
    keywords: list[str],
    sources: Optional[list[str]] = None,
    posted_within_days: int = 30,
    applied_keys: Optional[set[tuple[str, str]]] = None,
    license_key: Optional[str] = None,
    run_id: Optional[str] = None,
    cache_max_age_days: int = 7,
    force_refresh: bool = False,
    log: bool = True,
    progress: Optional[Callable[[int, str, int], None]] = None,
) -> tuple[list[Role], RunSummary]:
    """End-to-end search → scrape → filter → score → return.

    Returns (scored_roles, run_summary).

    `progress`: optional callback invoked as the pipeline advances. Signature
    is `progress(pct, current_step_label, current_step_index)` where:
      - pct is 0-100 progress through the whole search
      - current_step_label is a human-readable stage name
      - current_step_index is the 1-based step index used by the Running
        page UI (matches its STEPS array: 1=Profile/keywords ready,
        2=Scraping, 3=Filtering+liveness, 4=Embedding pre-filter,
        5=Scoring with AI cascade, 6=Building dashboard)
    Callback errors are swallowed so a misbehaving caller can't break a run.
    """
    started_at = time.perf_counter()
    run_id = run_id or str(uuid.uuid4())

    # Local helper — guards against caller-side exceptions in the progress
    # callback so they can't bubble up and abort the run mid-stage. The
    # `detail` arg is optional granular text shown under the active step
    # (e.g. "Scoring 47 of 120 with Flash") — falls back to "" when the
    # caller doesn't provide one.
    def _emit(pct: int, stage: str, step_index: int, detail: str = "") -> None:
        if progress is None:
            return
        try:
            # Best-effort 4-arg call; fall back to 3-arg for older callers
            try:
                progress(pct, stage, step_index, detail)
            except TypeError:
                progress(pct, stage, step_index)
        except Exception as e:
            if log:
                print(f"[progress] callback raised (ignored): {e}")

    if log:
        print(f"\n{'='*70}\nJOB SEARCH RUN STARTING (run_id={run_id})\n{'='*70}")
        print(f"Profile:   {profile.headline or '(none)'}")
        print(f"Keywords:  {keywords[:10]}{'...' if len(keywords) > 10 else ''}")

    # Open the archive run record. If no audit folder configured, this is a no-op.
    archive = _maybe_archive()
    profile_snapshot = profile.model_dump(mode="json")
    if archive is not None:
        try:
            archive.begin_run(
                run_id=run_id,
                profile_snapshot=profile_snapshot,
                keywords=[
                    {"text": k.text, "tier": k.tier} for k in profile.keywords
                ] if hasattr(profile, "keywords") else [{"text": k} for k in keywords],
            )
        except Exception as e:
            if log:
                print(f"[archive] begin_run failed (non-fatal): {e}")
            archive = None  # Disable for the rest of this run

    # ---- CACHE CHECK ----
    # If a recent completed run exists for this exact profile (same headline,
    # keywords, locations, etc.) and force_refresh is False, replay the cached
    # results instead of re-scraping. Cuts repeat searches from 12-25 min to
    # 30-60 sec. Profile-change invalidation is handled by hash_profile()
    # incorporating every user-changeable field.
    if archive is not None and not force_refresh:
        cached = archive.find_recent_run_for_profile(
            profile_snapshot=profile_snapshot,
            max_age_days=cache_max_age_days,
        )
        if cached:
            if log:
                age_hr = _hours_since(cached["completed_at"])
                print(f"\n[CACHE HIT] Replaying run {cached['run_id']} from {age_hr:.1f}h ago")
                print(f"            (use force_refresh=True to bypass cache)")
            scored = _replay_from_cache(archive, cached["run_id"])
            # Build a summary from the cached run
            import json as _json
            cached_summary = _json.loads(cached["summary_json"]) if cached["summary_json"] else {}
            summary = RunSummary(**{
                **cached_summary,
                "run_id": run_id,
                "duration_seconds": int(time.perf_counter() - started_at),
            })
            # Re-archive with the new run_id (so the audit-folder export shows
            # the user actually ran something today)
            try:
                archive.complete_run(
                    run_id=run_id,
                    status="completed",
                    summary=summary.model_dump(mode="json"),
                )
            except Exception:
                pass
            if log:
                print(f"[CACHE HIT] Returned {len(scored)} cached roles in {summary.duration_seconds}s")
            return scored, summary

    # ---- 1. Scrape ----
    # Count active boards dynamically so this stays accurate when the
    # registry changes (added JSearch in v0.1.4 → 16 boards).
    from backend.scraper.orchestrator import SCRAPER_REGISTRY as _SR
    _board_count = len(sources) if sources else len(_SR)
    _emit(5, "Scraping job boards", 2, f"Querying {_board_count} job boards in parallel...")
    if log:
        print(f"\n[1/9] Scraping job boards...")
    scrape_health: dict = {}

    # v0.1.4: build an EXPANDED keyword list for JSearch only.
    # JSearch's qualifying conversion (30.6%) is the highest of any source
    # AND it runs on paid Pro tier with substantial budget headroom. We
    # feed it search_terms + Tier 1/2 specific titles ("Senior AI
    # Strategist", "Federal AI Strategy Consultant") on top of the broad
    # search phrases. Tier 3 is excluded — too noisy. Other scrapers stay
    # on the base `keywords` list to preserve their per-source budgets.
    expanded_jsearch_kw: list[str] = list(keywords)
    if hasattr(profile, "keywords") and profile.keywords:
        for kw in profile.keywords:
            text = (getattr(kw, "text", "") or "").strip()
            tier = getattr(kw, "tier", 3) or 3
            if text and tier in (1, 2) and text not in expanded_jsearch_kw:
                expanded_jsearch_kw.append(text)

    # v0.3.5: surface user prefs to scrapers that can filter at the API.
    # Currently JSearch + Adzuna; GoogleJobs/BingJobs already filter by
    # location at the Serper level. Drops 30-50% of unrelated roles before
    # they enter the downstream pipeline (Stage 1 embeddings + Stage 3 LLM).
    user_filters: dict = {}
    locs = list(getattr(profile, "acceptable_locations", []) or [])
    if locs:
        # Pass the FIRST location only — Adzuna/JSearch take a single
        # `where`/`location` value. The downstream hard_filter still
        # checks the full acceptable_locations list, so we don't lose
        # roles that match alternate user locations; this just tightens
        # the upstream pull around the primary preference.
        user_filters["location_text"] = str(locs[0])
    if getattr(profile, "salary_minimum", None):
        user_filters["salary_minimum"] = int(profile.salary_minimum)
    if getattr(profile, "remote_only", False):
        user_filters["remote_only"] = True

    raw_roles = await scrape_all(
        keywords=keywords,
        sources=sources,
        posted_within_days=posted_within_days,
        log=log,
        health_out=scrape_health,
        extra_jsearch_keywords=expanded_jsearch_kw if len(expanded_jsearch_kw) > len(keywords) else None,
        user_filters=user_filters or None,
    )
    # Note: scrape_health gets attached to summary below, after score_roles
    # creates the summary object. Don't try to write to summary here.

    # Flag any source returning 0 — likely broken or rate-limited
    if log:
        zero_sources = [s for s, h in scrape_health.items() if h.get("roles", 0) == 0]
        if zero_sources:
            print(f"[scraper] [!] ZERO-ROLE SOURCES (investigate): {zero_sources}")

    # ---- 2. Salary enrichment ----
    _emit(
        25, "Filtering + verifying liveness", 3,
        f"Filtering {len(raw_roles):,} scraped roles by salary, location, and applied list...",
    )
    if log:
        print(f"\n[2/9] Enriching salary data from JDs...")
    enrich_salaries(raw_roles, log=log)

    # ---- 2.5 Hybrid reclassification (v0.2.0) ----
    # Run BEFORE hard filters so the location filter sees the corrected
    # arrangement. Scrapers default to On-site when JD doesn't say "hybrid"
    # literally, but multi-city listings + JDs with "X days in office"
    # language are functionally hybrid. Reclassifying here means the
    # Dashboard's Hybrid filter chip actually returns results, and stats
    # accurately reflect arrangement breakdown.
    from backend.filter.hard_filters import reclassify_hybrid_roles
    reclassify_hybrid_roles(raw_roles, log=log)

    # ---- 3. Hard filters ----
    if log:
        print(f"\n[3/9] Applying hard filters...")
    filtered = apply_hard_filters(
        raw_roles,
        profile=profile,
        max_age_days=posted_within_days,
        applied_keys=applied_keys,
        log=log,
    )

    # Tag each surviving role with its single most-relevant matched keyword
    # so the dashboard can show "via {keyword}" to explain why this surfaced.
    _tag_matched_keywords(filtered, profile)

    # ---- 4. Liveness check ----
    # v0.2.0: removed the v0.1.4 "skip if JD already fetched" optimization.
    # The original logic assumed: if the scraper successfully fetched the
    # JD body, the URL must be live. But the audit (May 4, 2026) showed
    # that scrapers cache JDs at scrape time — those cached JDs can be
    # for roles that were closed AFTER the scrape but BEFORE the user
    # opens them. Closure banners get added to the live page while the
    # cached JD body stays "fresh-looking." HEAD checks catch URLs that
    # 404, 410, or redirect to careers home regardless of cached JD.
    # The dead_listing regex (line ~261) catches the other failure mode:
    # 200 OK pages with closure banners in the JD prose. Together they
    # form a two-layer defense.
    #
    # Cost: adds ~30-60s to a typical run (HEAD requests on ~500 roles
    # with concurrency). On a 10-15 min run that's a rounding error.
    _emit(
        35, "Filtering + verifying liveness", 3,
        f"Verifying {len(filtered):,} URLs are still live...",
    )
    if log:
        print(f"\n[4/9] Verifying liveness on all {len(filtered)} roles "
              f"(v0.2.0: no longer skipping based on JD presence)...")
    alive = await verify_liveness(filtered, drop_dead=True, log=log)

    # ---- 4.5 Fetch missing JDs ----
    # Some scrapers (Workday) return search results without JD bodies.
    # Fetch them now for roles surviving hard filters, before scoring.
    missing_jd = [r for r in alive if not r.job_description_full]
    if missing_jd:
        _emit(
            42, "Filtering + verifying liveness", 3,
            f"Fetching missing job descriptions for {len(missing_jd)} roles...",
        )
        if log:
            print(f"\n[4.5/9] Fetching missing JDs for {len(missing_jd)} roles...")
        await _fetch_missing_jds(missing_jd, log=log)
        # Re-run salary extraction on newly-enriched JDs
        enrich_salaries(missing_jd, log=log)

    # ---- 4.6 Dead-listing regex pre-filter (AI-B) ----
    # Drop roles whose JD shouts "no longer hiring" / "filled" before we
    # spend embedding + LLM budget on them. Stage 2 has a stronger
    # LLM-based check for borderline cases.
    from backend.filter.hard_filters import is_dead_listing
    pre_dead = len(alive)
    alive = [r for r in alive if not is_dead_listing(r.job_description_full or "")]
    dead_dropped = pre_dead - len(alive)
    if log and dead_dropped > 0:
        print(f"[dead_listing] dropped {dead_dropped} 'no longer hiring' roles before scoring")

    # ---- 5-9. Cascade scoring (embedding + Stage 1/2/3) ----
    # Hand the progress callback through; score_roles emits its own
    # finer-grained progress events (50% embedding → 90% Stage 3).
    if log:
        print(f"\n[5-9/9] Running scoring cascade on {len(alive)} roles...")
    scored, summary = await score_roles(
        profile=profile,
        roles=alive,
        license_key=license_key,
        log=log,
        progress=progress,
    )

    # ---- Post-scoring salary re-filter -------------------------------------
    # Stage 3 reads JD bodies and extracts salary text the regex-based
    # extractor missed. That extracted salary lives on role.salary_min /
    # role.salary_max AFTER scoring. The hard salary filter ran BEFORE
    # scoring, so any role that passed with "no salary listed" but whose
    # JD body actually contained a sub-floor salary slipped through.
    #
    # Example from Run 3: Evidence Action "Manager, AI Strategy & Adoption"
    # passed hard filters with no salary, then Stage 3 extracted $84-94K
    # from the JD body — well below the $130K floor with 10% softness ($117K).
    # This loop catches those leaks by re-checking the post-scoring salary
    # against the same soft-floor rule.
    if profile.salary_minimum:
        from backend.filter.hard_filters import passes_salary_floor
        salary_demoted = 0
        for r in scored:
            if (r.final_score or 0) < 40:
                continue  # already below qualifying threshold
            # Re-check using current salary_max (which Stage 3 may have populated)
            if not passes_salary_floor(r, minimum=profile.salary_minimum):
                # Demote to STRETCH-or-below by clamping the final score.
                # We don't drop entirely (the user can still see it for context
                # in the audit JSON) but we ensure it doesn't surface as a
                # qualifying match.
                r.final_score = min(r.final_score or 39, 39)
                from backend.models import Tier
                r.final_tier = Tier.SKIP
                salary_demoted += 1
        if log and salary_demoted > 0:
            print(f"[runner] Post-Stage-3 salary re-filter: demoted {salary_demoted} "
                  f"roles whose JD-extracted salary was below the soft floor.")

    # ---- Regional-variant dedup (Writer-style) -----------------------------
    # Some companies post the same role in 3-4 regional variants
    # (Writer "Enterprise AI transformation lead (East)" / "(West)" /
    # "(Central)" / "(UK)"). Run 3 had 11 Writer roles, half of which were
    # region clones. We collapse them by stripping a trailing parenthetical
    # region and keeping only the highest-scoring variant per (company, base_title).
    # The other variants stay in the audit JSON for visibility but don't
    # double-count in the qualifying pool.
    import re as _re_dedup
    _REGION_RE = _re_dedup.compile(
        r"\s*\(\s*(east|west|central|north|south|uk|eu|emea|apac|latam|us|usa|canada|na|amer|americas|na/emea|hybrid|remote)\s*\)\s*$",
        _re_dedup.IGNORECASE,
    )
    def _base_title(t: str) -> str:
        return _REGION_RE.sub("", (t or "").strip()).strip().lower()

    region_keep: dict[tuple[str, str], "Role"] = {}
    region_drop_count = 0
    for r in scored:
        if (r.final_score or 0) < 40:
            continue
        company_key = (r.company or "").strip().lower()
        title_key = _base_title(r.job_title or "")
        if not company_key or not title_key:
            continue
        composite_key = (company_key, title_key)
        existing = region_keep.get(composite_key)
        if existing is None:
            region_keep[composite_key] = r
        else:
            # Keep the higher-scoring variant; demote the loser to STRETCH-or-below
            # so it disappears from qualifying.
            keeper, loser = (
                (r, existing) if (r.final_score or 0) > (existing.final_score or 0)
                else (existing, r)
            )
            region_keep[composite_key] = keeper
            loser.final_score = min(loser.final_score or 39, 39)
            from backend.models import Tier
            loser.final_tier = Tier.SKIP
            region_drop_count += 1
    if log and region_drop_count > 0:
        print(f"[runner] Regional-variant dedup: collapsed {region_drop_count} roles "
              f"(kept highest-scoring variant per company+base_title).")

    _emit(95, "Building dashboard", 6, "Assembling your scored results...")
    # Attach per-source health captured during scrape phase. This persists
    # in audit JSON + lets the dashboard / health monitor flag broken
    # scrapers per-run.
    summary.per_source_counts = scrape_health

    # ---- Per-source funnel: how many roles each source contributed at every
    # pipeline stage. Critical for diagnosing "Workday scraped 8147 roles but
    # 0 qualified" — without this we can't tell if they died at hard filter,
    # liveness, dead-listing, or scoring. Audit JSON consumers can spot the
    # broken stage at a glance. -----------------------------------------------
    def _count_by_source(rs: list[Role]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in rs:
            src = getattr(r, "primary_source", None) or getattr(r, "source", None) or "unknown"
            counts[src] = counts.get(src, 0) + 1
        return counts

    funnel: dict[str, dict[str, int]] = {}
    for src, h in scrape_health.items():
        funnel[src] = {"raw_scraped": int(h.get("roles", 0))}
    for src, n in _count_by_source(raw_roles).items():
        funnel.setdefault(src, {})["after_dedup"] = n
    for src, n in _count_by_source(filtered).items():
        funnel.setdefault(src, {})["after_hard_filters"] = n
    for src, n in _count_by_source(alive).items():
        funnel.setdefault(src, {})["after_liveness_and_deadlist"] = n
    qualifying_for_funnel = [r for r in scored if (getattr(r, "final_score", None) or 0) >= 40]
    for src, n in _count_by_source(qualifying_for_funnel).items():
        funnel.setdefault(src, {})["qualifying_final"] = n
    # Make sure every source has a "qualifying_final" entry even if 0 — that's
    # exactly the case we want visible (Workday 8147 raw → 0 qualifying).
    for src in funnel:
        funnel[src].setdefault("qualifying_final", 0)
    summary.per_source_funnel = funnel

    # Final summary patches not handled by score_roles
    summary.roles_scraped = len(raw_roles)
    summary.roles_after_filter = len(filtered)
    summary.boards_searched = sources or []
    summary.duration_seconds = int(time.perf_counter() - started_at)

    # Coverage stats — "usable JD" means >500 chars AND mostly printable text
    # (not binary garbage). The earlier brotli bug counted garbled JDs as
    # present, hiding the real issue. This metric is honest about what the
    # downstream scorer actually has to work with.
    summary.jd_coverage_pct = _pct(raw_roles, _has_usable_jd)
    summary.salary_coverage_pct = _pct(raw_roles, lambda r: r.salary_max is not None)
    summary.location_coverage_pct = _pct(raw_roles, lambda r: bool(r.location or r.location_type))

    # Salary coverage on the roles testers ACTUALLY see (qualifying ≥ 40).
    # The all-roles % above is misleading — it dilutes with thousands of
    # filtered-out roles and makes us think the dashboard has poor coverage.
    qualifying_for_coverage = [r for r in scored if (getattr(r, "final_score", None) or 0) >= 40]
    summary.salary_coverage_qualifying_pct = _pct(
        qualifying_for_coverage,
        lambda r: r.salary_max is not None or r.salary_min is not None or bool(r.salary_text),
    ) if qualifying_for_coverage else 0.0

    # Coverage gap signal — surface "your sector under-represented" warning
    # when target_industries don't match what scrapers actually return.
    try:
        from backend.storage.audit import _coverage_gap_analysis
        qualifying_for_gap = [
            r for r in scored if (getattr(r, "final_score", None) or 0) >= 40
        ]
        gap = _coverage_gap_analysis(qualifying_for_gap, profile)
        summary.coverage_gap_severity = gap.get("gap_severity")
        summary.coverage_gap_message = gap.get("dashboard_message")
        summary.coverage_gap_target_match_pct = gap.get("target_match_pct")
    except Exception:
        pass

    if log:
        print(f"\n{'='*70}\nRUN COMPLETE")
        print(f"  Total time:        {summary.duration_seconds}s")
        print(f"  Total cost:        ${summary.cost_total_usd:.4f}")
        print(f"  Scraped:           {summary.roles_scraped}")
        print(f"  After hard filter: {summary.roles_after_filter}")
        print(f"  After liveness:    {len(alive)}")
        print(f"  Final qualifying:  {summary.roles_qualifying}")
        print(f"    STRONG:   {summary.tier_strong}")
        print(f"    GOOD:     {summary.tier_good}")
        print(f"    MAYBE:    {summary.tier_maybe}")
        print(f"    STRETCH:  {summary.tier_stretch}")
        print(f"  Coverage:")
        print(f"    JD:       {summary.jd_coverage_pct:.0f}%")
        print(f"    Salary:   {summary.salary_coverage_pct:.0f}% of all scraped · "
              f"{summary.salary_coverage_qualifying_pct:.0f}% of qualifying (what testers see)")
        print(f"    Location: {summary.location_coverage_pct:.0f}%")
        print(f"{'='*70}\n")

    # ---- Archive: persist roles, scores, market contributions ----
    if archive is not None:
        try:
            _archive_run(archive, run_id, profile, scored, summary, log=log)
        except Exception as e:
            if log:
                print(f"[archive] persist failed (non-fatal): {e}")

    # ---- Phase D: upload audit JSON to central Worker (if configured) ----
    # Fires only when AUDIT_UPLOAD_URL is set in env (production .msi builds
    # ship with this configured to point at Ziad's Worker). Local dev runs
    # without it set — no upload happens, audit folder is all that's used.
    if config.AUDIT_UPLOAD_URL and archive is not None:
        try:
            from backend.storage.audit_uploader import upload_audit
            from backend.storage.audit import find_prior_audit_for_profile
            # Find the audit JSON we just wrote and upload it
            latest = find_prior_audit_for_profile(archive.folder)
            if latest and latest.exists():
                import json as _json
                audit_dict = _json.loads(latest.read_text(encoding="utf-8"))
                result = await upload_audit(audit_dict)
                if log and result:
                    print(f"[audit-upload] sent run to central worker: {result}")
                elif log:
                    print(f"[audit-upload] failed silently (non-fatal)")
        except Exception as e:
            if log:
                print(f"[audit-upload] error (non-fatal): {e}")

    return scored, summary


def _archive_run(
    archive,
    run_id: str,
    profile: CandidateProfile,
    scored: list[Role],
    summary: RunSummary,
    *,
    log: bool = False,
) -> None:
    """Write all roles + scores from a completed run into runs.db.
    Also records a market_contribution per qualifying role for closed-loop
    cross-tester learning."""
    profile_tags = list(getattr(profile, "profile_tags", []) or [])
    archived = 0
    contribs = 0
    for role in scored:
        if not (role.company or "").strip() or not (role.job_title or "").strip():
            continue
        role_id = archive.upsert_role(
            company=role.company or "",
            job_title=role.job_title or "",
            job_url=role.job_url,
            location=role.location,
            location_type=role.location_type,
            salary_min=role.salary_min,
            salary_max=role.salary_max,
            salary_text=role.salary_text,
            job_description_full=role.job_description_full,
            industry=getattr(role, "industry", None),
            posted_date=role.posted_date,
            source=getattr(role, "source", None),
        )
        archive.store_role_score(run_id=run_id, role_id=role_id, role_obj=role)
        archived += 1
        # Add to market contributions if it's a qualifying role
        score = role.final_score or 0
        if score >= 55 and profile_tags:
            archive.add_market_contribution(
                company=role.company or "",
                job_title=role.job_title or "",
                score=int(score),
                profile_tags=profile_tags,
                source_user="self",
            )
            contribs += 1
    # Write the comprehensive audit JSON + MD summary + diff files
    audit_paths: dict = {}
    try:
        from backend.storage import write_audit_files, find_prior_audit_for_profile
        prior = find_prior_audit_for_profile(archive.folder)
        keyword_strs = (
            [k.text for k in profile.keywords]
            if hasattr(profile, "keywords") and profile.keywords
            else []
        )
        audit_paths = write_audit_files(
            audit_folder=archive.folder,
            run_id=run_id,
            profile=profile,
            keywords=keyword_strs,
            scored_roles=scored,
            summary=summary,
            sources_searched=getattr(summary, "boards_searched", None),
            cache_was_hit=False,
            prior_audit_path=prior,
        )
        if log:
            print(f"[audit] wrote {audit_paths.get('json')}")
            if audit_paths.get("diff"):
                print(f"[audit] diff vs prior run: {audit_paths['diff']}")
    except Exception as e:
        if log:
            print(f"[audit] write failed (non-fatal): {e}")

    archive.complete_run(
        run_id=run_id,
        status="completed",
        summary=summary.model_dump(mode="json"),
        audit_file_path=audit_paths.get("json"),
    )
    # Export own contributions to market_contributions.jsonl in the audit
    # folder so other testers' apps (whose folders sync to the same shared
    # cloud-drive parent) automatically see them on their next run.
    #
    # v0.2.1: enriched contribution schema. Joins SQLite-stored basics
    # (company, title, score, profile_tags) with extra fields read from
    # the in-memory `scored` list — matched_keyword, tier, source,
    # industry, location_type, salary_min/max, posted_date. Existing
    # readers ignore unknown keys (forward-compatible). New readers in
    # v0.3 will use the richer signal for keyword-gen prompt + cohort
    # learning. No PII added: all fields come from public job postings.
    try:
        from backend.storage.market import export_contributions
        own = archive.get_self_contributions()
        # Build a (company.lower, title.lower) -> Role lookup from the
        # in-memory scored list so we can enrich each SQLite contribution
        # with extra fields. Missing matches degrade to the basic fields
        # (no error).
        role_lookup = {}
        for r in scored or []:
            ck = (r.company or "").strip().lower()
            tk = (r.job_title or "").strip().lower()
            if ck and tk:
                role_lookup[(ck, tk)] = r
        own_dicts = []
        for row in own:
            base = {
                "company": row["company"],
                "job_title": row["job_title"],
                "score": row["score"],
                "profile_tags": list(_safe_json_loads(row["profile_tags_json"]) or []),
                "contributed_date": row["contributed_date"],
            }
            r = role_lookup.get(((row["company"] or "").strip().lower(), (row["job_title"] or "").strip().lower()))
            if r is not None:
                base.update({
                    "matched_keyword": getattr(r, "matched_keyword", None) or None,
                    "tier": (getattr(getattr(r, "final_tier", None), "value", None)
                             or str(getattr(r, "final_tier", "") or "") or None),
                    "source": getattr(r, "source", None) or None,
                    "industry": getattr(r, "industry", None) or None,
                    "location_type": getattr(r, "location_type", None) or None,
                    "salary_min": getattr(r, "salary_min", None),
                    "salary_max": getattr(r, "salary_max", None),
                    "posted_date": getattr(r, "posted_date", None),
                })
                # Drop None values so existing readers don't choke + file stays compact
                base = {k: v for k, v in base.items() if v is not None and v != ""}
            own_dicts.append(base)
        n_exported = export_contributions(archive.folder, contributions=own_dicts)
        if log and n_exported > 0:
            print(f"[market] exported {n_exported} new contributions to {archive.folder}/market_contributions.jsonl")
    except Exception as e:
        if log:
            print(f"[market] export failed (non-fatal): {e}")

    # v0.2.1: write three new sidecar telemetry files alongside
    # market_contributions.jsonl. All three are anonymous, no PII, no
    # role-level data (run_telemetry/scraper_health) or sanitized
    # error-class-only data (bug_reports). Cloud-syncs with the rest of
    # the audit folder; consumed by v0.3+ cohort dashboards. Wrapped in
    # try/except so a write failure never kills a successful run.
    try:
        from backend.storage.telemetry import (
            write_run_telemetry, write_scraper_health, write_bug_reports,
        )
        from backend import __version__ as APP_VERSION
        # Run telemetry — one entry per completed run
        tier_breakdown = {
            "STRONG": getattr(summary, "tier_strong", 0),
            "GOOD": getattr(summary, "tier_good", 0),
            "MAYBE": getattr(summary, "tier_maybe", 0),
            "STRETCH": getattr(summary, "tier_stretch", 0),
        }
        write_run_telemetry(
            archive.folder,
            run_id=run_id,
            app_version=APP_VERSION,
            duration_seconds=getattr(summary, "duration_seconds", 0) or 0,
            total_scraped=getattr(summary, "roles_scraped", 0) or 0,
            after_hard_filters=getattr(summary, "roles_after_filter", 0) or 0,
            qualifying_final=getattr(summary, "roles_qualifying", 0) or 0,
            tier_breakdown=tier_breakdown,
            keyword_count=getattr(summary, "keywords_used", 0) or 0,
            target_title_count=len(getattr(profile, "target_titles", []) or []),
            cache_hit=False,  # v0.2.1: always-fresh policy
        )
        # Per-scraper health — one entry per scraper for this run
        per_source = getattr(summary, "per_source_funnel", None) or getattr(summary, "per_source_counts", None) or {}
        if per_source:
            write_scraper_health(
                archive.folder,
                run_id=run_id,
                app_version=APP_VERSION,
                per_source=per_source,
            )
        # Bug reports — derive from any scrapers that errored. v0.2.1
        # bootstraps the file with scraper-level errors only; v0.3 will
        # add deeper instrumentation across the pipeline. Each unique
        # error_class becomes one entry; sanitization happens inside
        # write_bug_reports (allowlist of class names + module prefixes).
        bugs: list[dict] = []
        for source, data in (per_source or {}).items():
            if not data:
                continue
            if data.get("errored") and data.get("error"):
                bugs.append({
                    "error_class": data.get("error"),
                    "module": f"backend.scraper.{source.lower()}",
                    "count": 1,
                })
        if bugs:
            write_bug_reports(
                archive.folder,
                run_id=run_id,
                app_version=APP_VERSION,
                bugs=bugs,
            )
        if log:
            print(f"[telemetry] wrote run_telemetry.jsonl + scraper_health.jsonl"
                  + (f" + bug_reports.jsonl ({len(bugs)} entries)" if bugs else "")
                  + f" to {archive.folder}")
    except Exception as e:
        if log:
            print(f"[telemetry] write failed (non-fatal): {e}")

    if log:
        print(f"[archive] persisted {archived} roles, {contribs} market contributions, run {run_id}")


def _safe_json_loads(s):
    import json as _json
    try:
        return _json.loads(s)
    except Exception:
        return None


def _tag_matched_keywords(roles: list[Role], profile) -> None:
    """For each role, set role.matched_keyword to the most relevant keyword
    or search term that the role actually matched.

    Uses the shared TOKEN-OVERLAP matcher (backend.scraper._keyword_match)
    so the tagger agrees with what each scraper accepted as a match.

    Priority order (best -> worst):
      Tier 1 keyword (specific title)        e.g. "AI Enablement Lead"
      Tier 2 keyword (specific title)        e.g. "AI Operations Manager"
      Search term (broad phrase)              e.g. "AI strategy"
    Each tier checks title first, then title+JD. Within a tier, longest
    text wins. Tier-3 keywords are intentionally excluded.

    v0.1.4: search_terms added as a fallback tier — when a role doesn't
    match any specific keyword (which is common when scrapers query upstream
    with broad search_terms), fall back to displaying the broader concept
    that brought the role into the pool.
    """
    from backend.scraper import _keyword_match as _kw_match

    keywords = list(getattr(profile, "keywords", None) or [])
    search_terms = list(getattr(profile, "search_terms", None) or [])
    if not keywords and not search_terms:
        return

    # Build the {tier: [(text, tokens)]} structure best_keyword_match expects.
    # Tier 1/2: specific keywords. Tier 3 (synthetic): search terms.
    # The matcher walks tiers in order and stops at first match within a tier.
    by_tier: dict[int, list[tuple[str, list[str]]]] = {1: [], 2: [], 3: []}
    for kw in keywords:
        text = (getattr(kw, "text", "") or "").strip()
        tier = getattr(kw, "tier", 3) or 3
        if not text or tier not in (1, 2):
            continue
        tokens = _kw_match.tokenize(text)
        if tokens:
            by_tier[tier].append((text, tokens))
    # Search terms get tier 3 — only used when no Tier 1/2 keyword matched.
    for st in search_terms:
        text = str(st or "").strip()
        if not text:
            continue
        tokens = _kw_match.tokenize(text)
        if tokens:
            by_tier[3].append((text, tokens))
    for t in by_tier:
        by_tier[t].sort(key=lambda p: -len(p[0]))

    for role in roles:
        m = _kw_match.best_keyword_match(
            role.job_title or "",
            role.job_description_full or "",
            by_tier,
        )
        if m:
            role.matched_keyword = m


def _hours_since(iso_ts: str) -> float:
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except Exception:
        return 0.0


def _replay_from_cache(archive, prior_run_id: str) -> list[Role]:
    """Reconstruct Role objects from a cached run."""
    from backend.models import score_to_tier
    rows = archive.replay_cached_run(prior_run_id)
    out: list[Role] = []
    for row in rows:
        try:
            r = Role(
                job_title=row.get("job_title") or "",
                company=row.get("company") or "",
                job_url=row.get("job_url"),
                location=row.get("location"),
                location_type=row.get("location_type"),
                salary_min=row.get("salary_min"),
                salary_max=row.get("salary_max"),
                salary_text=row.get("salary_text"),
                job_description_full=row.get("job_description_full"),
                posted_date=row.get("posted_date"),
            )
            r.final_score = row.get("final_score")
            r.stage2_score = row.get("stage2_score")
            r.stage2_reasoning = row.get("stage2_reasoning")
            r.stage3_score = row.get("stage3_score")
            r.stage3_analysis = row.get("stage3_analysis")
            r.stage3_application_strategy = row.get("stage3_application_strategy")
            if row.get("final_tier"):
                try:
                    r.final_tier = score_to_tier(r.final_score) if r.final_score else None
                except Exception:
                    pass
            out.append(r)
        except Exception:
            continue
    return out


def _has_usable_jd(role: Role) -> bool:
    """JD is usable if >500 chars AND >=85% printable (not binary garbage).
    Counts roles where the scorer has real text to read, not just
    'something is in the field.'"""
    jd = role.job_description_full or ""
    if len(jd) < 500:
        return False
    sample = jd[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\n\r\t")
    return (printable / max(1, len(sample))) >= 0.85


def _pct(roles: list[Role], predicate) -> float:
    if not roles:
        return 0.0
    return 100.0 * sum(1 for r in roles if predicate(r)) / len(roles)


async def _fetch_missing_jds(roles: list[Role], *, log: bool = True) -> None:
    """Fetch JD bodies for roles where the scraper didn't include them.

    Source-aware dispatch (v0.1.3 hot-fix):
      - Workday roles use WorkdayScraper.fetch_jd which calls the CXS detail
        endpoint. This was the v0.1.3 silent killer: previously we did a
        generic HTTP GET against the Workday URL, which returns the JS-shell
        of a SPA-rendered careers page. The shell contains the title and
        about 150 chars of company boilerplate but NO job description body.
        Embedding pre-filter then sees "Title at Company" + 150 chars of
        boilerplate, scores cosine similarity below the 40% cutoff, and
        kills 100% of Workday roles. The CXS endpoint returns the full JD
        as JSON — same path the Workday scraper already uses internally.
        Diagnostic showed 0/12 -> 8/12 Workday roles survive embedding
        with proper CXS-fetched JDs.
      - Everything else uses generic HTTP GET (current behavior). Greenhouse
        / Lever / Ashby / Remotive / etc. all serve real HTML pages with
        the JD inline.

    Mutates roles in place. Bounded concurrency to be polite to each tenant.
    """
    import asyncio
    from backend.scraper.client import ScraperClient
    from backend.scraper.greenhouse import _strip_html

    if not roles:
        return

    # Partition by source for source-aware dispatch
    workday_roles = [r for r in roles if (
        getattr(r, "primary_source", None) == "Workday"
        or getattr(r, "source", None) == "Workday"
        or "myworkdayjobs.com" in (r.job_url or "")
    )]
    other_roles = [r for r in roles if r not in workday_roles]

    semaphore = asyncio.Semaphore(8)

    async def _fetch_workday(scraper, role: Role) -> None:
        """Use WorkdayScraper.fetch_jd which hits the CXS JSON endpoint."""
        if not role.job_url or role.job_description_full:
            return
        async with semaphore:
            try:
                jd = await scraper.fetch_jd(role)
                role.job_description_full = jd or ""
                role.jd_completeness = (
                    "Full" if len(role.job_description_full) > 500
                    else ("Partial" if role.job_description_full else "Missing")
                )
            except Exception:
                pass

    async def _fetch_generic(client: ScraperClient, role: Role) -> None:
        """Generic HTTP GET + HTML strip. Works for Greenhouse/Lever/Ashby/etc."""
        if not role.job_url or role.job_description_full:
            return
        async with semaphore:
            try:
                response = await client.get(role.job_url)
                text = _strip_html(response.text)
                role.job_description_full = text
                role.jd_completeness = (
                    "Full" if len(text) > 500
                    else ("Partial" if text else "Missing")
                )
            except Exception:
                pass

    async with ScraperClient() as client:
        # Workday batch — uses the scraper's own CXS-aware fetch_jd
        if workday_roles:
            from backend.scraper.workday import WorkdayScraper
            wd_scraper = WorkdayScraper(client=client)
            wd_tasks = [_fetch_workday(wd_scraper, r) for r in workday_roles]
            await asyncio.gather(*wd_tasks)

        # Everything else — generic HTML fetch
        if other_roles:
            other_tasks = [_fetch_generic(client, r) for r in other_roles]
            await asyncio.gather(*other_tasks)

    enriched = sum(1 for r in roles if r.job_description_full)
    wd_enriched = sum(1 for r in workday_roles if r.job_description_full)
    if log:
        print(f"[fetch_jds] enriched {enriched}/{len(roles)} roles with full JD bodies "
              f"(Workday via CXS: {wd_enriched}/{len(workday_roles)})")

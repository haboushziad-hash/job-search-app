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

import time
import uuid
from typing import Optional

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
) -> tuple[list[Role], RunSummary]:
    """End-to-end search → scrape → filter → score → return.

    Returns (scored_roles, run_summary).
    """
    started_at = time.perf_counter()
    run_id = run_id or str(uuid.uuid4())

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
    if log:
        print(f"\n[1/9] Scraping job boards...")
    scrape_health: dict = {}
    raw_roles = await scrape_all(
        keywords=keywords,
        sources=sources,
        posted_within_days=posted_within_days,
        log=log,
        health_out=scrape_health,
    )
    # Note: scrape_health gets attached to summary below, after score_roles
    # creates the summary object. Don't try to write to summary here.

    # Flag any source returning 0 — likely broken or rate-limited
    if log:
        zero_sources = [s for s, h in scrape_health.items() if h.get("roles", 0) == 0]
        if zero_sources:
            print(f"[scraper] [!] ZERO-ROLE SOURCES (investigate): {zero_sources}")

    # ---- 2. Salary enrichment ----
    if log:
        print(f"\n[2/9] Enriching salary data from JDs...")
    enrich_salaries(raw_roles, log=log)

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
    if log:
        print(f"\n[4/9] Verifying liveness...")
    alive = await verify_liveness(filtered, drop_dead=True, log=log)

    # ---- 4.5 Fetch missing JDs ----
    # Some scrapers (Workday) return search results without JD bodies.
    # Fetch them now for roles surviving hard filters, before scoring.
    missing_jd = [r for r in alive if not r.job_description_full]
    if missing_jd:
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
    if log:
        print(f"\n[5-9/9] Running scoring cascade on {len(alive)} roles...")
    scored, summary = await score_roles(
        profile=profile,
        roles=alive,
        license_key=license_key,
        log=log,
    )
    # Attach per-source health captured during scrape phase. This persists
    # in audit JSON + lets the dashboard / health monitor flag broken
    # scrapers per-run.
    summary.per_source_counts = scrape_health

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
        print(f"    Salary:   {summary.salary_coverage_pct:.0f}%")
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
    try:
        from backend.storage.market import export_contributions
        own = archive.get_self_contributions()
        own_dicts = [
            {
                "company": row["company"],
                "job_title": row["job_title"],
                "score": row["score"],
                "profile_tags": list(_safe_json_loads(row["profile_tags_json"]) or []),
                "contributed_date": row["contributed_date"],
            }
            for row in own
        ]
        n_exported = export_contributions(archive.folder, contributions=own_dicts)
        if log and n_exported > 0:
            print(f"[market] exported {n_exported} new contributions to {archive.folder}/market_contributions.jsonl")
    except Exception as e:
        if log:
            print(f"[market] export failed (non-fatal): {e}")

    if log:
        print(f"[archive] persisted {archived} roles, {contribs} market contributions, run {run_id}")


def _safe_json_loads(s):
    import json as _json
    try:
        return _json.loads(s)
    except Exception:
        return None


def _tag_matched_keywords(roles: list[Role], profile) -> None:
    """For each role, set role.matched_keyword to the single most relevant
    keyword from profile.keywords that the role actually matched.

    Priority order (best → worst):
      1. Tier-1 keyword phrase appearing in job title
      2. Tier-2 keyword phrase appearing in job title
      3. Tier-1 keyword phrase appearing in JD body (first 2000 chars)
      4. Tier-2 keyword phrase appearing in JD body
    Within the same band, longer keywords win (more specific = more meaningful).
    """
    keywords = list(getattr(profile, "keywords", None) or [])
    if not keywords:
        return

    # Group by tier and sort each tier longest-first (so the first match
    # within a tier is the most specific). Also lowercase for matching.
    by_tier: dict[int, list[tuple[str, str]]] = {1: [], 2: [], 3: []}
    for kw in keywords:
        text = (getattr(kw, "text", "") or "").strip()
        tier = getattr(kw, "tier", 3) or 3
        if not text:
            continue
        by_tier.setdefault(tier, []).append((text, text.lower()))
    for t in by_tier:
        by_tier[t].sort(key=lambda p: -len(p[0]))

    def _find_match(text_lower: str, tier: int) -> str:
        for kw_orig, kw_lower in by_tier.get(tier, []):
            if kw_lower in text_lower:
                return kw_orig
        return ""

    for role in roles:
        title_l = (role.job_title or "").lower()
        jd_l = (role.job_description_full or "")[:2000].lower()

        # 1. Tier 1 in title
        m = _find_match(title_l, 1)
        if m:
            role.matched_keyword = m
            continue
        # 2. Tier 2 in title
        m = _find_match(title_l, 2)
        if m:
            role.matched_keyword = m
            continue
        # 3. Tier 1 in JD
        m = _find_match(jd_l, 1)
        if m:
            role.matched_keyword = m
            continue
        # 4. Tier 2 in JD
        m = _find_match(jd_l, 2)
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
    Mutates roles in place. Bounded concurrency so we don't hammer any one site."""
    import asyncio
    from backend.scraper.client import ScraperClient
    from backend.scraper.greenhouse import _strip_html

    if not roles:
        return

    semaphore = asyncio.Semaphore(8)

    async def _fetch_one(client: ScraperClient, role: Role) -> None:
        if not role.job_url or role.job_description_full:
            return
        async with semaphore:
            try:
                response = await client.get(role.job_url)
                # Strip HTML to plain text. Workday/iframe pages may have minimal content
                # in the initial HTML — we get what we can.
                text = _strip_html(response.text)
                # Workday job pages often embed JD in JSON. Try regex extraction.
                if "myworkdayjobs.com" in role.job_url and len(text) < 1500:
                    import re
                    # Look for jobDescription field in embedded JSON
                    m = re.search(r'"jobDescription"\s*:\s*"((?:[^"\\]|\\.)*)"', response.text)
                    if m:
                        # Unescape JSON string
                        jd = m.group(1).replace("\\n", "\n").replace("\\\"", '"').replace("\\/", "/")
                        text = _strip_html(jd) or text
                role.job_description_full = text
                role.jd_completeness = "Full" if len(text) > 500 else ("Partial" if text else "Missing")
            except Exception:
                pass

    enriched = 0
    async with ScraperClient() as client:
        tasks = [_fetch_one(client, r) for r in roles]
        await asyncio.gather(*tasks)
    enriched = sum(1 for r in roles if r.job_description_full)
    if log:
        print(f"[fetch_jds] enriched {enriched}/{len(roles)} roles with full JD bodies")

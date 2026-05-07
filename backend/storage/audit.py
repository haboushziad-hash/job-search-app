"""Per-run comprehensive audit file generator.

Every search run writes two files to <audit_folder>/runs/:
  - <ISO-timestamp>_<run_id>_audit.json   — machine-readable, full data
  - <ISO-timestamp>_<run_id>_summary.md   — human-readable summary

Plus a diff file in <audit_folder>/diffs/ if a prior run exists for the
same profile.

Audit JSON contains EVERYTHING needed to reproduce, review, and challenge
every decision the tool made — full JDs, scoring reasoning, dropped roles
with reasons, source breakdown, salary coverage, keyword effectiveness.
The user (or another Claude session) can audit a tester's run from the
single JSON file alone, without needing access to the app or DB.

Atomic write: file is created as `.tmp` first, then renamed only after
the run completes successfully. Partial files don't masquerade as completed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _safe(s: Optional[str], maxlen: int = 200) -> str:
    if not s:
        return ""
    return str(s)[:maxlen]


def _ts_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")


def _get_app_version() -> str:
    """Read the package version dynamically. v0.2.3 fix: this used to
    be a hardcoded string in write_audit_files, which meant every audit
    JSON reported the version string the developer last typed manually
    rather than the actual build version. Now reads from
    backend.__version__ which is auto-bumped by sed during release."""
    try:
        from backend import __version__ as v
        return v
    except Exception:
        return "unknown"


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _company_distribution(roles: list) -> list[dict]:
    """Group qualifying roles by company; counts + top score per company."""
    by_company: dict[str, dict] = {}
    for r in roles:
        c = (getattr(r, "company", "") or "").strip()
        if not c:
            continue
        score = getattr(r, "final_score", None) or 0
        bucket = by_company.setdefault(c, {"company": c, "count": 0, "top_score": 0, "qualifying_count": 0})
        bucket["count"] += 1
        if score >= 40:
            bucket["qualifying_count"] += 1
        if score > bucket["top_score"]:
            bucket["top_score"] = score
    return sorted(by_company.values(), key=lambda b: -b["count"])


def _salary_buckets(roles: list) -> dict:
    """Coverage + bucketed range distribution."""
    total = len(roles)
    with_salary = sum(1 for r in roles if getattr(r, "salary_max", None))
    buckets = {"<$80K": 0, "$80-100K": 0, "$100-130K": 0, "$130-160K": 0,
               "$160-200K": 0, "$200K+": 0}
    for r in roles:
        smax = getattr(r, "salary_max", None)
        if smax is None:
            continue
        if smax < 80_000:
            buckets["<$80K"] += 1
        elif smax < 100_000:
            buckets["$80-100K"] += 1
        elif smax < 130_000:
            buckets["$100-130K"] += 1
        elif smax < 160_000:
            buckets["$130-160K"] += 1
        elif smax < 200_000:
            buckets["$160-200K"] += 1
        else:
            buckets["$200K+"] += 1
    return {
        "total_roles": total,
        "salary_found_count": with_salary,
        "salary_found_pct": (100.0 * with_salary / total) if total else 0,
        "range_distribution": buckets,
    }


def _keyword_effectiveness(roles: list, keywords: list[str]) -> list[dict]:
    """For each keyword, count how many roles' titles contain it (rough proxy
    for whether the keyword is producing results)."""
    out = []
    for kw in keywords:
        kw_lower = kw.lower()
        matched = [r for r in roles if kw_lower in (getattr(r, "job_title", "") or "").lower()]
        qualifying = [r for r in matched if (getattr(r, "final_score", 0) or 0) >= 55]
        top = max((getattr(r, "final_score", 0) or 0 for r in matched), default=0)
        out.append({
            "keyword": kw,
            "roles_matched": len(matched),
            "roles_qualifying": len(qualifying),
            "top_score": top,
        })
    return sorted(out, key=lambda x: -x["roles_qualifying"])


def _coverage_gap_analysis(qualifying: list, profile) -> dict:
    """Detect when the candidate's target industries are under-represented in
    the scraper pool. This is an HONESTY signal — instead of silently showing
    a tester weak results from adjacent industries, we surface that the tool's
    employer pool doesn't yet cover their target sectors well.

    Returns a dict with:
      - target_industries: from profile (or [])
      - industries_found: {industry: count} across qualifying roles
      - target_match_pct: % of qualifying roles whose industry matches a target
      - gap_severity: HIGH / MEDIUM / LOW / UNKNOWN / NO_DATA
      - dashboard_message: short human-readable warning if HIGH/MEDIUM, else None
      - recommendation: which scrapers/employers would address the gap

    Severity bands:
      <30% target match → HIGH
      30-60% → MEDIUM
      >60% → LOW (good coverage)
    """
    targets = []
    if profile is not None:
        ti = getattr(profile, "target_industries", None) or []
        targets = [str(x).strip() for x in ti if x]
    targets_lower = {t.lower() for t in targets}

    industries_found: dict[str, int] = {}
    matched = 0
    total_with_industry = 0
    for r in qualifying:
        ind = (getattr(r, "industry", None) or "").strip()
        if not ind:
            continue
        total_with_industry += 1
        industries_found[ind] = industries_found.get(ind, 0) + 1
        if ind.lower() in targets_lower:
            matched += 1
        else:
            # Loose substring match — "Tech" target should match "Technology"
            for t in targets_lower:
                if t and (t in ind.lower() or ind.lower() in t):
                    matched += 1
                    break

    if not qualifying:
        return {
            "target_industries": targets,
            "industries_found": {},
            "target_match_pct": 0.0,
            "gap_severity": "NO_DATA",
            "dashboard_message": None,
            "recommendation": None,
        }

    if not targets:
        return {
            "target_industries": [],
            "industries_found": industries_found,
            "target_match_pct": 0.0,
            "gap_severity": "UNKNOWN",
            "dashboard_message": None,
            "recommendation": "Profile has no target_industries — can't measure coverage gap.",
        }

    pct = (100.0 * matched / total_with_industry) if total_with_industry else 0.0

    # Suppress the dashboard warning when the run already returned plenty of
    # quality matches. If the user has 30+ roles scoring >= 55 (STRONG+GOOD+
    # MAYBE), the absolute count of in-industry roles is already strong even
    # at a low target-industry %, and the warning hurts credibility more
    # than it helps. The severity is still recorded for the audit JSON.
    quality_count = sum(
        1 for r in qualifying
        if (getattr(r, "final_score", None) or 0) >= 55
    )
    SUPPRESS_THRESHOLD = 30

    if pct >= 60:
        severity = "LOW"
        dashboard = None
    elif pct >= 30:
        severity = "MEDIUM"
        if quality_count >= SUPPRESS_THRESHOLD:
            dashboard = None
        else:
            dashboard = (
                f"Some of your target industries ({', '.join(targets[:3])}) have "
                f"limited coverage in this version — only {pct:.0f}% of your "
                f"qualifying roles are in your stated target sectors. We're "
                f"expanding employer coverage; results will improve over time."
            )
    else:
        severity = "HIGH"
        if quality_count >= SUPPRESS_THRESHOLD:
            dashboard = None
        else:
            dashboard = (
                f"Your target industries ({', '.join(targets[:3])}) have limited "
                f"coverage in this version. Only {pct:.0f}% of your qualifying "
                f"roles match your target sectors — the rest are from adjacent "
                f"industries. More employers in your space are coming in the next "
                f"update. In the meantime, these results show transferable roles "
                f"at companies outside your primary targets."
            )

    # Build a recommendation string for the operator (us, not the user)
    recommendation = None
    if severity in ("HIGH", "MEDIUM"):
        gaps = [t for t in targets
                if not any(t.lower() in ind.lower() or ind.lower() in t.lower()
                           for ind in industries_found)]
        if gaps:
            recommendation = (
                f"Add scrapers / employers for: {', '.join(gaps[:5])}. "
                f"Phase 1.5: iCIMS (covers insurance/healthcare/retail/manufacturing) "
                f"and Workday Playwright (Deloitte, JPMorgan, PepsiCo, etc.)."
            )

    return {
        "target_industries": targets,
        "industries_found": industries_found,
        "target_match_pct": round(pct, 1),
        "gap_severity": severity,
        "dashboard_message": dashboard,
        "recommendation": recommendation,
    }


def _keyword_coverage_scoring(keywords: list[str], roles: list) -> list[dict]:
    """Score each keyword by how many DISTINCT companies it surfaces. A keyword
    that finds 1 company at 1 role is "low coverage" — that company may not
    even hire for that role title in a meaningful way. A keyword that finds
    20 companies is "high coverage" — broad signal across the employer pool.

    This is different from _keyword_effectiveness which counts qualifying
    matches. Coverage measures BREADTH (companies), effectiveness measures
    QUALITY (qualifying scores). Both belong in the audit file.

    Returns list[dict] with: keyword, distinct_companies, total_matches,
    coverage_band (HIGH/MEDIUM/LOW/NONE).
    """
    out = []
    for kw in keywords:
        kw_lower = kw.lower()
        companies = set()
        total = 0
        for r in roles:
            title = (getattr(r, "job_title", "") or "").lower()
            if kw_lower in title:
                total += 1
                comp = (getattr(r, "company", "") or "").strip().lower()
                if comp:
                    companies.add(comp)
        n = len(companies)
        if n == 0:
            band = "NONE"
        elif n <= 2:
            band = "LOW"
        elif n <= 8:
            band = "MEDIUM"
        else:
            band = "HIGH"
        out.append({
            "keyword": kw,
            "distinct_companies": n,
            "total_matches": total,
            "coverage_band": band,
        })
    return sorted(out, key=lambda x: -x["distinct_companies"])


def _calibration_anomalies(
    scored_roles: list,
    embedding_top_pct: float = 0.30,
    s2_strong_threshold: int = 65,
    s3_low_threshold: int = 55,
) -> dict:
    """v0.3.3: detect calibration disagreements suggesting Stage 3 is
    over-rejecting roles that other independent signals flag as strong fits.

    Two independent signals are checked, either of which flags an anomaly:

      1. Embedding signal (auditor's recommendation, percentile-based):
         A role with embedding similarity in the TOP 30% of THIS RUN's
         distribution but with stage3_score < 55. The embedding model's
         view is an independent, non-LLM measure — when it ranks a role
         high but Stage 3 ranks it low, that's a calibration disagreement
         worth flagging. (Implementation note: original spec used a fixed
         absolute threshold of 0.80, but real audit data showed embedding
         similarity values cluster much lower per-tester — e.g. Mac
         Operations tester's qualifying pool spans only 0.58-0.68.
         Percentile-based threshold is tester-agnostic and catches the
         right outliers regardless of the underlying distribution.)

      2. Stage 2 → Stage 3 drop signal:
         A role where Stage 2 scored 65+ (strong-match band) but Stage 3
         dropped it below 55 (STRETCH band). This is a direct signal that
         the deeper Stage 3 evaluation undercut the triage decision in a
         way worth reviewing. The Twin Health Director of Conversion
         Operations role exhibited exactly this pattern: Stage 2 = 68,
         Stage 3 = 47.

    Both signals together give belt-and-suspenders coverage — the
    embedding signal catches model-independent outliers, the Stage 2→3
    signal catches within-cascade disagreements. Either flag is enough
    to surface a role for review.

    A clean run has 0 anomalies. Persistent non-zero counts across runs
    indicate the Stage 2/3 prompts need further calibration tuning.

    Returns:
        {
          "total_anomalies": int,
          "embedding_top_pct": float,           # config used
          "s2_strong_threshold": int,           # config used
          "s3_low_threshold": int,              # config used
          "embedding_pctile_cutoff": float,     # absolute embedding sim
                                                #  at the chosen percentile
                                                #  for THIS run
          "roles": [
            {
              "title": str,
              "company": str,
              "embedding_similarity": float,
              "stage2_score": int | None,
              "stage3_score": int,
              "trigger": str,  # "embedding" | "stage2_drop" | "both"
              "stage3_analysis_excerpt": str,  # first 300 chars
              "url": str,
            },
            ...
          ]
        }

    Empty roles list = clean run, no calibration disagreement detected.
    """
    # Compute the embedding-similarity percentile cutoff for THIS run.
    # Roles with embedding_similarity at or above this value are in the
    # top embedding_top_pct fraction.
    sims = [getattr(r, "embedding_similarity", None) for r in scored_roles]
    sims = [s for s in sims if s is not None]
    embedding_cutoff: float = 1.0  # default: nothing qualifies if no sims
    if sims:
        sims_sorted = sorted(sims, reverse=True)
        cut_idx = max(0, int(len(sims_sorted) * embedding_top_pct) - 1)
        cut_idx = min(cut_idx, len(sims_sorted) - 1)
        embedding_cutoff = sims_sorted[cut_idx]

    anomalies = []
    for r in scored_roles:
        s3 = getattr(r, "stage3_score", None)
        # Stage 3 may have skipped roles outside its entry band; those are
        # NOT anomalies, they're correctly bypassed by the cascade.
        if s3 is None:
            continue
        if s3 >= s3_low_threshold:
            continue

        emb_sim = getattr(r, "embedding_similarity", None)
        s2 = getattr(r, "stage2_score", None)

        emb_flag = (emb_sim is not None) and (emb_sim >= embedding_cutoff)
        s2_flag = (s2 is not None) and (s2 >= s2_strong_threshold)

        if not (emb_flag or s2_flag):
            continue

        if emb_flag and s2_flag:
            trigger = "both"
        elif emb_flag:
            trigger = "embedding"
        else:
            trigger = "stage2_drop"

        analysis = getattr(r, "stage3_analysis", "") or ""
        anomalies.append({
            "title": (getattr(r, "job_title", "") or "")[:120],
            "company": (getattr(r, "company", "") or "")[:80],
            "embedding_similarity": (
                round(float(emb_sim), 4) if emb_sim is not None else None
            ),
            "stage2_score": int(s2) if s2 is not None else None,
            "stage3_score": int(s3),
            "trigger": trigger,
            "stage3_analysis_excerpt": analysis[:300],
            "url": getattr(r, "job_url", "") or "",
        })

    # Sort worst disagreements first: "both" triggers > Stage 2 drop size >
    # then lowest Stage 3 score. Most actionable rows surface at the top.
    def _sort_key(x):
        trigger_priority = {"both": 0, "stage2_drop": 1, "embedding": 2}.get(
            x["trigger"], 3
        )
        s2_drop = (x["stage2_score"] or 0) - x["stage3_score"]
        return (trigger_priority, -s2_drop, x["stage3_score"])

    anomalies.sort(key=_sort_key)

    return {
        "total_anomalies": len(anomalies),
        "embedding_top_pct": embedding_top_pct,
        "s2_strong_threshold": s2_strong_threshold,
        "s3_low_threshold": s3_low_threshold,
        "embedding_pctile_cutoff": round(embedding_cutoff, 4),
        "roles": anomalies,
    }


def _build_diff(prior: dict, current: dict) -> dict:
    """Compare two audit dicts. Returns summary of new/disappeared/score-changed roles."""
    def _key(r):
        return f"{(r.get('company') or '').lower()}::{(r.get('job_title') or '').lower()}"

    prior_map = {_key(r): r for r in prior.get("all_qualifying_roles", [])}
    current_map = {_key(r): r for r in current.get("all_qualifying_roles", [])}

    new = [current_map[k] for k in current_map if k not in prior_map]
    disappeared = [prior_map[k] for k in prior_map if k not in current_map]
    score_changes = []
    for k in current_map:
        if k not in prior_map:
            continue
        p_score = prior_map[k].get("score", 0)
        c_score = current_map[k].get("score", 0)
        if abs(p_score - c_score) >= 5:
            score_changes.append({
                "company": current_map[k].get("company"),
                "title": current_map[k].get("title"),
                "prior_score": p_score,
                "current_score": c_score,
                "delta": c_score - p_score,
            })
    return {
        "new_roles_count": len(new),
        "disappeared_roles_count": len(disappeared),
        "score_changes_count": len(score_changes),
        "new_roles": new[:30],
        "disappeared_roles": disappeared[:30],
        "score_changes": score_changes[:30],
    }


def write_audit_files(
    *,
    audit_folder: Path,
    run_id: str,
    profile,
    keywords: list[str],
    scored_roles: list,
    summary,
    sources_searched: Optional[list[str]] = None,
    cache_was_hit: bool = False,
    prior_audit_path: Optional[Path] = None,
) -> dict[str, str]:
    """Write the full audit JSON + human MD summary + diff (if prior exists).

    Returns a dict with paths of written files.
    """
    audit_folder = Path(audit_folder)
    runs_dir = audit_folder / "runs"
    diffs_dir = audit_folder / "diffs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    diffs_dir.mkdir(parents=True, exist_ok=True)

    ts = _ts_filename()
    json_path = runs_dir / f"{ts}_{run_id[:8]}_audit.json"
    md_path = runs_dir / f"{ts}_{run_id[:8]}_summary.md"

    qualifying = [r for r in scored_roles if (getattr(r, "final_score", None) or 0) >= 40]
    near_miss = [r for r in scored_roles if 20 <= (getattr(r, "final_score", None) or 0) < 40]

    profile_dump = profile.model_dump(mode="json") if hasattr(profile, "model_dump") else {}
    # v0.3.4: strip resume filenames from the audit JSON. Filenames often
    # contain personal info ("Joshua_Reiner_AI_Strategy_Resume_v3.pdf") and
    # the audit JSON gets uploaded to the operator dashboard. The filename
    # has no analytical value past profile-build time — Stage 3 references
    # resumes by index ("unified" or "0", "1", "2"), not by filename.
    # Replace each filename with a stable placeholder that preserves the
    # ORDER (so multi-resume best_resume_match references still work).
    if isinstance(profile_dump, dict) and isinstance(profile_dump.get("resumes"), list):
        for i, r in enumerate(profile_dump["resumes"]):
            if isinstance(r, dict) and "filename" in r:
                # Preserve only the file extension as a coarse type signal
                ext = ""
                orig = str(r.get("filename") or "")
                if "." in orig:
                    ext = "." + orig.rsplit(".", 1)[-1].lower()[:5]
                r["filename"] = f"resume_{i + 1}{ext}"
    summary_dump = summary.model_dump(mode="json") if hasattr(summary, "model_dump") else {}

    # ---- Build audit dict ----
    # Build the per-stage cost breakdown from the discrete cost fields on
    # RunSummary. Previously the serializer expected a `cost_by_stage` field
    # that didn't exist on the model, so the dict was always empty. Now we
    # synthesize it from cost_stage1_usd / cost_stage2_usd / cost_stage3_usd /
    # cost_embeddings_usd / cost_misc_usd which the cost tracker actually
    # populates. Surfaces "where did the $0.46 go?" in the audit.
    cost_by_stage = {
        "stage1_usd":     summary_dump.get("cost_stage1_usd", 0.0),
        "stage2_usd":     summary_dump.get("cost_stage2_usd", 0.0),
        "stage3_usd":     summary_dump.get("cost_stage3_usd", 0.0),
        "embeddings_usd": summary_dump.get("cost_embeddings_usd", 0.0),
        "misc_usd":       summary_dump.get("cost_misc_usd", 0.0),
    }
    audit = {
        "run_metadata": {
            "run_id": run_id,
            "run_date": _iso(),
            "duration_seconds": summary_dump.get("duration_seconds"),
            "cost_breakdown": {
                "total_usd": summary_dump.get("cost_total_usd"),
                "by_stage": cost_by_stage,
            },
            "sources_searched": sources_searched or summary_dump.get("boards_searched", []),
            "keywords_used": keywords,
            "cache_was_hit": cache_was_hit,
            # v0.2.3: was hardcoded "0.2.0" — read from package metadata
            # so the audit JSON correctly reports whichever build wrote
            # it. Imported lazily to avoid a circular dependency at
            # module-load time.
            "app_version": _get_app_version(),
        },
        "profile_snapshot": profile_dump,
        "pipeline_funnel": {
            "total_scraped": summary_dump.get("roles_scraped"),
            "after_hard_filters": summary_dump.get("roles_after_filter"),
            "qualifying_final": summary_dump.get("roles_qualifying"),
            "tier_breakdown": {
                "STRONG": summary_dump.get("tier_strong"),
                "GOOD": summary_dump.get("tier_good"),
                "MAYBE": summary_dump.get("tier_maybe"),
                "STRETCH": summary_dump.get("tier_stretch"),
            },
            "coverage": {
                "jd_coverage_pct": summary_dump.get("jd_coverage_pct"),
                "salary_coverage_pct": summary_dump.get("salary_coverage_pct"),
                "location_coverage_pct": summary_dump.get("location_coverage_pct"),
            },
            # Per-source funnel — populated by runner.py after scoring
            # completes. Reveals where each scraper's roles get dropped:
            # raw_scraped → after_dedup → after_hard_filters →
            # after_liveness_and_deadlist → qualifying_final.
            "per_source_funnel": summary_dump.get("per_source_funnel") or {},
        },
        # Per-source health (Phase D quality monitoring) — auto-populated by
        # the scraper orchestrator. Tells us at audit time which scrapers
        # produced 0/few results, so we can quickly flag breakage.
        "per_source_health": summary_dump.get("per_source_counts") or {},
        "all_qualifying_roles": [_role_to_audit_dict(r) for r in qualifying],
        "near_miss_roles_for_audit": [_role_to_audit_dict(r) for r in near_miss[:30]],
        "company_distribution": _company_distribution(qualifying),
        "salary_coverage": _salary_buckets(qualifying),
        "keyword_effectiveness": _keyword_effectiveness(qualifying, keywords),
        # Coverage breadth (across ALL scored roles, not just qualifying) tells
        # us which keywords actually surface a diverse employer pool.
        "keyword_coverage": _keyword_coverage_scoring(keywords, scored_roles),
        # Coverage gap analysis: are the candidate's target industries actually
        # represented in our scraper pool? Surfaces honest "this version of
        # the tool doesn't cover your sector well" signal to testers.
        "coverage_gap_analysis": _coverage_gap_analysis(qualifying, profile),
        # v0.3.3 calibration anomaly detector — flags roles where embedding
        # similarity says "high fit" but Stage 3 scored "low fit". A clean
        # run has 0 entries here. Persistent non-zero counts across runs
        # indicate the Stage 2/3 prompts need further calibration tuning.
        # See _calibration_anomalies() docstring for full rationale.
        "calibration_anomalies": _calibration_anomalies(scored_roles),
        "tester_feedback": None,  # Filled in if user submits feedback
    }

    # ---- Diff vs prior run ----
    diff_path = None
    if prior_audit_path and Path(prior_audit_path).exists():
        try:
            prior = json.loads(Path(prior_audit_path).read_text(encoding="utf-8"))
            diff = _build_diff(prior, audit)
            audit["comparison_to_prior_run"] = diff
            diff_path = diffs_dir / f"{ts}_vs_{Path(prior_audit_path).stem}.md"
            diff_path.write_text(_format_diff_md(prior, audit, diff), encoding="utf-8")
        except Exception:
            pass

    # ---- Atomic JSON write ----
    tmp_json = json_path.with_suffix(".json.tmp")
    tmp_json.write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    tmp_json.replace(json_path)

    # ---- MD summary ----
    md_path.write_text(_format_summary_md(audit), encoding="utf-8")

    return {
        "json": str(json_path),
        "md": str(md_path),
        "diff": str(diff_path) if diff_path else None,
    }


def _role_to_audit_dict(r) -> dict:
    """Flatten a Role object into the audit-file format."""
    out = {
        "score": getattr(r, "final_score", None),
        "tier": getattr(r, "final_tier", None),
        "title": getattr(r, "job_title", None),
        "company": getattr(r, "company", None),
        "url": getattr(r, "job_url", None),
        "location": getattr(r, "location", None),
        "location_type": getattr(r, "location_type", None),
        "salary_min": getattr(r, "salary_min", None),
        "salary_max": getattr(r, "salary_max", None),
        "salary_text": getattr(r, "salary_text", None),
        "industry": getattr(r, "industry", None),
        "posted_date": getattr(r, "posted_date", None),
        # Source attribution: which scraper found this role. Critical for
        # debugging "why did this role get scored?" and identifying which
        # scrapers contribute STRONG/GOOD matches per persona.
        "source": getattr(r, "primary_source", None) or getattr(r, "source", None),
        # The single most-relevant keyword from the candidate's profile that
        # surfaced this role. Used by the dashboard's "via {keyword}" hint.
        "matched_keyword": getattr(r, "matched_keyword", None),
        "stage2_score": getattr(r, "stage2_score", None),
        "stage2_reasoning": getattr(r, "stage2_reasoning", None),
        "stage3_score": getattr(r, "stage3_score", None),
        "stage3_analysis": getattr(r, "stage3_analysis", None),
        "stage3_application_strategy": getattr(r, "stage3_application_strategy", None),
        "embedding_similarity": getattr(r, "embedding_similarity", None),
        "full_job_description": getattr(r, "job_description_full", None),
        # AI-C: 2-sentence dashboard summary (Stage 3 output)
        "summary": getattr(r, "summary", None),
    }
    # Convert tier enum to string
    if out["tier"] is not None and hasattr(out["tier"], "value"):
        out["tier"] = out["tier"].value
    return out


def _format_summary_md(audit: dict) -> str:
    """Build the human-readable .md summary."""
    meta = audit["run_metadata"]
    funnel = audit["pipeline_funnel"]
    tiers = funnel.get("tier_breakdown") or {}
    coverage = funnel.get("coverage") or {}
    profile = audit.get("profile_snapshot") or {}

    lines = [
        f"# Job Search Run Summary",
        f"",
        f"**Run ID:** `{meta['run_id']}`",
        f"**Date:** {meta['run_date']}",
        f"**Duration:** {meta.get('duration_seconds') or '?'}s",
        f"**Cost:** ${meta['cost_breakdown'].get('total_usd') or 0:.4f}",
        f"**Cache hit:** {'YES' if meta.get('cache_was_hit') else 'no (full pipeline)'}",
        f"",
        f"## Profile",
        f"",
        f"- **Headline:** {_safe(profile.get('headline'))}",
        f"- **Target functions:** {', '.join((profile.get('target_functions') or [])[:8])}",
        f"- **Profile tags:** {', '.join((profile.get('profile_tags') or [])[:8])}",
        f"- **Location:** {', '.join(profile.get('acceptable_locations') or [])}",
        f"- **Salary minimum:** ${profile.get('salary_minimum') or 0:,}",
        f"",
        f"## Pipeline Funnel",
        f"",
        f"| Stage | Count |",
        f"|---|---|",
        f"| Scraped | {funnel.get('total_scraped') or 0:,} |",
        f"| After hard filters | {funnel.get('after_hard_filters') or 0:,} |",
        f"| Final qualifying | {funnel.get('qualifying_final') or 0:,} |",
        f"",
        f"## Tier Breakdown",
        f"",
        f"| Tier | Count |",
        f"|---|---|",
        f"| STRONG (85+) | {tiers.get('STRONG') or 0} |",
        f"| GOOD (70-84) | {tiers.get('GOOD') or 0} |",
        f"| MAYBE (55-69) | {tiers.get('MAYBE') or 0} |",
        f"| STRETCH (40-54) | {tiers.get('STRETCH') or 0} |",
        f"",
        f"## Coverage",
        f"",
        f"- JD coverage: {coverage.get('jd_coverage_pct') or 0:.0f}%",
        f"- Salary coverage: {coverage.get('salary_coverage_pct') or 0:.0f}%",
        f"- Location coverage: {coverage.get('location_coverage_pct') or 0:.0f}%",
        f"",
        f"## Top 25 Qualifying Roles",
        f"",
        f"| Score | Tier | Company | Title | Salary |",
        f"|---|---|---|---|---|",
    ]
    qual = audit.get("all_qualifying_roles") or []
    for r in sorted(qual, key=lambda x: -(x.get("score") or 0))[:25]:
        salary = r.get("salary_text") or (
            f"${(r.get('salary_min') or 0):,} - ${(r.get('salary_max') or 0):,}"
            if r.get("salary_min") else "—"
        )
        lines.append(
            f"| {r.get('score'):.0f} | {r.get('tier') or '?'} | {_safe(r.get('company'), 25)} | "
            f"{_safe(r.get('title'), 50)} | {salary} |"
        )

    # Company distribution
    lines += [
        f"",
        f"## Company Distribution (top 10)",
        f"",
        f"| Company | Roles | Qualifying | Top Score |",
        f"|---|---|---|---|",
    ]
    for c in (audit.get("company_distribution") or [])[:10]:
        lines.append(
            f"| {_safe(c['company'], 30)} | {c['count']} | {c.get('qualifying_count', 0)} | {c.get('top_score') or 0:.0f} |"
        )

    # Coverage gap warning (if HIGH or MEDIUM)
    gap = audit.get("coverage_gap_analysis") or {}
    severity = gap.get("gap_severity")
    if severity in ("HIGH", "MEDIUM"):
        msg = gap.get("dashboard_message") or ""
        lines += [
            f"",
            f"## ⚠ Coverage Gap: {severity}",
            f"",
            f"**Target match:** {gap.get('target_match_pct', 0):.0f}% of qualifying roles in target industries",
            f"",
            msg,
        ]
        if gap.get("recommendation"):
            lines += [f"", f"**Operator note:** {gap['recommendation']}"]

    # Diff (if present)
    if "comparison_to_prior_run" in audit:
        diff = audit["comparison_to_prior_run"]
        lines += [
            f"",
            f"## Comparison to Prior Run",
            f"",
            f"- New roles: {diff.get('new_roles_count')}",
            f"- Disappeared: {diff.get('disappeared_roles_count')}",
            f"- Score changes: {diff.get('score_changes_count')}",
        ]

    return "\n".join(lines)


def _format_diff_md(prior: dict, current: dict, diff: dict) -> str:
    """Build a human-readable diff file."""
    lines = [
        f"# Run Comparison",
        f"",
        f"**Prior:** {prior.get('run_metadata', {}).get('run_date', '?')}",
        f"**Current:** {current.get('run_metadata', {}).get('run_date', '?')}",
        f"",
        f"## Summary",
        f"",
        f"- New roles found: **{diff['new_roles_count']}**",
        f"- Disappeared roles: **{diff['disappeared_roles_count']}** (likely closed)",
        f"- Score changes (>=5 pts): **{diff['score_changes_count']}**",
        f"",
    ]
    if diff["new_roles"]:
        lines += ["## New Roles", "", "| Score | Company | Title |", "|---|---|---|"]
        for r in diff["new_roles"][:25]:
            lines.append(f"| {r.get('score') or 0:.0f} | {_safe(r.get('company'), 25)} | {_safe(r.get('title'), 50)} |")
        lines.append("")
    if diff["score_changes"]:
        lines += ["## Score Changes", "", "| Company | Title | Prior | Current | Δ |", "|---|---|---|---|---|"]
        for c in diff["score_changes"]:
            lines.append(
                f"| {_safe(c['company'], 25)} | {_safe(c['title'], 40)} | "
                f"{c['prior_score']} | {c['current_score']} | "
                f"{'+' if c['delta'] >= 0 else ''}{c['delta']} |"
            )
        lines.append("")
    if diff["disappeared_roles"]:
        lines += ["## Disappeared Roles (likely closed)", "", "| Company | Title |", "|---|---|"]
        for r in diff["disappeared_roles"][:25]:
            lines.append(f"| {_safe(r.get('company'), 25)} | {_safe(r.get('title'), 50)} |")
    return "\n".join(lines)


def find_prior_audit_for_profile(audit_folder: Path) -> Optional[Path]:
    """Most recent prior audit JSON in the runs/ folder. Used to build diffs."""
    runs_dir = Path(audit_folder) / "runs"
    if not runs_dir.exists():
        return None
    candidates = sorted(runs_dir.glob("*_audit.json"))
    return candidates[-1] if candidates else None

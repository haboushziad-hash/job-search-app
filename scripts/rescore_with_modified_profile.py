"""Rescore a previous run's roles against a modified candidate profile.

Use case: validate that the freeform "negative_signals" field actually
influences scoring, without paying for a full re-scrape. Takes a finished
run's audit JSON, lets you tweak the profile (most often by adding
negative_signals), and re-runs Stage 2 (and optionally Stage 3) against
the cached role data.

Why this is useful:
  - Test new exclusion language ("Exclude ANY role requiring clearance,
    including Public Trust") and see which qualifying roles get demoted.
  - Check whether the LLM correctly interprets edge cases like
    "Public Trust" or "clearance preferred but not required".
  - Validate v0.3.7 fixes (title-floor exception, Stage 3 threshold
    revert, etc.) on real data without burning $0.65 on a fresh search.

Cost: ~$0.07-0.15 per rescore (Stage 2 only) or ~$0.30-0.50 (Stage 2 +
Stage 3 on borderline roles). Compare to ~$0.65 for a full search.

Usage:
  python scripts/rescore_with_modified_profile.py \
      --input "C:/Users/habou/Downloads/run (10).json" \
      --add-signal "Exclude any role requiring any government clearance" \
      --add-signal "Exclude roles requiring relocation" \
      --rerun-stage3 \
      --output rescore_test_1.json

  # Or just diff the profile changes without rescoring:
  python scripts/rescore_with_modified_profile.py \
      --input "C:/Users/habou/Downloads/run (10).json" \
      --add-signal "Exclude clearance roles" \
      --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.models import CandidateProfile, Role, Tier, score_to_tier
from backend.scoring.stage2_triage import stage2_triage
from backend.scoring.stage3_deep_eval import stage3_deep_eval


def reconstruct_profile(snapshot: dict[str, Any]) -> CandidateProfile:
    """Build a CandidateProfile from the audit JSON's profile_snapshot.

    The profile_snapshot field captures everything the original run used.
    We reconstruct it here so the rescore reflects the exact starting
    state — only the deltas we explicitly add (e.g., new negative_signals)
    differ.
    """
    return CandidateProfile(
        name=snapshot.get("name") or "test",
        headline=snapshot.get("headline", ""),
        target_functions=list(snapshot.get("target_functions") or []),
        target_industries=list(snapshot.get("target_industries") or []),
        target_seniority=snapshot.get("target_seniority"),
        years_experience=snapshot.get("years_experience"),
        technical_skills=list(snapshot.get("technical_skills") or []),
        soft_skills=list(snapshot.get("soft_skills") or []),
        domain_expertise=list(snapshot.get("domain_expertise") or []),
        salary_minimum=snapshot.get("salary_minimum"),
        work_arrangements=list(snapshot.get("work_arrangements") or []),
        acceptable_locations=list(snapshot.get("acceptable_locations") or []),
        excluded_locations=list(snapshot.get("excluded_locations") or []),
        negative_signals=list(snapshot.get("negative_signals") or []),
        excluded_title_patterns=list(
            snapshot.get("excluded_title_patterns") or []
        ),
        # Other fields not relevant to scoring
        keywords=[],
        search_terms=[],
        resumes=[],
    )


def reconstruct_role(record: dict[str, Any]) -> Role:
    """Build a Role from an audit JSON entry. Preserves the JD body so
    Stage 2 has the same input as the original run."""
    return Role(
        job_title=record.get("title") or "",
        company=record.get("company") or "",
        job_url=record.get("url") or "",
        location=record.get("location"),
        location_type=record.get("location_type"),
        salary_min=record.get("salary_min"),
        salary_max=record.get("salary_max"),
        salary_text=record.get("salary_text"),
        industry=record.get("industry"),
        posted_date=record.get("posted_date"),
        primary_source=record.get("source") or "rescore",
        job_description_full=record.get("full_job_description") or "",
        date_first_seen=datetime.now(timezone.utc).date().isoformat(),
    )


def diff_summary(
    original: list[dict[str, Any]],
    rescored: list[Role],
) -> list[dict[str, Any]]:
    """Per-role before/after summary, sorted by absolute score delta."""
    orig_by_url = {r.get("url"): r for r in original if r.get("url")}
    out = []
    for new in rescored:
        old = orig_by_url.get(new.job_url, {})
        old_score = old.get("score") or 0
        new_score = new.stage2_score or 0
        delta = new_score - old_score
        out.append(
            {
                "title": new.job_title,
                "company": new.company,
                "old_score": old_score,
                "old_tier": old.get("tier"),
                "new_score": new_score,
                "new_tier": (
                    score_to_tier(new_score).value
                    if isinstance(score_to_tier(new_score), Tier)
                    else str(score_to_tier(new_score))
                ),
                "delta": delta,
                "old_reasoning": (old.get("stage2_reasoning") or "")[:300],
                "new_reasoning": (new.stage2_reasoning or "")[:300],
                "url": new.job_url,
            }
        )
    out.sort(key=lambda x: -abs(x["delta"]))
    return out


def print_report(
    diff: list[dict[str, Any]],
    new_signals_added: list[str],
    *,
    only_significant: bool = True,
):
    print()
    print("=" * 80)
    print("RESCORE REPORT")
    print("=" * 80)
    print(f"Profile changes:")
    for s in new_signals_added:
        print(f"  + ADDED negative_signal: {s!r}")
    print()

    big_movers = [d for d in diff if abs(d["delta"]) >= 5]
    drops = [d for d in big_movers if d["delta"] < 0]
    rises = [d for d in big_movers if d["delta"] > 0]

    print(f"Total rescored: {len(diff)}")
    print(f"Roles dropped >= 5 points: {len(drops)}")
    print(f"Roles raised >= 5 points: {len(rises)}")
    print()

    if drops:
        print("=" * 80)
        print(f"DROPS (sorted by largest demotion) — top {min(20, len(drops))}")
        print("=" * 80)
        for d in drops[:20]:
            print(
                f"  {d['old_score']:>3} -> {d['new_score']:>3} "
                f"(Δ{d['delta']:+3d}) "
                f"[{d['old_tier']}->{d['new_tier']}] "
                f"{d['title'][:50]} @ {d['company'][:25]}"
            )
            print(f"      OLD: {d['old_reasoning'][:150]}")
            print(f"      NEW: {d['new_reasoning'][:150]}")
            print()

    if rises:
        print("=" * 80)
        print(f"RISES (sorted by largest promotion) — top {min(10, len(rises))}")
        print("=" * 80)
        for d in rises[:10]:
            print(
                f"  {d['old_score']:>3} -> {d['new_score']:>3} "
                f"(Δ+{d['delta']:>2}) "
                f"[{d['old_tier']}->{d['new_tier']}] "
                f"{d['title'][:50]} @ {d['company'][:25]}"
            )

    if not only_significant:
        print()
        print(f"All {len(diff)} rescored roles (including unchanged):")
        for d in diff:
            print(
                f"  {d['old_score']:>3} -> {d['new_score']:>3} "
                f"({d['delta']:+3d}) {d['title'][:55]}"
            )


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", required=True, help="Path to run JSON")
    p.add_argument(
        "--add-signal",
        action="append",
        default=[],
        help="Add a negative_signal phrase. Can be passed multiple times.",
    )
    p.add_argument(
        "--remove-signal",
        action="append",
        default=[],
        help="Remove an existing negative_signal phrase.",
    )
    p.add_argument(
        "--add-excluded-pattern",
        action="append",
        default=[],
        help="Add a pattern to excluded_title_patterns.",
    )
    p.add_argument(
        "--filter-jd-contains",
        default=None,
        help="Only rescore roles whose JD contains this substring. "
        "Useful for targeted tests, e.g. --filter-jd-contains clearance",
    )
    p.add_argument(
        "--rerun-stage3",
        action="store_true",
        help="Also re-run Stage 3 on roles with new score >= 55. Costs more.",
    )
    p.add_argument(
        "--include-near-miss",
        action="store_true",
        help="Include near-miss roles (didn't qualify) in the rescore set.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print profile changes + role count without making LLM calls.",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Optional output JSON for full rescore details.",
    )
    args = p.parse_args()

    # Load the run JSON
    with open(args.input, encoding="utf-8") as f:
        run = json.load(f)

    snapshot = run.get("profile_snapshot", {})
    profile = reconstruct_profile(snapshot)

    # Apply profile modifications
    new_negs = list(profile.negative_signals)
    for sig in args.add_signal:
        if sig not in new_negs:
            new_negs.append(sig)
    for sig in args.remove_signal:
        new_negs = [s for s in new_negs if s != sig]
    profile.negative_signals = new_negs

    new_excl = list(profile.excluded_title_patterns)
    for pat in args.add_excluded_pattern:
        if pat.lower() not in [x.lower() for x in new_excl]:
            new_excl.append(pat.lower())
    profile.excluded_title_patterns = new_excl

    # Build role list
    role_records = run.get("all_qualifying_roles", [])
    if args.include_near_miss:
        role_records = role_records + run.get("near_miss_roles_for_audit", [])

    # Optional JD substring filter
    if args.filter_jd_contains:
        needle = args.filter_jd_contains.lower()
        role_records = [
            r
            for r in role_records
            if needle in (r.get("full_job_description") or "").lower()
        ]

    print(f"Loaded run from: {args.input}")
    print(f"Original qualifying roles: {len(run.get('all_qualifying_roles', []))}")
    print(f"Roles selected for rescore: {len(role_records)}")
    print(f"Profile changes:")
    print(f"  ADD signals: {args.add_signal}")
    print(f"  REMOVE signals: {args.remove_signal}")
    print(f"  ADD exclusion patterns: {args.add_excluded_pattern}")

    if args.dry_run:
        print()
        print("=== DRY RUN — not invoking LLM ===")
        print(f"Modified profile.negative_signals = {profile.negative_signals}")
        print(
            f"Modified profile.excluded_title_patterns = "
            f"{profile.excluded_title_patterns}"
        )
        return

    if not role_records:
        print("No roles selected — exiting.")
        return

    roles = [reconstruct_role(r) for r in role_records]

    print()
    print(f"=== Running Stage 2 on {len(roles)} roles ===")
    rescored = await stage2_triage(
        profile=profile,
        roles=roles,
        run_id=f"rescore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
    )

    if args.rerun_stage3:
        # Stage 3 only runs on roles in the borderline band
        borderline = [r for r in rescored if r.stage2_score and r.stage2_score >= 55]
        print(f"=== Running Stage 3 on {len(borderline)} borderline roles ===")
        if borderline:
            await stage3_deep_eval(
                profile=profile,
                roles=borderline,
                run_id=f"rescore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-s3",
            )

    # Compare
    diff = diff_summary(role_records, rescored)
    print_report(diff, args.add_signal)

    # Optional output JSON
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "input": args.input,
                    "profile_changes": {
                        "added_signals": args.add_signal,
                        "removed_signals": args.remove_signal,
                        "added_excluded_patterns": args.add_excluded_pattern,
                    },
                    "rescore_diff": diff,
                    "summary": {
                        "total_rescored": len(diff),
                        "drops_5_or_more": sum(1 for d in diff if d["delta"] <= -5),
                        "rises_5_or_more": sum(1 for d in diff if d["delta"] >= 5),
                    },
                },
                f,
                indent=2,
            )
        print(f"\nFull diff written to: {args.output}")


if __name__ == "__main__":
    asyncio.run(main())

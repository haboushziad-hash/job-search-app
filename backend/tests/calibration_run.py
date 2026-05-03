r"""Calibration validation — stratified sample with per-category analysis.

Compares the new Gemini cascade against the original tool's role_category
labels (A=best ... F=worst). Surfaces:

  - Score distribution per historical category
  - False negatives (Cat A roles that scored < 55)
  - False positives (Cat E/F roles that scored >= 55)
  - Stage-by-stage attrition with sampling at each gate
  - Cost projection at full production volume

Run:
  backend\venv\Scripts\python.exe -m backend.tests.calibration_run --per-cat 80
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from backend.config import config
from backend.models import Role
from backend.scoring.orchestrator import score_roles
from backend.tests.validation_run import ziad_profile


# ----------------------------------------------------------------------------
# Stratified loader
# ----------------------------------------------------------------------------

def load_stratified_sample(per_category: int) -> list[Role]:
    """Load `per_category` roles from each historical role_category bucket.

    Buckets: A (best), B, C, D, E, F (worst), and (null) for unscored.
    """
    if not config.REFERENCE_DB.exists():
        raise FileNotFoundError(f"Reference DB not found at {config.REFERENCE_DB}")

    conn = sqlite3.connect(str(config.REFERENCE_DB))
    conn.row_factory = sqlite3.Row

    categories = ["A", "B", "C", "D", "E", "F", None]
    all_roles: list[Role] = []
    counts: dict[str, int] = {}

    for cat in categories:
        if cat is None:
            where = "WHERE role_category IS NULL"
            label = "(null)"
        else:
            where = f"WHERE role_category = '{cat}'"
            label = cat
        sql = f"""
            SELECT * FROM roles
            {where}
              AND LENGTH(COALESCE(job_description_full, '')) > 200
            ORDER BY RANDOM()
            LIMIT ?
        """
        rows = conn.execute(sql, (per_category,)).fetchall()
        counts[label] = len(rows)

        for r in rows:
            d = dict(r)
            for bool_field in ("management_required", "consulting_experience_req"):
                if d.get(bool_field) is not None:
                    d[bool_field] = bool(d[bool_field])
            try:
                all_roles.append(Role(**d))
            except Exception as e:
                print(f"[loader] skipping malformed row {d.get('job_id')}: {e}")

    conn.close()
    print("Loaded stratified sample:")
    for label, n in counts.items():
        print(f"  Cat {label:>6}: {n}")
    print(f"  TOTAL:       {len(all_roles)}")
    return all_roles


# ----------------------------------------------------------------------------
# Analysis helpers
# ----------------------------------------------------------------------------

def per_category_stats(scored: list[Role]) -> None:
    """Score statistics broken down by historical role_category."""
    print("\n" + "=" * 70)
    print("SCORE DISTRIBUTION BY HISTORICAL CATEGORY")
    print("=" * 70)
    print(f"{'Cat':<6} {'N':>4} {'Mean':>6} {'Median':>7} {'P25':>5} {'P75':>5} "
          f"{'>=85':>5} {'>=70':>5} {'>=55':>5} {'<40':>5}")
    print("-" * 70)

    by_cat: dict[str, list[int]] = defaultdict(list)
    for r in scored:
        cat = r.role_category or "(null)"
        if r.final_score is not None:
            by_cat[cat].append(r.final_score)

    for cat in ["A", "B", "C", "D", "E", "F", "(null)"]:
        scores = by_cat.get(cat, [])
        if not scores:
            continue
        scores.sort()
        n = len(scores)
        mean = sum(scores) / n
        median = scores[n // 2]
        p25 = scores[max(0, n // 4)]
        p75 = scores[min(n - 1, (3 * n) // 4)]
        c_strong = sum(1 for s in scores if s >= 85)
        c_good = sum(1 for s in scores if s >= 70)
        c_qual = sum(1 for s in scores if s >= 55)
        c_skip = sum(1 for s in scores if s < 40)
        print(f"{cat:<6} {n:>4} {mean:>6.1f} {median:>7} {p25:>5} {p75:>5} "
              f"{c_strong:>5} {c_good:>5} {c_qual:>5} {c_skip:>5}")


def find_false_negatives(scored: list[Role], n: int = 10) -> list[Role]:
    """Cat A roles that scored < 55 — possible miscalibration we want to fix."""
    cands = [r for r in scored if r.role_category == "A" and (r.final_score or 0) < 55]
    cands.sort(key=lambda r: r.final_score or 0)
    return cands[:n]


def find_false_positives(scored: list[Role], n: int = 10) -> list[Role]:
    """Cat E/F roles that scored >= 55 — possible miscalibration."""
    cands = [r for r in scored if r.role_category in ("E", "F") and (r.final_score or 0) >= 55]
    cands.sort(key=lambda r: r.final_score or 0, reverse=True)
    return cands[:n]


def show_role(r: Role, prefix: str = "") -> None:
    title = (r.job_title or "")[:55]
    company = (r.company or "")[:30]
    score = r.final_score if r.final_score is not None else 0
    sim = f"{r.embedding_similarity:.2f}" if r.embedding_similarity is not None else " -- "
    cat = r.role_category or "?"
    print(f"{prefix}[{cat}|score={score:>3}|sim={sim}]  {title:<55}  @ {company}")
    if r.stage1_keep is False:
        print(f"{prefix}    KILLED in Stage 1: {(r.stage1_reason or '')[:120]}")
    elif r.stage2_reasoning:
        print(f"{prefix}    S2: {r.stage2_reasoning[:160]}")
    if r.stage3_analysis and not r.stage3_analysis.startswith("stage3_error"):
        print(f"{prefix}    S3: {r.stage3_analysis[:160]}")


def stage_attrition(scored: list[Role]) -> None:
    """Where in the pipeline did each historical-category role drop out?"""
    print("\n" + "=" * 70)
    print("PIPELINE ATTRITION BY CATEGORY")
    print("=" * 70)
    print(f"{'Cat':<6} {'Total':>6} {'Embed kept':>11} {'S1 kept':>9} {'S2 scored':>10} {'>=55':>6}")
    print("-" * 70)

    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {
        "total": 0, "embed_kept": 0, "s1_kept": 0, "s2_scored": 0, "qualifying": 0,
    })
    for r in scored:
        cat = r.role_category or "(null)"
        by_cat[cat]["total"] += 1
        if r.embedding_similarity is not None:
            by_cat[cat]["embed_kept"] += 1
        if r.stage1_keep is True:
            by_cat[cat]["s1_kept"] += 1
        if r.stage2_score is not None:
            by_cat[cat]["s2_scored"] += 1
        if (r.final_score or 0) >= 55:
            by_cat[cat]["qualifying"] += 1

    for cat in ["A", "B", "C", "D", "E", "F", "(null)"]:
        d = by_cat.get(cat)
        if not d:
            continue
        print(f"{cat:<6} {d['total']:>6} {d['embed_kept']:>11} {d['s1_kept']:>9} "
              f"{d['s2_scored']:>10} {d['qualifying']:>6}")


# ----------------------------------------------------------------------------
# Persistence — write full results to archive for offline inspection
# ----------------------------------------------------------------------------

def save_results(scored: list[Role], summary, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"calibration_{ts}.json"
    payload = {
        "summary": summary.model_dump(mode="json"),
        "roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in scored],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

async def main(per_category: int) -> int:
    errors = config.validate()
    if errors:
        print("Config errors:")
        for e in errors:
            print(f"  - {e}")
        return 1

    profile = ziad_profile()
    print(f"\nProfile: {profile.headline}\n")

    roles = load_stratified_sample(per_category)
    print(f"\nRunning Gemini cascade on {len(roles)} stratified roles...\n")

    scored, summary = await score_roles(profile=profile, roles=roles)

    # ---- Cost + scaling ----
    print("\n" + "=" * 70)
    print("COST AND SCALING")
    print("=" * 70)
    print(f"Total cost:                ${summary.cost_total_usd:.4f}")
    print(f"  Embeddings:              ${summary.cost_embeddings_usd:.4f}")
    print(f"  Stage 1 (Flash):         ${summary.cost_stage1_usd:.4f}")
    print(f"  Stage 2 (Pro):           ${summary.cost_stage2_usd:.4f}")
    print(f"  Stage 3 (Pro):           ${summary.cost_stage3_usd:.4f}")
    print(f"Duration:                  {summary.duration_seconds}s")
    n = max(1, summary.roles_scraped)
    print(f"Cost per input role:       ${summary.cost_total_usd / n:.5f}")
    print(f"Projected @ 1500 roles:    ${summary.cost_total_usd * 1500 / n:.2f}")
    print(f"Projected @ 2500 roles:    ${summary.cost_total_usd * 2500 / n:.2f}")

    # ---- Per-category analysis ----
    per_category_stats(scored)
    stage_attrition(scored)

    # ---- False negatives ----
    fn = find_false_negatives(scored, n=10)
    print("\n" + "=" * 70)
    print(f"FALSE NEGATIVES — Cat A roles that scored < 55  ({len(fn)} found)")
    print("=" * 70)
    if not fn:
        print("  (none — perfect Cat A coverage)")
    else:
        print("These are likely good roles the cascade rejected. Review carefully:")
        for r in fn:
            show_role(r, prefix="  ")

    # ---- False positives ----
    fp = find_false_positives(scored, n=10)
    print("\n" + "=" * 70)
    print(f"FALSE POSITIVES — Cat E/F roles that scored >= 55  ({len(fp)} found)")
    print("=" * 70)
    if not fp:
        print("  (none — clean rejection of low-quality roles)")
    else:
        print("These are likely bad roles the cascade let through. Review:")
        for r in fp:
            show_role(r, prefix="  ")

    # ---- Top picks ----
    print("\n" + "=" * 70)
    print("TOP 20 PICKS BY FINAL SCORE")
    print("=" * 70)
    top = sorted(scored, key=lambda r: r.final_score or 0, reverse=True)[:20]
    for r in top:
        show_role(r, prefix="  ")

    # ---- Save ----
    out_path = save_results(scored, summary, config.ARCHIVE_DIR / "validation_results")
    print(f"\nFull results saved to: {out_path}")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-cat", type=int, default=80,
                        help="Roles per historical category (A,B,C,D,E,F,null) — default 80 = ~560 total")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.per_cat)))

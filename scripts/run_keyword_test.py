"""One-off script: run keyword generation for Ziad and Zachary in parallel,
print + persist the results so we can audit before kicking off a full search.

Run from project root:
    backend\venv\Scripts\python.exe scripts\run_keyword_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

# Make `backend` importable when run from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import build_profile_from_resumes


ZIAD_RESUME = Path(r"C:\Users\habou\Downloads\Haboush_Ziad_Resume_April 2026.pdf")
ZACH_RESUME = Path(r"C:\Users\habou\Downloads\Zachary_Charles_Resume.pdf")

OUT_DIR = Path(__file__).resolve().parent / "keyword_test_output"
OUT_DIR.mkdir(exist_ok=True)


# Preferences mirror what the user enters via Setup.tsx in the app.
ZIAD_PREFS = {
    "salary_minimum": 100000,
    "work_arrangements": ["remote", "hybrid", "on-site"],
    "acceptable_locations": ["Washington, DC", "Richmond, VA"],
    "acceptable_location_radii": [40, 40],
    "excluded_locations": [],
    "freeform_context": (
        "Senior Consultant at Booz Allen Hamilton with 4 years federal AI strategy "
        "experience. Currently pursuing Georgetown MPS in AI Management & Strategy "
        "(graduating 2026). Targeting AI enablement, AI adoption, AI governance, "
        "and AI strategy roles at AI-native companies, Big 4 firms with AI practices, "
        "Microsoft Copilot partners, and modern enterprise software companies. "
        "Strong fit for Forward Deployed Strategist, Engagement Manager AI, "
        "Federal AI advisory, and Copilot enablement roles. Open to federal AI work "
        "if it's strategy/advisory rather than pure delivery — not actively leaving "
        "federal but want broader options. Avoid hands-on ML engineering / "
        "data engineering / solutions architect roles."
    ),
}

ZACH_PREFS = {
    "salary_minimum": 60000,
    "work_arrangements": ["hybrid", "on-site", "remote"],
    "acceptable_locations": ["Richmond, VA"],
    "acceptable_location_radii": [50],
    "excluded_locations": [],
    "freeform_context": (
        "Business Administration senior at VCU (May 2026), Finance minor, 3.917 GPA. "
        "Almost 4 years at PepsiCo Frito-Lay as Route Sales Rep — manage 27-account "
        "territory, grew revenue 44% from $1.66M to $2.39M, named Rookie of the Year. "
        "Targeting full-time roles in operations, consulting, project management, "
        "or finance — primary interest is operations and consulting given my "
        "P&L ownership and territory management experience. Strong fit for "
        "rotational / leadership development programs at Fortune 500 companies "
        "(PepsiCo, Coca-Cola, Anheuser-Busch, Target, etc). Open to CPG / retail / "
        "distribution roles where my Frito-Lay experience translates directly."
    ),
}


async def run_one(name: str, resume_path: Path, prefs: dict) -> dict:
    print(f"\n[{name}] starting...")
    t0 = time.time()

    def _progress(pct: int, stage: str) -> None:
        print(f"  [{name}] {pct:3d}%  {stage}")

    profile = await build_profile_from_resumes(
        resume_paths=[resume_path],
        user_preferences=prefs,
        progress=_progress,
    )

    elapsed = time.time() - t0
    print(f"[{name}] done in {elapsed:.1f}s")

    t1 = [k.text for k in profile.keywords if k.tier == 1]
    t2 = [k.text for k in profile.keywords if k.tier == 2]
    t3 = [k.text for k in profile.keywords if k.tier == 3]

    out = {
        "name": name,
        "elapsed_seconds": round(elapsed, 1),
        "headline": profile.headline,
        "years_experience": profile.years_experience,
        "target_seniority": profile.target_seniority,
        "target_functions": profile.target_functions,
        "target_industries": profile.target_industries,
        "technical_skills": profile.technical_skills,
        "domain_expertise": profile.domain_expertise,
        "keyword_counts": {"t1": len(t1), "t2": len(t2), "t3": len(t3), "total": len(profile.keywords)},
        "tier_1": t1,
        "tier_2": t2,
        "tier_3": t3,
    }

    (OUT_DIR / f"{name}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def print_summary(out: dict) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {out['name']}  —  {out['keyword_counts']['total']} keywords  ({out['elapsed_seconds']}s)")
    print(f"  T1: {out['keyword_counts']['t1']}  T2: {out['keyword_counts']['t2']}  T3: {out['keyword_counts']['t3']}")
    print(f"{'=' * 70}")
    print(f"\nHeadline: {out['headline']}")
    print(f"Seniority: {out['target_seniority']}")
    print(f"Target functions: {', '.join(out['target_functions'])}")
    print()
    print(f"--- TIER 1 ({len(out['tier_1'])}) ---")
    for kw in out["tier_1"]:
        print(f"  • {kw}")
    print(f"\n--- TIER 2 ({len(out['tier_2'])}) ---")
    for kw in out["tier_2"]:
        print(f"  • {kw}")
    print(f"\n--- TIER 3 ({len(out['tier_3'])}) ---")
    for kw in out["tier_3"]:
        print(f"  • {kw}")


async def main() -> None:
    if not ZIAD_RESUME.exists():
        sys.exit(f"Missing Ziad resume: {ZIAD_RESUME}")
    if not ZACH_RESUME.exists():
        sys.exit(f"Missing Zach resume: {ZACH_RESUME}")

    # Run both in parallel — saves ~2x time vs sequential
    ziad_out, zach_out = await asyncio.gather(
        run_one("Ziad", ZIAD_RESUME, ZIAD_PREFS),
        run_one("Zach", ZACH_RESUME, ZACH_PREFS),
    )

    print_summary(ziad_out)
    print_summary(zach_out)

    print(f"\n\nResults written to: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())

"""Run only the Zach profile (Ziad already completed earlier).

Avoids re-running Ziad which would burn another $0.14 + 7 minutes.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import build_profile_from_resumes, keywords_to_search_strings
from backend.runner import run_search
from backend.storage import set_audit_folder

ZACH_RESUME = Path(r"C:\Users\habou\Downloads\Zachary_Charles_Resume.pdf")
OUT_DIR = Path(__file__).resolve().parent / "full_search_output"

set_audit_folder(OUT_DIR)

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


async def main():
    print(f"\n[Zach] Building profile + keywords...")
    t0 = time.time()
    profile = await build_profile_from_resumes(
        resume_paths=[ZACH_RESUME],
        user_preferences=ZACH_PREFS,
    )
    build_elapsed = time.time() - t0
    print(f"[Zach] Profile built in {build_elapsed:.1f}s — {len(profile.keywords)} keywords")
    print(f"[Zach] excluded_title_patterns: {profile.excluded_title_patterns}")

    keywords = keywords_to_search_strings(profile, max_tier=2)
    print(f"[Zach] Searching with {len(keywords)} T1+T2 keywords")

    t0 = time.time()
    scored, summary = await run_search(
        profile=profile,
        keywords=keywords,
        sources=None,
        posted_within_days=30,
        log=True,
    )
    search_elapsed = time.time() - t0
    print(f"[Zach] Full search done in {search_elapsed:.1f}s")

    qualifying = [r for r in scored if (r.final_score or 0) >= 40]

    out = {
        "name": "Zach",
        "duration_seconds": int(build_elapsed + search_elapsed),
        "build_seconds": int(build_elapsed),
        "search_seconds": int(search_elapsed),
        "profile": {
            "headline": profile.headline,
            "target_seniority": profile.target_seniority,
            "target_functions": profile.target_functions,
            "excluded_title_patterns": profile.excluded_title_patterns,
            "tier_1": [k.text for k in profile.keywords if k.tier == 1],
            "tier_2": [k.text for k in profile.keywords if k.tier == 2],
            "tier_3": [k.text for k in profile.keywords if k.tier == 3],
        },
        "summary": summary.model_dump(mode="json"),
        "qualifying_roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in qualifying],
        "all_roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in scored],
    }
    (OUT_DIR / "Zach_full.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    print(f"\n[Zach] Final qualifying: {len(qualifying)}")
    print(f"  STRONG:   {summary.tier_strong}")
    print(f"  GOOD:     {summary.tier_good}")
    print(f"  MAYBE:    {summary.tier_maybe}")
    print(f"  STRETCH:  {summary.tier_stretch}")
    print(f"\n  Coverage gap: {summary.coverage_gap_severity} ({summary.coverage_gap_target_match_pct}%)")
    if summary.coverage_gap_message:
        print(f"  Message: {summary.coverage_gap_message[:120]}...")


if __name__ == "__main__":
    asyncio.run(main())

"""Final validation run — Ziad + Zach + Ryan (accountant) sequentially.

Tests the full Phase A+B+C pipeline:
  - 15 active scrapers (10 ATSes + 5 broad aggregators)
  - Per-source health captured to audit JSON
  - Source attribution per qualifying role
  - Per-company hard-filter cap disabled
  - All Phase A audit fixes (cap=0, summary in schema, temp=0.0, etc.)

Sequential to avoid API rate limits across the 3 simultaneous runs.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import build_profile_from_resumes, keywords_to_search_strings
from backend.runner import run_search
from backend.storage import set_audit_folder


OUT_DIR = Path(__file__).resolve().parent / "full_search_output"
set_audit_folder(OUT_DIR)


PROFILES = [
    {
        "name": "Ziad",
        "resume": Path(r"C:\Users\habou\Downloads\Haboush_Ziad_Resume_April 2026.pdf"),
        "prefs": {
            "salary_minimum": 100000,
            "work_arrangements": ["remote", "hybrid", "on-site"],
            "acceptable_locations": ["Washington, DC", "Richmond, VA"],
            "acceptable_location_radii": [40, 40],
            "excluded_locations": [],
            "freeform_context": (
                "Senior Consultant at Booz Allen Hamilton with 4 years federal AI strategy "
                "experience. Currently pursuing Georgetown MPS in AI Management & Strategy. "
                "Targeting AI enablement, AI adoption, AI governance, and AI strategy roles "
                "at AI-native companies, Big 4 firms with AI practices, Microsoft Copilot "
                "partners, and modern enterprise software companies. Strong fit for Forward "
                "Deployed Strategist, Engagement Manager AI, Federal AI advisory, and "
                "Copilot enablement roles. Avoid hands-on ML engineering."
            ),
        },
    },
    {
        "name": "Zach",
        "resume": Path(r"C:\Users\habou\Downloads\Zachary_Charles_Resume.pdf"),
        "prefs": {
            "salary_minimum": 60000,
            "work_arrangements": ["hybrid", "on-site", "remote"],
            "acceptable_locations": ["Richmond, VA"],
            "acceptable_location_radii": [50],
            "excluded_locations": [],
            "freeform_context": (
                "Business Administration senior at VCU (May 2026), Finance minor, 3.917 GPA. "
                "Almost 4 years at PepsiCo Frito-Lay as Route Sales Rep. Targeting full-time "
                "roles in operations, consulting, project management, or finance — primary "
                "interest is operations and consulting given my P&L ownership and territory "
                "management experience. Strong fit for rotational / leadership development "
                "programs at Fortune 500 companies. Open to CPG / retail / distribution roles."
            ),
        },
    },
    {
        "name": "Ryan",
        "resume": Path(r"C:\Users\habou\Downloads\Ryan Abouzaki's Resume.docx"),
        "prefs": {
            "salary_minimum": 70000,
            "work_arrangements": ["hybrid", "on-site", "remote"],
            "acceptable_locations": ["Richmond, VA", "Washington, DC"],
            "acceptable_location_radii": [50, 40],
            "excluded_locations": [],
            "freeform_context": (
                "Accountant with Bachelor's in Accounting from VCU (2019). Based in "
                "Richmond, VA. Looking for tax, audit, financial reporting, or "
                "staff/senior accountant roles. Open to Big 4, regional firms, in-house "
                "corporate accounting, or government/federal accounting positions (IRS, "
                "Treasury, GAO). Targeting roles where I can grow my technical accounting "
                "skills and progress toward CPA / Senior Accountant / Manager track."
            ),
        },
    },
]


async def run_one(p: dict) -> dict:
    name = p["name"]
    resume = p["resume"]
    prefs = p["prefs"]
    print(f"\n{'#' * 70}")
    print(f"# {name.upper()}  —  {resume.name}")
    print(f"{'#' * 70}")

    if not resume.exists():
        print(f"  ERROR: resume not found at {resume}")
        return {"name": name, "status": "ERROR_RESUME_MISSING"}

    t0 = time.time()
    try:
        profile = await build_profile_from_resumes(
            resume_paths=[resume],
            user_preferences=prefs,
        )
    except Exception as e:
        print(f"  ERROR building profile: {type(e).__name__}: {e}")
        return {"name": name, "status": f"ERROR_PROFILE_{type(e).__name__}"}
    build_elapsed = time.time() - t0
    print(f"[{name}] Profile built in {build_elapsed:.1f}s — {len(profile.keywords)} keywords")

    keywords = keywords_to_search_strings(profile, max_tier=2)
    print(f"[{name}] Searching with {len(keywords)} T1+T2 keywords")

    t0 = time.time()
    try:
        scored, summary = await run_search(
            profile=profile,
            keywords=keywords,
            sources=None,
            posted_within_days=30,
            log=True,
        )
    except Exception as e:
        print(f"  ERROR in run_search: {type(e).__name__}: {e}")
        return {"name": name, "status": f"ERROR_SEARCH_{type(e).__name__}"}
    search_elapsed = time.time() - t0
    print(f"[{name}] Full search done in {search_elapsed:.1f}s")

    qualifying = [r for r in scored if (r.final_score or 0) >= 40]

    out = {
        "name": name,
        "status": "OK",
        "duration_seconds": int(build_elapsed + search_elapsed),
        "summary": summary.model_dump(mode="json"),
        "qualifying_count": len(qualifying),
        "tier_strong": summary.tier_strong,
        "tier_good": summary.tier_good,
        "tier_maybe": summary.tier_maybe,
        "tier_stretch": summary.tier_stretch,
        "qualifying_roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in qualifying],
        "per_source_counts": summary.per_source_counts,
    }
    (OUT_DIR / f"{name}_final_validation.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )

    print(f"\n[{name}] Final qualifying: {len(qualifying)}")
    print(f"  STRONG:   {summary.tier_strong}")
    print(f"  GOOD:     {summary.tier_good}")
    print(f"  MAYBE:    {summary.tier_maybe}")
    print(f"  STRETCH:  {summary.tier_stretch}")
    print(f"\n  Per-source counts:")
    for src, h in (summary.per_source_counts or {}).items():
        flag = "ERR" if h.get("errored") else ("OK " if h.get("roles", 0) > 0 else "0  ")
        print(f"    [{flag}] {src:<18} {h.get('roles', 0):>5} roles  {h.get('elapsed_s', 0):>5.1f}s")

    return out


async def main():
    started = time.time()
    print(f"Starting final validation: {len(PROFILES)} profiles sequentially")
    print(f"All 15 scrapers active, per-company cap disabled, source attribution on")
    print()

    results = []
    for p in PROFILES:
        r = await run_one(p)
        results.append(r)

    total_elapsed = time.time() - started
    print(f"\n{'='*70}")
    print(f"ALL DONE — total {total_elapsed/60:.1f} min")
    print(f"{'='*70}")
    print(f"\n{'profile':<10} {'status':<14} {'qual':>4} {'STR':>3} {'GOOD':>4} {'MAYBE':>5} {'STRETCH':>7}")
    print("-" * 60)
    for r in results:
        if r["status"] == "OK":
            print(f"{r['name']:<10} {'OK':<14} {r['qualifying_count']:>4} "
                  f"{r['tier_strong']:>3} {r['tier_good']:>4} {r['tier_maybe']:>5} {r['tier_stretch']:>7}")
        else:
            print(f"{r['name']:<10} {r['status']:<14}")


if __name__ == "__main__":
    asyncio.run(main())

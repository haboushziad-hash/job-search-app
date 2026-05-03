"""Run only the Ziad profile (Phase B with iCIMS active)."""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import build_profile_from_resumes, keywords_to_search_strings
from backend.runner import run_search
from backend.storage import set_audit_folder

ZIAD_RESUME = Path(r"C:\Users\habou\Downloads\Haboush_Ziad_Resume_April 2026.pdf")
OUT_DIR = Path(__file__).resolve().parent / "full_search_output"

set_audit_folder(OUT_DIR)

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


async def main():
    print(f"\n[Ziad] Building profile + keywords...")
    t0 = time.time()
    profile = await build_profile_from_resumes(
        resume_paths=[ZIAD_RESUME],
        user_preferences=ZIAD_PREFS,
    )
    build_elapsed = time.time() - t0
    print(f"[Ziad] Profile built in {build_elapsed:.1f}s — {len(profile.keywords)} keywords")

    keywords = keywords_to_search_strings(profile, max_tier=2)
    print(f"[Ziad] Searching with {len(keywords)} T1+T2 keywords")

    t0 = time.time()
    scored, summary = await run_search(
        profile=profile,
        keywords=keywords,
        sources=None,
        posted_within_days=30,
        log=True,
    )
    search_elapsed = time.time() - t0
    print(f"[Ziad] Full search done in {search_elapsed:.1f}s")

    qualifying = [r for r in scored if (r.final_score or 0) >= 40]
    out = {
        "name": "Ziad",
        "duration_seconds": int(build_elapsed + search_elapsed),
        "summary": summary.model_dump(mode="json"),
        "qualifying_roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in qualifying],
    }
    (OUT_DIR / "Ziad_phase_b_full.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    print(f"\n[Ziad] Final qualifying: {len(qualifying)}")
    print(f"  STRONG:   {summary.tier_strong}")
    print(f"  GOOD:     {summary.tier_good}")
    print(f"  MAYBE:    {summary.tier_maybe}")
    print(f"  STRETCH:  {summary.tier_stretch}")


if __name__ == "__main__":
    asyncio.run(main())

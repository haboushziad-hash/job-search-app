"""Run the full search end-to-end for both Ziad and Zach.

Sequential (not parallel) to avoid sharing Google API quota under load.
Saves scored roles + summary to disk for review. Mirrors all stdout/stderr
to a log file so we don't depend on shell redirection.

Run from project root:
    backend/venv/Scripts/python.exe scripts/run_full_search.py
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


# ---- Tee stdout/stderr to a log file so we have a record even if shell
# redirection eats output (PowerShell's Tee-Object writes UTF-16 by default
# and can swallow lines from a child process). ----
class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


ZIAD_RESUME = Path(r"C:\Users\habou\Downloads\Haboush_Ziad_Resume_April 2026.pdf")
ZACH_RESUME = Path(r"C:\Users\habou\Downloads\Zachary_Charles_Resume.pdf")

OUT_DIR = Path(__file__).resolve().parent / "full_search_output"
OUT_DIR.mkdir(exist_ok=True)


# Same preferences as the keyword test
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


def role_to_dict(role) -> dict:
    """Serialize a Role to a flat dict suitable for JSON / Excel."""
    d = role.model_dump(mode="json", exclude={"embedding"})
    return d


async def run_one(name: str, resume_path: Path, prefs: dict) -> None:
    print(f"\n{'#' * 70}")
    print(f"# {name.upper()}")
    print(f"{'#' * 70}")

    # ---- 1. Build profile ----
    print(f"\n[{name}] Building profile + keywords...")
    t0 = time.time()
    profile = await build_profile_from_resumes(
        resume_paths=[resume_path],
        user_preferences=prefs,
    )
    build_elapsed = time.time() - t0
    print(f"[{name}] Profile built in {build_elapsed:.1f}s — {len(profile.keywords)} keywords")

    # Use T1 + T2 keywords (default search behavior)
    keywords = keywords_to_search_strings(profile, max_tier=2)
    print(f"[{name}] Searching with {len(keywords)} T1+T2 keywords")

    # ---- 2. Run full search ----
    t0 = time.time()
    scored, summary = await run_search(
        profile=profile,
        keywords=keywords,
        sources=None,                 # all active scrapers (Greenhouse / Lever / Ashby / Workday)
        posted_within_days=30,
        log=True,
    )
    search_elapsed = time.time() - t0
    print(f"[{name}] Full search done in {search_elapsed:.1f}s")

    # ---- 3. Persist ----
    qualifying = [r for r in scored if (r.final_score or 0) >= 40]

    out = {
        "name": name,
        "duration_seconds": int(build_elapsed + search_elapsed),
        "build_seconds": int(build_elapsed),
        "search_seconds": int(search_elapsed),
        "profile": {
            "headline": profile.headline,
            "target_seniority": profile.target_seniority,
            "target_functions": profile.target_functions,
            "tier_1": [k.text for k in profile.keywords if k.tier == 1],
            "tier_2": [k.text for k in profile.keywords if k.tier == 2],
            "tier_3": [k.text for k in profile.keywords if k.tier == 3],
        },
        "summary": summary.model_dump(mode="json"),
        "qualifying_roles": [role_to_dict(r) for r in qualifying],
        "all_roles": [role_to_dict(r) for r in scored],
    }
    (OUT_DIR / f"{name}_full.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    # Compact summary for quick scan — applies the same per-company cap (3)
    # the dashboard uses, so the top-25 reflects what users actually see.
    sorted_qual = sorted(qualifying, key=lambda r: -(r.final_score or 0))
    seen_per_company: dict[str, int] = {}
    capped: list = []
    for r in sorted_qual:
        key = (r.company or "").lower()
        n = seen_per_company.get(key, 0)
        if n >= 3:
            continue
        seen_per_company[key] = n + 1
        capped.append(r)
        if len(capped) >= 25:
            break

    compact = {
        "name": name,
        "summary": summary.model_dump(mode="json"),
        "qualifying_count": len(qualifying),
        "top_25_after_company_cap": [
            {
                "score": r.final_score,
                "tier": r.final_tier.value if r.final_tier else None,
                "title": r.job_title,
                "company": r.company,
                "location": r.location,
                "salary": r.salary_text,
                "url": r.job_url,
            }
            for r in capped
        ],
    }
    (OUT_DIR / f"{name}_top25.json").write_text(json.dumps(compact, indent=2, default=str), encoding="utf-8")

    print(f"\n[{name}] Saved:")
    print(f"  Full: {OUT_DIR / (name + '_full.json')}")
    print(f"  Top25: {OUT_DIR / (name + '_top25.json')}")
    print(f"\n[{name}] Top 10:")
    for r in qualifying[:10]:
        tier_str = r.final_tier.value if r.final_tier else "?"
        print(f"  {r.final_score:>5.1f}  {tier_str:<7}  {r.company} — {r.job_title}")


async def main() -> None:
    if not ZIAD_RESUME.exists():
        sys.exit(f"Missing Ziad resume: {ZIAD_RESUME}")
    if not ZACH_RESUME.exists():
        sys.exit(f"Missing Zach resume: {ZACH_RESUME}")

    # Initialize the SQLite archive so the runner writes runs.db + audit
    # files alongside the JSON we already export. This validates the full
    # archive pipeline end-to-end.
    set_audit_folder(OUT_DIR)
    print(f"[archive] using audit folder: {OUT_DIR}")

    overall_start = time.time()

    # Sequential — share Google API quota cleanly.
    # Skip names whose _full.json already exists (resume-after-crash logic).
    if (OUT_DIR / "Ziad_full.json").exists():
        print("[Ziad] _full.json exists — skipping (delete it to re-run).")
    else:
        await run_one("Ziad", ZIAD_RESUME, ZIAD_PREFS)

    if (OUT_DIR / "Zach_full.json").exists():
        print("[Zach] _full.json exists — skipping (delete it to re-run).")
    else:
        await run_one("Zach", ZACH_RESUME, ZACH_PREFS)

    print(f"\n{'#' * 70}")
    print(f"# ALL DONE")
    print(f"# Total elapsed: {(time.time() - overall_start) / 60:.1f} min")
    print(f"# Output: {OUT_DIR}")
    print(f"{'#' * 70}")


if __name__ == "__main__":
    log_path = OUT_DIR / "run.log"
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)
    try:
        asyncio.run(main())
    finally:
        log_file.close()

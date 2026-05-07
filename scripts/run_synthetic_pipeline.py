"""Run the full search pipeline against a CACHED profile from the
profile-fixture suite (tests/profile_fixtures/.cache/).

Use this AFTER `tests/profile_fixtures/run_all.py` has populated the cache,
when you want to validate the END-TO-END behavior — does this profile
produce STRONG matches in the right industries? — without paying the
$0.35 profile-build cost again.

Cost: ~$0.65 per run (scrape + Stage 1/2/3 only; profile is cached).

Usage:
  cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
  backend/venv/Scripts/python.exe scripts/run_synthetic_pipeline.py \\
      --scenario 01_operations_qa_lab

The script:
  1. Loads the cached AFTER profile dict from .cache/<key>.json
     (computes the cache key the same way run_all.py does so it picks
     up the LATEST profile for that scenario)
  2. Reconstructs a CandidateProfile via Pydantic
  3. Calls run_search() directly — same code path as the live app
  4. Prints final tier breakdown + top 5 STRONG roles

Output: audit JSON written to the configured audit folder. Run ID printed
so it can be inspected in the admin dashboard alongside real runs.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid as _uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


SCENARIOS_DIR = PROJECT_ROOT / "tests" / "profile_fixtures" / "scenarios"
CACHE_DIR = PROJECT_ROOT / "tests" / "profile_fixtures" / ".cache"


def _load_yaml(path: Path) -> dict:
    """Load a yaml file via PyYAML if available, else the simple parser
    from run_all.py. Re-imported here for self-containment."""
    try:
        import yaml  # type: ignore
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        sys.path.insert(0, str(PROJECT_ROOT / "tests" / "profile_fixtures"))
        from run_all import _load_yaml_or_simple  # type: ignore
        return _load_yaml_or_simple(path)


def _compute_cache_key(resume_path: Path, user_preferences: dict) -> str:
    """Same logic as tests/profile_fixtures/run_all.py."""
    from backend.profile.builder import (
        PROFILE_BUILDER_PROMPT,
        PROFILE_SYNTHESIS_PROMPT,
        PROFILE_AUDIT_PROMPT,
    )
    h = hashlib.sha256()
    h.update(b"resume::")
    h.update(resume_path.read_bytes())
    h.update(b"\n::prefs::")
    h.update(json.dumps(user_preferences, sort_keys=True).encode("utf-8"))
    h.update(b"\n::builder_prompt::")
    h.update(PROFILE_BUILDER_PROMPT.encode("utf-8"))
    h.update(b"\n::synthesis_prompt::")
    h.update(PROFILE_SYNTHESIS_PROMPT.encode("utf-8"))
    h.update(b"\n::audit_prompt::")
    h.update(PROFILE_AUDIT_PROMPT.encode("utf-8"))
    return h.hexdigest()[:32]


def _find_resume(scenario_dir: Path) -> Path:
    for ext in (".txt", ".pdf", ".docx"):
        p = scenario_dir / f"resume{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"No resume.txt/.pdf/.docx in {scenario_dir}")


async def _run(scenario_name: str, tester_uuid: str) -> int:
    scenario_dir = SCENARIOS_DIR / scenario_name
    if not scenario_dir.is_dir():
        # Try prefix match (e.g., "01" → 01_operations_qa_lab)
        candidates = [d for d in SCENARIOS_DIR.iterdir()
                      if d.is_dir() and d.name.startswith(scenario_name)]
        if len(candidates) == 1:
            scenario_dir = candidates[0]
        else:
            print(f"Scenario not found: {scenario_name}", file=sys.stderr)
            print(f"Available: {sorted(d.name for d in SCENARIOS_DIR.iterdir() if d.is_dir())}",
                  file=sys.stderr)
            return 1

    yaml_path = scenario_dir / "scenario.yaml"
    sc = _load_yaml(yaml_path)
    user_prefs = sc.get("user_preferences") or {}

    resume_path = _find_resume(scenario_dir)
    cache_key = _compute_cache_key(resume_path, user_prefs)
    cache_file = CACHE_DIR / f"{cache_key}.json"
    if not cache_file.exists():
        print(
            f"Cache miss for scenario '{scenario_dir.name}'. Run "
            f"`tests/profile_fixtures/run_all.py --only {scenario_dir.name}` "
            f"first to populate the cache.",
            file=sys.stderr,
        )
        return 2

    profile_dict = json.loads(cache_file.read_text(encoding="utf-8"))

    # Reconstruct CandidateProfile. The cache stores the harness's flat dict
    # (headline / target_functions / target_industries / keywords_tier_*).
    # CandidateProfile expects keywords as Keyword objects with text+tier,
    # and the user_preferences fields (salary_minimum, locations) come from
    # the scenario.yaml not the cache.
    from backend.models import CandidateProfile, Keyword

    keywords: list[Keyword] = []
    for kw in profile_dict.get("keywords_tier_1") or []:
        keywords.append(Keyword(text=str(kw).strip(), tier=1))
    for kw in profile_dict.get("keywords_tier_2") or []:
        keywords.append(Keyword(text=str(kw).strip(), tier=2))
    for kw in profile_dict.get("keywords_tier_3") or []:
        keywords.append(Keyword(text=str(kw).strip(), tier=3))

    profile = CandidateProfile(
        headline=profile_dict.get("headline") or "",
        target_functions=profile_dict.get("target_functions") or [],
        target_industries=profile_dict.get("target_industries") or [],
        keywords=keywords,
        salary_minimum=user_prefs.get("salary_minimum"),
        acceptable_locations=user_prefs.get("acceptable_locations") or [],
    )

    # Use Tier-1 keyword text as search inputs — same as the live app's
    # search-term derivation when no explicit search_terms are present.
    search_keywords = [k.text for k in profile.keywords if k.tier <= 2][:25]

    print(f"\n{'='*70}")
    print(f"SYNTHETIC FULL PIPELINE — {scenario_dir.name}")
    print(f"{'='*70}")
    print(f"Profile: {profile.headline[:80]}")
    print(f"Functions: {profile.target_functions}")
    print(f"Industries: {profile.target_industries}")
    print(f"Keywords: {len(search_keywords)} (T1+T2)")
    print(f"Tester UUID: {tester_uuid}")
    print(f"Cache key:   {cache_key}")
    print()

    from backend.runner import run_search
    from backend import __version__ as cur_version

    def progress(pct: int, stage: str, step_idx: int, detail: str = ""):
        bar_w = 30
        filled = int(bar_w * pct / 100)
        bar = "[" + "#" * filled + "-" * (bar_w - filled) + "]"
        line = f"  {bar} {pct:3d}%  step {step_idx} - {stage}"
        if detail:
            line += f"  ({detail})"
        print(line)

    scored, summary = await run_search(
        profile=profile,
        keywords=search_keywords,
        log=True,
        progress=progress,
    )

    print()
    print(f"{'='*70}")
    print(f"RUN COMPLETE on v{cur_version}")
    print(f"{'='*70}")
    print(f"Duration: {summary.duration_seconds}s")
    print(f"Cost: ${summary.cost_total_usd:.3f}")
    print(f"Scraped: {summary.roles_scraped}")
    print(f"Qualifying: {summary.roles_qualifying}")
    print(f"STRONG/GOOD/MAYBE/STRETCH: "
          f"{summary.tier_strong}/{summary.tier_good}/"
          f"{summary.tier_maybe}/{summary.tier_stretch}")
    print()
    # Top 10 roles
    sorted_roles = sorted(scored or [], key=lambda r: -(r.final_score or 0))
    print(f"TOP 10 by score:")
    for i, r in enumerate(sorted_roles[:10], 1):
        sc = r.final_score or 0
        tier = (r.final_tier.value if hasattr(r.final_tier, "value")
                else str(r.final_tier))
        title = (r.job_title or "")[:55]
        company = (r.company or "")[:25]
        ind = (r.industry or "")[:15]
        print(f"  {i:>2}. [{tier:<7}] {sc:>5.1f}  {company:<25}  [{ind:<15}]  {title}")

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--scenario", required=True,
                   help="Scenario folder name (e.g. 01_operations_qa_lab) "
                        "or unique prefix (e.g. 01)")
    p.add_argument("--uuid", default=None,
                   help="Tester UUID. Generated if omitted.")
    args = p.parse_args()

    # Set UUID before importing backend so config picks it up
    if args.uuid:
        os.environ["TESTER_UUID"] = args.uuid
        tester_uuid = args.uuid
    else:
        tester_uuid = str(_uuid.uuid4())
        os.environ["TESTER_UUID"] = tester_uuid

    try:
        return asyncio.run(_run(args.scenario, tester_uuid))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

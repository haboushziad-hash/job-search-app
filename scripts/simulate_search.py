"""Simulate a search using an existing tester's profile + keywords from a
prior audit JSON. Used to A/B test scoring/calibration changes against a
specific tester's profile WITHOUT needing them to re-run themselves.

Loads:
  - profile_snapshot from the prior audit JSON (no resume files needed —
    we already have the derived profile)
  - keywords_used from the prior audit JSON (so the simulation uses the
    exact same input keyword set as the original run)
  - tester UUID is set via TESTER_UUID env var so the resulting audit
    uploads under the original tester's identity (visible in the admin
    dashboard alongside their real runs)

Output:
  - Writes audit JSON to the configured audit folder
  - Uploads to the Cloudflare Worker so it appears in /v1/admin/runs
  - Prints summary metrics + path to the local audit JSON

Usage:
  cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
  python scripts/simulate_search.py \
      --audit "C:/Users/habou/Downloads/run (mac).json" \
      --uuid "ab46af6a-XXXX-XXXX-XXXX-XXXXXXXXXXXX"

The --uuid arg is the tester's full UUID. Find it by opening the admin
dashboard, filtering to the tester, and inspecting the per-tester
summary card (or copy from the JSON link URL on any of their rows).

If --uuid is omitted, generates a fresh UUID — the simulation still
runs but won't appear under the original tester on the dashboard.

This script bypasses the normal Tauri shell sidecar entry point and
calls the runner directly. It uses the operator's API keys (Gemini/
Anthropic) and JSearch quota; cost is ~$1 per simulation run.

Notes / caveats:
  - Day-to-day scraper variance still applies. JSearch / Adzuna /
    Greenhouse return different roles each day; results aren't
    perfectly comparable to the source audit's role list. But scoring
    behavior on the same profile + keywords IS comparable.
  - The simulation uses CURRENT app version (whatever's in
    backend.__version__), NOT the version of the source audit.
    That's the point — we're testing the new version against the
    old profile.
  - If the tester re-runs themselves later, both the simulation and
    their real run appear under the same UUID. Filter by date or
    keep this in mind when reviewing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid as _uuid
from pathlib import Path
from typing import Any

# Make sure backend package is importable when this script runs from
# the project root (or anywhere — we resolve based on file location).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set TESTER_UUID BEFORE importing backend modules so the config
# initializes with it (some modules read env at import time).
def _maybe_set_tester_uuid(uuid_arg: str | None) -> str:
    if uuid_arg:
        os.environ["TESTER_UUID"] = uuid_arg
        return uuid_arg
    # Generate fresh — simulation will be its own tester on the dashboard
    new_uuid = str(_uuid.uuid4())
    os.environ["TESTER_UUID"] = new_uuid
    return new_uuid


def _load_audit(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Audit JSON not found: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


async def _simulate(audit_path: Path, tester_uuid: str) -> None:
    audit = _load_audit(audit_path)
    profile_dict = audit.get("profile_snapshot") or {}
    keywords_used = audit.get("run_metadata", {}).get("keywords_used") or []

    if not profile_dict:
        raise ValueError("Audit JSON is missing profile_snapshot")
    if not keywords_used:
        raise ValueError("Audit JSON has no keywords_used")

    # Reconstruct the CandidateProfile object. Pydantic v2 will validate
    # all the typed fields. We strip the runtime-only `embedding` field
    # from any keywords (it's not persisted normally and shouldn't round-
    # trip through the model).
    from backend.models import CandidateProfile

    # Defensive: profile_snapshot may have extra fields the model doesn't
    # know about (e.g. older audit fields removed in newer models).
    # Pydantic v2 ignores extras by default in BaseModel.
    profile = CandidateProfile(**profile_dict)

    print(f"\n{'='*70}")
    print(f"SIMULATING SEARCH for existing tester profile")
    print(f"{'='*70}")
    print(f"Source audit:    {audit_path.name}")
    print(f"Source version:  {audit.get('run_metadata', {}).get('app_version', '?')}")
    print(f"Source date:     {audit.get('run_metadata', {}).get('run_date', '?')[:19]}")
    print(f"Tester UUID:     {tester_uuid}")
    print(f"Profile headline: {(profile.headline or '(none)')[:100]}")
    print(f"Years exp:       {profile.years_experience}")
    print(f"Target funcs:    {profile.target_functions}")
    print(f"Acceptable locs: {profile.acceptable_locations}")
    print(f"Salary min:      {profile.salary_minimum}")
    print(f"Keywords (input): {len(keywords_used)} keywords")
    print()

    # Source audit's tier breakdown for comparison
    src_funnel = audit.get("pipeline_funnel", {})
    src_tier = src_funnel.get("tier_breakdown", {})
    src_anom = audit.get("calibration_anomalies", {})
    print(f"Source baseline (for comparison):")
    print(f"  qualifying: {src_funnel.get('qualifying_final')}")
    print(f"  STRONG/GOOD/MAYBE/STRETCH: "
          f"{src_tier.get('STRONG')}/{src_tier.get('GOOD')}/"
          f"{src_tier.get('MAYBE')}/{src_tier.get('STRETCH')}")
    print(f"  calibration anomalies: {src_anom.get('total_anomalies', '(not tracked in this version)')}")
    print(f"  cost: ${audit.get('run_metadata', {}).get('cost_breakdown', {}).get('total_usd', 0):.3f}")
    print()
    print(f"Now running on current version (will print progress)...")
    print()

    # Run the search
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
        keywords=keywords_used,
        log=True,
        progress=progress,
    )

    print()
    print(f"{'='*70}")
    print(f"SIMULATION COMPLETE")
    print(f"{'='*70}")
    print(f"App version run: {cur_version}")
    print(f"Run ID: {summary.run_id}")
    print(f"Duration: {summary.duration_seconds}s")
    print(f"Total cost: ${summary.cost_total_usd:.3f}")
    print()
    print(f"Pipeline funnel:")
    print(f"  Scraped: {summary.roles_scraped}")
    print(f"  After hard filters: {summary.roles_after_filter}")
    print(f"  Qualifying: {summary.roles_qualifying}")
    print(f"  STRONG/GOOD/MAYBE/STRETCH: "
          f"{summary.tier_strong}/{summary.tier_good}/"
          f"{summary.tier_maybe}/{summary.tier_stretch}")
    print()
    print(f"DELTAS vs source audit:")
    src_q = src_funnel.get("qualifying_final") or 0
    print(f"  qualifying:  {src_q} -> {summary.roles_qualifying} "
          f"({summary.roles_qualifying - src_q:+d})")
    for tier_name, src_key, summary_attr in [
        ("STRONG",  "STRONG",  "tier_strong"),
        ("GOOD",    "GOOD",    "tier_good"),
        ("MAYBE",   "MAYBE",   "tier_maybe"),
        ("STRETCH", "STRETCH", "tier_stretch"),
    ]:
        old = src_tier.get(src_key) or 0
        new = getattr(summary, summary_attr) or 0
        print(f"  {tier_name:<8}: {old} -> {new} ({new - old:+d})")
    print()
    print(f"Audit JSON written by archive layer (default location:")
    print(f"  $AUDIT_FOLDER/runs/<run_id>.json), and uploaded to Worker if")
    print(f"  AUDIT_UPLOAD_URL is configured.")
    print(f"  Run ID for grep: {summary.run_id}")
    print()
    print(f"Open the admin dashboard, filter to UUID '{tester_uuid[:8]}', and you")
    print(f"should see this run alongside the original at the top of the table.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--audit", required=True, type=Path,
                   help="Path to the source audit JSON (the tester's prior run)")
    p.add_argument("--uuid", default=None,
                   help="Full tester UUID to attribute this simulation to. "
                        "If omitted, generates a fresh UUID.")
    args = p.parse_args()

    tester_uuid = _maybe_set_tester_uuid(args.uuid)

    try:
        asyncio.run(_simulate(args.audit, tester_uuid))
    except KeyboardInterrupt:
        print("\nSimulation interrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nSimulation FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

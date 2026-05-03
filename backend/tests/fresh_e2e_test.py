r"""Fresh end-to-end production test.

Real scrape → filter → liveness → cascade scoring → tier-bucketed output.
This is the "moment of truth" validation — proves the entire pipeline
works on live data, not historical reference data.

Run from project root:
  backend\venv\Scripts\python.exe -m backend.tests.fresh_e2e_test
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from backend.config import config
from backend.runner import run_search
from backend.tests.validation_run import ziad_profile


async def main() -> int:
    profile = ziad_profile()
    keywords = [
        # Tier 1 — exact targets
        "AI strategy", "AI consultant", "AI enablement", "AI governance",
        "AI advisor", "AI advisory",
        # Tier 2 — adjacent
        "responsible AI", "AI transformation", "AI program manager",
        "Copilot adoption", "Copilot enablement",
        "AI strategist", "AI engagement manager",
    ]

    print("=" * 70)
    print("FRESH END-TO-END PRODUCTION TEST")
    print("=" * 70)
    print(f"Profile: {profile.headline}")
    print(f"Keywords: {keywords}")
    print(f"Sources:  Greenhouse + Lever + Ashby")
    print(f"Posted:   last 60 days")
    print()

    scored, summary = await run_search(
        profile=profile,
        keywords=keywords,
        sources=["Greenhouse", "Lever", "Ashby", "Workday"],
        posted_within_days=60,
        license_key="ziad_dev",
        log=True,
    )

    # Save full results to archive
    out_dir = config.ARCHIVE_DIR / "validation_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"fresh_e2e_{ts}.json"
    payload = {
        "summary": summary.model_dump(mode="json"),
        "roles": [r.model_dump(mode="json", exclude={"embedding"}) for r in scored],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nFull results saved to: {out_path}")

    # ---- Tier breakdown table ----
    qualifying = [r for r in scored if (r.final_score or 0) >= 40]
    qualifying.sort(key=lambda r: r.final_score or 0, reverse=True)

    print(f"\n{'='*70}")
    print(f"TIER BREAKDOWN")
    print(f"{'='*70}")

    for tier_name, tier_min, tier_max in [
        ("STRONG  (85+)", 85, 100),
        ("GOOD    (70-84)", 70, 84),
        ("MAYBE   (55-69)", 55, 69),
        ("STRETCH (40-54)", 40, 54),
    ]:
        in_tier = [r for r in qualifying if tier_min <= (r.final_score or 0) <= tier_max]
        print(f"\n{tier_name}: {len(in_tier)} roles")
        for r in in_tier[:8]:
            title = (r.job_title or "")[:60]
            company = (r.company or "")[:25]
            sal = (r.salary_text or "")[:30]
            loc = r.location_type or "?"
            print(f"  {r.final_score:>3}  [{loc:<6}] {title:<60} @ {company:<25}  {sal}")

    print(f"\n{'='*70}")
    print(f"FINAL SUMMARY")
    print(f"{'='*70}")
    print(f"Cost:                 ${summary.cost_total_usd:.4f}")
    print(f"Duration:             {summary.duration_seconds}s")
    print(f"Total scraped:        {summary.roles_scraped}")
    print(f"After hard filters:   {summary.roles_after_filter}")
    print(f"Qualifying (40+):     {summary.roles_qualifying}")
    print(f"  STRONG:  {summary.tier_strong}")
    print(f"  GOOD:    {summary.tier_good}")
    print(f"  MAYBE:   {summary.tier_maybe}")
    print(f"  STRETCH: {summary.tier_stretch}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

r"""Dry-run pipeline test — scrape → enrich → filter → liveness, NO scoring.

Validates the data pipeline without spending API tokens. Useful while Gemini
Pro daily quota is exhausted.

Run from project root:
  backend\venv\Scripts\python.exe -m backend.tests.pipeline_dry_run
"""
from __future__ import annotations

import asyncio
import sys

from backend.filter.hard_filters import apply_hard_filters
from backend.filter.salary_extractor import enrich_salaries
from backend.scraper.liveness import verify_liveness
from backend.scraper.orchestrator import scrape_all
from backend.tests.validation_run import ziad_profile


async def main() -> int:
    profile = ziad_profile()
    keywords = [
        "AI strategy", "AI consultant", "AI enablement", "AI governance",
        "AI advisor", "responsible AI", "AI transformation",
        "Copilot adoption", "Copilot enablement",
    ]

    print(f"Profile: {profile.headline}")
    print(f"Keywords: {keywords}\n")

    # 1. Scrape
    raw = await scrape_all(
        keywords=keywords,
        posted_within_days=60,
        log=True,
    )

    # 2. Salary enrichment
    print()
    enrich_salaries(raw, log=True)

    # 3. Hard filters
    print()
    filtered = apply_hard_filters(
        raw,
        profile=profile,
        max_age_days=60,
        log=True,
    )

    # 4. Liveness — just sample 50 for speed (full liveness on 1000s would take a while)
    print()
    sample = filtered[:50]
    print(f"[liveness] sampling {len(sample)} roles for liveness check...")
    alive = await verify_liveness(sample, drop_dead=True, log=True)

    # Summary
    print(f"\n{'='*70}")
    print(f"PIPELINE SUMMARY (no scoring — dry run)")
    print(f"{'='*70}")
    print(f"Scraped:                   {len(raw)}")
    print(f"After hard filters:        {len(filtered)}")
    print(f"  Salary coverage:         {sum(1 for r in raw if r.salary_max is not None)}/{len(raw)} ({100*sum(1 for r in raw if r.salary_max is not None)/max(1,len(raw)):.0f}%)")
    print(f"  Location coverage:       {sum(1 for r in raw if r.location_type)}/{len(raw)} ({100*sum(1 for r in raw if r.location_type)/max(1,len(raw)):.0f}%)")
    print(f"  JD coverage:             {sum(1 for r in raw if r.job_description_full)}/{len(raw)} ({100*sum(1 for r in raw if r.job_description_full)/max(1,len(raw)):.0f}%)")
    print(f"After liveness (sample):   {len(alive)}/{len(sample)}")

    print(f"\nTop 15 surviving roles by freshness_score:")
    sorted_alive = sorted(alive, key=lambda r: r.freshness_score or 0, reverse=True)[:15]
    for r in sorted_alive:
        title = (r.job_title or "")[:55]
        company = (r.company or "")[:25]
        fresh = r.freshness_score or 0
        print(f"  fresh={fresh:>3}  [{r.primary_source:<10}]  {title:<55}  @ {company}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

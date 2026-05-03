r"""End-to-end validation — score N reference roles from job_market.db
against Ziad's profile via the new Gemini cascade. Reports cost + tier
distribution + a comparison summary.

Run from project root:
  backend\venv\Scripts\python.exe -m backend.tests.validation_run --n 100

Cheap dry run (10 roles to verify plumbing without burning budget):
  backend\venv\Scripts\python.exe -m backend.tests.validation_run --n 10
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from collections import Counter
from typing import Optional

from backend.config import config
from backend.models import CandidateProfile, ResumeMetadata, Role
from backend.scoring.orchestrator import score_roles


# ----------------------------------------------------------------------------
# Reference candidate profile (Ziad — placeholder, easily swappable)
# ----------------------------------------------------------------------------

def ziad_profile() -> CandidateProfile:
    return CandidateProfile(
        name="Ziad Pierre Haboush",
        headline=(
            "Senior Consultant at Booz Allen Hamilton with 7+ years in management "
            "consulting, currently pursuing Georgetown MPS in AI Management. "
            "Targeting AI strategy, AI governance, AI enablement, and AI advisory roles."
        ),
        target_functions=[
            "AI strategy",
            "AI governance",
            "AI enablement and adoption",
            "AI advisory consulting",
            "AI program management",
            "Forward deployed strategist",
            "Engagement management",
            "Responsible AI",
        ],
        target_industries=[
            "Federal consulting",
            "Big 4 consulting",
            "AI-native firms",
            "Enterprise SaaS",
            "Financial services",
        ],
        target_seniority="Senior IC / Manager",
        years_experience=7,
        technical_skills=[
            "Strategic planning",
            "Program management",
            "Stakeholder management",
            "AI literacy",
            "Generative AI use cases",
            "Microsoft Copilot",
            "Process automation",
            "Change management",
            "Workshop facilitation",
            "Technical writing",
        ],
        domain_expertise=[
            "Federal consulting",
            "AI readiness assessment",
            "AI adoption frameworks",
            "Workforce transformation",
            "Responsible AI implementation",
        ],
        soft_skills=[
            "Client-facing communication",
            "Cross-functional leadership",
            "Strategic thinking",
        ],
        salary_minimum=130_000,
        # Include "on-site" for DC-area roles — many consulting roles tagged on-site
        # are actually hybrid in practice, and we want to surface DC opportunities.
        work_arrangements=["remote", "hybrid", "on-site"],
        acceptable_locations=[
            "Washington", "Washington DC", "Washington, DC", "DC",
            "Virginia", "VA", "McLean", "Arlington", "Reston", "Tysons",
            "Maryland", "MD", "Bethesda", "Rockville", "Silver Spring",
            "Remote",
        ],
        excluded_locations=[
            "India", "Bangalore", "Hyderabad", "Mumbai", "Delhi", "Chennai", "Pune",
            "London", "Dublin", "Singapore", "Tokyo", "Sydney",
        ],
        resumes=[
            ResumeMetadata(
                filename="resume_ai_strategy.pdf",
                emphasis="AI strategy + governance",
                headline="Strategy-forward framing for advisory roles",
            ),
            ResumeMetadata(
                filename="resume_consulting.pdf",
                emphasis="Federal consulting + program management",
                headline="Booz Allen consulting framing",
            ),
        ],
    )


# ----------------------------------------------------------------------------
# Reference role loader
# ----------------------------------------------------------------------------

def load_reference_roles(n: int, *, only_with_jd: bool = True) -> list[Role]:
    """Load N roles from the historical job_market.db, preferring ones with full JDs."""
    if not config.REFERENCE_DB.exists():
        raise FileNotFoundError(
            f"Reference DB not found at {config.REFERENCE_DB}. "
            "Place job_market.db at archive/reference_data/job_market.db"
        )
    conn = sqlite3.connect(str(config.REFERENCE_DB))
    conn.row_factory = sqlite3.Row
    where = "WHERE LENGTH(COALESCE(job_description_full, '')) > 200" if only_with_jd else ""
    rows = conn.execute(f"""
        SELECT * FROM roles
        {where}
        ORDER BY RANDOM()
        LIMIT ?
    """, (n,)).fetchall()
    conn.close()

    roles: list[Role] = []
    for r in rows:
        d = dict(r)
        # Pydantic doesn't coerce 0/1 to bool in some cases — normalize
        for bool_field in ("management_required", "consulting_experience_req"):
            v = d.get(bool_field)
            if v is not None:
                d[bool_field] = bool(v)
        try:
            roles.append(Role(**d))
        except Exception as e:
            print(f"[loader] skipping malformed row {d.get('job_id')}: {e}")
    return roles


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def print_top_roles(scored: list[Role], n: int = 10) -> None:
    qualifying = sorted(
        [r for r in scored if (r.final_score or 0) >= 55],
        key=lambda r: r.final_score or 0,
        reverse=True,
    )
    print(f"\nTop {min(n, len(qualifying))} qualifying roles (score >= 55):")
    for r in qualifying[:n]:
        print(f"  {r.final_score:>3}  [{r.final_tier.value if r.final_tier else '?':<7}]  "
              f"{r.job_title[:60]:<60}  @ {r.company[:30]:<30}")
        if r.stage3_analysis and not r.stage3_analysis.startswith("stage3_error"):
            print(f"        ↳ {r.stage3_analysis[:140]}")


def print_score_histogram(scored: list[Role]) -> None:
    print("\nScore distribution (final_score):")
    buckets = Counter()
    for r in scored:
        s = r.final_score or 0
        bucket = f"{(s // 10) * 10:>3}-{(s // 10) * 10 + 9}"
        buckets[bucket] += 1
    for k in sorted(buckets):
        bar = "#" * buckets[k]
        print(f"  {k}  {buckets[k]:>3}  {bar}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

async def main(n: int) -> int:
    errors = config.validate()
    if errors:
        print("Config errors:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"Loading {n} reference roles from job_market.db...")
    roles = load_reference_roles(n, only_with_jd=True)
    print(f"Loaded {len(roles)} roles with JDs.")

    profile = ziad_profile()
    print(f"\nProfile: {profile.headline}")

    print("\n" + "=" * 70)
    print("RUNNING GEMINI CASCADE")
    print("=" * 70)
    scored, summary = await score_roles(profile=profile, roles=roles)

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"Roles scraped (input):      {summary.roles_scraped}")
    print(f"After embedding pre-filter: {summary.roles_after_filter}")
    print(f"After Stage 1 pre-filter:   {summary.roles_after_stage1}")
    print(f"Scored by Stage 2:          {summary.roles_after_stage2}")
    print(f"Final qualifying (>=40):    {summary.roles_qualifying}")
    print(f"  STRONG (85+):  {summary.tier_strong}")
    print(f"  GOOD   (70+):  {summary.tier_good}")
    print(f"  MAYBE  (55+):  {summary.tier_maybe}")
    print(f"  STRETCH(40+):  {summary.tier_stretch}")
    print()
    print(f"Duration:                   {summary.duration_seconds}s")
    print(f"Total cost:                 ${summary.cost_total_usd:.4f}")
    print(f"  Embeddings:               ${summary.cost_embeddings_usd:.4f}")
    print(f"  Stage 1 (Flash):          ${summary.cost_stage1_usd:.4f}")
    print(f"  Stage 2 (Pro):            ${summary.cost_stage2_usd:.4f}")
    print(f"  Stage 3 (Pro):            ${summary.cost_stage3_usd:.4f}")
    print()
    print(f"Cost per qualifying role:   "
          f"${summary.cost_total_usd / max(1, summary.roles_qualifying):.4f}")
    print(f"Projected cost @ 1500 input roles: "
          f"${summary.cost_total_usd * 1500 / max(1, summary.roles_scraped):.4f}")

    print_score_histogram(scored)
    print_top_roles(scored, n=15)

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=100, help="Number of roles to score")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.n)))

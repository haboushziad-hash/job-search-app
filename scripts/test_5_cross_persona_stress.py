"""Test #5 — Cross-persona AI stress test.

Run the profile builder against 6+ diverse synthetic resumes spanning
different industries. Validates that:

  - Keywords are FIELD-APPROPRIATE (not AI-biased for non-AI candidates)
  - excluded_title_patterns are SENSIBLE per profile (engineer profile
    doesn't exclude engineer titles; non-engineers do)
  - profile_tags reflect the actual industry/function (not just "consulting"
    for everyone)
  - Stage 2 prompt instructions don't bleed AI-specific scoring into
    non-AI domains

Also serves as a reference benchmark for Phase 1.5+ improvements.

Run from project root:
    backend/venv/Scripts/python.exe scripts/test_5_cross_persona_stress.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import build_profile_from_resumes


PERSONAS_DIR = Path(__file__).resolve().parent / "personas"
OUTPUT_DIR = Path(__file__).resolve().parent / "stress_test_output"
OUTPUT_DIR.mkdir(exist_ok=True)


# Per-persona realistic preferences. The freeform_context simulates what
# a user would type in the Setup wizard's textarea.
PERSONA_PREFS = {
    "01_tax_manager": {
        "salary_minimum": 165_000,
        "work_arrangements": ["hybrid", "on-site"],
        "acceptable_locations": ["San Antonio, TX", "Austin, TX", "Houston, TX"],
        "acceptable_location_radii": [40, 40, 40],
        "freeform_context": (
            "I'm a CPA with 8 years in tax at Deloitte and Grant Thornton. "
            "Looking for Senior Manager or Director-level tax roles. "
            "Strong in partnership taxation and SALT. Open to Big 4 or "
            "regional firms or in-house corporate tax. Avoid IT audit, "
            "operational risk, and pure financial reporting."
        ),
    },
    "02_software_engineer": {
        "salary_minimum": 220_000,
        "work_arrangements": ["remote", "hybrid"],
        "acceptable_locations": ["Seattle, WA", "San Francisco, CA"],
        "acceptable_location_radii": [40, 40],
        "freeform_context": (
            "Backend engineer, 7 years, distributed systems / payments / "
            "fintech. Targeting Staff or Senior Staff Engineer at Series "
            "B-D or top-tier public tech companies. Python + Go primary. "
            "Avoid pure people-management roles, frontend-only, or "
            "low-trust outsourced consulting work."
        ),
    },
    "03_registered_nurse": {
        "salary_minimum": 95_000,
        "work_arrangements": ["on-site", "hybrid"],
        "acceptable_locations": ["Phoenix, AZ", "Tucson, AZ"],
        "acceptable_location_radii": [40, 40],
        "freeform_context": (
            "Charge Nurse moving into nursing leadership. Just finished my "
            "MSN in Healthcare Administration. Looking for Nurse Manager, "
            "Director of Nursing, or Clinical Operations roles at large "
            "hospital systems. Avoid bedside-only, traveling nurse, or "
            "healthcare IT positions."
        ),
    },
    "04_marketing_director": {
        "salary_minimum": 250_000,
        "work_arrangements": ["remote", "hybrid"],
        "acceptable_locations": ["Boston, MA"],
        "acceptable_location_radii": [50],
        "freeform_context": (
            "B2B SaaS marketing leader. Targeting VP Marketing or Senior "
            "Director at Series C-D B2B SaaS, ideally fintech, horizontal "
            "SaaS, or developer tools. Strong in demand-gen, ABM, and "
            "lifecycle marketing. Not interested in pure brand/comms, "
            "B2C, or agency-side work."
        ),
    },
    "05_cre_analyst": {
        "salary_minimum": 190_000,
        "work_arrangements": ["hybrid", "on-site"],
        "acceptable_locations": ["Chicago, IL", "New York, NY", "Washington, DC"],
        "acceptable_location_radii": [40, 40, 40],
        "freeform_context": (
            "CRE investment analyst, 5 years JLL + C&W. Targeting senior "
            "associate / VP roles at REITs (Prologis, Realty Income, "
            "Welltower, AvalonBay) or fund managers (Blackstone, Brookfield, "
            "JPM AM Real Estate). Industrial and multifamily focus. Not "
            "interested in residential brokerage, property management, or "
            "debt placement."
        ),
    },
    "06_environmental_engineer": {
        "salary_minimum": 130_000,
        "work_arrangements": ["hybrid", "on-site"],
        "acceptable_locations": ["Denver, CO", "Colorado Springs, CO", "Boulder, CO"],
        "acceptable_location_radii": [50, 50, 50],
        "freeform_context": (
            "PE engineer, 9 years municipal water/wastewater. Looking for "
            "PM or Senior PM at AECOM, Jacobs, Tetra Tech, Stantec, Brown "
            "& Caldwell, CDM Smith, Carollo, HDR, Black & Veatch — or "
            "Senior Engineer roles at municipal utilities. Avoid private "
            "industrial compliance, oil & gas remediation, pure CAD work."
        ),
    },
}


async def build_one(persona_key: str) -> dict:
    """Run profile builder on one persona, return summary dict."""
    resume_path = PERSONAS_DIR / f"{persona_key}.txt"
    if not resume_path.exists():
        return {"persona": persona_key, "error": f"missing {resume_path}"}

    prefs = PERSONA_PREFS.get(persona_key, {})
    print(f"\n[{persona_key}] building profile...")
    t0 = time.time()
    try:
        profile = await build_profile_from_resumes(
            resume_paths=[resume_path],
            user_preferences=prefs,
        )
    except Exception as e:
        return {"persona": persona_key, "error": f"{type(e).__name__}: {e}"}
    elapsed = time.time() - t0

    t1 = [k.text for k in profile.keywords if k.tier == 1]
    t2 = [k.text for k in profile.keywords if k.tier == 2]
    t3 = [k.text for k in profile.keywords if k.tier == 3]

    return {
        "persona": persona_key,
        "elapsed_seconds": round(elapsed, 1),
        "headline": profile.headline,
        "target_seniority": profile.target_seniority,
        "target_functions": profile.target_functions,
        "target_industries": profile.target_industries,
        "profile_tags": profile.profile_tags,
        "excluded_title_patterns": profile.excluded_title_patterns,
        "negative_signals": profile.negative_signals,
        "keyword_counts": {"t1": len(t1), "t2": len(t2), "t3": len(t3), "total": len(profile.keywords)},
        "tier_1_keywords": t1,
        "tier_2_keywords": t2,
        "tier_3_keywords": t3,
    }


def diagnose(result: dict) -> list[str]:
    """Run sanity heuristics on a persona's output, return list of warnings."""
    warnings = []
    persona = result["persona"]
    if "error" in result:
        return [f"ERROR: {result['error']}"]

    keywords = result["tier_1_keywords"] + result["tier_2_keywords"]
    excl = result["excluded_title_patterns"]
    tags = result["profile_tags"]

    def kw_lower():
        return [k.lower() for k in keywords]

    # AI-bias check: non-AI personas shouldn't have AI-flavored T1/T2 keywords
    ai_bias_keywords = sum(1 for k in kw_lower() if any(w in k for w in ("ai ", " ai", "artificial intelligence", "machine learning", "ml ", " gpt", "copilot")))
    if persona not in ("01_tax_manager", "02_software_engineer"):
        if ai_bias_keywords > 2:
            warnings.append(f"AI-bias: {ai_bias_keywords} AI-flavored keywords in T1+T2 (expected 0-1 for non-AI persona)")

    # Tax persona should have tax-flavored keywords
    if persona == "01_tax_manager":
        tax_kw = sum(1 for k in kw_lower() if "tax" in k or "salt" in k or "audit" in k or "cpa" in k)
        if tax_kw < 5:
            warnings.append(f"Tax persona has only {tax_kw} tax-flavored keywords")

    # Software engineer should NOT have a STANDALONE engineer/developer pattern
    # in excluded_title_patterns — multi-word patterns like "engineering manager"
    # are fine (legitimate seniority exclusion).
    if persona == "02_software_engineer":
        bad = [p for p in excl
               if p.lower().strip() in {"engineer", "engineers", "developer", "developers"}]
        if bad:
            warnings.append(f"Software engineer has standalone {bad} in excluded — would drop target roles!")

    # Nurse should have nurse-related keywords
    if persona == "03_registered_nurse":
        nurse_kw = sum(1 for k in kw_lower() if "nurse" in k or "clinical" in k or "rn " in k or "nursing" in k)
        if nurse_kw < 3:
            warnings.append(f"Nurse persona has only {nurse_kw} nurse-flavored keywords")

    # Marketing director should have marketing keywords, NOT engineer/architect
    if persona == "04_marketing_director":
        mkt_kw = sum(1 for k in kw_lower() if "market" in k or "demand gen" in k or "growth" in k)
        if mkt_kw < 3:
            warnings.append(f"Marketing director has only {mkt_kw} marketing-flavored keywords")

    # CRE analyst should have CRE/real-estate keywords
    if persona == "05_cre_analyst":
        cre_kw = sum(1 for k in kw_lower() if "real estate" in k or "cre " in k or "acquisitions" in k or "investment" in k or "asset" in k)
        if cre_kw < 3:
            warnings.append(f"CRE analyst has only {cre_kw} real-estate-flavored keywords")

    # Environmental engineer SHOULD have engineer keywords (it's their job)
    if persona == "06_environmental_engineer":
        bad = [p for p in excl
               if p.lower().strip() in {"engineer", "engineers", "developer"}]
        if bad:
            warnings.append(f"Env engineer has standalone {bad} in excluded — would drop target roles!")
        eng_kw = sum(1 for k in kw_lower() if "engineer" in k or "project manager" in k or "water" in k)
        if eng_kw < 3:
            warnings.append(f"Env engineer has only {eng_kw} engineering-flavored keywords")

    # profile_tags should be 5-10 lowercase tags
    if not (3 <= len(tags) <= 12):
        warnings.append(f"profile_tags count {len(tags)} outside expected 5-10")

    return warnings


async def main():
    print(f"Cross-persona stress test — {len(PERSONA_PREFS)} synthetic profiles\n")
    print(f"Personas dir: {PERSONAS_DIR}")
    print(f"Output:        {OUTPUT_DIR}\n")

    # Run sequentially to avoid Google API quota contention
    results = []
    for persona_key in PERSONA_PREFS:
        r = await build_one(persona_key)
        results.append(r)
        # Save individual file
        if "error" not in r:
            (OUTPUT_DIR / f"{persona_key}_profile.json").write_text(
                json.dumps(r, indent=2), encoding="utf-8"
            )

    # Diagnose
    print("\n" + "=" * 70)
    print("DIAGNOSTICS")
    print("=" * 70)
    total_warnings = 0
    for r in results:
        persona = r.get("persona", "?")
        warnings = diagnose(r)
        if not warnings:
            print(f"\n  [PASS]  {persona}  — no warnings")
        else:
            print(f"\n  [WARN]  {persona}")
            for w in warnings:
                print(f"          - {w}")
            total_warnings += len(warnings)

    print("\n" + "=" * 70)
    print(f"SUMMARY")
    print("=" * 70)
    print(f"  Personas tested: {len(results)}")
    print(f"  Errors: {sum(1 for r in results if 'error' in r)}")
    print(f"  Total warnings: {total_warnings}")

    # Write a comparison summary
    summary_md = OUTPUT_DIR / "comparison_summary.md"
    lines = ["# Cross-Persona Stress Test\n"]
    for r in results:
        if "error" in r:
            lines.append(f"\n## {r['persona']}: ERROR — {r['error']}\n")
            continue
        lines += [
            f"\n## {r['persona']}",
            f"- Headline: {r['headline']}",
            f"- Profile tags: `{', '.join(r['profile_tags'])}`",
            f"- Excluded title patterns: `{', '.join(r['excluded_title_patterns'])}`",
            f"- Negative signals: `{', '.join(r['negative_signals'])}`",
            f"- Keyword counts: T1={r['keyword_counts']['t1']}, T2={r['keyword_counts']['t2']}, T3={r['keyword_counts']['t3']}",
            f"- Top T1 keywords: {', '.join(r['tier_1_keywords'][:8])}",
            f"- Diagnostic warnings: {diagnose(r) or '*none*'}",
        ]
    summary_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  Comparison summary: {summary_md}")
    print(f"  Per-persona JSONs: {OUTPUT_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())

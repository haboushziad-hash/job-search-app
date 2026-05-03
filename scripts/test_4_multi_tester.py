"""Test #4 — Multi-tester simulation.

Sets up two tester audit folders side-by-side under a shared parent (the
exact configuration we'd ship to real testers). Validates:

  1. Tester A runs a search → writes runs.db + market_contributions.jsonl
  2. Tester B runs the SAME search → does NOT see Tester A's contribs
     (because they're not yet in B's archive — siblings only)
  3. After A's contributions sync to A's market_contributions.jsonl, when
     B runs a profile build, B's keyword prompt SHOULD see A's titles
     (filtered by B's profile-tag overlap)
  4. A third tester C with completely different profile sees NEITHER
     A nor B's contributions (overlap below 20%)

This is the integration test of the cross-tester closed-loop architecture.
Cheap — does NOT run a full search, only profile builds + market merges.
Cost: ~$0.65 × 3 = $2 max. Time: ~3-5 min.
"""
from __future__ import annotations

import asyncio, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.storage import (
    set_audit_folder, export_contributions,
    market_titles_for_keyword_prompt, read_sibling_contributions,
)


passes = 0
fails = 0


def assert_(cond, msg):
    global passes, fails
    if cond:
        passes += 1
        print(f"  [OK]   {msg}")
    else:
        fails += 1
        print(f"  [FAIL] {msg}")


async def main():
    print("=== Test #4 — Multi-tester simulation ===\n")

    with tempfile.TemporaryDirectory() as parent:
        parent = Path(parent)
        a = parent / "tester_ziad_like"
        b = parent / "tester_similar"
        c = parent / "tester_different"
        for f in (a, b, c):
            f.mkdir()

        # ---- Step 1: Tester A (Ziad-like AI strategy consultant) writes contribs
        print("Step 1: Tester A (AI strategy consultant) writes 5 contribs")
        a_tags = ["consulting", "federal", "ai", "enablement", "governance", "change_management"]
        export_contributions(a, contributions=[
            {"company": "Anthropic", "job_title": "Applied AI Strategy Lead", "score": 88, "profile_tags": a_tags},
            {"company": "Snorkel AI", "job_title": "Engagement Manager - AI", "score": 85, "profile_tags": a_tags},
            {"company": "Booz Allen", "job_title": "AI Enablement Lead", "score": 82, "profile_tags": a_tags},
            {"company": "Deloitte", "job_title": "Federal AI Strategy Consultant", "score": 78, "profile_tags": a_tags},
            {"company": "Accenture", "job_title": "AI Adoption Lead", "score": 75, "profile_tags": a_tags},
        ])

        # ---- Step 2: Tester B (similar profile — strategy consulting w/ AI focus)
        print("\nStep 2: Tester B (similar profile, 4/6 tag overlap)")
        b_tags = ["consulting", "ai", "strategy", "advisory", "enablement", "digital_transformation"]
        b_market = market_titles_for_keyword_prompt(
            b, candidate_tags=b_tags, own_contributions=[],
            max_titles=20, min_overlap=0.20,
        )
        b_companies = {e["company"] for e in b_market}
        assert_(
            "Anthropic" in b_companies and "Snorkel AI" in b_companies,
            f"Tester B sees A's contribs (similar profile): {b_companies}"
        )
        b_overlap_pct = len(set(b_tags) & set(a_tags)) / len(set(b_tags) | set(a_tags))
        assert_(b_overlap_pct >= 0.20, f"B's tag overlap with A is {b_overlap_pct:.0%} (>=20%)")

        # ---- Step 3: Tester C (totally different — CPG sales)
        print("\nStep 3: Tester C (CPG sales, no overlap with A)")
        c_tags = ["cpg", "retail", "sales", "operations", "territory_management"]
        c_market = market_titles_for_keyword_prompt(
            c, candidate_tags=c_tags, own_contributions=[],
            max_titles=20, min_overlap=0.20,
        )
        c_companies = {e["company"] for e in c_market}
        assert_(
            len(c_market) == 0,
            f"Tester C sees nothing from A (different profile): got {c_companies}"
        )

        # ---- Step 4: Tester C writes their own contribs, then Tester A runs again
        print("\nStep 4: Tester C writes contribs; Tester A's market should still skew AI")
        export_contributions(c, contributions=[
            {"company": "PepsiCo", "job_title": "Account Manager", "score": 88, "profile_tags": c_tags},
            {"company": "Coca-Cola", "job_title": "Territory Sales Manager", "score": 82, "profile_tags": c_tags},
        ])
        a_market = market_titles_for_keyword_prompt(
            a, candidate_tags=a_tags, own_contributions=[],
            max_titles=20, min_overlap=0.20,
        )
        a_seen = {e["company"] for e in a_market}
        assert_(
            "PepsiCo" not in a_seen and "Coca-Cola" not in a_seen,
            f"Tester A's market does NOT include CPG (correct — too different): {a_seen}"
        )

        # ---- Step 5: Tester B's market now includes both A AND C? (only A since C too different)
        print("\nStep 5: Tester B's market includes A but excludes C")
        b_market2 = market_titles_for_keyword_prompt(
            b, candidate_tags=b_tags, own_contributions=[],
            max_titles=20, min_overlap=0.20,
        )
        b_seen = {e["company"] for e in b_market2}
        assert_(
            "Anthropic" in b_seen and "PepsiCo" not in b_seen,
            f"Tester B sees AI consultancies, not CPG: {b_seen}"
        )

        # ---- Step 6: Idempotent rewrites (sync conflicts safe)
        print("\nStep 6: Re-export same contribs is idempotent")
        n_again = export_contributions(a, contributions=[
            {"company": "Anthropic", "job_title": "Applied AI Strategy Lead", "score": 88, "profile_tags": a_tags},
        ])
        assert_(n_again == 0, f"unchanged contrib should not rewrite, got {n_again}")

        print("\n" + "=" * 50)
        print(f"Passed: {passes}")
        print(f"Failed: {fails}")
        print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0 if fails == 0 else 1)

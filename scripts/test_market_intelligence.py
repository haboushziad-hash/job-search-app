"""Verify the cross-tester market intelligence flow end-to-end:

1. Two tester folders side-by-side (simulating shared cloud-drive setup)
2. Tester A (Ziad-like) writes contributions
3. Tester B (similar profile) reads sibling contributions, gets relevant titles
4. Tester C (different profile, low overlap) reads sibling contributions, gets nothing

Confirms the 20% Jaccard overlap filter actually segments correctly.
"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.storage import (
    set_audit_folder, get_archive,
    export_contributions, market_titles_for_keyword_prompt,
)


with tempfile.TemporaryDirectory() as parent:
    parent = Path(parent)

    # ---- Tester A: Ziad-like profile, writes 3 high-score contributions ----
    folder_a = parent / "ziad"
    folder_a.mkdir()
    a_contribs = [
        {"company": "Anthropic", "job_title": "Applied AI Strategy Lead", "score": 88,
         "profile_tags": ["consulting", "ai", "enablement", "federal", "governance"]},
        {"company": "Snorkel AI", "job_title": "Engagement Manager - AI", "score": 85,
         "profile_tags": ["consulting", "ai", "enablement", "federal", "governance"]},
        {"company": "Komodo Health", "job_title": "AI Enablement Lead", "score": 80,
         "profile_tags": ["consulting", "ai", "enablement", "federal", "governance"]},
    ]
    n = export_contributions(folder_a, contributions=a_contribs)
    print(f"Tester A wrote {n} contributions to {folder_a}")

    # ---- Tester B: similar profile (3 of 5 tags overlap = 60% Jaccard) ----
    folder_b = parent / "tester_b"
    folder_b.mkdir()
    arc_b = set_audit_folder(folder_b)
    b_tags = ["consulting", "ai", "strategy", "advisory", "enablement"]  # 3 overlap (consulting/ai/enablement)
    b_market = market_titles_for_keyword_prompt(
        folder_b,
        candidate_tags=b_tags,
        own_contributions=[],
        max_titles=10,
        min_overlap=0.20,
    )
    print(f"\nTester B (overlap {len(set(b_tags) & {'consulting','ai','enablement','federal','governance'})}/5 tags = 38% Jaccard):")
    for e in b_market:
        print(f"  [{e.get('score')}] {e.get('job_title')} — {e.get('company')}  (overlap {e.get('_overlap', 0):.0%})")
    assert len(b_market) == 3, f"expected 3 contribs visible to B, got {len(b_market)}"
    arc_b.close()

    # ---- Tester C: completely different profile (new grad CPG sales) ----
    folder_c = parent / "tester_c"
    folder_c.mkdir()
    arc_c = set_audit_folder(folder_c)
    c_tags = ["cpg", "retail", "operations", "sales", "territory_management"]  # 0 overlap
    c_market = market_titles_for_keyword_prompt(
        folder_c,
        candidate_tags=c_tags,
        own_contributions=[],
        max_titles=10,
        min_overlap=0.20,
    )
    print(f"\nTester C (no overlap with Ziad's tags):")
    print(f"  {len(c_market)} contribs visible (expect 0 — overlap too low)")
    assert len(c_market) == 0, f"expected 0 contribs visible to C, got {len(c_market)}"
    arc_c.close()

    print("\n=== MARKET INTELLIGENCE TESTS PASS ===")
    print("Confirmed:")
    print("  - Sibling folders correctly read each other's contributions")
    print("  - 20% Jaccard overlap filter correctly segments compatible vs incompatible profiles")
    print("  - Tester B (similar tags) sees Ziad's contributions")
    print("  - Tester C (different tags) correctly sees nothing")

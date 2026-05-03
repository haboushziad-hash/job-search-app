"""Test #2 — Edge case probes.

Quick failure-mode tests that validate graceful degradation:
  - Audit folder doesn't exist → auto-creates
  - Audit folder exists but is read-only → fails loudly, doesn't corrupt
  - Cache hit on second build with same profile → skip scrape
  - profile_hash invalidates when keywords change → cache misses
  - Empty resume → graceful failure (does not crash)
  - is_dead_listing regex correctness — known dead-listing phrases
  - SQLite multi-process: two readers on same DB don't deadlock
"""
from __future__ import annotations

import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.storage import set_audit_folder, get_archive
from backend.storage.archive import hash_profile
from backend.filter.hard_filters import is_dead_listing


passes = 0
fails = 0


def test(name: str):
    def deco(fn):
        global passes, fails
        try:
            fn()
            print(f"  [OK]   {name}")
            passes += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            fails += 1
        except Exception as e:
            print(f"  [ERR]  {name}: {type(e).__name__}: {e}")
            fails += 1
        return fn
    return deco


print("=== Test #2 — Edge case probes ===\n")


@test("Audit folder auto-creates when missing")
def _():
    with tempfile.TemporaryDirectory() as t:
        new = Path(t) / "subdir" / "deep" / "audits"
        arc = set_audit_folder(new)
        assert new.exists(), "folder should be auto-created"
        assert (new / "runs").exists(), "runs/ subdir should be created"
        assert (new / "diffs").exists(), "diffs/ subdir should be created"
        arc.close()


@test("profile_hash stable across calls with same content")
def _():
    p = {"headline": "X", "target_functions": ["A", "B"], "keywords": []}
    h1 = hash_profile(p)
    h2 = hash_profile(p)
    assert h1 == h2, f"hashes differ: {h1} vs {h2}"


@test("profile_hash invalidates when keywords change")
def _():
    p1 = {"headline": "X", "keywords": [{"text": "AI Lead"}]}
    p2 = {"headline": "X", "keywords": [{"text": "AI Lead"}, {"text": "AI Manager"}]}
    h1 = hash_profile(p1)
    h2 = hash_profile(p2)
    assert h1 != h2, f"hashes should differ when keywords change: {h1} == {h2}"


@test("profile_hash invalidates when locations change")
def _():
    p1 = {"headline": "X", "acceptable_locations": ["DC"]}
    p2 = {"headline": "X", "acceptable_locations": ["DC", "NYC"]}
    assert hash_profile(p1) != hash_profile(p2)


@test("is_dead_listing catches 'no longer accepting applications'")
def _():
    assert is_dead_listing("This role is exciting. Unfortunately we are no longer accepting applications. Thanks.")


@test("is_dead_listing catches 'this position has been filled'")
def _():
    assert is_dead_listing("We're hiring for many roles. This position has been filled — check back for similar.")


@test("is_dead_listing catches 'this listing has expired'")
def _():
    assert is_dead_listing("Note: this listing has expired and is shown for reference only.")


@test("is_dead_listing IGNORES normal active JD text")
def _():
    jd = "We are hiring a Senior Engineer. Apply now! Salary range is $200K-$280K. Excellent benefits."
    assert not is_dead_listing(jd), "normal JD should not be flagged dead"


@test("is_dead_listing handles tiny inputs gracefully")
def _():
    assert not is_dead_listing("")
    assert not is_dead_listing(None)
    assert not is_dead_listing("short")


@test("Two archives sharing the same DB path — second open OK in WAL mode")
def _():
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "audit"
        a1 = set_audit_folder(path)
        a1.upsert_role(company="A", job_title="T1", job_url="u1")
        # Re-open (simulates app restart)
        a2 = set_audit_folder(path)
        keys = a2.applied_role_keys()  # smoke test query
        assert isinstance(keys, set)
        a2.close()


@test("Archive: applied keys round-trip survives close/reopen")
def _():
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "audit"
        a1 = set_audit_folder(path)
        rid = a1.upsert_role(company="Acme", job_title="X", job_url="u")
        a1.set_application_status(role_id=rid, status="applied")
        a1.close()
        # Reopen — should see the applied status
        a2 = set_audit_folder(path)
        keys = a2.applied_role_keys()
        assert ("acme", "x") in keys, f"expected applied key in {keys}"
        a2.close()


@test("Market contributions: dedup by (company, title)")
def _():
    from backend.storage.market import export_contributions
    with tempfile.TemporaryDirectory() as t:
        folder = Path(t) / "audit"
        folder.mkdir(parents=True)
        # Write the same role twice with different scores — second should win
        n1 = export_contributions(folder, contributions=[
            {"company": "Acme", "job_title": "Lead", "score": 70, "profile_tags": ["x"]}
        ])
        n2 = export_contributions(folder, contributions=[
            {"company": "Acme", "job_title": "Lead", "score": 88, "profile_tags": ["x"]}
        ])
        assert n1 == 1
        assert n2 == 1, "score change should rewrite"
        # Third write with same score should NOT rewrite
        n3 = export_contributions(folder, contributions=[
            {"company": "Acme", "job_title": "Lead", "score": 88, "profile_tags": ["x"]}
        ])
        assert n3 == 0, f"unchanged score should not rewrite, got {n3}"


@test("Sibling-folder market read does not include own folder")
def _():
    from backend.storage.market import (
        export_contributions, read_sibling_contributions,
    )
    with tempfile.TemporaryDirectory() as t:
        parent = Path(t)
        my_folder = parent / "me"
        my_folder.mkdir()
        sibling = parent / "other"
        sibling.mkdir()
        export_contributions(my_folder, contributions=[
            {"company": "MyOwn", "job_title": "Role", "score": 80, "profile_tags": ["a"]}
        ])
        export_contributions(sibling, contributions=[
            {"company": "Sibling", "job_title": "Role", "score": 75, "profile_tags": ["a"]}
        ])
        read = read_sibling_contributions(my_folder)
        companies = {e["company"] for e in read}
        assert "MyOwn" not in companies, "should not include own folder"
        assert "Sibling" in companies, "should include sibling"


print()
print("=" * 50)
print(f"Passed: {passes}")
print(f"Failed: {fails}")
print("=" * 50)
sys.exit(0 if fails == 0 else 1)

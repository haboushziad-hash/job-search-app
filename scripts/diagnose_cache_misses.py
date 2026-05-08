"""Cache-miss diagnostic — explains exactly why run-replay isn't hitting.

The runner's `find_recent_run_for_profile()` cache should hit when:
  1. Same `profile_hash` exists in runs.db within max_age_days (default 7)
  2. That run has status='completed'
  3. force_refresh is False on the new request

If you're re-running the same profile and not seeing cache hits, this
script identifies which of those three conditions is failing.

How to run:
  backend\\venv\\Scripts\\python.exe scripts\\diagnose_cache_misses.py

What it outputs:
  1. Where runs.db actually lives (and whether it exists)
  2. Recent runs with profile_hash + status + age
  3. Which fields in profile_snapshot differ between runs (if hashes differ)
  4. Whether the same profile_hash has shown up multiple times
  5. Whether any runs are stuck in status='running'
  6. A clear diagnosis of which of the 5 cache-miss reasons is firing

Cost: $0. Read-only. Doesn't touch the network.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Step 1: Find runs.db
# ---------------------------------------------------------------------------

def find_runs_db() -> list[Path]:
    """The runs.db lives in <audit_folder>/runs.db. The audit folder is
    set by the user via the desktop app's Settings, so we have to search
    common locations.
    """
    candidates = []
    home = Path.home()
    for parent in (
        home / "Documents",
        home / "OneDrive" / "Documents",
        home / "Desktop",
        home / "OneDrive" / "Desktop",
        home / "AppData" / "Roaming" / "app.jobsearch.desktop",
        home / "AppData" / "Local" / "app.jobsearch.desktop",
    ):
        if not parent.exists():
            continue
        # Look for runs.db in subfolders matching JobSearchApp / audits
        for db_path in parent.rglob("runs.db"):
            # Heuristic: within 4 levels deep, has a 'runs' or 'audits' folder nearby
            if any("runs" in p.name.lower() or "audit" in p.name.lower() for p in db_path.parents[:3]):
                candidates.append(db_path)
            elif "JobSearch" in str(db_path) or "audits" in str(db_path).lower():
                candidates.append(db_path)
    # Deduplicate
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


print("=" * 70)
print("CACHE-MISS DIAGNOSTIC")
print("=" * 70)
print()

print("Step 1: Locate runs.db")
print("-" * 40)
db_paths = find_runs_db()
if not db_paths:
    print("  No runs.db found in common locations.")
    print("  Check your desktop app's Settings to see the audit folder path,")
    print("  then run: dir /s /b <that-folder>\\runs.db")
    sys.exit(1)

for p in db_paths:
    print(f"  Found: {p}")
print()

# Use the most recently-modified runs.db
db_paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
db_path = db_paths[0]
print(f"Using:  {db_path}")
print(f"  Last modified: {db_path.stat().st_mtime}")
print()


# ---------------------------------------------------------------------------
# Step 2: Inspect the runs table
# ---------------------------------------------------------------------------

print("Step 2: Read runs table")
print("-" * 40)

# Copy to temp (avoid OneDrive locking conflicts)
import shutil
import tempfile
tmp_dir = Path(tempfile.gettempdir())
tmp_db = tmp_dir / "runs_diagnostic.db"
try:
    shutil.copy(db_path, tmp_db)
    print(f"  Copied to: {tmp_db}")
except Exception as e:
    print(f"  Copy failed: {e}")
    print(f"  Trying direct read...")
    tmp_db = db_path

con = sqlite3.connect(str(tmp_db))
con.row_factory = sqlite3.Row
cur = con.cursor()

# What columns exist?
cur.execute("PRAGMA table_info(runs)")
columns = [r["name"] for r in cur.fetchall()]
print(f"  runs columns: {columns}")
print()

# Recent runs
cur.execute("""
    SELECT run_id, started_at, status, profile_hash, completed_at,
           qualifying_count
    FROM runs
    ORDER BY started_at DESC
    LIMIT 20
""")
runs = cur.fetchall()

if not runs:
    print("  No runs found in runs.db. Cache can never hit if no prior runs exist.")
    sys.exit(0)

print(f"  Found {len(runs)} recent runs:")
print()
print(f"  {'Started':22s} {'Status':12s} {'Profile Hash':18s} {'Qual':>5s} {'Run ID':36s}")
for r in runs:
    print(f"  {r['started_at'][:19]:22s} {r['status']:12s} {(r['profile_hash'] or 'NULL'):18s} {r['qualifying_count'] or 0:>5d} {r['run_id']:36s}")
print()


# ---------------------------------------------------------------------------
# Step 3: Hash uniqueness analysis
# ---------------------------------------------------------------------------

print("Step 3: profile_hash uniqueness analysis")
print("-" * 40)

hash_groups = defaultdict(list)
for r in runs:
    if r["profile_hash"]:
        hash_groups[r["profile_hash"]].append(r)

print(f"  Total runs: {len(runs)}")
print(f"  Unique profile_hashes: {len(hash_groups)}")
print()

if len(hash_groups) == len(runs):
    print("  EVERY run has a different profile_hash.")
    print("  Cache CANNOT hit because no two runs share a profile.")
    print()
    print("  This means the profile_snapshot is non-deterministic between runs.")
    print("  Even with the same resume, profile-build produces slightly different")
    print("  output each time (likely from PROFILE_BUILD_TEMPERATURE=0.5 diversity")
    print("  sampling).")
elif len(hash_groups) < len(runs):
    print("  Some runs share profile_hashes (good — cache COULD hit).")
    for h, rs in sorted(hash_groups.items(), key=lambda kv: -len(kv[1])):
        if len(rs) > 1:
            print(f"  Hash {h}: {len(rs)} runs")
            for r in rs[:5]:
                print(f"    - {r['started_at'][:19]} {r['status']} {r['run_id'][:8]}...")
print()


# ---------------------------------------------------------------------------
# Step 4: status='running' check (abandoned runs)
# ---------------------------------------------------------------------------

print("Step 4: Abandoned runs (status='running')")
print("-" * 40)
cur.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'running'")
n_running = cur.fetchone()["n"]
cur.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'completed'")
n_completed = cur.fetchone()["n"]
print(f"  status='completed': {n_completed}")
print(f"  status='running':   {n_running}")
if n_running > 0:
    print(f"  WARNING: {n_running} runs stuck in 'running' state.")
    print(f"  These won't qualify for cache (cache requires status='completed').")
    print(f"  If a run aborted mid-pipeline, the run_summary never got marked completed.")
print()


# ---------------------------------------------------------------------------
# Step 5: profile_snapshot field-level comparison (when hashes differ)
# ---------------------------------------------------------------------------

print("Step 5: Field-level comparison (when hashes differ)")
print("-" * 40)

# Look for the profile_snapshot column or related
cur.execute("PRAGMA table_info(runs)")
all_cols = [r["name"] for r in cur.fetchall()]

snapshot_col = None
for candidate in ("profile_snapshot_json", "profile_snapshot", "summary_json"):
    if candidate in all_cols:
        snapshot_col = candidate
        break

if not snapshot_col:
    print("  No profile_snapshot column found in runs table.")
    print(f"  Available columns: {all_cols}")
else:
    print(f"  Reading from column: {snapshot_col}")
    cur.execute(f"""
        SELECT run_id, started_at, status, profile_hash, {snapshot_col}
        FROM runs
        WHERE status='completed' AND profile_hash IS NOT NULL
        ORDER BY started_at DESC
        LIMIT 5
    """)
    recent_completed = cur.fetchall()

    if len(recent_completed) >= 2:
        # Compare the 2 most recent completed runs
        r1, r2 = recent_completed[0], recent_completed[1]
        snap1_raw = r1[snapshot_col]
        snap2_raw = r2[snapshot_col]

        if not snap1_raw or not snap2_raw:
            print(f"  Snapshot column is empty for at least one run.")
        else:
            try:
                snap1 = json.loads(snap1_raw) if isinstance(snap1_raw, str) else snap1_raw
                snap2 = json.loads(snap2_raw) if isinstance(snap2_raw, str) else snap2_raw
            except Exception as e:
                print(f"  Could not parse snapshots as JSON: {e}")
                snap1 = snap2 = None

            if snap1 and snap2:
                # If snap is wrapped in summary, dig deeper
                if "profile_snapshot" in snap1:
                    snap1 = snap1["profile_snapshot"]
                    snap2 = snap2["profile_snapshot"]

                print(f"  Comparing 2 most recent completed runs:")
                print(f"    Run A: {r1['run_id'][:8]} ({r1['started_at'][:19]}) hash={r1['profile_hash']}")
                print(f"    Run B: {r2['run_id'][:8]} ({r2['started_at'][:19]}) hash={r2['profile_hash']}")
                print()

                if r1["profile_hash"] == r2["profile_hash"]:
                    print(f"  Hashes MATCH. Cache should have hit on Run B.")
                    print(f"  If Run B still ran the full pipeline, the cause is:")
                    print(f"    - force_refresh was True on the request")
                    print(f"    - max_age_days exceeded (>7 days between runs)")
                    print(f"    - Run B started before Run A completed (race condition)")
                else:
                    print(f"  Hashes DIFFER. Comparing canonical fields:")
                    canonical_keys = (
                        "headline", "target_functions", "target_industries",
                        "target_seniority", "technical_skills", "domain_expertise",
                        "salary_minimum", "work_arrangements", "acceptable_locations",
                        "acceptable_location_radii", "excluded_locations",
                        "negative_signals", "excluded_title_patterns",
                    )
                    for k in canonical_keys:
                        v1 = snap1.get(k)
                        v2 = snap2.get(k)
                        if v1 != v2:
                            print(f"    {k}: DIFFERS")
                            print(f"      A: {repr(v1)[:120]}")
                            print(f"      B: {repr(v2)[:120]}")
                    # Keywords specifically
                    kw1 = sorted([
                        (kw.get("text") if isinstance(kw, dict) else str(kw))
                        for kw in (snap1.get("keywords") or [])
                    ])
                    kw2 = sorted([
                        (kw.get("text") if isinstance(kw, dict) else str(kw))
                        for kw in (snap2.get("keywords") or [])
                    ])
                    if kw1 != kw2:
                        only1 = set(kw1) - set(kw2)
                        only2 = set(kw2) - set(kw1)
                        print(f"    keywords: DIFFERS")
                        if only1: print(f"      Only in A: {sorted(only1)}")
                        if only2: print(f"      Only in B: {sorted(only2)}")
print()


# ---------------------------------------------------------------------------
# Step 6: Diagnosis
# ---------------------------------------------------------------------------

print("=" * 70)
print("DIAGNOSIS")
print("=" * 70)
if len(hash_groups) == len(runs) and len(runs) > 1:
    print("  ROOT CAUSE: Profile build is non-deterministic.")
    print("  Each run produces a slightly different profile (different keywords,")
    print("  different excluded patterns), so profile_hash is unique per run,")
    print("  so cache never finds a prior match.")
    print()
    print("  FIX OPTIONS:")
    print("    1. Switch profile-build temperature to 0.0 (lose diversity, gain stability)")
    print("    2. Hash on resume content + version, not on built output")
    print("    3. Round/normalize profile fields before hashing (e.g., sort keywords)")
elif n_running > 0 and n_completed == 0:
    print("  ROOT CAUSE: All runs are stuck in 'running' state.")
    print("  Cache requires status='completed'. No runs qualify.")
    print()
    print("  FIX: Investigate why complete_run() isn't being called.")
elif len(hash_groups) < len(runs):
    print("  Some runs share profile_hashes — cache COULD have hit.")
    print("  If it didn't, check:")
    print("    - Is force_refresh=true being sent by the desktop app?")
    print("    - Was there >7 days gap between matching-hash runs?")

con.close()
try:
    if tmp_db != db_path:
        tmp_db.unlink()
except Exception:
    pass

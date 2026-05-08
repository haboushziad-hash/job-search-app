"""Diagnose the $7.91 Gemini cost leak from the v0.3.13 run.

Audit JSON says $1.55 logged. Real Gemini delta was $7.91.
$6.36 of cost is INVISIBLE to our cost_tracker.

This script answers four questions in one run:
  1. What's actually IN cost_log.db for the last 3 hours?
  2. How does it sum vs the $1.55 audit?
  3. Are there orphan calls (no run_id) that might be untracked?
  4. Are the 3 abandoned "running" runs draining cost retroactively?

Run from project root:
  backend\\venv\\Scripts\\python.exe scripts\\diagnose_cost_leak.py

If the venv python is OneDrive-locked, copy first:
  copy archive\\cost_log.db %TEMP%\\cost_log_now.db
  python scripts\\diagnose_cost_leak.py %TEMP%\\cost_log_now.db
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Allow override from command line
if len(sys.argv) > 1:
    db_path = Path(sys.argv[1])
else:
    db_path = PROJECT_ROOT / "archive" / "cost_log.db"

if not db_path.exists():
    print(f"ERROR: cost_log.db not found at {db_path}")
    sys.exit(1)

# Copy to TEMP to avoid OneDrive lock conflicts (search may still be writing)
tmp_dir = Path(tempfile.gettempdir())
tmp_db = tmp_dir / "cost_log_diagnostic.db"
try:
    shutil.copy(db_path, tmp_db)
    print(f"Copied {db_path} -> {tmp_db}")
except Exception as e:
    print(f"Copy failed ({e}), trying direct read")
    tmp_db = db_path

con = sqlite3.connect(str(tmp_db))
con.row_factory = sqlite3.Row
cur = con.cursor()

# =============================================================
# 1. All llm_calls in last 3 hours, summed
# =============================================================
print()
print("=" * 70)
print("Q1: What's in cost_log.db for the last 3 hours?")
print("=" * 70)

cur.execute("""
    SELECT timestamp, run_id, stage, model, cost_usd, input_tokens, output_tokens,
           success, error_message
    FROM llm_calls
    WHERE timestamp > datetime('now', '-3 hours')
    ORDER BY timestamp DESC
""")
rows = cur.fetchall()
total_cost = sum(r["cost_usd"] or 0 for r in rows)

print(f"  Total calls last 3 hours: {len(rows)}")
print(f"  Total cost summed: ${total_cost:.4f}")
print()
if rows:
    print(f"  Most recent 20 calls:")
    print(f"    {'Timestamp':25s} {'Stage':28s} {'Model':25s} {'Cost':>8s} {'OK':>4s}")
    for r in rows[:20]:
        print(f"    {str(r['timestamp'])[:25]:25s} {(r['stage'] or '')[:28]:28s} {(r['model'] or '')[:25]:25s} ${r['cost_usd'] or 0:>7.4f} {('Y' if r['success'] else 'N'):>4s}")

# =============================================================
# 2. Per-stage breakdown (last 3 hours)
# =============================================================
print()
print("=" * 70)
print("Q2: Per-stage cost breakdown (last 3 hours)")
print("=" * 70)

cur.execute("""
    SELECT stage, model,
           COUNT(*) AS n_calls,
           SUM(cost_usd) AS total_cost,
           SUM(input_tokens) AS total_input,
           SUM(output_tokens) AS total_output,
           SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures
    FROM llm_calls
    WHERE timestamp > datetime('now', '-3 hours')
    GROUP BY stage, model
    ORDER BY total_cost DESC
""")
stage_rows = cur.fetchall()

print(f"  {'Stage':30s} {'Model':25s} {'Calls':>6s} {'Cost':>10s} {'Failed':>7s}")
print(f"  {'-'*30:30s} {'-'*25:25s} {'-'*6:>6s} {'-'*10:>10s} {'-'*7:>7s}")
for r in stage_rows:
    print(f"  {(r['stage'] or 'NULL')[:30]:30s} {(r['model'] or 'NULL')[:25]:25s} {r['n_calls']:>6d} ${r['total_cost'] or 0:>9.4f} {r['failures']:>7d}")

# =============================================================
# 3. Orphan calls (no run_id)
# =============================================================
print()
print("=" * 70)
print("Q3: Orphan calls (no run_id) — could be from retries, prewarm, or bugs")
print("=" * 70)

cur.execute("""
    SELECT COUNT(*) AS n, SUM(cost_usd) AS cost
    FROM llm_calls
    WHERE (run_id IS NULL OR run_id = '')
      AND timestamp > datetime('now', '-3 hours')
""")
r = cur.fetchone()
print(f"  Orphan calls: {r['n']}, total cost: ${r['cost'] or 0:.4f}")
if r['n']:
    cur.execute("""
        SELECT timestamp, stage, cost_usd, error_message
        FROM llm_calls
        WHERE (run_id IS NULL OR run_id = '')
          AND timestamp > datetime('now', '-3 hours')
        ORDER BY timestamp DESC
        LIMIT 10
    """)
    for orphan in cur.fetchall():
        print(f"    {str(orphan['timestamp'])[:19]:19s} {(orphan['stage'] or 'NULL')[:25]:25s} ${orphan['cost_usd'] or 0:>7.4f}")

# =============================================================
# 4. Per-run aggregates (compare with audit JSON)
# =============================================================
print()
print("=" * 70)
print("Q4: Per-run aggregates (last 3 hours)")
print("=" * 70)

cur.execute("""
    SELECT run_id,
           MIN(timestamp) AS first_call,
           MAX(timestamp) AS last_call,
           COUNT(*) AS n_calls,
           SUM(cost_usd) AS total_cost,
           SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures
    FROM llm_calls
    WHERE timestamp > datetime('now', '-3 hours')
    GROUP BY run_id
    ORDER BY first_call DESC
""")
run_rows = cur.fetchall()
print(f"  {'Run ID':38s} {'First call':20s} {'Last call':20s} {'Calls':>6s} {'Cost':>10s} {'Fails':>5s}")
for r in run_rows[:15]:
    rid = (r["run_id"] or "ORPHAN")[:38]
    print(f"  {rid:38s} {str(r['first_call'])[:19]:20s} {str(r['last_call'])[:19]:20s} {r['n_calls']:>6d} ${r['total_cost'] or 0:>9.4f} {r['failures']:>5d}")

# =============================================================
# 5. Run summaries vs llm_calls — compare what's tracked vs claimed
# =============================================================
print()
print("=" * 70)
print("Q5: run_summaries totals vs llm_calls totals (consistency check)")
print("=" * 70)

cur.execute("""
    SELECT run_id, status, cost_total_usd, started_at, finished_at
    FROM run_summaries
    WHERE started_at > datetime('now', '-3 hours')
    ORDER BY started_at DESC
""")
summaries = cur.fetchall()

if not summaries:
    print("  No run_summaries in last 3 hours.")
else:
    print(f"  {'Run ID':38s} {'Status':14s} {'cost_total_usd':>16s} {'llm_calls SUM':>16s} {'GAP':>10s}")
    for s in summaries:
        # Sum llm_calls for this run
        cur.execute(
            "SELECT SUM(cost_usd) AS s FROM llm_calls WHERE run_id = ?",
            (s["run_id"],),
        )
        llm_sum = (cur.fetchone()["s"] or 0)
        gap = (s["cost_total_usd"] or 0) - llm_sum
        rid = s["run_id"][:38]
        print(f"  {rid:38s} {(s['status'] or '')[:14]:14s} ${s['cost_total_usd'] or 0:>14.4f}  ${llm_sum:>14.4f} ${gap:>+9.4f}")

# =============================================================
# 6. THE smoking gun: total_cost vs known $7.91 actual deduction
# =============================================================
print()
print("=" * 70)
print("DIAGNOSIS — $7.91 actual Gemini deduction vs logged")
print("=" * 70)

print(f"  Logged in cost_log.db (last 3 hours): ${total_cost:.4f}")
print(f"  Actual Gemini deduction:              $7.91")
print(f"  GAP:                                  ${7.91 - total_cost:+.4f}")
print()

if total_cost < 2.5:
    print(f"  CONCLUSION: ~${7.91 - total_cost:.2f} of cost is INVISIBLE to our logging.")
    print(f"  Most likely causes:")
    print(f"    1. SDK-internal retries (google-genai SDK retries below tenacity wrapper)")
    print(f"    2. Abandoned 'running' runs from earlier had pending charges")
    print(f"       that finalized in Google billing during/after this run")
    print(f"    3. Cache CREATE calls (cache prewarm) may not be in cost_log")
    print(f"    4. Multi-key rotation: failed-key1 + retried-key2 both billed")
    print()
    print(f"  IMMEDIATE ACTIONS:")
    print(f"    A. Roll back STAGE3_CONCURRENCY 3 -> 6 (concurrency=3 may be increasing")
    print(f"       SDK-retry rate due to longer wall-clock window)")
    print(f"    B. Implement Worker-side request counting (v0.3.14 P0 instead of v0.3.15)")
    print(f"    C. Check Cloud Console -> Generative Language API -> Metrics -> Total Requests")
    print(f"       for today. If it shows 5x our logged call count, SDK retries confirmed.")
elif 2.5 <= total_cost <= 5.0:
    print(f"  CONCLUSION: Partial recovery — some retries are being logged but not all.")
    print(f"  The retry-aware logging from v0.3.13 is partially working.")
elif total_cost > 5.0:
    print(f"  CONCLUSION: Most cost IS being logged in cost_log.db.")
    print(f"  The audit JSON's cost_total_usd ($1.55) doesn't reflect the cost_log totals.")
    print(f"  Bug is in the audit roll-up, not in tracking. Fix the audit serializer.")

con.close()
try:
    if tmp_db != db_path:
        tmp_db.unlink()
except Exception:
    pass

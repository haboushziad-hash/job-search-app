"""Pull REAL cost + timing data from runs.db. Used to seed the audit
package with concrete numbers (not estimates)."""
import json, sqlite3, sys
from pathlib import Path

db = Path(r"C:\Users\habou\OneDrive\Desktop\Job Search App\scripts\full_search_output\runs.db")
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute(
    "SELECT run_id, started_at, completed_at, status, qualifying_count, "
    "cost_total_usd, summary_json, profile_snapshot_json "
    "FROM runs ORDER BY started_at"
)

print("=" * 78)
print("REAL RUN DATA — runs.db")
print("=" * 78)
print()

total_cost = 0.0
total_dur = 0
n_complete = 0

for row in cur.fetchall():
    summary = json.loads(row["summary_json"]) if row["summary_json"] else {}
    profile = json.loads(row["profile_snapshot_json"]) if row["profile_snapshot_json"] else {}
    status = row["status"]
    print(f"Run {row['run_id'][:8]}  status={status}")
    print(f"  Profile headline: {(profile.get('headline') or '')[:80]}")
    print(f"  Started:    {row['started_at']}")
    print(f"  Completed:  {row['completed_at']}")
    print(f"  Duration:   {summary.get('duration_seconds')}s")
    print(f"  Cost:       ${row['cost_total_usd'] or 0:.4f}")
    print(f"  Scraped:    {summary.get('roles_scraped')}")
    print(f"  After filter:   {summary.get('roles_after_filter')}")
    print(f"  Qualifying:     {row['qualifying_count']}")
    print(f"  STRONG:    {summary.get('tier_strong')}")
    print(f"  GOOD:      {summary.get('tier_good')}")
    print(f"  MAYBE:     {summary.get('tier_maybe')}")
    print(f"  STRETCH:   {summary.get('tier_stretch')}")
    cost_by = summary.get("cost_by_stage") or {}
    if cost_by:
        print(f"  Cost breakdown:")
        for k, v in cost_by.items():
            print(f"    {k}: ${v:.4f}")
    if status == "completed":
        total_cost += row["cost_total_usd"] or 0
        total_dur += summary.get("duration_seconds") or 0
        n_complete += 1
    print()

print("=" * 78)
print(f"AGGREGATE — {n_complete} completed runs")
print("=" * 78)
if n_complete > 0:
    print(f"  Total cost:      ${total_cost:.4f}")
    print(f"  Avg cost/run:    ${total_cost / n_complete:.4f}")
    print(f"  Total duration:  {total_dur}s ({total_dur/60:.1f} min)")
    print(f"  Avg duration:    {total_dur / n_complete:.0f}s ({total_dur/n_complete/60:.1f} min)")

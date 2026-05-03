"""Sample Zach's STRETCH roles to validate they're real stretches."""
import sqlite3
c = sqlite3.connect(r"C:\Users\habou\OneDrive\Desktop\Job Search App\scripts\full_search_output\runs.db")
c.row_factory = sqlite3.Row

# Find Zach's run_id (Business Admin profile)
zach_run = c.execute("""
    SELECT run_id FROM runs
    WHERE profile_snapshot_json LIKE '%PepsiCo%' AND status='completed'
    ORDER BY started_at DESC LIMIT 1
""").fetchone()
if not zach_run:
    # Fallback: pick the most recent completed run that's not Ziad-AI
    zach_run = c.execute("""
        SELECT run_id FROM runs
        WHERE profile_snapshot_json NOT LIKE '%AI Strategy%' AND status='completed'
        ORDER BY started_at DESC LIMIT 1
    """).fetchone()
if not zach_run:
    print("No Zach-style run found")
    raise SystemExit(1)
zach_id = zach_run["run_id"]

print(f"=== ZACH RUN — STRETCH ROLES (40-54) ===\n")
cur = c.execute("""
    SELECT r.company, r.job_title, rs.final_score, rs.stage2_reasoning
    FROM role_scores rs JOIN roles r ON r.id = rs.role_id
    WHERE rs.final_score BETWEEN 40 AND 54
    AND rs.run_id = ?
    ORDER BY rs.final_score DESC LIMIT 8
""", (zach_id,))
for row in cur:
    reasoning = (row["stage2_reasoning"] or "")[:200]
    print(f"  Score {row['final_score']:.0f}  {row['company'][:25]:25}  {row['job_title'][:45]:45}")
    print(f"    Reasoning: {reasoning}")
    print()

print(f"\n=== ZACH STRONG/GOOD/MAYBE (>=55) — what 'should be' the actual recommendations ===\n")
cur = c.execute("""
    SELECT r.company, r.job_title, rs.final_score
    FROM role_scores rs JOIN roles r ON r.id = rs.role_id
    WHERE rs.final_score >= 55
    AND rs.run_id = ?
    ORDER BY rs.final_score DESC
""", (zach_id,))
for row in cur:
    print(f"  {row['final_score']:.0f}  {row['company'][:25]:25}  {row['job_title'][:60]}")

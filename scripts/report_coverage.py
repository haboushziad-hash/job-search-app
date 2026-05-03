"""Report real coverage stats from runs.db — answers 'are we hitting 60%+ salary?'"""
import json, sqlite3, sys
db = r"C:\Users\habou\OneDrive\Desktop\Job Search App\scripts\full_search_output\runs.db"
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== RUN-LEVEL COVERAGE (from summary_json) ===\n")
cur.execute("SELECT run_id, summary_json FROM runs WHERE status='completed'")
for row in cur.fetchall():
    s = json.loads(row["summary_json"])
    rid = row["run_id"][:8]
    print(f"  Run {rid}")
    print(f"    JD usable:         {s.get('jd_coverage_pct', 0):.0f}%")
    print(f"    Salary coverage:   {s.get('salary_coverage_pct', 0):.0f}%")
    print(f"    Location coverage: {s.get('location_coverage_pct', 0):.0f}%")
    print(f"    Scraped {s.get('roles_scraped', 0)} -> Qualifying {s.get('roles_qualifying', 0)}")
    print()

print()
print("=== QUALIFYING-ROLES FIELD COMPLETENESS ===\n")
cur.execute("""
    SELECT r.*
    FROM roles r
    JOIN role_scores rs ON rs.role_id = r.id
    WHERE rs.final_score >= 40
""")
rows = cur.fetchall()
n = len(rows)
print(f"Total qualifying (>=40): {n}\n")
if n:
    metrics = [
        ("job_url",              lambda r: bool(r["job_url"])),
        ("location text",        lambda r: bool(r["location"])),
        ("location_type",        lambda r: bool(r["location_type"])),
        ("salary_min OR max",    lambda r: bool(r["salary_min"] or r["salary_max"])),
        ("salary_text label",    lambda r: bool(r["salary_text"])),
        ("JD body (>500ch)",     lambda r: bool(r["job_description_full"] and len(r["job_description_full"]) > 500)),
        ("posted_date",          lambda r: bool(r["posted_date"])),
        ("industry (AI-A)",      lambda r: bool(r["industry"])),
        ("summary (AI-C)",       lambda r: bool(r["summary"])),
    ]
    for label, fn in metrics:
        cnt = sum(1 for r in rows if fn(r))
        flag = "OK" if cnt/n >= 0.60 else ("LOW" if cnt/n >= 0.30 else "GAP")
        print(f"  [{flag:3}]  {label:30}  {cnt}/{n}  ({100*cnt/n:5.1f}%)")

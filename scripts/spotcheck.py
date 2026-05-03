"""Quick spotcheck of latest audit JSON for industry + summary fields."""
import json
from pathlib import Path

p = Path(r"C:\Users\habou\OneDrive\Desktop\Job Search App\scripts\full_search_output\runs")
latest = max(p.glob("*_audit.json"), key=lambda f: f.stat().st_mtime)
print(f"Reading {latest.name}\n")
d = json.loads(latest.read_text(encoding="utf-8"))
roles = d.get("all_qualifying_roles", [])

n_industry = sum(1 for r in roles if r.get("industry"))
n_summary = sum(1 for r in roles if r.get("summary"))
print(f"Total qualifying: {len(roles)}")
print(f"With industry: {n_industry}/{len(roles)} ({100*n_industry/max(1,len(roles)):.0f}%)")
print(f"With summary:  {n_summary}/{len(roles)} ({100*n_summary/max(1,len(roles)):.0f}%)")
print()

# Sample first 5 with score >=70 (since Stage 3 runs on those)
high = [r for r in roles if (r.get("score") or 0) >= 70][:5]
print(f"=== Sample of high-tier roles ===")
for r in high:
    print()
    print(f"  Score: {r.get('score')}  {r.get('company')[:30]} - {r.get('title')[:50]}")
    print(f"    industry: {r.get('industry')!r}")
    summ = r.get("summary") or "(empty)"
    print(f"    summary:  {summ[:160]!r}")

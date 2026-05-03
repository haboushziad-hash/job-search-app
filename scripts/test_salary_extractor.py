"""Test enhanced salary extractor against real JDs from saved runs."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.filter.salary_extractor import extract_salary_from_jd

RUN_DIRS = [
    Path(r"C:\Users\habou\OneDrive\Desktop\Job Search App\scripts\full_search_output"),
]

all_roles = []
for d in RUN_DIRS:
    for jf in d.glob("*_full.json"):
        data = json.loads(jf.read_text(encoding="utf-8"))
        all_roles.extend(data.get("all_roles", []))

print(f"Loaded {len(all_roles)} roles from saved runs")

with_jd = [r for r in all_roles if r.get("job_description_full")]
print(f"  {len(with_jd)} have JD bodies")

hits = 0
misses = 0
results = []
for r in with_jd:
    text, smin, smax = extract_salary_from_jd(r["job_description_full"])
    company = r.get("company", "?")
    title = r.get("job_title", "?")
    if smin or smax:
        hits += 1
        results.append((True, company, title, text, smin, smax))
    else:
        misses += 1
        results.append((False, company, title, None, None, None))

print(f"\nExtractor hits: {hits}/{len(with_jd)} ({100*hits/max(1,len(with_jd)):.0f}%)")
print()
print("=== HITS ===")
for ok, c, t, text, smin, smax in results:
    if ok:
        print(f"  ${smin:,} - ${smax:,}    {c} — {t}")
print()
print("=== MISSES ===")
for ok, c, t, _, _, _ in results:
    if not ok:
        print(f"  (no salary)         {c} — {t}")

"""Test per-company cap in apply_hard_filters."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.filter.hard_filters import apply_hard_filters
from backend.models import CandidateProfile, Role


def make_role(company: str, title: str, posted_date: str = "2026-04-25T00:00:00+00:00") -> Role:
    return Role(
        company=company,
        job_title=title,
        job_url=f"https://example.com/{company}/{title}",
        location="Remote",
        location_type="Remote",
        posted_date=posted_date,
        primary_source="test",
        date_first_seen="2026-05-03",
    )


def make_profile(**overrides) -> CandidateProfile:
    base = dict(
        headline="test candidate",
        target_seniority="Senior",
        target_functions=["operations"],
        target_industries=["tech"],
        excluded_title_patterns=[],
        negative_signals=[],
        keywords=[],
        resumes=[],
    )
    base.update(overrides)
    return CandidateProfile(**base)


def t(name, *, roles, profile, expected_total, expected_per_company):
    out = apply_hard_filters(roles, profile=profile, log=False)
    actual_total = len(out)
    from collections import Counter
    actual_per_company = dict(Counter((r.company or "").lower() for r in out))
    ok = actual_total == expected_total and actual_per_company == expected_per_company
    print(f"  {'OK' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        expected total={expected_total} per_company={expected_per_company}")
        print(f"        got      total={actual_total} per_company={actual_per_company}")
    return ok


print("=== per_company_cap tests ===\n")
results = []

# 20 Snorkel roles + cap=0 (default) → all 20 kept
results.append(t(
    "Default cap=0 keeps all roles",
    roles=[make_role("Snorkel", f"Operations Manager {i}") for i in range(20)],
    profile=make_profile(),
    expected_total=20,
    expected_per_company={"snorkel": 20},
))

# Mix with cap=0: all kept
results.append(t(
    "cap=0 keeps multi-company roles all",
    roles=(
        [make_role("Snorkel", f"Operations Manager {i}") for i in range(20)]
        + [make_role("Cresta", f"AI Manager {i}") for i in range(3)]
        + [make_role("Anthropic", f"Engineer {i}") for i in range(2)]
    ),
    profile=make_profile(),
    expected_total=25,
    expected_per_company={"snorkel": 20, "cresta": 3, "anthropic": 2},
))

# Empty roles
results.append(t(
    "Empty roles list",
    roles=[],
    profile=make_profile(),
    expected_total=0,
    expected_per_company={},
))

# Each company has 1 role — nothing capped
results.append(t(
    "Single-role companies all kept",
    roles=[make_role(f"Company{i}", "Operations Manager") for i in range(8)],
    profile=make_profile(),
    expected_total=8,
    expected_per_company={f"company{i}": 1 for i in range(8)},
))


# Caps newest-first
roles_dated = [
    make_role("X", f"Role {chr(65+i)}", posted_date=f"2026-04-{15+i:02d}T00:00:00+00:00")
    for i in range(18)  # Roles A through R spanning 04-15 to 05-02
]
# Explicit cap=15 to test newest-first ordering
out = apply_hard_filters(roles_dated, profile=make_profile(), per_company_cap=15, log=False)
titles_kept = sorted(r.job_title for r in out)
expected = [f"Role {chr(65+i)}" for i in range(3, 18)]  # D through R
ok = titles_kept == expected
results.append(ok)
print(f"  {'OK' if ok else 'FAIL'}  Newest-first cap (drops oldest)")
if not ok:
    print(f"        expected={expected}")
    print(f"        got={titles_kept}")

# Disable cap with 0
out = apply_hard_filters(
    [make_role("Snorkel", f"M{i}") for i in range(10)],
    profile=make_profile(),
    per_company_cap=0,
    log=False,
)
ok = len(out) == 10
results.append(ok)
print(f"  {'OK' if ok else 'FAIL'}  per_company_cap=0 disables cap (got {len(out)} of 10)")

# Custom cap value (3)
out = apply_hard_filters(
    [make_role("Snorkel", f"M{i}") for i in range(10)],
    profile=make_profile(),
    per_company_cap=3,
    log=False,
)
ok = len(out) == 3
results.append(ok)
print(f"  {'OK' if ok else 'FAIL'}  per_company_cap=3 caps at 3 (got {len(out)})")

passed = sum(1 for r in results if r)
total = len(results)
print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)

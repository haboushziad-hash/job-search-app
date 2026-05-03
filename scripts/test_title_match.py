"""Test the suffix-expanded title matching catches engineer/engineering/engineers."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.filter.hard_filters import _match_phrase_in_title


def t(name, *, phrase, title, expected):
    got = _match_phrase_in_title(phrase, title.lower())
    ok = got == expected
    print(f"  {'OK' if ok else 'FAIL'}  {name} -> {got}")
    if not ok:
        print(f"        phrase={phrase!r} title={title!r} expected={expected}")
    return ok


print("=== Title-match suffix expansion tests ===")
results = []

# Audit case
results.append(t("'engineer' matches 'Senior Engineer'",
    phrase="engineer", title="Senior Engineer", expected=True))
results.append(t("'engineer' matches 'Engineering Manager' (audit case)",
    phrase="engineer", title="Engineering Manager", expected=True))
results.append(t("'engineer' matches 'Engineers Hub Lead'",
    phrase="engineer", title="Engineers Hub Lead", expected=True))
results.append(t("'engineer' does NOT match 'Engagement Manager'",
    phrase="engineer", title="Engagement Manager", expected=False))
results.append(t("'engineer' does NOT match 'Engaging Director'",
    phrase="engineer", title="Engaging Director", expected=False))

# Other plurals/gerunds
results.append(t("'developer' matches 'Senior Developers'",
    phrase="developer", title="Senior Developers", expected=True))
results.append(t("'manager' does NOT match 'Managing Director' (different stem)",
    phrase="manager", title="Managing Director", expected=False))
results.append(t("'sales' matches 'Sales Director'",
    phrase="sales", title="Sales Director", expected=True))
results.append(t("'sales' does NOT match 'Pre-Sale Specialist'",
    phrase="sales", title="Pre-Sale Specialist", expected=False))

# Multi-word patterns
results.append(t("'ml engineer' matches 'ML Engineering Manager'",
    phrase="ml engineer", title="ML Engineering Manager", expected=True))
results.append(t("'ml engineer' does NOT match 'Software Engineer' (no ml)",
    phrase="ml engineer", title="Software Engineer", expected=False))
results.append(t("'data scientist' matches 'Senior Data Scientists'",
    phrase="data scientist", title="Senior Data Scientists", expected=True))
results.append(t("'account executive' matches 'Junior Account Executive'",
    phrase="account executive", title="Junior Account Executive", expected=True))

# Stopwords ignored
results.append(t("'the engineer' matches 'Engineering Lead' (stopwords skipped)",
    phrase="the engineer", title="Engineering Lead", expected=True))

# Edge: empty / whitespace
results.append(t("Empty phrase returns False",
    phrase="", title="anything", expected=False))
results.append(t("Whitespace-only phrase returns False",
    phrase="   ", title="anything", expected=False))

# Edge: title contains word as substring but not whole word
results.append(t("'rn' does NOT match 'Cornucopia Manager'",
    phrase="rn", title="Cornucopia Manager", expected=False))
results.append(t("'rn' matches 'Charge RN'",
    phrase="rn", title="Charge RN", expected=True))

# Edge: case-insensitive (title comes lowercased)
results.append(t("Case-insensitive: 'ENGINEER' matches lowercased title",
    phrase="ENGINEER", title="senior engineer", expected=True))

passed = sum(results)
total = len(results)
print(f"\n{passed}/{total} passed")
sys.exit(0 if passed == total else 1)

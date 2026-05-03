"""Test coverage_gap_analysis + keyword_coverage_scoring."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.storage.audit import _coverage_gap_analysis, _keyword_coverage_scoring


class FakeRole:
    def __init__(self, company, title, industry):
        self.company = company
        self.job_title = title
        self.industry = industry


class FakeProfile:
    def __init__(self, target_industries):
        self.target_industries = target_industries


_PASS = 0
_FAIL = 0


def assert_eq(name, actual, expected):
    global _PASS, _FAIL
    if actual == expected:
        _PASS += 1
        print(f"  OK    {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name}")
        print(f"        expected: {expected}")
        print(f"        got:      {actual}")


def section(t):
    print(f"\n=== {t} ===")


# ============================================================================
# Coverage gap analysis
# ============================================================================
section("Coverage gap analysis: HIGH severity (Zach scenario)")

zach = FakeProfile(target_industries=["CPG", "Retail", "Consulting"])
# 30 roles, ALL Tech (matches Ziad's run pattern)
roles_all_tech = [FakeRole(f"co{i}", f"Manager {i}", "Tech") for i in range(30)]
gap = _coverage_gap_analysis(roles_all_tech, zach)
assert_eq("Severity HIGH when 0% target match", gap["gap_severity"], "HIGH")
assert_eq("Match pct = 0", gap["target_match_pct"], 0.0)
assert_eq("industries_found = {Tech: 30}", gap["industries_found"], {"Tech": 30})
assert_eq("dashboard_message non-null", gap["dashboard_message"] is not None, True)
assert_eq("recommendation mentions iCIMS", "iCIMS" in (gap["recommendation"] or ""), True)


section("Coverage gap analysis: LOW severity (good coverage)")

ziad = FakeProfile(target_industries=["Tech", "Consulting"])
mixed = (
    [FakeRole(f"t{i}", f"Manager {i}", "Tech") for i in range(15)]
    + [FakeRole(f"c{i}", f"Consultant {i}", "Consulting") for i in range(8)]
    + [FakeRole(f"f{i}", f"Analyst {i}", "Finance") for i in range(2)]
)
gap = _coverage_gap_analysis(mixed, ziad)
assert_eq("Severity LOW when >60% match", gap["gap_severity"], "LOW")
assert_eq("Dashboard message null on LOW", gap["dashboard_message"], None)


section("Coverage gap analysis: MEDIUM severity")

med_profile = FakeProfile(target_industries=["Healthcare"])
mixed_med = (
    [FakeRole(f"h{i}", f"Manager {i}", "Healthcare") for i in range(8)]  # 40%
    + [FakeRole(f"t{i}", f"Manager {i}", "Tech") for i in range(12)]
)
gap = _coverage_gap_analysis(mixed_med, med_profile)
assert_eq("Severity MEDIUM at 40%", gap["gap_severity"], "MEDIUM")
assert_eq("Match pct = 40.0", gap["target_match_pct"], 40.0)


section("Coverage gap analysis: edge cases")

# No qualifying roles
gap = _coverage_gap_analysis([], zach)
assert_eq("NO_DATA when zero qualifying roles", gap["gap_severity"], "NO_DATA")

# Profile has no target_industries
empty_targets = FakeProfile(target_industries=[])
gap = _coverage_gap_analysis(roles_all_tech, empty_targets)
assert_eq("UNKNOWN when no targets", gap["gap_severity"], "UNKNOWN")

# Loose substring match: target "Tech" should match role industry "Technology"
ziad2 = FakeProfile(target_industries=["Tech"])
roles_tech_variants = [
    FakeRole("a", "x", "Technology"),
    FakeRole("b", "x", "Tech"),
    FakeRole("c", "x", "tech"),  # case insensitive
]
gap = _coverage_gap_analysis(roles_tech_variants, ziad2)
assert_eq("Loose substring match Tech<->Technology", gap["target_match_pct"], 100.0)

# Profile is None
gap = _coverage_gap_analysis(roles_all_tech, None)
assert_eq("Profile=None safe", gap["gap_severity"], "UNKNOWN")

# Roles with no industry field
no_ind = [FakeRole("a", "x", None), FakeRole("b", "x", "")]
gap = _coverage_gap_analysis(no_ind, zach)
# These roles get skipped from industry counting; total_with_industry=0 → divides by zero protected
# but qualifying != 0, so we check what happens
# Actual: len(qualifying)=2 != 0, targets exist, total_with_industry=0, pct=0/0 protected → 0.0 → HIGH
assert_eq("Roles without industry -> HIGH (no signal)", gap["gap_severity"], "HIGH")


# ============================================================================
# Keyword coverage scoring
# ============================================================================
section("Keyword coverage scoring")

roles_diverse = [
    FakeRole(f"company{i}", f"AI Manager at company{i}", "Tech")
    for i in range(15)
]
scoring = _keyword_coverage_scoring(["AI Manager"], roles_diverse)
assert_eq("HIGH band (15 distinct companies)", scoring[0]["coverage_band"], "HIGH")
assert_eq("15 distinct companies", scoring[0]["distinct_companies"], 15)

roles_narrow = [
    FakeRole("snorkel", "AI Manager A", "Tech"),
    FakeRole("snorkel", "AI Manager B", "Tech"),
    FakeRole("cresta", "AI Manager C", "Tech"),
]
scoring = _keyword_coverage_scoring(["AI Manager"], roles_narrow)
assert_eq("LOW band (2 distinct companies, 3 matches)", scoring[0]["coverage_band"], "LOW")
assert_eq("2 distinct companies", scoring[0]["distinct_companies"], 2)
assert_eq("3 total matches", scoring[0]["total_matches"], 3)

scoring = _keyword_coverage_scoring(["nonexistent"], roles_narrow)
assert_eq("NONE band when no matches", scoring[0]["coverage_band"], "NONE")
assert_eq("0 distinct companies", scoring[0]["distinct_companies"], 0)

# Multi-keyword sort by distinct_companies desc
roles = (
    [FakeRole(f"co{i}", "Sales Director title", "x") for i in range(10)]
    + [FakeRole("solo", "Operations Director title", "x")]
)
scoring = _keyword_coverage_scoring(["Sales", "Operations"], roles)
assert_eq("Sorted by distinct_companies desc", scoring[0]["keyword"], "Sales")
assert_eq("HIGH for Sales (10 cos)", scoring[0]["coverage_band"], "HIGH")
assert_eq("LOW for Operations (1 co)", scoring[1]["coverage_band"], "LOW")

# Empty
scoring = _keyword_coverage_scoring([], [])
assert_eq("Empty inputs return []", scoring, [])


total = _PASS + _FAIL
print(f"\n{_PASS}/{total} passed")
sys.exit(0 if _FAIL == 0 else 1)

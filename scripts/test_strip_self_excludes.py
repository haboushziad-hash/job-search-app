"""Comprehensive unit test for _strip_self_excludes.

Goal: prove the function is correct under adversarial conditions. The bug
this guards against — software engineers having "engineer" in their excluded
patterns — is the highest-impact bug in the tool because it would silently
drop ALL of the candidate's target roles.

Test categories:
  1. Audit-flagged bugs (must pass for ship-readiness)
  2. Cross-industry coverage (15+ professions)
  3. Adversarial input (None, empty, unicode, special chars, very long)
  4. Whole-word boundary correctness
  5. Multi-word pattern handling
  6. Dangerous generics (seniority + role-pattern words)
  7. Deduplication & normalization
  8. Cap enforcement
  9. Headline/tier1/target_functions interaction edge cases
  10. Negative tests (patterns that MUST be kept, not stripped)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.profile.builder import _strip_self_excludes  # noqa: E402


_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def t(name: str, *, patterns, headline, target_functions, tier1, expected):
    """Run one assertion. Order-insensitive comparison."""
    global _PASS, _FAIL
    got = _strip_self_excludes(
        patterns=patterns,
        headline=headline,
        target_functions=target_functions,
        tier1_keywords=tier1,
    )
    ok = sorted(got) == sorted(expected)
    if ok:
        _PASS += 1
        print(f"  OK    {name}")
    else:
        _FAIL += 1
        msg = (
            f"FAIL  {name}\n"
            f"        patterns:        {patterns}\n"
            f"        headline:        {headline!r}\n"
            f"        target_functions:{target_functions}\n"
            f"        tier1:           {tier1}\n"
            f"        expected:        {sorted(expected)}\n"
            f"        got:             {sorted(got)}\n"
        )
        _FAILURES.append(msg)
        print(f"  {msg}")


def section(title: str):
    print(f"\n=== {title} ===")


# ============================================================================
# Category 1: AUDIT-FLAGGED BUGS — these caused the ship-blocker. Must pass.
# ============================================================================
section("Category 1: Audit-flagged engineer/nurse bugs")

t(
    "Software engineer must NOT exclude 'engineer'",
    patterns=["engineer", "developer", "sales", "marketing"],
    headline="Senior software engineer with 8 years",
    target_functions=["software engineering", "backend engineering"],
    tier1=["Senior Software Engineer", "Backend Engineer"],
    expected=["sales", "marketing"],  # developer also stripped (in haystack via tier1? No - tier1 is engineer not developer. Keep developer.)
)
# Wait — does headline mention developer? No. Should keep "developer" then.
t(
    "Software engineer ALSO strips 'developer' via synonym expansion",
    patterns=["engineer", "developer"],
    headline="Senior software engineer 8 years backend",
    target_functions=["software engineering"],
    tier1=["Senior Software Engineer"],
    # Synonyms map: engineer -> {developer}, so haystack augmented with
    # "developer" → pattern "developer" also strips. This is the desired
    # behavior — software engineer should not exclude developer roles.
    expected=[],
)

t(
    "Environmental engineer must NOT exclude 'engineer'",
    patterns=["engineer", "physician", "rn", "broker"],
    headline="Environmental engineer water wastewater PE",
    target_functions=["environmental engineering"],
    tier1=["Environmental Engineer", "Water Resources Engineer"],
    expected=["physician", "rn", "broker"],
)

t(
    "Registered nurse must NOT exclude 'nurse' or 'rn'",
    patterns=["nurse", "rn", "engineer", "developer"],
    headline="Registered nurse with charge experience",
    target_functions=["nursing", "clinical care"],
    tier1=["RN", "Charge Nurse", "Nurse Manager"],
    expected=["engineer", "developer"],
)

t(
    "Marketing director must NOT exclude 'marketing'",
    patterns=["marketing", "engineer", "actuary"],
    headline="Marketing director B2B SaaS",
    target_functions=["marketing", "demand generation"],
    tier1=["Demand Gen Lead", "Marketing Director"],
    expected=["engineer", "actuary"],
)

t(
    "Tax accountant must NOT exclude 'accountant' or 'tax'",
    patterns=["accountant", "tax accountant", "engineer", "underwriter"],
    headline="Senior tax accountant Big 4 CPA",
    target_functions=["tax accounting", "audit"],
    tier1=["Senior Tax Accountant", "Tax Manager", "Tax Senior"],
    expected=["engineer", "underwriter"],
)


# ============================================================================
# Category 2: CROSS-INDUSTRY COVERAGE (15+ professions)
# ============================================================================
section("Category 2: Cross-industry coverage")

t(
    "Lawyer / attorney must NOT exclude 'attorney' or 'lawyer'",
    patterns=["attorney", "lawyer", "engineer", "broker"],
    headline="Corporate attorney at AmLaw 100",
    target_functions=["corporate law", "M&A"],
    tier1=["Corporate Attorney", "M&A Attorney", "Corporate Counsel"],
    expected=["engineer", "broker"],
)

t(
    "UX designer must NOT exclude 'designer' or 'ux'",
    patterns=["designer", "ux", "engineer", "physician"],
    headline="Senior UX designer fintech",
    target_functions=["ux design", "product design"],
    tier1=["Senior UX Designer", "Product Designer", "UX Researcher"],
    expected=["engineer", "physician"],
)

t(
    "Sales account executive must NOT exclude 'account executive' or 'sales'",
    patterns=["account executive", "sales", "engineer", "designer"],
    headline="Enterprise account executive SaaS",
    target_functions=["enterprise sales"],
    tier1=["Senior Account Executive", "Account Executive", "Enterprise Sales"],
    expected=["engineer", "designer"],
)

t(
    "Data scientist must NOT exclude 'scientist' or 'data scientist'",
    patterns=["data scientist", "scientist", "engineer", "physician"],
    headline="Senior data scientist ML platforms",
    target_functions=["data science", "machine learning"],
    tier1=["Senior Data Scientist", "Staff Data Scientist", "ML Scientist"],
    # "engineer" not in haystack → keep
    expected=["engineer", "physician"],
)

t(
    "Product manager must NOT exclude 'product' or 'product manager'",
    patterns=["product manager", "product", "engineer", "nurse"],
    headline="Senior product manager B2B SaaS",
    target_functions=["product management"],
    tier1=["Senior Product Manager", "Product Lead", "Product Owner"],
    expected=["engineer", "nurse"],
)

t(
    "Recruiter must NOT exclude 'recruiter'",
    patterns=["recruiter", "engineer", "auditor"],
    headline="Senior technical recruiter SaaS",
    target_functions=["recruiting", "talent acquisition"],
    tier1=["Senior Recruiter", "Technical Recruiter", "Sourcer"],
    expected=["engineer", "auditor"],
)

t(
    "Architect (building) must NOT exclude 'architect'",
    patterns=["architect", "engineer", "physician"],
    headline="Senior architect commercial buildings AIA",
    target_functions=["architecture", "design"],
    tier1=["Senior Architect", "Project Architect", "Design Architect"],
    expected=["engineer", "physician"],
)

t(
    "Hospitality / hotel manager must NOT exclude 'hotel' or generics",
    patterns=["hotel", "general manager", "engineer", "nurse"],
    headline="General manager full-service hotel",
    target_functions=["hospitality management", "hotel operations"],
    tier1=["Hotel General Manager", "Director of Operations Hotel"],
    expected=["engineer", "nurse"],
)

t(
    "Hairstylist must NOT exclude 'stylist' or 'hairstylist'",
    patterns=["hairstylist", "stylist", "engineer", "developer"],
    headline="Master hairstylist 15 years salon ownership",
    target_functions=["hair styling", "salon management"],
    tier1=["Master Stylist", "Senior Hairstylist"],
    expected=["engineer", "developer"],
)

t(
    "Chef / culinary must NOT exclude 'chef' or 'cook'",
    patterns=["chef", "cook", "engineer", "lawyer"],
    headline="Executive chef fine dining 12 years",
    target_functions=["culinary leadership"],
    tier1=["Executive Chef", "Sous Chef", "Head Chef"],
    expected=["engineer", "lawyer"],
)

t(
    "Veterinarian must NOT exclude 'veterinarian' or 'vet'",
    patterns=["veterinarian", "vet", "engineer", "broker"],
    headline="Small animal veterinarian DVM 10 years",
    target_functions=["veterinary medicine"],
    tier1=["Senior Veterinarian", "Associate Veterinarian"],
    expected=["engineer", "broker"],
)

t(
    "Pilot must NOT exclude 'pilot'",
    patterns=["pilot", "engineer", "nurse"],
    headline="Commercial airline pilot ATP rating",
    target_functions=["commercial aviation"],
    tier1=["First Officer", "Senior Pilot", "Captain"],
    expected=["engineer", "nurse"],
)

t(
    "Teacher / educator must NOT exclude 'teacher'",
    patterns=["teacher", "educator", "engineer", "auditor"],
    headline="High school physics teacher 10 years",
    target_functions=["secondary education", "STEM teaching"],
    tier1=["Senior Teacher", "Lead Teacher", "Department Chair"],
    expected=["engineer", "auditor"],
)

t(
    "Real estate agent must NOT exclude 'agent' or 'realtor'",
    patterns=["agent", "realtor", "engineer", "physician"],
    headline="Licensed real estate agent residential",
    target_functions=["real estate sales"],
    tier1=["Real Estate Agent", "Senior Realtor", "Listing Agent"],
    expected=["engineer", "physician"],
)


# ============================================================================
# Category 3: ADVERSARIAL INPUT
# ============================================================================
section("Category 3: Adversarial input")

t(
    "Empty patterns list",
    patterns=[],
    headline="anything",
    target_functions=[],
    tier1=[],
    expected=[],
)

t(
    "All-empty input — empty result",
    patterns=["", "  ", None, "\t"],
    headline="",
    target_functions=[],
    tier1=[],
    expected=[],
)

t(
    "None values in pattern list are skipped",
    patterns=[None, "engineer", None, "broker"],
    headline="data scientist",
    target_functions=["data science"],
    tier1=["Senior Data Scientist"],
    expected=["engineer", "broker"],
)

t(
    "Non-string pattern values are skipped",
    patterns=[42, "engineer", {"a": 1}, "broker", ["x"]],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "broker"],
)

t(
    "Whitespace-only patterns are dropped",
    patterns=["   ", "\t\n", "engineer"],
    headline="marketer",
    target_functions=["marketing"],
    tier1=[],
    expected=["engineer"],
)

t(
    "Mixed-case patterns are normalized to lowercase",
    patterns=["ENGINEER", "Engineer", "engineer", "BrOker"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "broker"],
)

t(
    "Patterns with punctuation are normalized",
    patterns=["engineer!", "engineer,", "engineer.", "br0ker?"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "br0ker"],
)

t(
    "Patterns with leading/trailing whitespace are trimmed",
    patterns=["  engineer  ", "\tbroker\n"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "broker"],
)

t(
    "Very long pattern is kept (no length cap)",
    patterns=["a" * 100, "engineer"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["a" * 100, "engineer"],
)

t(
    "Unicode characters are stripped to spaces (English-only tool)",
    patterns=["ingéniero", "übermanager", "engineer"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    # ingéniero -> "ing niero" (collapsed " ngeneer" no), let's see
    # "ingéniero" lower → "ingéniero" → strip non-alphanum → "ing niero" → collapse → "ing niero"
    # ubermanager lower -> "übermanager" -> strip -> " bermanager" -> collapse -> "bermanager"
    expected=["ing niero", "bermanager", "engineer"],
)


# ============================================================================
# Category 4: WHOLE-WORD BOUNDARY CORRECTNESS
# ============================================================================
section("Category 4: Whole-word boundaries")

t(
    "'engineer' must NOT match 'engineering' (no word boundary fail)",
    patterns=["engineer"],
    headline="engineering manager 5 years",
    target_functions=[],
    tier1=[],
    # "engineer" should NOT whole-word match "engineering" — different words
    # so this pattern is NOT stripped (kept).
    expected=["engineer"],
)

t(
    "'engineering' DOES match 'engineering manager'",
    patterns=["engineering"],
    headline="engineering manager 5 years",
    target_functions=[],
    tier1=[],
    expected=[],
)

t(
    "'manager' word would match but is dangerous-generic so stripped",
    patterns=["manager"],
    headline="senior software engineer",
    target_functions=[],
    tier1=[],
    expected=[],  # manager always stripped as dangerous generic
)

t(
    "'engineer' must match 'Senior Software Engineer' tier1 keyword",
    patterns=["engineer"],
    headline="career changer",
    target_functions=[],  # vague headline - tier1 carries the signal
    tier1=["Senior Software Engineer", "Backend Engineer"],
    expected=[],  # stripped via tier1 match
)

t(
    "'engineer' is NOT stripped when haystack has only 'engagement manager'",
    patterns=["engineer"],
    headline="engagement manager 5 years",
    target_functions=[],
    tier1=[],
    expected=["engineer"],  # 'engineer' is NOT a whole word in 'engagement'
)


# ============================================================================
# Category 5: MULTI-WORD PATTERNS
# ============================================================================
section("Category 5: Multi-word patterns")

t(
    "Multi-word 'data scientist' stripped if in tier1",
    patterns=["data scientist"],
    headline="senior data scientist 10 years",
    target_functions=["data science"],
    tier1=["Senior Data Scientist", "Lead Data Scientist"],
    expected=[],
)

t(
    "Multi-word 'data engineer' KEPT for software engineer (different role)",
    patterns=["data engineer"],
    headline="senior software engineer 10 years",
    target_functions=["software engineering"],
    tier1=["Senior Software Engineer", "Backend Engineer"],
    # Multi-word phrase 'data engineer' does not whole-phrase-match
    # 'software engineer' or 'backend engineer'. Pattern is intentional.
    expected=["data engineer"],
)

t(
    "Multi-word 'machine learning engineer' stripped for ML engineer",
    patterns=["machine learning engineer"],
    headline="senior machine learning engineer",
    target_functions=["machine learning"],
    tier1=["Senior Machine Learning Engineer", "Staff ML Engineer"],
    expected=[],  # whole phrase matches in headline
)

t(
    "Multi-word 'account executive' stripped for AE candidate",
    patterns=["account executive"],
    headline="enterprise account executive 8 years",
    target_functions=["enterprise sales"],
    tier1=["Senior Account Executive", "Account Executive"],
    expected=[],
)

t(
    "Multi-word 'physical therapist' KEPT for nurse",
    patterns=["physical therapist"],
    headline="Registered nurse 10 years",
    target_functions=["nursing"],
    tier1=["RN", "Charge Nurse"],
    expected=["physical therapist"],
)


# ============================================================================
# Category 6: DANGEROUS GENERICS
# ============================================================================
section("Category 6: Dangerous generics always stripped")

DANGEROUS = [
    "senior", "junior", "associate", "principal", "lead", "head", "chief",
    "vp", "officer", "manager", "director", "specialist", "consultant",
    "analyst", "advisor", "executive", "coordinator", "administrator",
]

# Each one should be stripped regardless of haystack content
for word in DANGEROUS:
    t(
        f"Dangerous generic '{word}' always stripped",
        patterns=[word, "engineer"],
        headline="non-overlapping headline",
        target_functions=[],
        tier1=[],
        expected=["engineer"],
    )


# ============================================================================
# Category 7: DEDUPLICATION & NORMALIZATION
# ============================================================================
section("Category 7: Dedup + normalization")

t(
    "Exact duplicates removed",
    patterns=["engineer", "engineer", "engineer", "broker"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "broker"],
)

t(
    "Case-variant duplicates removed",
    patterns=["Engineer", "ENGINEER", "engineer", "broker"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer", "broker"],
)

t(
    "Punctuation-variant duplicates removed",
    patterns=["engineer", "engineer!", "engineer.", "engineer  "],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer"],
)

t(
    "Output preserves first-seen order",
    patterns=["broker", "engineer", "physician", "broker", "engineer"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["broker", "engineer", "physician"],
)


# ============================================================================
# Category 8: CAP ENFORCEMENT (max 20)
# ============================================================================
section("Category 8: Cap at 20")

big_list = [f"role{i}" for i in range(50)]
result = _strip_self_excludes(
    patterns=big_list,
    headline="data scientist",
    target_functions=[],
    tier1_keywords=[],
)
if len(result) == 20:
    _PASS += 1
    print(f"  OK    Output capped at 20 (got {len(result)} from 50 input)")
else:
    _FAIL += 1
    msg = f"FAIL  Output cap broken: expected 20 got {len(result)}"
    _FAILURES.append(msg)
    print(f"  {msg}")


# ============================================================================
# Category 9: HEADLINE / TIER1 / TARGET_FUNCTIONS INTERACTION
# ============================================================================
section("Category 9: Multi-source haystack")

t(
    "Pattern stripped based on tier1 ALONE (headline doesn't mention)",
    patterns=["nurse"],
    headline="career professional with healthcare background",
    target_functions=[],
    tier1=["Charge Nurse", "Nurse Manager"],
    expected=[],
)

t(
    "Pattern stripped based on target_functions ALONE",
    patterns=["physician"],
    headline="medical professional",
    target_functions=["physician practice", "internal medicine"],
    tier1=[],
    expected=[],
)

t(
    "Pattern stripped based on headline ALONE",
    patterns=["lawyer"],
    headline="corporate lawyer 10 years",
    target_functions=[],
    tier1=[],
    expected=[],
)

t(
    "Empty headline + tier1 has signal — tier1 still triggers strip",
    patterns=["engineer"],
    headline="",
    target_functions=[],
    tier1=["Software Engineer"],
    expected=[],
)


# ============================================================================
# Category 10: NEGATIVE TESTS — patterns MUST be kept
# ============================================================================
section("Category 10: Patterns that must be KEPT")

t(
    "Strategy consultant should keep 'engineer' (legitimately not their function)",
    patterns=["engineer", "developer", "data scientist"],
    headline="senior strategy consultant federal AI",
    target_functions=["AI strategy", "policy advisory"],
    tier1=["AI Strategy Consultant", "AI Strategist"],
    expected=["engineer", "developer", "data scientist"],
)

t(
    "Marketing director should keep 'engineer', 'actuary', 'underwriter'",
    patterns=["engineer", "actuary", "underwriter"],
    headline="marketing director B2B SaaS",
    target_functions=["marketing", "demand generation"],
    tier1=["Marketing Director", "Demand Gen Lead"],
    expected=["engineer", "actuary", "underwriter"],
)

t(
    "Accountant should keep 'engineer', 'developer'",
    patterns=["engineer", "developer", "physician"],
    headline="senior accountant CPA Big 4",
    target_functions=["accounting", "audit"],
    tier1=["Senior Accountant", "Audit Senior"],
    expected=["engineer", "developer", "physician"],
)

t(
    "Real estate analyst should keep 'engineer', 'nurse', 'lawyer'",
    patterns=["engineer", "nurse", "lawyer"],
    headline="commercial real estate analyst REPE",
    target_functions=["real estate investment", "underwriting"],
    tier1=["Real Estate Analyst", "Investment Associate"],
    expected=["engineer", "nurse", "lawyer"],
)


# ============================================================================
# Category 11: EXTRA STRESS TESTS - synthetic LLM mistakes
# ============================================================================
section("Category 11: Realistic LLM mistake patterns")

t(
    "LLM mistake: included 'consultant' for AI consultant candidate",
    patterns=["consultant", "developer", "engineer"],
    headline="Senior AI Strategy Consultant Booz Allen",
    target_functions=["AI consulting", "strategy advisory"],
    tier1=["Senior AI Consultant", "AI Strategy Consultant", "AI Strategist"],
    # 'consultant' is dangerous generic → strip. 'developer'/'engineer' kept.
    expected=["developer", "engineer"],
)

t(
    "LLM mistake: included multi-tier seniority as exclusion",
    patterns=["senior", "junior", "principal", "engineer"],
    headline="data scientist",
    target_functions=[],
    tier1=[],
    expected=["engineer"],  # all seniority words stripped
)

t(
    "LLM mistake: included 'sales' for sales candidate",
    patterns=["sales", "engineer", "physician"],
    headline="enterprise sales director SaaS",
    target_functions=["enterprise sales"],
    tier1=["Senior Account Executive", "Sales Director"],
    expected=["engineer", "physician"],
)

t(
    "LLM mistake: included 'finance' for finance candidate",
    patterns=["finance", "financial analyst", "engineer"],
    headline="senior financial analyst FP&A",
    target_functions=["finance", "FP&A"],
    tier1=["Senior Financial Analyst", "Finance Manager"],
    # 'financial analyst' multi-word matches in tier1 → strip
    # 'finance' single word matches in haystack via headline → strip
    # 'engineer' kept
    expected=["engineer"],
)

t(
    "LLM mistake: included full title 'software engineer' as exclusion",
    patterns=["software engineer", "physician"],
    headline="senior software engineer",
    target_functions=["software engineering"],
    tier1=["Senior Software Engineer"],
    expected=["physician"],  # whole phrase matches
)


# ============================================================================
# Category 12: BOUNDARY OF normalization rules
# ============================================================================
section("Category 12: Normalization edge cases")

t(
    "Pattern with multiple spaces normalizes correctly",
    patterns=["software   engineer", "broker"],
    headline="software engineer 5 years",
    target_functions=[],
    tier1=[],
    expected=["broker"],  # normalized 'software engineer' matches headline
)

t(
    "Pattern with newline/tab normalizes correctly",
    patterns=["software\tengineer", "broker"],
    headline="software engineer",
    target_functions=[],
    tier1=[],
    expected=["broker"],
)

t(
    "Pattern keeps + char (c++ developer)",
    patterns=["c++ developer", "broker"],
    headline="python developer 5 years",
    target_functions=["python development"],
    tier1=["Python Developer"],
    # 'c++ developer' — 'developer' is in haystack so 'c++ developer' phrase
    # alone won't match exact phrase 'c++ developer' anywhere (haystack has
    # 'python developer'). Multi-word patterns require whole-phrase match.
    # So 'c++ developer' is KEPT (kept since exact phrase not in haystack).
    # But broker is also kept.
    expected=["c++ developer", "broker"],
)


# ============================================================================
# Category 13: SYNONYM EXPANSION BEHAVIOR
# ============================================================================
section("Category 13: Synonym expansion")

# engineer ↔ developer
t(
    "Synonym: software engineer profile strips 'developer'",
    patterns=["developer"],
    headline="senior software engineer",
    target_functions=["software engineering"],
    tier1=["Senior Software Engineer"],
    expected=[],
)

t(
    "Synonym: developer profile strips 'engineer'",
    patterns=["engineer"],
    headline="senior python developer 5 years",
    target_functions=["python development"],
    tier1=["Senior Python Developer", "Backend Developer"],
    expected=[],
)

# lawyer ↔ attorney ↔ counsel
t(
    "Synonym: attorney headline strips 'lawyer' pattern",
    patterns=["lawyer"],
    headline="corporate attorney AmLaw 100",
    target_functions=["corporate law"],
    tier1=["Corporate Attorney"],
    expected=[],
)

t(
    "Synonym: lawyer headline strips 'attorney' pattern",
    patterns=["attorney"],
    headline="senior lawyer 10 years",
    target_functions=["legal practice"],
    tier1=["Senior Lawyer"],
    expected=[],
)

t(
    "Synonym: corporate counsel headline strips both 'lawyer' and 'attorney'",
    patterns=["lawyer", "attorney"],
    headline="senior corporate counsel",
    target_functions=["corporate counsel"],
    tier1=["Corporate Counsel"],
    expected=[],
)

# nurse ↔ rn
t(
    "Synonym: 'rn' headline strips 'nurse' pattern",
    patterns=["nurse"],
    headline="charge rn icu 10 years",
    target_functions=["critical care"],
    tier1=["Charge RN", "ICU RN"],
    expected=[],
)

# physician ↔ doctor
t(
    "Synonym: doctor headline strips 'physician' pattern",
    patterns=["physician"],
    headline="emergency room doctor 8 years",
    target_functions=["emergency medicine"],
    tier1=["ER Doctor", "Senior Doctor"],
    expected=[],
)

# veterinarian ↔ vet
t(
    "Synonym: 'vet' headline strips 'veterinarian' pattern",
    patterns=["veterinarian"],
    headline="small animal vet dvm 10 years",
    target_functions=["veterinary medicine"],
    tier1=["Senior Vet"],
    expected=[],
)

# teacher ↔ educator ↔ instructor
t(
    "Synonym: 'educator' headline strips 'teacher' pattern",
    patterns=["teacher"],
    headline="senior science educator k-12",
    target_functions=["science education"],
    tier1=["Senior Educator"],
    expected=[],
)

# accountant ↔ cpa
t(
    "Synonym: 'cpa' headline strips 'accountant' pattern",
    patterns=["accountant"],
    headline="senior cpa big 4 audit",
    target_functions=["audit"],
    tier1=["Senior CPA"],
    expected=[],
)

# chef ↔ cook
t(
    "Synonym: 'chef' headline strips 'cook' pattern",
    patterns=["cook"],
    headline="executive chef fine dining",
    target_functions=["culinary leadership"],
    tier1=["Executive Chef"],
    expected=[],
)

# scientist ↔ researcher
t(
    "Synonym: scientist headline strips 'researcher' pattern",
    patterns=["researcher"],
    headline="senior data scientist ml platforms",
    target_functions=["data science"],
    tier1=["Senior Data Scientist"],
    expected=[],
)

# Negative: synonyms must NOT cross-pollinate to unrelated functions
t(
    "Synonym safety: marketing director keeps 'developer' (no engineer in haystack)",
    patterns=["developer", "engineer"],
    headline="senior marketing director B2B",
    target_functions=["marketing"],
    tier1=["Senior Marketing Director"],
    expected=["developer", "engineer"],
)

t(
    "Synonym safety: CRE analyst keeps 'attorney' even though law-adjacent",
    patterns=["attorney"],
    headline="senior commercial real estate analyst",
    target_functions=["real estate investment"],
    tier1=["Senior CRE Analyst"],
    expected=["attorney"],
)

# Synonym MUST NOT trigger on substring matches
t(
    "Synonym safety: 'engineer' in 'engineering' does NOT trigger (no whole-word)",
    patterns=["developer"],
    headline="engineering management consultant",
    target_functions=[],
    tier1=[],
    # 'engineer' is not a whole word in 'engineering'. Synonym expansion
    # only happens when 'engineer' is a whole word in haystack.
    expected=["developer"],
)


# ============================================================================
# Category 14: REGRESSION GUARDS — these MUST NOT change behavior
# ============================================================================
section("Category 14: Regression guards")

# A strategy consultant has the function word "strategy" but pattern "strategy"
# isn't in our dangerous-generics set. If LLM adds "strategy" — should keep.
# (Real-world: candidate has 'strategy' in headline, pattern 'strategy' would
#  whole-word match → stripped. That's correct because excluding "strategy"
#  would drop "AI Strategy Consultant" tier1.)
t(
    "Strategy consultant excluding 'strategy' is stripped (would drop tier1)",
    patterns=["strategy", "engineer"],
    headline="senior AI strategy consultant",
    target_functions=["AI strategy"],
    tier1=["AI Strategy Consultant", "AI Strategist"],
    expected=["engineer"],
)

# Operations candidate excluding "operations"
t(
    "Operations candidate excluding 'operations' is stripped",
    patterns=["operations", "engineer"],
    headline="senior operations manager retail",
    target_functions=["operations"],
    tier1=["Senior Operations Manager"],
    expected=["engineer"],
)

# Adversarial: pattern is exactly "engineer" but NO engineer in haystack
t(
    "Pure-non-engineer profile keeps 'engineer'",
    patterns=["engineer", "developer"],
    headline="senior brand marketing director CPG",
    target_functions=["brand marketing"],
    tier1=["Brand Marketing Director", "Senior Brand Manager"],
    # 'engineer' not in haystack, no synonym matches → both kept
    expected=["engineer", "developer"],
)


# ============================================================================
# RESULTS
# ============================================================================

total = _PASS + _FAIL
print(f"\n{'='*60}")
print(f"RESULT: {_PASS}/{total} passed, {_FAIL} failed")
print(f"{'='*60}")

if _FAILURES:
    print("\nFailures:")
    for f in _FAILURES:
        print(f)

sys.exit(0 if _FAIL == 0 else 1)

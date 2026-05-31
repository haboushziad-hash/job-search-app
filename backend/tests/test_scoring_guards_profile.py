"""Profile-aware guard tests (v0.3.25).

The P0-3 excluded-work guard must adapt to WHO the candidate is:
  - An AI-strategy consultant (Ziad) excludes hands-on engineering AND sales.
  - A software engineer TARGETS engineering -> those roles must NOT be demoted.
  - A sales AE TARGETS sales -> those roles must NOT be demoted.
  - But each still excludes the OTHER category (a SWE doesn't want sales; an AE
    doesn't want hands-on dev), and a profile-less call excludes both (legacy).

Run:
  backend/venv/Scripts/python.exe backend/tests/test_scoring_guards_profile.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import Role, Tier, CandidateProfile
from backend.scoring.orchestrator import _apply_scoring_guards
from backend.scoring._scoring_guards import (
    profile_targets_engineering, profile_targets_sales,
)

# Capturable (>=250 char) JDs that reach P0-3.
ENG_JD = (
    "Senior Software Engineer. You will own full-stack development across our "
    "platform, writing production code in React and Node.js every day. Build "
    "and deliver scalable services and features. Strong engineering best "
    "practices and modern frameworks expected. This is a hands-on role."
)
SALES_JD = (
    "Enterprise Account Executive. You will own a sales quota and drive pipeline "
    "generation across your territory. Manage your book of business end to end, "
    "lead pre-sales discovery and objection handling, run demos to prospects, and "
    "close deals to drive net new revenue. Partner closely with sales enablement "
    "to ramp reps and improve seller productivity. Exceed revenue targets."
)

ZIAD = CandidateProfile(headline="Senior AI Strategy & Enablement Consultant",
                        target_functions=["AI Strategy", "AI Enablement", "Change Management"])
SWE = CandidateProfile(headline="Senior Software Engineer",
                       target_functions=["Software Engineering", "Backend Development"])
AE = CandidateProfile(headline="Enterprise Account Executive",
                      target_functions=["Sales", "Account Management"])


def _scored(jd: str, profile, score: int = 90) -> Role:
    r = Role(job_title="X", company="Y", job_description_full=jd)
    r.final_score = score
    r.final_tier = Tier.STRONG
    _apply_scoring_guards(r, profile)
    return r


# --- profile target detection -------------------------------------------------

def test_target_detection() -> None:
    assert profile_targets_engineering(SWE) is True
    assert profile_targets_engineering(ZIAD) is False   # has AI funcs, not eng
    assert profile_targets_engineering(AE) is False
    assert profile_targets_sales(AE) is True
    assert profile_targets_sales(ZIAD) is False
    assert profile_targets_sales(SWE) is False
    assert profile_targets_engineering(None) is False    # no profile -> no target


# --- engineering JD -----------------------------------------------------------

def test_eng_role_demoted_for_consultant() -> None:
    assert _scored(ENG_JD, ZIAD).final_score == 46

def test_eng_role_kept_for_software_engineer() -> None:
    # THE generalization fix: a SWE's ideal eng role must survive.
    assert _scored(ENG_JD, SWE).final_score == 90

def test_eng_role_demoted_for_sales_ae() -> None:
    assert _scored(ENG_JD, AE).final_score == 46


# --- sales JD -----------------------------------------------------------------

def test_sales_role_demoted_for_consultant() -> None:
    assert _scored(SALES_JD, ZIAD).final_score == 46

def test_sales_role_demoted_for_software_engineer() -> None:
    # A SWE targets eng, NOT sales -> sales still excluded for them.
    assert _scored(SALES_JD, SWE).final_score == 46

def test_sales_role_kept_for_sales_ae() -> None:
    assert _scored(SALES_JD, AE).final_score == 90


# --- legacy / no-profile ------------------------------------------------------

def test_no_profile_excludes_both() -> None:
    # Backward-compat: a profile-less call behaves like the original guard.
    assert _scored(ENG_JD, None).final_score == 46
    assert _scored(SALES_JD, None).final_score == 46


def main() -> int:
    tests = [
        test_target_detection,
        test_eng_role_demoted_for_consultant,
        test_eng_role_kept_for_software_engineer,
        test_eng_role_demoted_for_sales_ae,
        test_sales_role_demoted_for_consultant,
        test_sales_role_demoted_for_software_engineer,
        test_sales_role_kept_for_sales_ae,
        test_no_profile_excludes_both,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print()
    print(f"=== {len(tests) - failed}/{len(tests)} passed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

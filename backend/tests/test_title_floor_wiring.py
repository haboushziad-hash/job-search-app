"""Regression test for the title_floor → stage 2/3 wiring (v0.3.4).

Catches the exact bug found in v0.3.4 testing: stage2_triage and
stage3_deep_eval were calling apply_floor_to_score with role.title, but the
Role model has role.job_title, not role.title. Pydantic raised
AttributeError at runtime, crashing every search.

This test invokes apply_floor_to_score with the EXACT signature used in
both stage 2 and stage 3, plus asserts the Role model's field names.

Run:
  cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
  backend/venv/Scripts/python.exe -m backend.tests.test_title_floor_wiring
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import Role, CandidateProfile
from backend.scoring.title_floor import (
    apply_floor_to_score, compute_title_floor,
)


def test_role_field_name_is_job_title_not_title() -> None:
    """The Role model exposes job_title, NOT title. If this changes, the
    title_floor wiring in stage2_triage.py and stage3_deep_eval.py also
    needs updating."""
    role = Role(job_title="Test", company="Test Co", source="test")
    # Confirm correct field exists
    assert role.job_title == "Test"
    # Confirm wrong field name DOES raise (proves the regression vector)
    try:
        _ = role.title
        raise AssertionError(
            "Role.title attribute exists — this regression test is now obsolete. "
            "Update stage2_triage.py and stage3_deep_eval.py to use whichever "
            "name the Role model now exposes, then update this test."
        )
    except AttributeError:
        pass  # expected


def test_stage2_signature_works_with_real_role() -> None:
    """Reproduce the EXACT wiring used in stage2_triage.stage2_triage():
    after _score_one populates role.stage2_score, the post-LLM floor pass
    calls apply_floor_to_score with role.job_title."""
    role = Role(
        job_title="Marketing Operations Manager",
        company="Test Co",
        source="test",
        stage2_score=47,
    )
    profile = CandidateProfile(
        headline="Operations and Strategic Support Leader",
        target_functions=["Business Operations", "Lab Operations"],
    )

    # This signature MUST match what's in stage2_triage.py and
    # stage3_deep_eval.py. Any field-name drift breaks every search.
    new_score, tag = apply_floor_to_score(
        score=role.stage2_score,
        role_title=role.job_title,
        candidate_headline=profile.headline,
        candidate_target_functions=profile.target_functions,
    )

    assert new_score == 55, f"expected floor at 55, got {new_score}"
    assert tag is not None and "title-floor" in tag, f"expected tag, got {tag}"


def test_no_floor_when_score_above() -> None:
    """When LLM scored above the floor, don't adjust."""
    role = Role(
        job_title="Marketing Operations Manager",
        company="Test Co",
        source="test",
        stage2_score=78,  # above the 55 floor
    )
    profile = CandidateProfile(
        headline="Operations and Strategic Support Leader",
        target_functions=["Business Operations"],
    )

    new_score, tag = apply_floor_to_score(
        score=role.stage2_score,
        role_title=role.job_title,
        candidate_headline=profile.headline,
        candidate_target_functions=profile.target_functions,
    )

    assert new_score == 78, "score above floor should be unchanged"
    assert tag is None, "no tag should be emitted when no adjustment"


def test_three_word_overlap_floors_at_70() -> None:
    role = Role(
        job_title="AI Strategy Consultant",
        company="Test Co",
        source="test",
        stage2_score=42,  # well below 70
    )
    profile = CandidateProfile(
        headline="AI Strategy and Enablement Consultant with 7 years",
        target_functions=["AI Strategy", "AI Enablement", "AI Adoption"],
    )

    new_score, tag = apply_floor_to_score(
        score=role.stage2_score,
        role_title=role.job_title,
        candidate_headline=profile.headline,
        candidate_target_functions=profile.target_functions,
    )

    assert new_score == 70, f"expected 3-word floor 70, got {new_score}"
    assert tag is not None


def main() -> int:
    tests = [
        test_role_field_name_is_job_title_not_title,
        test_stage2_signature_works_with_real_role,
        test_no_floor_when_score_above,
        test_three_word_overlap_floors_at_70,
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

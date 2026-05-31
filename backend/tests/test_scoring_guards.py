"""Unit tests for the v0.3.24 anti-inflation scoring guards (FIX 29).

Covers the two deterministic guard mechanisms found by the 2026-05-30
adversarial scoring audit (systematic one-directional over-scoring):

  P0-1  jd_is_capturable()        -> stub / redirect / bot-wall / too-short
                                     JD bodies can't support a confident score.
  P0-3  scan_excluded_body_signals() -> hands-on engineering / sales-GTM work
                                     in the JD BODY that evaded the TITLE filter.

and the orchestrator glue _apply_scoring_guards() that applies the caps
(P0-1 -> MAYBE/55, P0-3 -> STRETCH/46) AFTER the base final score.

The single most important regression here is the CANARY: a legitimate AI
Enablement advisory role (the Applied Systems "AI Enablement Services Lead"
81->91 promotion) must NOT be capped by either guard.

Run:
  cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
  backend/venv/Scripts/python.exe -m backend.tests.test_scoring_guards
or simply:
  backend/venv/Scripts/python.exe backend/tests/test_scoring_guards.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import Role, Tier, score_to_tier
from backend.scoring._scoring_guards import (
    jd_is_capturable,
    scan_excluded_body_signals,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# A legitimate, clean AI-enablement ADVISORY JD. No coding, no selling.
# This is the canary: guards must leave it untouched. ~440 chars.
CANARY_JD = (
    "Lead our enterprise AI enablement strategy. Define AI governance "
    "frameworks, responsible-AI policies, and adoption playbooks. Partner "
    "with business stakeholders and senior leadership to drive change "
    "management and training programs for GenAI rollout across the "
    "organization. Measure adoption, outcomes, and risk. Advise executives "
    "on prioritization and capability building. No coding required."
)

# A hands-on engineering JD (the title would say "Engineer"/"Consultant" but
# the BODY is excluded build work). Capturable, P0-3 should fire.
ENG_JD = (
    "Senior Software Engineer. You will own full-stack development across "
    "our platform, writing production code in React and Node.js every day. "
    "Build and deliver scalable services and features with minimal "
    "oversight. Strong engineering best practices and modern frameworks "
    "expected. This is a hands-on software engineering role."
)

# Stub bodies (P0-1 should treat all as uncapturable).
STUB_SHORT = "Please enable JavaScript to view this job posting."
STUB_MEDIUM = (
    "We are excited to share this opportunity with qualified candidates. " * 5
    + "Please enable JavaScript to continue browsing this listing."
)
# A real, long JD that merely MENTIONS a marker word in passing (>=1200 chars
# -> the marker must NOT kill it).
LONG_WITH_MARKER = (
    "We are hiring a Senior AI Enablement Lead to drive enterprise adoption "
    "and governance. " * 18
    + " A captcha may appear before you apply."
)
SHORT_REAL = "Senior AI Enablement Consultant. Remote. Apply now."


def _make_role(score, jd, analysis=None) -> Role:
    role = Role(job_title="Test Role", company="Test Co", job_description_full=jd)
    role.final_score = score
    role.final_tier = score_to_tier(score) if score is not None else None
    if analysis is not None:
        role.stage3_analysis = analysis
    return role


def _apply_guards(role: Role) -> None:
    # Lazy import so the helper tests below still run even if the orchestrator
    # module's heavier imports ever become a problem in a bare test env.
    from backend.scoring.orchestrator import _apply_scoring_guards
    _apply_scoring_guards(role)


# ---------------------------------------------------------------------------
# P0-1: jd_is_capturable
# ---------------------------------------------------------------------------

def test_capturable_none_and_empty() -> None:
    assert jd_is_capturable(None) is False
    assert jd_is_capturable("") is False
    assert jd_is_capturable("        ") is False


def test_capturable_too_short() -> None:
    assert jd_is_capturable(SHORT_REAL) is False           # real but < 250 chars
    assert jd_is_capturable("x" * 249) is False            # boundary: below
    assert jd_is_capturable("x" * 250) is True             # boundary: at min, no marker


def test_capturable_short_stub_marker() -> None:
    assert jd_is_capturable(STUB_SHORT) is False


def test_capturable_medium_body_with_marker() -> None:
    # 250 <= len < 1200 AND contains a stub marker -> uncapturable.
    assert 250 <= len(STUB_MEDIUM) < 1200, "fixture length precondition"
    assert jd_is_capturable(STUB_MEDIUM) is False


def test_capturable_long_real_body_tolerates_marker() -> None:
    # >= 1200 chars: a passing "captcha" mention must NOT kill a real JD.
    assert len(LONG_WITH_MARKER) >= 1200, "fixture length precondition"
    assert jd_is_capturable(LONG_WITH_MARKER) is True


def test_capturable_clean_real_jd() -> None:
    assert jd_is_capturable(CANARY_JD) is True
    assert jd_is_capturable(ENG_JD) is True


# ---------------------------------------------------------------------------
# P0-3: scan_excluded_body_signals
# ---------------------------------------------------------------------------

def test_scan_none_and_empty() -> None:
    for jd in (None, ""):
        out = scan_excluded_body_signals(jd)
        assert out["strong"] is False
        assert out["engineering"] == [] and out["sales"] == []


def test_scan_clean_canary_not_flagged() -> None:
    out = scan_excluded_body_signals(CANARY_JD)
    assert out["strong"] is False, f"canary wrongly flagged: {out}"


def test_scan_engineering_high_single_hit_fires() -> None:
    out = scan_excluded_body_signals("You will own full-stack development here.")
    assert out["strong"] is True
    assert out["engineering"], "expected an engineering hit"


def test_scan_engineering_generic_needs_two() -> None:
    one = scan_excluded_body_signals("We value strong software development.")
    assert one["strong"] is False, "a single GENERIC eng phrase must not fire"
    two = scan_excluded_body_signals(
        "We value software development and engineering best practices."
    )
    assert two["strong"] is True, "two GENERIC eng phrases should fire"


def test_scan_sales_high_single_hit_fires() -> None:
    out = scan_excluded_body_signals("You will carry a quota and own pipeline generation.")
    assert out["strong"] is True
    assert out["sales"], "expected a sales hit"


def test_scan_ai_enablement_false_friend_not_flagged() -> None:
    # "AI enablement" is the candidate's TARGET; it must not collide with the
    # "sales enablement" exclusion on its own.
    out = scan_excluded_body_signals(
        "Drive AI enablement and enterprise adoption. Build an enablement program."
    )
    assert out["strong"] is False, f"AI-enablement false friend tripped: {out}"


def test_scan_sales_false_friend_with_corroboration_fires() -> None:
    # sales enablement + coaching reps + closing deals = real sales work.
    out = scan_excluded_body_signals(
        "Lead sales enablement, coach reps, and help them close deals."
    )
    assert out["strong"] is True


def test_scan_only_first_4500_chars() -> None:
    # A decisive marker beyond the 4500-char window must not be detected.
    jd = "All work is collaborative and supportive. " * 120  # ~5040 clean chars
    jd += "You must hit your sales quota every quarter."
    assert len(jd) > 4500, "fixture length precondition"
    assert scan_excluded_body_signals(jd)["strong"] is False


# ---------------------------------------------------------------------------
# Orchestrator glue: _apply_scoring_guards
# ---------------------------------------------------------------------------

def test_guard_caps_stub_jd_to_maybe() -> None:
    role = _make_role(91, STUB_SHORT)          # hallucinated-high on a stub
    _apply_guards(role)
    assert role.final_score == 55, role.final_score
    assert role.final_tier == Tier.MAYBE
    assert "jd-not-captured" in (role.stage3_application_strategy or "")


def test_guard_caps_excluded_engineering_to_stretch() -> None:
    role = _make_role(90, ENG_JD)
    _apply_guards(role)
    assert role.final_score == 46, role.final_score
    assert role.final_tier == Tier.STRETCH
    assert "excluded-engineering-work" in (role.stage3_application_strategy or "")


def test_guard_excluded_flag_goes_to_analysis_when_present() -> None:
    role = _make_role(90, ENG_JD, analysis="Original Stage 3 analysis.")
    _apply_guards(role)
    assert role.final_score == 46
    assert "excluded-engineering-work" in (role.stage3_analysis or "")
    assert "Original Stage 3 analysis." in (role.stage3_analysis or "")


def test_guard_canary_stays_strong() -> None:
    # THE regression check: a legit AI-enablement advisory role at 91 must
    # survive both guards untouched.
    role = _make_role(91, CANARY_JD)
    _apply_guards(role)
    assert role.final_score == 91, f"canary was capped to {role.final_score}"
    assert role.final_tier == Tier.STRONG


def test_guard_clean_good_role_unchanged() -> None:
    role = _make_role(80, CANARY_JD)
    _apply_guards(role)
    assert role.final_score == 80
    assert role.final_tier == Tier.GOOD


def test_guard_stub_below_cap_flagged_not_raised() -> None:
    # Cap only LOWERS: a stub already at 50 keeps 50 but still gets flagged.
    role = _make_role(50, STUB_SHORT)
    _apply_guards(role)
    assert role.final_score == 50, "cap must never raise a score"
    assert "jd-not-captured" in (role.stage3_application_strategy or "")


def test_guard_noop_when_no_score() -> None:
    role = _make_role(None, STUB_SHORT)
    _apply_guards(role)                        # must not raise
    assert role.final_score is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_capturable_none_and_empty,
        test_capturable_too_short,
        test_capturable_short_stub_marker,
        test_capturable_medium_body_with_marker,
        test_capturable_long_real_body_tolerates_marker,
        test_capturable_clean_real_jd,
        test_scan_none_and_empty,
        test_scan_clean_canary_not_flagged,
        test_scan_engineering_high_single_hit_fires,
        test_scan_engineering_generic_needs_two,
        test_scan_sales_high_single_hit_fires,
        test_scan_ai_enablement_false_friend_not_flagged,
        test_scan_sales_false_friend_with_corroboration_fires,
        test_scan_only_first_4500_chars,
        test_guard_caps_stub_jd_to_maybe,
        test_guard_caps_excluded_engineering_to_stretch,
        test_guard_excluded_flag_goes_to_analysis_when_present,
        test_guard_canary_stays_strong,
        test_guard_clean_good_role_unchanged,
        test_guard_stub_below_cap_flagged_not_raised,
        test_guard_noop_when_no_score,
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

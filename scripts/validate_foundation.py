"""Validate foundation pieces after Phase 1 work."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 1. Imports
from backend.api import app
from backend.runner import run_search, _has_usable_jd
from backend.storage import set_audit_folder, get_archive
from backend.models import CandidateProfile, Role
from backend.profile.builder import build_profile_from_resumes
from backend.scoring.orchestrator import score_roles, _backpop_salary_from_reasoning
from backend.filter.hard_filters import passes_title_function_match
from backend.scraper.greenhouse import GREENHOUSE_COMPANIES
from backend.scraper.lever import LEVER_COMPANIES
from backend.scraper.ashby import ASHBY_COMPANIES
from backend.scraper.workday import WORKDAY_TENANTS

print("=== Imports OK ===")
print(f"Greenhouse: {len(GREENHOUSE_COMPANIES)}")
print(f"Lever:      {len(LEVER_COMPANIES)}")
print(f"Ashby:      {len(ASHBY_COMPANIES)}")
print(f"Workday:    {len(WORKDAY_TENANTS)}")
print(f"TOTAL:      {len(GREENHOUSE_COMPANIES) + len(LEVER_COMPANIES) + len(ASHBY_COMPANIES) + len(WORKDAY_TENANTS)}")
print()

# 2. CandidateProfile new fields
print("=== CandidateProfile fields ===")
p = CandidateProfile(
    headline='test',
    excluded_title_patterns=['engineer'],
    negative_signals=['avoid X'],
    profile_tags=['consulting', 'ai', 'enablement'],
)
assert len(p.profile_tags) == 3, p.profile_tags
print(f"  profile_tags: {p.profile_tags}  OK")
print()

# 3. Salary back-population
print("=== Salary back-population ===")
class StubRole:
    def __init__(self):
        self.salary_min = None
        self.salary_max = None
        self.salary_text = None
        self.stage3_analysis = "The role pays $130,000 - $165,000 depending on location."
        self.stage2_reasoning = ""
r = StubRole()
_backpop_salary_from_reasoning(r)
print(f"  min=${r.salary_min}  max=${r.salary_max}")
assert r.salary_min == 130000 and r.salary_max == 165000, (r.salary_min, r.salary_max)
print("  PASS")
print()

# 4. JD usable metric
print("=== JD usable metric ===")
class FakeRole:
    def __init__(self, jd):
        self.job_description_full = jd
assert _has_usable_jd(FakeRole("a" * 600)) is True
assert _has_usable_jd(FakeRole("short")) is False
assert _has_usable_jd(FakeRole("\x01\x02\x03" * 200)) is False  # binary garbage
print("  PASS")
print()

# 5. Title-function filter (dynamic)
print("=== Dynamic title filter ===")
ziad = CandidateProfile(
    headline='AI Strategy Consultant',
    target_functions=['AI Strategy', 'Enablement'],
    technical_skills=['Excel', 'Copilot'],
    excluded_title_patterns=['architect', 'engineer', 'data scientist'],
    negative_signals=[],
)
def ok(title, expected):
    role = Role(job_title=title, company='X')
    actual, _ = passes_title_function_match(role, profile=ziad)
    return actual == expected
assert ok('AI Architect', False)
assert ok('AI Engagement Manager', True)
assert ok('Software Engineer', False)
assert ok('AI Enablement Lead', True)
print("  PASS")
print()

# 6. Archive write/read
print("=== Archive round-trip ===")
with tempfile.TemporaryDirectory() as t:
    arc = set_audit_folder(t)
    role_id = arc.upsert_role(company='Acme', job_title='AI Lead', job_url='https://acme.com/job/1')
    arc.set_application_status(role_id=role_id, status='applied')
    arc.add_market_contribution(
        company='Acme', job_title='AI Lead', score=88,
        profile_tags=['ai', 'consulting']
    )
    apps = arc.applied_role_keys()
    contribs = arc.get_self_contributions()
    assert ('acme', 'ai lead') in apps, apps
    assert len(contribs) == 1, len(contribs)
    print(f"  applied: {apps}")
    print(f"  contribs: {len(contribs)} entry")
    arc.close()
print("  PASS")
print()

print("=== ALL FOUNDATION TESTS PASS ===")

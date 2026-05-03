"""Unit test for the dynamic title-pattern hard filter."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.models import CandidateProfile, Role
from backend.filter.hard_filters import passes_title_function_match

print('=== ZIAD: AI strategy consultant, no engineering ===')
ziad = CandidateProfile(
    headline='AI Strategy & Enablement Consultant',
    target_functions=['AI Strategy', 'AI Enablement', 'AI Adoption'],
    technical_skills=['NIST AI RMF', 'Microsoft Copilot', 'Excel'],
    excluded_title_patterns=[
        'architect', 'engineer', 'developer', 'data scientist',
        'ml researcher', 'devops',
    ],
    negative_signals=['hands-on ML engineering', 'data engineering'],
)

ziad_tests = [
    ('Anthropic', 'Applied AI Architect, Federal Civilian', False),
    ('Anthropic', 'Applied AI Engineer, Enterprise Tech', False),
    ('Anthropic', 'Compliance Governance & Oversight Lead', True),
    ('Anthropic', 'Customer Success Manager, Scaled', True),
    ('Snorkel AI', 'Engagement Manager - AI Solutions', True),
    ('Snorkel AI', 'Staff Applied AI Engineer - Pre-Sales', False),
    ('Airtable', 'Senior Solutions Architect', False),
    ('Acme', 'AI Enablement Lead', True),
    ('Acme', 'Data Scientist, Senior', False),
    ('Acme', 'Software Developer', False),
    ('Acme', 'AI Engagement Manager', True),
    ('Acme', 'Senior Engineer, Backend Platform', False),
    ('Acme', 'Manager, Engineering Operations', True),  # mgr OF eng, not eng itself
]
passed = failed = 0
for company, title, expected in ziad_tests:
    role = Role(job_title=title, company=company)
    actual, reason = passes_title_function_match(role, profile=ziad)
    ok = (actual == expected)
    mark = 'OK  ' if ok else 'FAIL'
    print(f'  [{mark}] {title}  ->  passes={actual}  reason={reason}')
    if ok: passed += 1
    else: failed += 1
print(f'  Result: {passed}/{passed+failed}')

print('\n=== ENGINEER: software engineer, no sales bg ===')
engineer = CandidateProfile(
    headline='Senior Software Engineer with 8 yrs Python/Go',
    target_functions=['Backend', 'Distributed Systems', 'Engineering Mgmt'],
    technical_skills=['Python', 'Go', 'Kubernetes', 'AWS', 'PostgreSQL'],
    excluded_title_patterns=[
        'account executive', 'sdr', 'bdr', 'sales manager',
        'sales representative', 'business development',
    ],
    negative_signals=['quota-carrying', 'cold outreach'],
)
eng_tests = [
    ('Acme', 'Senior Software Engineer', True),
    ('Acme', 'Staff Backend Engineer', True),
    ('Acme', 'Engineering Manager, Platform', True),
    ('Acme', 'Account Executive, Enterprise', False),
    ('Acme', 'SDR, US East', False),
    ('Acme', 'Senior Sales Manager', False),
    ('Acme', 'Sales Engineer', True),  # has 'sales' AND 'engineer' but excluded patterns are 'sales manager' (needs both) and 'engineer' isn't in eng's exclusions
    ('Acme', 'Business Development Manager', False),
    ('Acme', 'Customer Success Manager', True),  # not in his exclusions
]
passed = failed = 0
for company, title, expected in eng_tests:
    role = Role(job_title=title, company=company)
    actual, reason = passes_title_function_match(role, profile=engineer)
    ok = (actual == expected)
    mark = 'OK  ' if ok else 'FAIL'
    print(f'  [{mark}] {title}  ->  passes={actual}  reason={reason}')
    if ok: passed += 1
    else: failed += 1
print(f'  Result: {passed}/{passed+failed}')

print('\n=== ZACH: business admin senior, no exclusions specified ===')
zach = CandidateProfile(
    headline='Business Administration senior',
    target_functions=['Operations', 'Consulting', 'Finance', 'PM'],
    technical_skills=['Excel', 'Salesforce'],
    excluded_title_patterns=['engineer', 'developer', 'physician', 'rn'],
    negative_signals=[],
)
zach_tests = [
    ('Acme', 'Software Engineer', False),
    ('Acme', 'Account Manager', True),
    ('Acme', 'Operations Manager', True),
    ('Acme', 'Project Coordinator', True),
    ('Acme', 'Backend Developer', False),
    ('Acme', 'Engineering Operations Lead', True),  # not "engineer" alone — wait, has "engineering" which contains "engineer"... need to test
]
passed = failed = 0
for company, title, expected in zach_tests:
    role = Role(job_title=title, company=company)
    actual, reason = passes_title_function_match(role, profile=zach)
    ok = (actual == expected)
    mark = 'OK  ' if ok else 'FAIL'
    print(f'  [{mark}] {title}  ->  passes={actual}  reason={reason}')
    if ok: passed += 1
    else: failed += 1
print(f'  Result: {passed}/{passed+failed}')

print('\n=== TRUE GENERALIST: empty exclusion list — nothing should drop ===')
gen = CandidateProfile(
    headline='Open to anything',
    excluded_title_patterns=[],
    negative_signals=[],
)
test = Role(job_title='Senior Software Engineer', company='Acme')
ok, reason = passes_title_function_match(test, profile=gen)
print(f'  Generalist + Software Engineer: passes={ok} reason={reason} (expect True)')

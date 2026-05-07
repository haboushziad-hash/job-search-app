# Profile Builder Fixture Suite

Permanent regression tests for the profile-builder pipeline (multi-LLM
keyword/profile generation). Each scenario feeds a synthetic resume +
preferences through the FULL `build_profile_from_resumes()` pipeline
(3× Gemini Pro + 1× Opus + 1× Opus synthesis) and asserts properties
about the generated profile.

## Why this exists

The biggest source of bad job results is upstream: the profile builder
generates wrong target_functions / target_industries / Tier 1 keywords
from a resume, and every downstream stage (scraping → embedding → triage →
deep eval) inherits that mistake. By the time a tester sees the output,
the bug looks like "scoring is broken" but it's really "we built the
wrong profile from your resume."

These fixtures stress the builder against the failure modes we've actually
observed in tester runs:
  - Non-AI candidate with Copilot in skills section → tool generates AI keywords
  - Healthcare consultant with one real estate client → tool adds Real Estate
  - Generalist new grad with internships in 3 fields → tool spreads Tier 1 thin

## Adding a new scenario

```
tests/profile_fixtures/scenarios/NN_short_name/
  resume.txt        # the synthetic resume text (realistic enough)
  scenario.yaml     # preferences + freeform context + assertions
```

`scenario.yaml` schema:

```yaml
description: One-line human-readable description
user_preferences:
  salary_minimum: 100000
  acceptable_locations: ["Richmond VA"]
  freeform_context: "What the user typed into the freeform box"
expected:
  # All keys are optional — only specify what you care about for THIS scenario
  must_not_contain_in_target_functions: ["AI Strategy", "AI Adoption"]
  must_not_contain_in_target_industries: ["Technology", "AI"]
  must_not_contain_in_keywords_t1: ["AI Strategy Consultant"]
  must_contain_in_target_functions_themes: ["Operations", "Quality"]
  must_contain_in_target_industries_themes: ["Life Sciences", "Healthcare"]
  must_contain_in_keywords_t1_themes: ["Operations Manager", "QA Specialist"]
  headline_must_not_start_with: "AI"
  headline_must_contain_one_of: ["Operations", "Lab", "Quality"]
```

Theme assertions are case-insensitive substring checks across the array.

## Running

```bash
# All scenarios (sequential — calls cost ~$0.50-0.80 each in LLM fees)
backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py

# Single scenario
backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py --only 01_operations_qa_lab

# Show generated profiles even on pass
backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py --verbose
```

## Caveats

- LLM nondeterminism: a scenario can flake. If a scenario fails once,
  re-run before treating it as a real regression. If it fails 2/3 runs,
  that's a real regression.
- Cost: each scenario = full profile build = ~$0.30-0.50 in API fees.
  Don't run on every commit; run before each release.
- Scenarios are deliberately stylized to push specific failure modes.
  Real resumes have more complexity, but the builder should still respect
  these rules in real cases.

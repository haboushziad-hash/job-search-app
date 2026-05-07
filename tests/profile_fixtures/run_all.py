"""Profile builder regression test runner.

Iterates over scenarios in tests/profile_fixtures/scenarios/, feeds each
synthetic resume through the FULL multi-LLM profile-build pipeline, and
asserts properties of the generated profile.

This is a real end-to-end test (real LLM calls, real cost ~$0.30-0.50
per scenario). Use before each release to catch regressions in profile
generation behavior — particularly the failure modes we've observed in
tester runs (AI over-promotion for non-AI candidates, industry over-
reach from incidental mentions, etc.).

Usage:
  cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
  backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py
  backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py --only 01_operations_qa_lab
  backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py --verbose
  backend/venv/Scripts/python.exe tests/profile_fixtures/run_all.py --no-llm  # skip LLM, just lint scenarios
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Path bootstrap so backend/ imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Lightweight YAML reader — the project doesn't ship PyYAML in venv reliably
# across all environments. We accept either YAML (if available) or a tiny
# subset parser inline. The fixture files use a simple subset so this works.
try:
    import yaml  # type: ignore
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


SCENARIOS_DIR = Path(__file__).parent / "scenarios"
CACHE_DIR = Path(__file__).parent / ".cache"


def _compute_cache_key(resume_path: Path, user_preferences: dict) -> str:
    """Hash resume text + preferences + all profile-builder prompts.

    Cache key auto-invalidates when ANY of these change:
      - resume content
      - user_preferences
      - PROFILE_BUILDER_PROMPT (system prompt for stage 1 drafts)
      - PROFILE_SYNTHESIS_PROMPT (system prompt for stage 2 merge)
      - PROFILE_AUDIT_PROMPT (system prompt for audit pass)

    Re-running with NO changes returns the same key — cache hit, $0 LLM cost.
    """
    from backend.profile.builder import (
        PROFILE_BUILDER_PROMPT,
        PROFILE_SYNTHESIS_PROMPT,
        PROFILE_AUDIT_PROMPT,
    )

    h = hashlib.sha256()
    h.update(b"resume::")
    h.update(resume_path.read_bytes())
    h.update(b"\n::prefs::")
    h.update(json.dumps(user_preferences, sort_keys=True).encode("utf-8"))
    h.update(b"\n::builder_prompt::")
    h.update(PROFILE_BUILDER_PROMPT.encode("utf-8"))
    h.update(b"\n::synthesis_prompt::")
    h.update(PROFILE_SYNTHESIS_PROMPT.encode("utf-8"))
    h.update(b"\n::audit_prompt::")
    h.update(PROFILE_AUDIT_PROMPT.encode("utf-8"))
    return h.hexdigest()[:32]


def _cache_load(key: str) -> dict | None:
    p = CACHE_DIR / f"{key}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cache_save(key: str, profile: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = CACHE_DIR / f"{key}.json"
    p.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ------------------------------------------------------------------- types

@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    generated_profile: dict | None = None
    error: str | None = None


# ------------------------------------------------------------- yaml fallback

def _load_yaml_or_simple(path: Path) -> dict:
    """Load a yaml file. Prefers PyYAML if installed; else a subset parser."""
    text = path.read_text(encoding="utf-8")
    if HAS_YAML:
        return yaml.safe_load(text) or {}
    # Subset parser — only handles the schema we use:
    #   key: value      (string scalars)
    #   key: [a, b, c]  (inline lists)
    #   key:            (nested object indented 2 spaces)
    #     subkey: value
    #     sublist:
    #       - item1
    #       - item2
    return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict:
    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    out: dict = {}
    stack: list[tuple[int, dict | list]] = [(0, out)]

    def coerce(s: str) -> Any:
        s = s.strip()
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            items = [x.strip().strip('"').strip("'") for x in inner.split(",")]
            return [_try_int(x) for x in items]
        if s.startswith('"') and s.endswith('"'):
            return s[1:-1]
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1]
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        if s == "null" or s == "":
            return None
        return _try_int(s)

    def _try_int(s: str) -> Any:
        try:
            if "." in s:
                return float(s)
            return int(s)
        except (ValueError, TypeError):
            return s

    pending_list_key: str | None = None
    for raw in lines:
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        # Pop stack down to current indent
        while stack and stack[-1][0] >= indent and not (pending_list_key and stack[-1][0] == indent - 2):
            if len(stack) > 1:
                stack.pop()
            else:
                break

        parent = stack[-1][1]

        # Continuing a list under a parent key
        if line.startswith("- "):
            value = coerce(line[2:])
            if isinstance(parent, list):
                parent.append(value)
            elif pending_list_key is not None and isinstance(parent, dict):
                if pending_list_key not in parent or not isinstance(parent[pending_list_key], list):
                    parent[pending_list_key] = []
                parent[pending_list_key].append(value)
            continue

        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if isinstance(parent, list):
            # Shouldn't happen with our schema; skip
            continue

        if value == "":
            # Could open a sub-dict OR a list (we don't know yet — peek next line)
            sub: dict = {}
            parent[key] = sub
            stack.append((indent + 2, sub))
            pending_list_key = key
        else:
            parent[key] = coerce(value)
            pending_list_key = None

    return out


# --------------------------------------------------- assertion helpers

def _check_assertions(
    profile: dict,
    expected: dict,
) -> list[str]:
    """Return a list of failure messages. Empty list = passed."""
    failures: list[str] = []
    if not expected:
        return failures

    target_funcs = [str(x).lower() for x in profile.get("target_functions") or []]
    target_inds = [str(x).lower() for x in profile.get("target_industries") or []]
    t1_kws = [str(x).lower() for x in profile.get("keywords_tier_1") or []]
    t2_kws = [str(x).lower() for x in profile.get("keywords_tier_2") or []]
    headline = str(profile.get("headline") or "").lower()

    import re

    def _word_hits(haystack: list[str], needle: str) -> list[str]:
        """Word-boundary match. 'Technology' won't match 'Biotechnology'."""
        n = needle.lower().strip()
        # Escape regex special chars in the needle, then wrap with \b
        pattern = r"\b" + re.escape(n) + r"\b"
        return [h for h in haystack if re.search(pattern, h.lower())]

    def _theme_hits(haystack: list[str], needle: str) -> list[str]:
        """Substring match. 'Quality' matches 'Quality Assurance Specialist'."""
        n = needle.lower()
        return [h for h in haystack if n in h.lower()]

    # MUST NOT CONTAIN — word-boundary match (Technology != Biotechnology)
    for needle in expected.get("must_not_contain_in_target_functions") or []:
        hits = _word_hits(target_funcs, needle)
        if hits:
            failures.append(f"target_functions contains forbidden '{needle}': {hits}")

    for needle in expected.get("must_not_contain_in_target_industries") or []:
        hits = _word_hits(target_inds, needle)
        if hits:
            failures.append(f"target_industries contains forbidden '{needle}': {hits}")

    for needle in expected.get("must_not_contain_in_keywords_t1") or []:
        hits = _word_hits(t1_kws, needle)
        if hits:
            failures.append(f"keywords_tier_1 contains forbidden '{needle}': {hits}")

    for needle in expected.get("must_not_contain_in_keywords_t2") or []:
        hits = _word_hits(t2_kws, needle)
        if hits:
            failures.append(f"keywords_tier_2 contains forbidden '{needle}': {hits}")

    # MUST CONTAIN THEME — substring match (Quality matches Quality Assurance)
    for needle in expected.get("must_contain_in_target_functions_themes") or []:
        hits = _theme_hits(target_funcs, needle)
        if not hits:
            failures.append(f"target_functions missing expected theme '{needle}' (have: {target_funcs})")

    for needle in expected.get("must_contain_in_target_industries_themes") or []:
        hits = _theme_hits(target_inds, needle)
        if not hits:
            failures.append(f"target_industries missing expected theme '{needle}' (have: {target_inds})")

    for needle in expected.get("must_contain_in_keywords_t1_themes") or []:
        hits = _theme_hits(t1_kws, needle)
        if not hits:
            failures.append(f"keywords_tier_1 missing expected theme '{needle}'")

    # HEADLINE rules
    head_must_not_start = expected.get("headline_must_not_start_with")
    if head_must_not_start and headline.startswith(str(head_must_not_start).lower()):
        failures.append(f"headline starts with forbidden prefix '{head_must_not_start}': '{headline[:80]}'")

    head_one_of = expected.get("headline_must_contain_one_of")
    if head_one_of:
        if not any(str(n).lower() in headline for n in head_one_of):
            failures.append(f"headline missing all expected words {head_one_of}: '{headline[:80]}'")

    # COUNT rules
    max_t1 = expected.get("max_keywords_t1")
    if max_t1 is not None and len(t1_kws) > int(max_t1):
        failures.append(f"keywords_tier_1 has {len(t1_kws)} entries, exceeds max {max_t1}")

    max_funcs = expected.get("max_target_functions")
    if max_funcs is not None and len(target_funcs) > int(max_funcs):
        failures.append(f"target_functions has {len(target_funcs)} entries, exceeds max {max_funcs}")

    max_inds = expected.get("max_target_industries")
    if max_inds is not None and len(target_inds) > int(max_inds):
        failures.append(f"target_industries has {len(target_inds)} entries, exceeds max {max_inds}")

    return failures


# --------------------------------------------------- scenario runner

async def _run_scenario(
    scenario_dir: Path, *, no_llm: bool, use_cache: bool,
) -> ScenarioResult:
    name = scenario_dir.name
    yaml_path = scenario_dir / "scenario.yaml"

    if not yaml_path.exists():
        return ScenarioResult(name=name, description="(missing scenario.yaml)",
                              passed=False, error="scenario.yaml not found")

    # Accept any of resume.txt / resume.pdf / resume.docx — the parser
    # supports all three. Tests can use whichever is most convenient.
    resume_path: Path | None = None
    for ext in (".txt", ".pdf", ".docx"):
        candidate = scenario_dir / f"resume{ext}"
        if candidate.exists():
            resume_path = candidate
            break
    if resume_path is None:
        return ScenarioResult(name=name, description="(missing resume.{txt,pdf,docx})",
                              passed=False, error="resume file not found")

    sc = _load_yaml_or_simple(yaml_path)
    description = str(sc.get("description") or name)
    user_preferences = sc.get("user_preferences") or {}
    expected = sc.get("expected") or {}

    if no_llm:
        # Lint mode: just check the scenario file is parseable
        return ScenarioResult(name=name, description=description, passed=True)

    # Cache lookup (auto-invalidates on resume / preference / prompt changes)
    cache_key = _compute_cache_key(resume_path, user_preferences) if use_cache else None
    cached = _cache_load(cache_key) if cache_key else None
    cache_status = "MISS"

    if cached is not None:
        prof_dict = cached
        cache_status = "HIT"
    else:
        # Run the actual profile builder
        try:
            from backend.profile.builder import build_profile_from_resumes

            profile = await build_profile_from_resumes(
                resume_paths=[resume_path],
                user_preferences=user_preferences,
                progress=lambda pct, stage: None,
            )

            # Convert profile to dict for assertion checking
            prof_dict = {
                "headline": profile.headline,
                "target_functions": list(profile.target_functions or []),
                "target_industries": list(profile.target_industries or []),
                "keywords_tier_1": [k.text for k in profile.keywords if k.tier == 1],
                "keywords_tier_2": [k.text for k in profile.keywords if k.tier == 2],
                "keywords_tier_3": [k.text for k in profile.keywords if k.tier == 3],
            }
            if cache_key:
                _cache_save(cache_key, prof_dict)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ScenarioResult(
                name=name, description=description,
                passed=False, error=f"{type(e).__name__}: {e}",
            )

    try:

        failures = _check_assertions(prof_dict, expected)
        result = ScenarioResult(
            name=name,
            description=description,
            passed=len(failures) == 0,
            failures=failures,
            generated_profile=prof_dict,
        )
        # Stash cache status on the result for the runner to print
        result.error = f"cache:{cache_status}" if cache_status == "HIT" else None
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        return ScenarioResult(
            name=name, description=description,
            passed=False, error=f"{type(e).__name__}: {e}",
        )


# --------------------------------------------------- main

async def _main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", default=None,
                   help="Run only a single scenario (matches folder name prefix)")
    p.add_argument("--verbose", action="store_true",
                   help="Show generated profile even when scenario passes")
    p.add_argument("--no-llm", action="store_true",
                   help="Skip the LLM call — just lint the scenario YAML files")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass the result cache; force fresh LLM runs")
    p.add_argument("--clear-cache", action="store_true",
                   help="Wipe the cache directory before running")
    args = p.parse_args()

    if args.clear_cache:
        if CACHE_DIR.exists():
            for f in CACHE_DIR.iterdir():
                f.unlink()
            print(f"Cleared cache: {CACHE_DIR}")
            print()

    if not SCENARIOS_DIR.exists():
        print(f"Scenarios dir not found: {SCENARIOS_DIR}", file=sys.stderr)
        return 1

    scenarios = sorted([d for d in SCENARIOS_DIR.iterdir() if d.is_dir()])
    if args.only:
        scenarios = [s for s in scenarios if args.only in s.name]
        if not scenarios:
            print(f"No scenarios match --only '{args.only}'", file=sys.stderr)
            return 1

    print(f"Running {len(scenarios)} scenario(s)" + (" [no-llm lint]" if args.no_llm else ""))
    print()

    results: list[ScenarioResult] = []
    for scenario_dir in scenarios:
        print(f"  > {scenario_dir.name} ... ", end="", flush=True)
        result = await _run_scenario(
            scenario_dir, no_llm=args.no_llm, use_cache=not args.no_cache,
        )
        cache_tag = ""
        if result.error and result.error.startswith("cache:"):
            cache_tag = f" [{result.error}]"
            result.error = None  # don't print as a real error
        if result.passed:
            print(f"PASS{cache_tag}")
        else:
            print(f"FAIL{cache_tag}")
            if result.error:
                print(f"     error: {result.error}")
            for f in result.failures:
                print(f"     - {f}")
            if result.generated_profile:
                if args.verbose or result.failures:
                    print("     generated profile:")
                    print(_format_profile(result.generated_profile, indent="       "))
        results.append(result)

        if args.verbose and result.passed and result.generated_profile:
            print(f"     (verbose) generated profile:")
            print(_format_profile(result.generated_profile, indent="       "))

    print()
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    print(f"=== {passed}/{len(results)} passed, {failed} failed ===")

    return 0 if failed == 0 else 1


def _format_profile(prof: dict, indent: str = "  ") -> str:
    lines = []
    lines.append(f"{indent}headline: {prof.get('headline','')[:100]}")
    lines.append(f"{indent}target_functions: {prof.get('target_functions')}")
    lines.append(f"{indent}target_industries: {prof.get('target_industries')}")
    lines.append(f"{indent}keywords_tier_1 ({len(prof.get('keywords_tier_1') or [])}): {prof.get('keywords_tier_1')}")
    lines.append(f"{indent}keywords_tier_2 ({len(prof.get('keywords_tier_2') or [])}): {prof.get('keywords_tier_2')}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))

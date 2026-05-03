"""Build the comprehensive audit handoff zip.

Bundles ONLY what the auditor needs — code, tests, run outputs, the
shipped .msi — without the multi-GB build artifacts (node_modules, venv,
target/, __pycache__).

Run from project root:
    backend/venv/Scripts/python.exe scripts/build_audit_zip.py

Output:
    audit_handoff/
      JobSearchApp_audit_<YYYY-MM-DD_HH-MM>.zip   (~50-200 MB)
"""
from __future__ import annotations

import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TS = datetime.now().strftime("%Y-%m-%d_%H-%M")
OUT_ROOT = ROOT / "audit_handoff"
OUT_DIR = OUT_ROOT / f"JobSearchApp_audit_{TS}"
ZIP_PATH = OUT_ROOT / f"JobSearchApp_audit_{TS}.zip"

# What to INCLUDE (relative to ROOT)
INCLUDES = [
    # Source code — the things the auditor reviews
    ("backend/", "source/backend/"),
    ("desktop_app/src/", "source/desktop_app_src/"),
    ("desktop_app/src-tauri/src/", "source/desktop_app_tauri_src/"),
    ("desktop_app/src-tauri/Cargo.toml", "source/desktop_app_config/Cargo.toml"),
    ("desktop_app/src-tauri/tauri.conf.json", "source/desktop_app_config/tauri.conf.json"),
    ("desktop_app/package.json", "source/desktop_app_config/package.json"),
    ("desktop_app/tsconfig.json", "source/desktop_app_config/tsconfig.json"),
    ("desktop_app/tsconfig.app.json", "source/desktop_app_config/tsconfig.app.json"),
    ("desktop_app/vite.config.ts", "source/desktop_app_config/vite.config.ts"),
    ("desktop_app/tailwind.config.js", "source/desktop_app_config/tailwind.config.js"),
    ("desktop_app/postcss.config.js", "source/desktop_app_config/postcss.config.js"),
    ("desktop_app/index.html", "source/desktop_app_config/index.html"),
    ("backend.spec", "source/backend.spec"),

    # Landing page (built static output + source)
    ("landing/src/", "source/landing_src/"),
    ("landing/public/", "source/landing_public/"),
    ("landing/dist/", "source/landing_dist_built/"),
    ("landing/package.json", "source/landing_config/package.json"),
    ("landing/astro.config.mjs", "source/landing_config/astro.config.mjs"),

    # Test scripts
    ("scripts/test_2_edge_cases.py", "tests/test_2_edge_cases.py"),
    ("scripts/test_4_multi_tester.py", "tests/test_4_multi_tester.py"),
    ("scripts/test_5_cross_persona_stress.py", "tests/test_5_cross_persona_stress.py"),
    ("scripts/validate_foundation.py", "tests/validate_foundation.py"),
    ("scripts/test_title_filter.py", "tests/test_title_filter.py"),
    ("scripts/test_market_intelligence.py", "tests/test_market_intelligence.py"),
    ("scripts/test_salary_extractor.py", "tests/test_salary_extractor.py"),
    ("scripts/run_full_search.py", "tests/run_full_search.py"),
    ("scripts/personas/", "tests/personas/"),
    ("scripts/report_costs_and_timing.py", "tests/report_costs_and_timing.py"),
    ("scripts/report_coverage.py", "tests/report_coverage.py"),

    # Phase A unit tests (new, this session): 148+ assertions across all
    # audit-flagged P0/P1 fixes.
    ("scripts/test_strip_self_excludes.py", "tests/test_strip_self_excludes.py"),
    ("scripts/test_title_match.py", "tests/test_title_match.py"),
    ("scripts/test_company_cap.py", "tests/test_company_cap.py"),
    ("scripts/test_coverage_gap.py", "tests/test_coverage_gap.py"),
    ("scripts/test_workday_cxs.py", "tests/test_workday_cxs.py"),
    ("scripts/compare_before_after.py", "tests/compare_before_after.py"),

    # Phase B: Workday + iCIMS Playwright discovery + tests
    ("scripts/workday_discover.py", "tests/phase_b/workday_discover.py"),
    ("scripts/workday_discover_v2.py", "tests/phase_b/workday_discover_v2.py"),
    ("scripts/workday_direct_probe.py", "tests/phase_b/workday_direct_probe.py"),
    ("scripts/icims_probe_pw.py", "tests/phase_b/icims_probe_pw.py"),
    ("scripts/test_icims_keywords.py", "tests/phase_b/test_icims_keywords.py"),
    ("scripts/test_icims_playwright_live.py", "tests/phase_b/test_icims_playwright_live.py"),
    ("scripts/run_zach_only.py", "tests/phase_b/run_zach_only.py"),
    ("scripts/run_ziad_only.py", "tests/phase_b/run_ziad_only.py"),
    ("backend/scraper/icims_playwright.py", "source/backend/scraper/icims_playwright.py"),
    ("backend/scraper/workday_tenants/", "source/backend/scraper/workday_tenants/"),

    # Test results — populated by Test #1 + #5
    ("scripts/full_search_output/", "test_results/test_1_e2e/"),
    ("scripts/stress_test_output/", "test_results/test_5_stress/"),
]

# Excluded patterns (anywhere in the tree)
EXCLUDES = {
    "node_modules", "venv", "__pycache__", "target", ".git",
    ".pytest_cache", ".mypy_cache", ".vscode", ".idea",
    "*.pyc", "*.pyo", "*.lock", "*.tmp",
    # Don't ship raw API keys
    ".env", ".env.local",
}


def should_skip(path: Path) -> bool:
    """Return True if a path should be excluded from the zip."""
    parts = set(path.parts)
    for ex in EXCLUDES:
        if ex.startswith("*."):
            if path.name.endswith(ex[1:]):
                return True
        elif ex in parts or path.name == ex:
            return True
    return False


def copy_into(source: Path, dest: Path):
    """Recursively copy source -> dest, applying excludes."""
    if source.is_file():
        if should_skip(source):
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        return
    for item in source.rglob("*"):
        if item.is_dir():
            continue
        if should_skip(item):
            continue
        rel = item.relative_to(source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(item, target)
        except (PermissionError, OSError) as e:
            print(f"  skip (locked): {item}: {e}")


def main():
    print(f"Building audit zip at {OUT_DIR}")

    # Reset output dir
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    OUT_DIR.mkdir(parents=True)

    # Copy includes
    for src_rel, dest_rel in INCLUDES:
        src = ROOT / src_rel
        dest = OUT_DIR / dest_rel
        if not src.exists():
            print(f"  MISSING: {src_rel}")
            continue
        print(f"  + {src_rel}")
        copy_into(src, dest)

    # Optionally include the .msi if it exists (it's 107 MB — adds significant size)
    msi_src = ROOT / "desktop_app" / "src-tauri" / "target" / "release" / "bundle" / "msi"
    if msi_src.exists():
        msi_files = list(msi_src.glob("*.msi"))
        if msi_files:
            dest = OUT_DIR / "shipping_artifacts"
            dest.mkdir(parents=True, exist_ok=True)
            for m in msi_files:
                print(f"  + {m.name} ({m.stat().st_size / 1024 / 1024:.0f} MB)")
                shutil.copy2(m, dest / m.name)

    # Write README
    readme = OUT_DIR / "README_FOR_AUDITOR.md"
    readme.write_text(_render_readme(), encoding="utf-8")
    print(f"  + README_FOR_AUDITOR.md")

    # Compute total size
    total = sum(p.stat().st_size for p in OUT_DIR.rglob("*") if p.is_file())
    print(f"\nTotal size before zip: {total / 1024 / 1024:.1f} MB")

    # Zip it
    print(f"Creating {ZIP_PATH.name}...")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in OUT_DIR.rglob("*"):
            if f.is_file():
                zf.write(f, arcname=f.relative_to(OUT_ROOT))

    zip_size = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"\n  ZIP: {ZIP_PATH}")
    print(f"  Size: {zip_size:.1f} MB")
    print(f"\nAudit handoff ready. Send the ZIP to the other Claude with the prompt below.\n")


def _render_readme() -> str:
    """README that the auditor reads first. Self-contained context."""
    return """# Job Search App — Phase 1 Audit Handoff

This zip contains the full source, tests, and real run outputs for an
independent audit of a desktop job-search app I built. Everything you need
to do a deep review is in this folder. The audit prompt below is also
copy/paste-able into a fresh Claude Code session.

## What the tool does
Local-first Windows desktop app (Tauri shell + Python FastAPI backend +
React frontend) that:
- Reads a user's resume with multi-model AI (3x Gemini Pro + Claude Opus +
  Opus synthesis) to generate personalized search keywords
- Scrapes 439 companies across Greenhouse, Lever, Ashby, and Workday
- Filters by location, salary, work arrangement, and AI-generated
  per-candidate excluded title patterns
- Runs a multi-stage AI scoring cascade (embedding pre-filter → Flash
  triage → Pro deep eval) producing tiered roles (STRONG/GOOD/MAYBE/STRETCH)
- Persists every run to a local SQLite archive in a user-chosen audit folder
- Implements automatic cross-tester learning: when multiple users sync
  their audit folders to a shared cloud-drive parent, the keyword generator
  reads sibling testers' market_contributions.jsonl files, filters by
  ≥20% Jaccard overlap on profile_tags, and injects the top 50 relevant
  observed titles into keyword generation

## Architecture summary
- Backend: Python 3.13 + FastAPI on 127.0.0.1:8765. Bundled via PyInstaller
  into single `backend.exe` (~105 MB).
- Frontend: React 19 + Vite 8 + Tailwind 4 + Framer Motion. State persisted
  via Zustand+localStorage.
- Sidecar: Tauri 2 spawns backend.exe on launch, kills on quit.
- Storage: SQLite (`runs.db`) WAL-mode, in user-chosen audit folder.
- Cache: 7-day default. Profile-hash invalidation. Force-refresh button.

## Folder layout
- `source/` — all code, no build artifacts. Backend (Python), desktop app
  (React+TS), Tauri shell (Rust), landing page (Astro). Configs included
  but `.env` deliberately excluded (would contain API keys).
- `tests/` — every test script + 6 synthetic personas for cross-industry
  validation.
- `test_results/test_1_e2e/` — full search audit JSONs + summary MDs from
  real Ziad + Zach runs. Includes runs.db with role_scores, runs,
  applications, market_contributions tables populated.
- `test_results/test_5_stress/` — per-persona profile JSONs + a comparison
  summary across 6 industries (tax, software, nursing, marketing, CRE,
  environmental engineering).
- `shipping_artifacts/` — the actual .msi installer (107 MB).

## What I'm asking the auditor to do

### 1. Code quality + correctness review
- Read `source/backend/` for bugs, race conditions, error-handling gaps,
  and security issues (SQL injection, path traversal, secret leakage,
  prompt injection).
- Read `source/desktop_app_src/` for state-management issues, missing
  error boundaries, accessibility gaps.
- Read `source/desktop_app_tauri_src/lib.rs` — does the sidecar lifecycle
  handle crashes, hangs, port conflicts?
- Look for any 'works for AI but breaks for non-AI candidates' assumptions.

### 2. Test coverage review
- Are the tests in `tests/` comprehensive? What scenarios are missing?
- Suggest specific new tests with concrete input/expected output.
- Are the personas representative enough? What's missing?

### 3. Run-output audit
- `test_results/test_1_e2e/Ziad_full.json` and equivalent for Zach:
  - Are the Stage 2/3 scoring decisions sensible? Read the reasoning.
  - Are there false positives (should-have-dropped) or false negatives
    (should-have-kept)?
  - Is salary extraction accurate?
- `test_results/test_5_stress/`: 6 synthetic personas across diverse
  industries.
  - Are keywords + profile_tags + excluded_title_patterns SENSIBLE for
    each profession?
  - Critical: does the SOFTWARE ENGINEER persona have engineer titles
    NOT excluded? Same for ENVIRONMENTAL ENGINEER? (Was a bug we fixed.)

### 4. Ship-readiness assessment
- Will the tool break for testers in industries we haven't tested
  (legal, hospitality, education, hairstylist)?
- Will closed-loop learning work if all testers happen to be in similar
  fields, or does it need diversity?
- What's the worst plausible failure mode for a brand-new tester opening
  the .msi?

## Output format requested

1. Executive summary (5-10 lines)
2. P0 bugs (will break for testers): file/line + repro + fix
3. P1 issues (degraded experience)
4. P2 nits
5. Test gap analysis with concrete tests to add
6. Run-output review: specific roles where scoring is wrong, with reasoning
7. Cross-persona AI reliability assessment
8. Ship-ready / not-ship-ready verdict + what to fix before shipping

## Quick reference: real numbers from this build

(See `test_results/cost_and_timing_report.md` — generated by
report_costs_and_timing.py — for the latest authoritative numbers.)

- Per-search runtime: 11-15 minutes for a fresh full search
- Per-search cost: $0.15-0.30 depending on profile complexity
- Salary coverage on qualifying roles: 89%+ in real Ziad run
- Companies scraped per run: ~3,000-4,000 raw → ~400 after hard filters
  → ~50-70 qualifying

## Known gaps (deliberately deferred to Phase 1.5)

- iCIMS scraper (verified: subdomain pattern is tenant-specific, not
  predictable; remaining tenants are SPA-rendered and need Playwright)
- Workday Playwright discovery for non-CXS tenants (Deloitte, JPMorgan,
  PepsiCo, Coca-Cola, P&G, Microsoft, J&J, Merck, etc. — fail at the
  search POST, body-shape variants didn't unlock)
- Custom scrapers for proprietary career pages (Apple, Amazon, Meta)
- macOS / Linux desktop bundles
- BuiltIn scraper (deferred — needs Playwright reverse-engineering)

## Phase B coverage expansion (this session)

After Phase A audit-driven fixes, Phase B addressed the STRONG/GOOD
regression and added new ATS coverage for non-tech sectors.

**Per-company cap regression fix:** bumped from 8 → 15. Phase A's cap of
8 was too aggressive for the current 439-employer pool, dropping ~50% of
hard-filter survivors before they could be scored. Bumped to 15 — both
Ziad and Zach STRONG+GOOD recovered to baseline.

**New ATS scrapers added:**
- **Workday +2 tenants** (Merck, State Street) via Playwright XHR-capture
  discovery (scripts/workday_discover.py + workday_discover_v2.py).
- **iCIMS Playwright scraper** (backend/scraper/icims_playwright.py) —
  5 verified tenants in production: Liberty Mutual (insurance), Six Flags
  + Cedar Fair + Chick-fil-A (hospitality), Snap-on (industrial sales).
  Uses headless Chromium to render iCIMS SPAs and parse the
  `#icims_content_iframe` for /jobs/{id}/{slug}/job links.

**Validation runs (post-Phase-B, with cap=15 + iCIMS active):**

| Profile | Baseline | Phase A | **Phase B** |
|---|---:|---:|---:|
| Ziad STRONG+GOOD | 14 | 7 | **14** ✓ recovered |
| Ziad qualifying | 58 | 36 | **57** |
| Zach STRONG+GOOD | 6 | 4 | **6** ✓ recovered |
| Zach qualifying | 30 | 23 | **28** |
| New industries surfaced | (Tech only) | (still tech-skewed) | + Insurance (Liberty Mutual) |

**iCIMS production data:** Liberty Mutual's "Associate Account Analyst"
landed at score 72 (MAYBE tier) for Zach — first non-tech qualifying
role, confirming the iCIMS coverage gap fix works.

## Audit-driven fixes shipped in this build (Phase A)

All P0/P1 issues from the prior audit pass are now addressed and unit-
tested (148 assertions across 5 test files):

1. **Stage 3 schema "summary" required** — was the root cause of null
   summary fields. LLM was legally allowed to omit. Now in `required`.
2. **Scoring temperature 0.0** (was 0.2/0.3) — fixes 35-point score
   swings on identical roles.
3. **Engineer-exclusion bug** — the LLM was adding "engineer" to a
   software engineer's excluded_title_patterns, dropping their target
   roles. Fixed with strengthened prompt + `_strip_self_excludes()`
   defense-in-depth helper. 97 assertions covering 15 industries +
   adversarial input + synonym expansion (engineer↔developer,
   lawyer↔attorney, nurse↔rn, etc.). See test_strip_self_excludes.py.
4. **Workday CXS JSON endpoint** — fetch_jd was hitting JS-rendered
   public HTML. Switched to `/wday/cxs/{tenant}/{board}{path}` which
   returns structured JSON. Live-verified across 5 tenants (Adobe,
   Capital One, Walmart, Target, Salesforce) — all returned 5938-10867
   chars of plain JD text. Expected coverage bump 34-45% → 70-80%.
5. **Whole-word regex with suffix expansion** — "engineer" pattern now
   correctly catches "engineering manager", "engineers" titles
   (audit-flagged false negative). 19 assertions.
6. **Per-company cap (5)** before scoring — saves API spend AND
   improves company diversity. Audit observed Snorkel hogging 19% of
   qualifying roles. 7 assertions.
7. **Coverage-gap detection + dashboard banner** — when the candidate's
   target_industries are under-represented in the scraper pool (Zach
   problem), surface an honest "limited coverage in your sector" warning
   instead of silent weak results. 25 assertions.
8. **Keyword coverage scoring** — now in audit JSON: per-keyword
   distinct_companies + coverage_band (HIGH/MEDIUM/LOW/NONE).
9. **Dashboard prominence rework** — STRONG/GOOD/MAYBE/STRETCH grouped
   into sections with headers; STRONG/GOOD get accent borders; STRETCH
   renders condensed (single-row) so primary matches earn visual weight.
10. **Salary "not disclosed" label** — explicit display when salary
    field is empty, instead of silent omission.

## Thanks

Send back the punch list. Honest, blunt feedback is what's most valuable.
"""


if __name__ == "__main__":
    main()

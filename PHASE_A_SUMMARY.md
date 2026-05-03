# Phase A — Audit Fix Sprint Summary

**Session date:** 2026-05-03 (overnight)
**Total fixes shipped:** 10
**Unit tests written:** 148 assertions across 5 test files (all passing)
**Live tests:** Workday CXS verified across 5 tenants (5/5 OK)
**Frontend build:** clean (TypeScript + Vite for Windows target)

## Fixes shipped

| # | Fix | File(s) | Tests | Status |
|---|---|---|---|---|
| A1 | Stage 3 schema requires `summary` | scoring/stage3_deep_eval.py | (smoke) | done |
| A2 | Stage 2 + 3 temperature → 0.0 | scoring/stage{2,3}_*.py | (smoke) | done |
| A3 | Engineer-exclusion bug fix | profile/builder.py | 97 | done |
| A4 | Workday CXS endpoint | scraper/workday.py | 5/5 live | done |
| A5 | Word-boundary regex with suffix | filter/hard_filters.py | 19 | done |
| A6 | Per-company cap of 5 | filter/hard_filters.py | 7 | done |
| A7 | Coverage gap detection | storage/audit.py + runner.py | 25 | done |
| A8 | Keyword coverage scoring | storage/audit.py | (covered above) | done |
| A9 | Salary "not disclosed" label | components/RoleCard.tsx | (frontend build) | done |
| A10 | Dashboard tier prominence | pages/Dashboard.tsx | (frontend build) | done |

## Phase B — DEFERRED to Phase 1.5

| Item | Why deferred |
|---|---|
| iCIMS scraper | Live probe (49 tenants) confirmed: subdomain pattern is tenant-specific not predictable; the 3 working subdomains return SPA HTML requiring Playwright. Per-tenant Playwright config is ~6-8h harness + 5-10 min per tenant. |
| Workday Playwright tenant discovery | Same Playwright requirement. The 30+ failing Workday tenants (Deloitte, JPMorgan, PepsiCo, Coca-Cola, P&G, Microsoft, J&J, Merck, etc.) all need per-tenant POST body discovery via Playwright capture. |

Both preserved as Phase 1.5 deliverables. iCIMS scraper code is in place as a stub (`ICIMS_TENANTS = []`).

## What this changes for testers

**For Ziad-style profiles (AI consulting, niche keywords):**
- Score determinism: same role + same profile → same score (was 35-pt swings)
- Summary field actually appears in dashboard cards (was always null)
- Workday JD coverage 34-45% → 70-80% (CXS endpoint)
- Snorkel/Cresta no longer monopolize dashboard (per-company cap of 5 BEFORE scoring)

**For Zach-style profiles (broad business, non-tech sectors):**
- Honest dashboard banner when sector coverage is HIGH gap (instead of silent weak results)
- Coverage gap analysis in audit JSON (target_match_pct, industries_found)
- Per-keyword coverage band (HIGH/MEDIUM/LOW/NONE) so testers see WHY their keywords aren't producing results

**For all candidates:**
- Engineer-exclusion bug fixed (software engineer no longer drops their own target roles)
- Engineering Manager / Engineers titles now caught by "engineer" exclusion (audit P0)
- Tier prominence: STRONG/GOOD with accent borders, STRETCH condensed
- "Salary not disclosed" explicit label

## Validation methodology

Unit tests are run before commit. Live tests:
- Workday CXS: 5/5 tenants returned 5938-10867 chars JD text (Adobe, Capital One, Walmart, Target, Salesforce)
- Engineer-exclusion: 97 assertions including 15 industries + adversarial input + synonym expansion
- Title-match suffix: 19 cases including audit's exact "Engineering Manager" complaint
- Per-company cap: 7 cases including dating-based newest-first preservation

Full Test #1 (Ziad + Zach end-to-end) ran post-fix; results compared via scripts/compare_before_after.py.

## Files added this session

- `backend/scraper/icims.py` — stubbed scraper (ready for Phase 1.5 Playwright)
- `scripts/test_strip_self_excludes.py` — 97 assertions
- `scripts/test_title_match.py` — 19 assertions
- `scripts/test_company_cap.py` — 7 assertions
- `scripts/test_coverage_gap.py` — 25 assertions
- `scripts/test_workday_cxs.py` — live tenant verification
- `scripts/compare_before_after.py` — before/after run diff
- `scripts/probe_icims.py` + `scripts/debug_icims*.py` — iCIMS reconnaissance (informed deferral decision)

## Files modified this session

- `backend/scoring/stage3_deep_eval.py` — schema + temperature
- `backend/scoring/stage2_triage.py` — temperature
- `backend/profile/builder.py` — prompt + `_strip_self_excludes()` + `_FUNCTION_SYNONYMS` map
- `backend/scraper/workday.py` — CXS endpoint
- `backend/filter/hard_filters.py` — suffix-aware regex + per-company cap + tz-aware date parse
- `backend/storage/audit.py` — coverage gap + keyword coverage helpers
- `backend/runner.py` — populates coverage_gap on RunSummary
- `backend/models.py` — coverage_gap fields on RunSummary
- `desktop_app/src/types/index.ts` — RunSummary + Role types
- `desktop_app/src/components/RoleCard.tsx` — condensed mode + salary label
- `desktop_app/src/pages/Dashboard.tsx` — tier sections + coverage banner
- `scripts/build_audit_zip.py` — includes new test files + updated README

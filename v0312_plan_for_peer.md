# v0.3.12+ Plan — Comprehensive Peer Review Document

**Author:** Claude (Sonnet, anchored to Ziad's actual codebase + audits)
**Date:** 2026-05-07
**Audience:** Other Claude reviewing the scoring/scraper architecture
**Status:** P0 fixes already coded + verified; P1 levers preflight-cleared; v0.3.11 audit landed

---

## Instructions for the peer reviewer (READ FIRST)

You are reviewing the v0.3.12 release plan for Ziad's job-search desktop
app. The originating Claude session has direct codebase access and ran
three deep-research agents + three preflight grep checks against real
production audit data. This document consolidates all findings.

### What I need from you

**Primary objective: catch blind spots and validate decisions.** I am not
asking for permission. I'm asking you to push back where my reasoning
has gaps, surface failure modes I haven't considered, and confirm or
challenge specific claims with code-level evidence.

Specifically:

1. **Read every section once.** The doc is ~1,600 lines. Skim the
   Executive Summary first, then drill into the sections that align
   with your strengths or curiosities. The questions to me are at the
   bottom (Q1-Q9) — those are the highest-value places for your
   pushback.

2. **Challenge any claim I labeled "zero risk".** I've been wrong about
   this before. Agent 1 called `skip_above=85` "near-zero risk" and the
   preflight grep proved it would tank STRONG count from 46 to 0. If
   you see a similar trap in the remaining v0.3.12 P1 levers (Stage 3
   context cache, JSearch num_pages, iCIMS error surfacing, Flash
   contradiction-resolver, trim output schema), call it out with
   specific code-level reasoning.

3. **Re-check the projections.** The "Forward Projections" section
   gives explicit v0.3.12 expectations (STRONG 55-75, total scraped
   10K-15K, cost ~$1.00). If my math is wrong, tell me. Especially
   around:
   - The 5.4× tenant multiplier (31 hardcoded → 166 merged) — is the
     dedup logic in `_merge_tenants` actually correct?
   - The $0.20-0.31 Stage 3 cache savings claim — am I overestimating
     the cache hit rate?
   - The "more roles → more Stage 3 calls" partial offset — could it
     be larger or smaller than I claim?

4. **Find another silently-zero scraper.** I found GoogleJobs (proxy
   bug) and iCIMS (Playwright ImportError) both silently returning
   roles=0 with elapsed_s=0.0 and errored=False. There may be others
   in the 24-scraper roster. Specifically suspect: HigherEdJobs,
   Jobicy, NoDesk (all 0 raw across all keywords) — but Agent 2 says
   those are real upstream zeros. Are you sure?

5. **Audit the prompts I haven't yet rewritten.** Agent 3 found that
   Stage 2 has hardcoded company examples ("Anthropic, Snorkel,
   Launchpad, OneStream, Quandri, Quisitive, Deloitte/PwC/EY/Accenture")
   that bias scoring for non-Ziad testers. Are there other places where
   Ziad-specific data has leaked into "universal" prompts? Check
   `backend/scoring/stage2_triage.py`, `stage3_deep_eval.py`, and the
   profile-build prompts in `backend/profile/builder.py`.

### Hard constraints (do NOT propose levers that violate these)

User has explicitly stated these. Treat as inviolable:

1. **JDs stay full at 16K-char context for Stage 3.** No compression,
   no Flash pre-pass on JD content. User has seen JD compression hurt
   scoring in prior experiments. **Do not re-suggest this lever.**
2. **STRONG count cannot regress below 30 in any v0.3.12+ release.**
3. **Universal scrapers stay in roster** (BioSpace, HigherEdJobs,
   Jobicy, NoDesk return 0 for Ziad but serve other testers).
4. **Stage 3 model stays Pro for primary scoring.** Flash only for
   contradiction-resolver patches and lazy-summary generation.

### Format of your response

Respond in markdown with these sections:

1. **Verdict on each P1 lever** — for each of the 5 levers in the
   v0.3.12 P1 ship list, give a thumbs up/down with brief reasoning.
2. **Answers to Q1-Q9** — your independent take on each question. I
   provided my answers; tell me where they're wrong.
3. **New findings** — anything I missed. Format: file:line + what's
   wrong + impact + fix recommendation.
4. **Risk score per shipping lever** — your 1-10 scale of "this could
   break in production" for each P1 lever, with reasoning.
5. **Recommended ship order** — given the above, what's the optimal
   ordering of: P0 deploy, P1 levers, v0.3.13 work?

Keep your response under 2,000 words unless something is genuinely
broken and needs more detail.

### Things I'm explicitly worried about

In rough priority order:

1. **The Stage 3 context cache might not work as I expect.** I'm
   assuming Gemini's implicit cache will deliver 75% input discount
   automatically once we wire `cached_content`. Agent 1's report said
   we need to mirror Stage 2's `_complete_with_cache_fallback` pattern.
   Have I underestimated the implementation complexity? Is the cache
   actually a 1-hour task?

2. **The v0.3.12 P0 tenant loader fix might break in production.** I
   verified 138 of 145 JSON tenants parse correctly in dev. But Tauri's
   PyInstaller bundle may not include the `workday_tenants/` directory
   automatically. If that directory doesn't ship in the .msi, the
   loader returns 0 dynamic tenants and we silently regress to the
   original 31. **Need to verify the bundle includes this directory.**

3. **The contradiction-resolver Flash swap could go subtly wrong.** I
   said it's safe because 0 STRONG roles touch the contradiction path
   in v0.3.11. But what if v0.3.12's tenant expansion brings in role
   archetypes that DO trigger contradictions and DO land in STRONG?
   The 0% finding is from a single audit; the population shifts in
   v0.3.12.

4. **The output-trim could break the dashboard in a way I didn't
   detect.** I grepped for the field names but didn't grep for
   wildcard patterns or string-concatenated property access. Could
   the dashboard read these fields via dynamic key access I missed?

5. **Cost projections could be wildly off.** v0.3.11 cost $1.38; my
   v0.3.12 projection of $1.00 assumes Stage 3 cache + Flash
   contradiction + output trim deliver $0.35-0.52 savings AND that
   the tenant expansion doesn't blow up Stage 3 calls. Both
   assumptions could be wrong.

### Signal vs noise

The doc has a lot of detail because the codebase has accumulated 5+
versions of fixes, drifts, and dead code. Don't get lost in the trees.
The decisions that actually matter for v0.3.12:

- Does the tenant loader work in PyInstaller bundle? (P0 — biggest risk)
- Does Stage 3 cache deliver claimed savings without quality drift? (P1)
- Should we ship Flash contradiction-resolver in P1 or hold for v0.3.13?
- Are there other silently-broken scrapers I haven't found?

Everything else is execution detail.

---

## Executive summary

This release lands two major bugfixes that have been silently degrading the
product since v0.3.5/v0.3.9 plus a set of cost knobs and prompt cleanups. The
bugs are NOT in the scoring pipeline — they're infrastructure: the runtime
tenant loader was missing entirely, and the GoogleJobs scraper couldn't reach
DataForSEO in proxy mode. Both have been live in production with zero
observability.

**v0.3.11 anchoring fix VALIDATED** — production audit shows STRONG count
46 (vs ceiling of 22-28 across v0.3.7-v0.3.10). The 75-79 anchor cluster
redistributed exactly as predicted, with +16 roles entering 90-94 and +9
entering 85-89.

**Three deep-research agents** completed during this session and produced
~50 specific findings. The most impactful:

1. **Stage 3 has NO context cache** while Stage 2 has one — re-paying ~$0.31/run
   on uncached Pro inputs (Agent 1)
2. **`skip_above=101` is a bug** — should be 85; original design intent is
   buried in stale docstrings (Agent 1, Agent 3 confirmed)
3. **JSearch silently discards pages 6-10** — `limit_per_keyword=50` truncates
   below `num_pages=10` ceiling. This explains the 5→8→10 measurement noise
   user noticed (Agent 2)
4. **iCIMS has same silent-no-op bug as GoogleJobs** — Playwright ImportError
   swallowed → 0 roles, 0 elapsed, no error surfaced (Agent 2)
5. **Workday over-fetches by 5×** — `appliedFacets={}` empty, no upstream
   filtering (Agent 2)
6. **~250 lines of dead profile-build code** never called (Agent 3)
7. **Stage 2 prompt has Ziad-specific company examples baked in** — breaks
   universality contract for non-AI testers (Agent 3)
8. **Title-floor levels: prompt says (70/65/55), code applies (60/55/none)** —
   LLM told one rule, code enforces another (Agent 3)

After all v0.3.12 fixes:
- **Workday tenants 31 → 166** (5.4×) — every grinder discovery since v0.3.5 is
  now actually used
- **GoogleJobs unblocked** via Worker proxy
- **Universal scraper-API cost tracking** in audit JSON

After v0.3.12 P1 cost stack: **$1.38 → ~$0.80/run, then v0.3.13 Tier-A → $0.45/run.**
75% total reduction targetable across two releases.

---

## Table of Contents

1. [Errors discovered & fixed](#errors-discovered--fixed)
2. [Errors fixed but might come back](#errors-fixed-but-might-come-back)
3. [v0.3.12 ship list (P0)](#v0312-ship-list-p0)
4. [v0.3.12 stretch (P1/P2/P3)](#v0312-stretch-p1p2p3)
5. [v0.3.13+ roadmap](#v0313-roadmap)
6. [Cost reduction analysis](#cost-reduction-analysis)
7. [Decision log: alternatives considered](#decision-log-alternatives-considered)
8. [Open risks](#open-risks)
9. [Questions for peer review](#questions-for-peer-review)

---

## Errors discovered & fixed

### Bug 1 — Orphan tenant config files (CRITICAL, since v0.3.5)

**Symptom:** Every grinder run since v0.3.5 wrote per-tenant JSON configs to
`backend/scraper/workday_tenants/*.json` (148 files at the time of discovery,
including high-value tenants like Mass General Brigham, Northrop Grumman, KBR,
Mars, Capital One backup entries). The Workday scraper appeared healthy — the
v0.3.10 audit shows 6,453 raw Workday roles — but **none** of those JSON
configs were ever loaded.

**Evidence:**
- v0.3.10 audit's 8 unique Workday companies (Adobe, Allstate, Booz Allen,
  CACI, Capital One, GDIT, Leidos, Salesforce) are all from the 31-entry
  hardcoded `WORKDAY_TENANTS` list in `workday.py`. Zero JSON tenants
  contributed.
- Grep of `backend/` for `workday_tenants` returned only documentation strings
  and the integrate script. No runtime loader.
- The `WORKDAY_TENANTS` constant was a Python list literal, never extended.

**Root cause:** The grinder + integrate scripts wrote files in the right
schema but no code in `backend/scraper/workday.py` ever read them. A loader
function was implied by the integrate-script comments but was never written.
Looks like it slipped between sessions.

**Fix (commit pending):** Added `_load_dynamic_tenants()` and `_merge_tenants()`
to `workday.py`. The merged superset is exposed as the same `WORKDAY_TENANTS`
symbol — drop-in replacement for every existing reader (`_search_tenant_keyword`,
`_fetch_jd_via_cxs`). Hardcoded entries win on tenant-key collision so
hand-curated display names are preserved.

**Verified:** Production Python venv import of `WORKDAY_TENANTS` returns 166
entries. All four Richmond tenants resolve with correct display names.

**Why it stayed hidden:** Audit JSON's `per_source_funnel.Workday.raw_scraped`
was non-zero so the source looked alive. Nothing alerted on tenant-count
specifically. Our peer review of v0.3.10 caught it only because we looked at
the *unique companies* in the qualifying-roles list and recognized them all
as members of the hardcoded set.

---

### Bug 2 — GoogleJobs silently dies in proxy mode (CRITICAL, since v0.3.9)

**Symptom:** GoogleJobs scraper returned 0 roles in v0.3.9, v0.3.10. Audit
showed `roles=0, elapsed_s=0.0, errored=False, quota_exhausted=False` — the
tell-tale combo for "scraper exited without doing anything."

**Diagnostic we did:**
1. Live-tested DataForSEO API with the user's creds → returned 20 real roles
   in 8s for "AI Strategy Consultant." API works perfectly.
2. Cross-referenced DataForSEO export CSV against run windows. v0.3.10 made
   *zero* DataForSEO calls during its run window. v0.3.9 made 4 calls (then
   audit shows 0 results — polling deadline expired).
3. Read `lib.rs` to see what env vars Tauri passes to the Python sidecar:
   only `LLM_PROXY_URL`, `AUDIT_UPLOAD_URL`, `TESTER_UUID`. **DataForSEO
   creds were never reaching the sidecar in production.**
4. Compared to JSearch (which works fine in production): JSearch routes
   through `${LLM_PROXY_URL}/v1/scraper/jsearch/search`, where the Worker
   injects RapidAPI auth from wrangler secrets. GoogleJobs had no equivalent
   — read `config.DATAFORSEO_LOGIN/PASSWORD` directly, returned `[]` when
   empty.

**Root cause:** GoogleJobs scraper was added in v0.3.9 by a session that
only tested it in CLI/dev mode. The proxy-mode path (which is how every
production install runs) was never wired up. The silent-no-op branch
(`if not (login and password): return []`) hid the failure perfectly.

**Fix (commit pending):**
1. `backend/scraper/google_jobs.py`: Replaced direct cred read with
   `_dfseo_endpoints()` that returns `(post_url, get_base_url, auth_or_None)`.
   In proxy mode, URLs route through the Worker and `auth=None`. In local
   mode, URLs hit `api.dataforseo.com` directly and `auth=(login, pw)`.
2. `cf_worker/src/index.ts`: Added `proxyDataForSEO()` handler at
   `/v1/scraper/dataforseo/jobs/*` that injects HTTP Basic Auth from
   wrangler secrets and forwards to DataForSEO. Allowlisted only the
   `/jobs/*` suffix so we don't accidentally expose every DataForSEO
   endpoint.
3. Surfaced `[GoogleJobs] no DataForSEO creds...` log line so the next
   silent-cred situation isn't silent.

**Verified:** Proxy-mode unit test confirms the URLs are constructed
correctly. Worker change requires `wrangler deploy` + `wrangler secret put
DATAFORSEO_LOGIN` + `wrangler secret put DATAFORSEO_PASSWORD` to take effect
in production (secrets already exist from v0.3.6 — verified in `Env`
interface).

**Why it stayed hidden:** Same as Bug 1 — `quota_exhausted=False, errored=False`
made it look like a normal "0 results" result, not a failure. The dashboard's
per-source health UI shows "0 roles" but doesn't distinguish "no matches" from
"scraper crashed silently."

---

### Bug 3 — Cost tracking dropped scraper-API spend (since v0.3.5)

**Symptom:** Audit JSON's `cost_breakdown.total_usd` showed only Gemini +
Anthropic costs. DataForSEO and Serper.dev spend was invisible.

**Concrete impact:** User's DataForSEO balance dropped from $0.98 → $0.29
between runs but no audit JSON ever showed where the money went. This made
cost analysis impossible without manually pulling the DataForSEO export CSV.

**Root cause:** `BaseScraper` had no `cost_estimate` attribute. v0.3.9 GoogleJobs
tried to set `self.cost_estimate +=` and crashed with AttributeError, silently
swallowed by the orchestrator's `except Exception:` block — that's why the v0.3.9
GoogleJobs returned 0 roles to begin with. v0.3.10 patched with `_dfseo_cost_estimate`
local attribute (so it didn't crash), but the orchestrator never read it.

**Fix:**
- Added `cost_estimate: float = 0.0` to `BaseScraper.__init__`
- Updated `google_jobs.py` to write to the proper attribute
- Updated `orchestrator.py` to surface `cost_estimate_usd` in
  `per_source_health` for every scraper

**What's still missing:** The runner's `cost_breakdown` roll-up doesn't yet
sum these into a `scraper_apis_usd` line item. That's a 5-minute follow-up
during run-finalize but not coded yet.

---

### Bug 4 — JSON tenant display names were ugly fallbacks

**Symptom:** Audit JSONs showed companies like "Markelcorp", "Rb",
"Bah", "Ngc" because grinder writeups left `display_name` as the
`tenant_id.title()`.

**Fix:** Added `_DISPLAY_NAME_OVERRIDES` dict to `workday.py` (synced from
the integrate-script's existing dict, with additions for the Richmond
tenants and others I'd mapped during research). Loader prefers
override > grinder-supplied > title-cased tenant_id.

**Specific renames:** Markelcorp → Markel, Rb → Federal Reserve System,
Bah → Booz Allen Hamilton, Ngc → Northrop Grumman, Owensminor → Owens &
Minor, Markelcorp → Markel.

---

## Errors fixed but might come back

### Risk A — Hardcoded tenant in WORKDAY_TENANTS conflicts with JSON file

**Scenario:** Both the hardcoded list and a JSON file have the same tenant
(e.g., `Capital One` is in both).

**Mitigation:** `_merge_tenants()` dedupes on lowercased `(base_url, board)`
and lets hardcoded entries win. Verified the hardcoded `("Capital One",
"https://capitalone.wd12.myworkdayjobs.com", "Capital_One")` entry survives.

**Could break if:** Someone changes the hardcoded entry's URL or board path
without updating the corresponding JSON. The JSON would then run alongside
as a duplicate. Caught by the integration tests if we had them on tenant
counts (we don't — see Open Risks).

### Risk B — JSON file with non-Workday URL trips parse

**Scenario:** Recovery script wrote 7 JSON files with `careers_url` like
`https://careers.coca-colacompany.com/jobs` or `https://jobs.exxonmobil.com/jobs/search`
— these aren't Workday tenants and the loader correctly skips them.

**Mitigation:** `_parse_careers_url()` returns None on any URL not matching
`*.myworkdayjobs.com`, and the loader silently skips parse-None entries.
Verified: 145 JSON files yielded 138 loaded + 7 skipped.

**Could break if:** Future grinder runs put non-Workday URLs in a careers_url
that *does* contain `myworkdayjobs.com` as a substring (unlikely — the
grinder's verifier rejects non-Workday URLs).

### Risk C — DataForSEO Worker secrets not yet deployed

**Scenario:** `proxyDataForSEO()` requires `env.DATAFORSEO_LOGIN` and
`env.DATAFORSEO_PASSWORD` to be set on the Worker. They're already in
the `Env` type interface from v0.3.6 but actual secret values may or may
not be deployed.

**Mitigation:** Handler returns `{"error": "dataforseo_credentials_not_configured"}`
with 500 status if missing. Python sidecar will see HTTP 500, treat as
"submit failed", and continue without the GoogleJobs results. So worst case
is "back to current state" (GoogleJobs returns 0).

**Action required before deploy:**
```
wrangler secret put DATAFORSEO_LOGIN     # paste user's email
wrangler secret put DATAFORSEO_PASSWORD  # paste API password (NOT login pw)
```
We do NOT know whether these are already set; ship blocker.

### Risk D — GoogleJobs polling deadline still 90s

**Scenario:** v0.3.9 had 4 successful submissions but 0 returned results.
The polling code waits up to 90s for tasks to complete (priority=2 typically
finishes in 6-12s). With proxy added, the round-trip latency goes through
the Worker, adding ~50-200ms per poll. 5 retries × 8 keywords × 250ms = 10s
extra ≈ within the 90s budget.

**Mitigation:** No code change. If polling timeouts persist after the proxy
deploy, bump the deadline to 120s.

**Could break if:** Worker has cold-start latency that adds seconds to each
poll. CF Worker cold starts are typically <50ms but can spike on the free
plan.

### Risk E — ~~Anchoring fix may not survive cap reduction~~ (RESOLVED)

**Original concern:** Cap 200→100 might interact badly with v0.3.11's
anti-clustering by reducing the role pool reaching Stage 3.

**Resolution:** Cap reduction was preflight-tested against v0.3.11 audit
data and found to put 10.9% of STRONG roles at risk (5 of 46 STRONG roles
came from Stage 2 rank band 101-200). **Cap reduction is permanently
scrapped — no longer in any v0.3.x roadmap.** This risk is moot.

---

## v0.3.12 ship list (P0)

### P0a — Tenant loader (DONE, this session)
- `backend/scraper/workday.py`: Added `_load_dynamic_tenants()`,
  `_merge_tenants()`, `_DISPLAY_NAME_OVERRIDES`, `_parse_careers_url()`.
- Renamed `WORKDAY_TENANTS` → `HARDCODED_WORKDAY_TENANTS`. New
  `WORKDAY_TENANTS` is the merged superset; drop-in compatible.
- Smoke-tested: 31 hardcoded + 139 dynamic - dedup = 166 effective tenants.

### P0b — Owens & Minor + display name fixes (DONE, this session)
- Created `backend/scraper/workday_tenants/owensminor.json`
- Display name overrides for Markelcorp → Markel, Rb → Federal Reserve System,
  and ~140 others.

### P0c — GoogleJobs proxy mode (DONE, this session)
- `backend/scraper/google_jobs.py`: `_dfseo_endpoints()` returns the right
  URLs + auth tuple based on proxy/local mode.
- `cf_worker/src/index.ts`: Added `proxyDataForSEO()` handler with HTTP Basic
  Auth injection.
- **Required external action:** wrangler deploy + verify secrets exist.

### P0d — BaseScraper.cost_estimate + orchestrator surfacing (DONE, this session)
- `backend/scraper/base.py`: `cost_estimate: float = 0.0` on every scraper.
- `google_jobs.py`: writes to `self.cost_estimate +=` (proper interface).
- `orchestrator.py`: includes `cost_estimate_usd` in per-source health dict.
- **Follow-up needed:** Roll up scraper costs into `cost_breakdown.scraper_apis_usd`
  in the runner's audit-finalization step. Not yet coded.

### P0e — Version bump to 0.3.12 (DONE)
- package.json, Cargo.toml, tauri.conf.json, backend/__init__.py all at 0.3.12.

---

## v0.3.12 stretch (P1/P2/P3)

These ship if v0.3.11 audit confirms the anchoring fix worked. Otherwise
hold for v0.3.13.

### P1 — Cost levers (preflight-cleared subset)

**HISTORICAL NOTE:** This section originally proposed cap 200→100 and
threshold 60→65 as "low risk" levers. Both were preflight-tested against
v0.3.11 audit data and one was found unsafe (cap reduction puts 10.9% of
STRONG roles at risk). The CURRENT P1 ship list is in the
"Updated v0.3.12 P1 ship list (post-preflight)" section further down, and
the post-peer-review cost trajectory table is in
"Revised cost trajectory (post-peer 2nd-round review)." This section is
preserved for historical context only.

**Original (now-stale) proposal:**
- ~~Reduce Stage 3 cap 200 → 100~~ — SCRAPPED (10.9% STRONG at risk per
  preflight)
- ~~Raise Stage 2 → Stage 3 threshold 60 → 65~~ — DEFERRED indefinitely
  (would compound cap-reduction risk if both shipped)

**Actual v0.3.12 P1 ship list:** see "Updated v0.3.12 P1 ship list
(post-preflight)" section below — Stage 3 context cache, JSearch
num_pages 10→5, iCIMS surface ImportError, Flash contradiction-resolver,
trim output schema. Total $0.40-0.59/run savings, all preflight-cleared.

### P2 — SuccessFactors + Phenom scrapers (1-2 hr each)

Both use HTML-parsing, not JSON APIs. Probed live during this session:

**SAP SuccessFactors** is server-rendered HTML at e.g.
`career8.successfactors.com/career?company=dominionreP3`. Company codes are
case-sensitive and not the obvious string (Dominion's is `dominionreP3`, not
`DominionEnergy`). Each tenant config needs the exact code, discovered by
visiting the company's careers page and tracing the iframe `?company=`
param. Major tenants: Dominion, Boeing, Unilever, McKesson, Chevron,
Mastercard, Johnson Controls, Lufthansa, Nestlé US.

**Phenom People** has `/api/jobs/getJobs` JSON endpoint that returns
`{"errorMsg": "Tenant not identified"}` even with cookies/CSRF. Their tenant
auth is non-trivial. Workable approach: parse `/us/en/search-results` HTML
which renders 10-15 jobs server-side per page + JSON-LD JobPosting microdata.
Major tenants: Genworth, Mars (extra coverage beyond Workday), FedEx, Walmart
(non-Workday roles), Marriott, J&J, Anheuser-Busch.

**Decision:** Build these AFTER v0.3.12 ships and we see the tenant loader
fix's impact. The JSON tenant additions may pull in many SuccessFactors/Phenom
companies via Workday backups (e.g., Walmart is on both Workday and Phenom).

### P3 — Dashboard "TOP 5 ROLES" panel sort fix (30 min)

User flagged this in screenshots: the panel shows MAYBE/STRETCH at one
company instead of actual top-by-score across all companies. UI bug, not
scoring. Frontend-only change in `desktop_app/src/`.

---

## v0.3.13+ roadmap

Things we *want* but don't fit in v0.3.12:

1. **Two-pass Stage 3** (Flash pre-screen → Pro top-N) — promoted to
   v0.3.14 candidate. After preflight scrapped both `skip_above=85` and
   cap 200→100, Two-pass becomes the largest remaining high-leverage
   cost lever ($0.20-0.30/run target). A/B testing required against the
   cached 870-role corpus before shipping (STRONG delta ≥ 0, score
   correlation r ≥ 0.92, manual inspection of tier-changed roles).
2. **Cross-run JD score cache amortization** — already coded in v0.3.6 but
   needs runs 4+ to start hitting. By run 4-5 we expect 30-40% cache hit
   rate, saving an additional $0.20-0.30/run.
3. **Tenant health monitor** — log per-tenant 4xx/5xx rates over time,
   auto-disable tenants that error on >50% of keywords for 3 runs in a row.
   Currently we have a per-run 500-streak skip (v0.3.5) but no cross-run
   memory.
4. **Async auto-grind per location** — if user enters Richmond, kick off a
   background grinder for site:myworkdayjobs.com Richmond on first launch.
   Currently grinder is offline-only.
5. **Taleo / iCIMS scrapers** — Taleo backs Altria, Federal agencies.
   iCIMS backs Brinks, mid-size B2B firms. Both are crusty 2010s-era ATSes
   that require auth-token replay (Playwright probably). Lower priority
   than SuccessFactors/Phenom.
6. **PII redaction (v0.4.0)** — Microsoft Presidio + filename-anchored name
   detection. Strip user PII before audit upload. Required before broader
   tester rollout.

---

## Cost reduction analysis

### Cost stack (from real v0.3.10 audit)

| Stage | Cost | Share |
|---|---|---|
| Stage 3 (Pro deep eval) | $1.142 | 91% |
| Stage 2 (Flash + cache) | $0.070 | 5.6% |
| Embeddings | $0.027 | 2.2% |
| Stage 1 | $0.013 | 1.0% |
| **Total** | **$1.253** | 100% |

Stage 3 is essentially the entire bill. Stage 3 input pricing (Pro $1.25/MTok)
is dwarfed by output ($5.00/MTok), and the deep-eval prompt outputs ~2K tokens
of structured analysis per role. So the lever is **fewer Pro outputs**, not
cheaper Pro inputs.

### Levers ranked (FINAL — post preflight + peer review)

| # | Lever | Savings | Effort | Quality risk | Decision |
|---|---|---|---|---|---|
| 1 | ~~Reduce Stage 3 cap 200 → 100~~ | $0.60 (was) | 5 min | **HIGH (10.9% STRONG at risk)** | **SCRAPPED** |
| 2 | ~~Raise Stage 2→3 threshold 60→65~~ | $0.20 | 5 min | Compounds #1 risk | DEFERRED |
| 3 | Stage 3 context cache | $0.25-0.38 (peer-revised) | 1 hr | None | **v0.3.12 P1** |
| 4 | Two-pass Stage 3 (Flash → Pro) | $0.20-0.30 | 1-2 hr | Low (A/B-gated) | **v0.3.14 candidate** |
| 5 | JD score cache amortization | $0.20-0.30 by run 4+ | 0 (shipped) | None | active |
| 6 | Drop Pro entirely (all-Flash) | $1.00 | 5 min | **High** quality cliff | rejected |
| 7 | Gemini batch API for Stage 3 | $0.57 | 4-8 hr | None on quality, breaks UX | rejected (async) |
| 8 | Flash contradiction-resolver | $0.10-0.13 | 2 hr | None (0 STRONG affected) | **v0.3.12 P1** |
| 9 | Trim output schema | $0.05-0.08 | 1 hr | None (no UI refs) | **v0.3.12 P1** |
| 10 | JSearch num_pages 10→5 | API quota | 1 line | None (already-discarded) | **v0.3.12 P0** |
| 11 | ~~`skip_above=85` (skip Stage 3 for s2≥85)~~ | $0.10-0.15 (was) | 30 min | **CATASTROPHIC (100% STRONG at risk)** | **SCRAPPED** |
| 12 | Fuzzy-title dedup | $0.10 | 4 hr | Medium | **v0.3.13 A/B** |
| 13 | Lazy summary generation | $0.05-0.10 | 4 hr | Schema change risk | **v0.3.13 A/B** |
| 14 | Embedding role clustering | $0.30-0.50 | 4-8 hr | Medium-High | **v0.3.15+ A/B** |

### Why Two-pass is NOW the v0.3.14 candidate

Originally deferred in favor of cap reduction (which seemed lower-effort).
But cap reduction was preflight-killed. Two-pass becomes the largest
remaining cost lever in the post-v0.3.11 architecture, where Stage 3 does
all tier determination. Shipping in v0.3.14 (after one v0.3.13 stabilization
round) under A/B protocol.

### DataForSEO top-up calculation

User asked whether to add $50 to DataForSEO. Math:
- GoogleJobs operating cost (post-fix): ~$0.034/run × 100 runs/month = $3.40/mo
- Grinder spillover (when Serper free tier exhausted): ~$2-5/mo
- $50 = ~5 months of headroom

**Recommendation:** Hold on top-up until v0.3.12 ships and we validate that
GoogleJobs is actually pulling roles. Right now the $50 wouldn't buy any
production GoogleJobs queries because the scraper isn't reaching DataForSEO
in proxy mode. After v0.3.12 deploy + audit confirms calls are landing, top
up $50 once.

### DataForSEO other products evaluated

User asked if other DataForSEO products could lower other costs. Honest
review:

| Product | Could it help? | Verdict |
|---|---|---|
| AI Optimization | SEO ranking for AI search engines | Irrelevant |
| SERP | Already used | Same product |
| Keyword Data | Search volume / CPC | Irrelevant — Gemini does keyword expansion in profile build |
| Domain Analytics | Competitor SEO data | Irrelevant |
| DataForSEO Labs | Keyword suggestions/intent | Marginal — Gemini does this for free |
| Backlinks | Link profiles | Irrelevant |
| OnPage | Site audit | Irrelevant — we do HTTP probes |
| Content Analysis | SEO content scoring | Irrelevant — embeddings cheaper |
| Merchant | Product data | Irrelevant |
| App Data | Mobile app store | Irrelevant |
| Business Data | US business directory | Limited US coverage; not better than what we already have |

**Verdict:** None of DataForSEO's other products meaningfully reduce our
$1.25/run cost. The cost driver is Gemini Pro output generation — not a
search-provider problem.

---

## Decision log: alternatives considered

### Decision 1: Tenant loader vs. hardcode-only

**Options:**
- **A.** Stay hardcoded, drop the JSON files (simple, one source of truth)
- **B.** Switch hardcoded → JSON-only (uniform format)
- **C.** Hybrid: hardcoded as the "verified core", JSON as "discovered extras"
  (chose this)

**Why C:** The hardcoded list represents 30+ hand-curated, verified-working
tenants with hand-picked display names. Throwing those away to standardize
on JSON would either lose the curation or require re-writing 30 JSON files
with the right display names. The hybrid keeps the curation as the safety
net (always works, even if JSON parsing breaks) and lets the grinder add to
the long tail.

### Decision 2: GoogleJobs proxy vs. env-pass-through

**Options:**
- **A.** Pass `DATAFORSEO_LOGIN/PASSWORD` from `lib.rs` to the sidecar env
  (simpler — one-line Rust change)
- **B.** Route through Worker like JSearch (chose this)

**Why B:**
- Testers (the eventual audience) won't have DataForSEO accounts. Even if
  Ziad's .env had the creds, those creds wouldn't ship to anyone else.
- Proxy mode is *the* production architecture per v0.3.5 design — the whole
  reason `LLM_PROXY_URL` exists is so testers don't need API keys.
- Adding env-pass-through would mean every future scraper API key needs
  another `lib.rs` line, vs. the proxy pattern which scales by adding a
  Worker route.

### Decision 3: Two-pass Stage 3 vs. cap reduction (UPDATED post-preflight)

**Original framing:** Two-pass is architecturally cleaner but 1-2 hr build;
cap reduction is "an 80/20 win" with 5 min effort.

**Updated:** Cap reduction was preflight-killed (10.9% STRONG at risk on
v0.3.11 audit data). Two-pass is now the v0.3.14 candidate because it
remains the largest unblocked cost lever in the post-v0.3.11 architecture
(where Stage 3 does all tier determination). A/B testing required before
ship: STRONG count delta ≥ 0, score correlation r ≥ 0.92, manual
inspection of every tier-changed role.

### Decision 4: Build SuccessFactors/Phenom now vs. later

**Options:**
- **A.** Ship in v0.3.12 alongside other fixes
- **B.** Wait for v0.3.13 and validate v0.3.12 first (chose this)

**Why B:**
- v0.3.12 already ships two infrastructure fixes (tenant loader + GoogleJobs
  proxy). Adding new scrapers would muddy the audit signal — was the role
  count change from the new scrapers, or from the tenant fix?
- The 145 JSON tenants probably include SuccessFactors/Phenom-backed
  companies *also* on Workday (Walmart, Mars, J&J — multi-ATS shops).
  Loading them might already cover the same hires.
- HTML-parsing scrapers are fragile. Better to invest the time after we
  know whether they're needed.

### Decision 5: Drop BioSpace from defaults

**Initially proposed:** Yes (0% qualifying for Ziad's profile)
**User pushback:** "Workday tenants and stuff like BioSpace aren't a waste,
they help round out other industries"
**Reversed:** Keep BioSpace + every other "0% for Ziad" scraper.

**Why reversed:** The whole v0.3.5+ thesis is universal coverage for non-AI
testers (clinical-research, billing, teaching, etc.). Cutting BioSpace would
break the universality contract. A clinical-research tester would absolutely
get value from 396 BioSpace raw → ~10-30 qualifying.

---

## Open risks

### R1 — v0.3.11 anchoring fix unproven
The user is currently running v0.3.11. We haven't seen the audit yet. If
STRONG count is still ≤ 22, the anchoring fix didn't work and we shouldn't
ship cost reductions on top of it.

### R2 — DataForSEO Worker secrets not verified
We *added* the DataForSEO route to the Worker but haven't verified the
secrets are deployed. Need to run:
```
wrangler secret list --env production
```
…and confirm `DATAFORSEO_LOGIN` and `DATAFORSEO_PASSWORD` are present. If
absent, set them before merging.

### R3 — Tenant explosion may slow the run
Going 31 → 166 tenants means 5.4× more concurrent Workday requests. Workday
scraper has `Semaphore(20)` concurrency but per-tenant timeout is 30s. Worst
case: a tenant runs slow on every keyword × 35 keywords × 30s timeout = 17
minutes per slow tenant. The 500-streak short-circuit (kicks in at 3
consecutive 500s) helps, but slow-but-200 tenants don't trigger it.

**Mitigation:** Watch v0.3.12 audit's `Workday.elapsed_s` — should be ≤ 5
minutes. If it blows up, cap to top-N tenants by historical role count.

### R4 — Bug 2 might re-occur on next new scraper
The pattern that hid GoogleJobs (silent-no-op when creds missing) is repeatable.
Any future scraper that takes API creds could fall into the same trap if its
author tests in CLI but not in proxy mode. **Process change needed:** Every
new scraper should have a CI check or smoke test that:
- Asserts `_endpoints_and_auth()` (or equivalent) returns non-default URLs
  in proxy mode
- Asserts the scraper logs SOMETHING when it returns 0 roles (no silent exit)

### R5 — Cost tracking integration incomplete
We added `cost_estimate_usd` to per-source health but haven't yet rolled it
up into `cost_breakdown.scraper_apis_usd`. The data is captured but not
surfaced in the top-line audit total. 5-minute follow-up.

---

## v0.3.11 audit results (LANDED — anchoring fix validated)

The v0.3.11 production run completed during this session. **The anti-anchoring
fix worked as designed.** This validates shipping v0.3.12 P1 cost knobs safely.

### Headline numbers

| Metric | v0.3.9 | v0.3.10 | v0.3.11 | Δ vs 3.10 |
|---|---|---|---|---|
| Cost | $1.35 | $1.25 | $1.38 | +$0.13 |
| Stage 3 cost | $1.24 | $1.14 | $1.26 | +$0.12 |
| Total scraped | 3,791 | 3,887 | 3,882 | -5 |
| Qualifying | 173 | 168 | 188 | +20 |
| **STRONG** | 20 | 16 | **46** | **+30 (+187%)** |
| GOOD | 46 | 57 | 49 | -8 |
| MAYBE | 45 | 31 | 37 | +6 |

The STRONG count blew past the 22-28 ceiling we'd been stuck at since v0.3.7.

### Score-bucket redistribution (the smoking gun)

| Bucket | v3.10 | v3.11 | Δ |
|---|---|---|---|
| 90-94 | 11 | 27 | +16 |
| 85-89 | 6 | 15 | +9 |
| 80-84 | 19 | 19 | 0 |
| **75-79 (anchor cluster)** | 46 | 40 | -6 |

Roles that exited the 75-79 anchor pile redistributed UP, not down. Exactly
the predicted outcome.

### What's NOT in v0.3.11 (still pending v0.3.12)

- Total scraped is still ~3,800 because the tenant loader fix is v0.3.12.
  v0.3.11 still uses the 31 hardcoded tenants only. Post-v0.3.12 should
  land at ~10K-15K total scraped (Workday raw 6160 → ~25K with 166
  tenants).
- GoogleJobs still 0 raw in v0.3.11 (proxy fix is v0.3.12).
- iCIMS still 0 raw (Playwright `ImportError` silent-no-op, same bug
  class as GoogleJobs — Agent 2 found this).

### v0.3.12 STRONG count target validation

We targeted ≥30 STRONG to call v0.3.12 successful. v0.3.11 alone hit 46.
With v0.3.12's tenant loader (5.4× tenants) + GoogleJobs unblocked, the
expected v0.3.12 STRONG range is **50-70**. Anything below 40 in v0.3.12
audit would suggest a regression — investigate immediately.

---

## Forward projections: what v0.3.12 audit should look like

This section consolidates the run-over-run progression and projects v0.3.12
numbers based on the changes shipping. Use these as ship-gate targets.

### Run progression: v0.3.9 → v0.3.10 → v0.3.11 → v0.3.12 (projected)

| Metric | v0.3.9 | v0.3.10 | v0.3.11 | **v0.3.12 (projected)** | Driver |
|---|---|---|---|---|---|
| **Total scraped (raw)** | 3,791 | 3,887 | 3,882 | **10,000-15,000** | Tenant loader (31→166), GoogleJobs unblocked |
| Workday raw | 6,164 | 6,453 | 6,160 | **20,000-25,000** | 5.4× tenants |
| GoogleJobs raw | 0 | 0 | 0 | **150-300** | Proxy fix |
| iCIMS raw | 0 | 0 | 0 | 0 (still broken) | ImportError surfacing only logs it |
| After hard filters | 505 | 496 | 513 | **800-1,200** | More raw → more pass-through |
| **Qualifying total** | 173 | 168 | 188 | **240-320** | More filters surviving |
| **STRONG** | 20 | 16 | **46** | **55-75** | Anchoring fix + new tenants |
| GOOD | 46 | 57 | 49 | 55-70 | Some redistribute up to STRONG |
| MAYBE | 45 | 31 | 37 | 50-65 | More volume |
| STRETCH | 56 | 59 | 53 | 75-110 | More volume |
| **Cost (total)** | $1.35 | $1.25 | $1.38 | **~$1.15-1.25** | More Stage 3 calls but Stage 3 cache saves $0.20-0.30 |
| Stage 3 cost | $1.24 | $1.14 | $1.26 | $1.00-1.10 | Cache hits + more roles |
| Duration (sec) | 1,013 | 962 | 998 | **1,200-1,500** | More tenants to scrape |

### Score-bucket trajectory (the anchoring-fix smoking gun)

| Bucket | v0.3.10 | v0.3.11 | **v0.3.12 (projected)** |
|---|---|---|---|
| 95-99 | 0 | 5 | 8-12 |
| 90-94 | 11 | 27 | 35-45 |
| 85-89 | 6 | 15 | 18-25 |
| 80-84 | 19 | 19 | 22-30 |
| 75-79 (former anchor) | 46 | 40 | 38-50 |
| 70-74 | 7 | 3 | 5-10 |

The anchoring fix in v0.3.11 already redistributed the 75-79 cluster up.
v0.3.12 should preserve this distribution AND add ~50 more roles to all
buckets via tenant expansion + GoogleJobs.

### Job accuracy expectations (qualitative)

**Will improve in v0.3.12:**
- Richmond-area employer coverage (CarMax, Markel, Owens & Minor, Federal
  Reserve all newly live)
- Federal contractor coverage (Northrop Grumman, KBR, Booz Allen extended,
  Mass General Brigham, Highmark Health all now active via tenant loader)
- LinkedIn / Indeed coverage via GoogleJobs (any role indexed by Google
  Jobs that's NOT on a curated ATS)
- Pharma / biotech for non-AI testers (the 145 grinder tenants include
  several pharma companies)

**Will NOT change in v0.3.12 (unless v0.3.13+):**
- BioSpace 0% qualifying (Ziad's profile, not a bug)
- HigherEdJobs / Jobicy / NoDesk 0% (real upstream zeros for Ziad)
- Workday's 0.2% qualifying-rate (over-fetch via empty `appliedFacets`,
  fix is v0.3.13)
- iCIMS 0 raw (Playwright fix is v0.3.13+)
- Dominion Energy / Genworth coverage (SuccessFactors/Phenom scrapers are
  v0.3.13)
- Salary coverage % (no change in extraction logic; will still hover ~45%)
- JD coverage % (no change in JD-fetch logic; will still hover ~75%)

### Cost projection breakdown

**v0.3.11 actual:** $1.38 = $1.26 Stage 3 + $0.07 Stage 2 + $0.027 embed +
$0.013 Stage 1

**v0.3.12 projected (P0 + P1 conservative):**
- More qualifying roles (188 → ~280) means more Stage 3 calls
- Stage 3 cap is still 200, so additional roles spill into "GOOD/MAYBE
  via Stage 2 only" — Stage 3 stays bounded
- Stage 3 context cache: -$0.20 to -$0.30 (75% input discount on cached
  prompt)
- Net Stage 3: ~$1.00-1.10
- Stage 2 + embed + Stage 1: +$0.05 (more input from more roles)
- **Total: ~$1.15-1.25/run**

**Why the cost doesn't drop more in v0.3.12:**
- Tenant loader brings MORE roles into the pipeline → more Stage 3 calls
- The cost lever (Stage 3 cache) saves on each call, but more calls
  partially offset
- Net effect: cost holds or drops slightly while STRONG count grows ~25%

**v0.3.13 cost target (UPDATED post-peer-review):**
- ~~`skip_above=85`~~ SCRAPPED (100% STRONG at risk)
- ~~Cap 200→100~~ SCRAPPED (10.9% STRONG at risk, see Verification C)
- Flash contradiction-resolver: SHIPS IN v0.3.12 P1 instead
- Output schema trim: SHIPS IN v0.3.12 P1 instead (cf_worker grep clean)
- Fuzzy-title dedup: -$0.10/run (medium risk, A/B required)
- Lazy summary generation: -$0.05-0.10/run (schema change)
- **Realistic v0.3.13 target: $0.75-0.90/run** (was $0.50-0.65 — corrected)

**v0.3.14+ targets (require A/B testing):**
- Two-pass Stage 3 (Flash pre-screen → Pro top-N): -$0.20-0.30
- Embedding clustering (with same-company restriction): -$0.30-0.50
- **Realistic v0.3.14 target: $0.50-0.65/run** (only if Two-pass passes A/B)
- **Realistic v0.3.15+ stretch: $0.40-0.50/run** (only if both pass A/B)

### Ship-gate criteria for v0.3.12 audit

When you run v0.3.12 in production, the audit JSON should show:

**Pass criteria (must all hold):**
- ✅ Total scraped ≥ 8,000 (proves tenant loader fix works)
- ✅ Workday raw ≥ 15,000 (proves 5.4× tenant multiplier active)
- ✅ GoogleJobs raw > 0 (proves proxy fix lands and Worker secrets are set)
- ✅ STRONG count ≥ 40 (regression check vs v0.3.11's 46)
- ✅ Total cost ≤ $1.50 (proves Stage 3 cache offsets new Stage 3 calls)
- ✅ Per-source `cost_estimate_usd` populated for GoogleJobs (proves
  cost surfacing works)
- ✅ Per-source funnel shows new Workday tenants contributing
  (e.g., Mass General Brigham, Northrop Grumman, KBR appear in
  qualifying roles)

**Investigate immediately if any of:**
- ❌ Total scraped still ~3,800 → tenant loader fix not deployed properly
- ❌ STRONG count drops below 30 → cost lever caused regression OR
  anchoring fix degraded
- ❌ GoogleJobs still 0 → Worker not deployed OR DataForSEO secrets missing
- ❌ Cost > $1.80 → Stage 3 cache not engaging (cache miss rate too high)
- ❌ iCIMS still silent (no error logged) → ImportError surfacing didn't ship

---

## All-scrapers health audit (Agent 2 findings)

### v0.3.11 per-source qualifying yield

| Scraper | Raw | Qualifying | Yield % | Notes |
|---|---|---|---|---|
| JSearch | 734 | 69 | 9.4% | Top performer; pages 6-10 silently discarded (see below) |
| Adzuna | 40 | 12 | 30% | Highest yield rate; rate-limited fast |
| BuiltIn | 233 | 16 | 6.9% | Healthy this run, 403 anti-bot mid-run risk |
| Findwork | 234 | 15 | 6.4% | Single-page only — API supports more |
| USAJOBS | 52 | 3 | 5.8% | Doesn't use upstream `DatePosted=N` |
| Greenhouse | 2,583 | 49 | 1.9% | Healthy; client-side keyword match |
| Workday | 6,160 | 14 | 0.2% | 88% dedup rate from over-fetch |
| Ashby | 471 | 7 | 1.5% | OK |
| Climatebase | 50 | 1 | 2% | Misses `posted_within_days` filter |
| RemoteOK | 10 | 1 | 10% | OK |
| BioSpace | 395 | 0 | 0% | Universal coverage (biotech testers) |
| TheMuse | 100 | 0 | 0% | Universal coverage; cross-board dedup heavy |
| Lever | 70 | 0 | 0% | Universal coverage |
| iCIMS | **0** | 0 | — | **BROKEN** Playwright ImportError silent |
| GoogleJobs | **0** | 0 | — | **BROKEN** proxy mode missing creds (v0.3.12 fix) |
| HigherEdJobs | 0 | 0 | — | Real upstream zero (RSS doesn't have AI-strategy roles) |
| Jobicy / NoDesk | 0 | 0 | — | Real upstream zeros |

### Top scraper bugs found

**1. JSearch num_pages drift IS real** — `jsearch.py:315` truncates with
`for j in items[:limit]` where `limit_per_keyword=50`. But `num_pages=10`
instructs upstream to return up to 100 items. **Pages 6-10 are silently
discarded.** That's why your 5→8→10 experiment didn't scale linearly. Two
fixes possible:
- Drop `num_pages` to 5 (matches cap, saves API quota)
- Raise `limit_per_keyword` to 100+ (uses all pages — more roles, more cost)
- **Recommend Option A** (drop to 5) — saves quota, no functional loss.

**2. Workday over-fetching by ~5×** — `workday.py:544` sends
`appliedFacets={}` (empty) to the CXS API. The API supports `locations`,
`timeType`, `jobFamily` facets. Passing `user_filters.locations` would cut
raw by 60%+, dramatically reducing the 88% dedup waste. Mirrors the
JSearch pattern at `jsearch.py:267-275`.

**3. iCIMS silent ImportError** — `icims_playwright.py:64-67`:
```python
except ImportError:
    return []
```
No `quota_exhausted` set, no error surfaced. Same silent-no-op class as
the GoogleJobs bug. Tester sees "iCIMS = 0" with no clue Playwright is
missing from the bundle.

**4. Adzuna no monthly-cap auto-recovery** — `adzuna.py:91-95` short-circuits
on first 429/403 (good for current run), but `quota_exhausted` persists
forever per-instance. Monthly cap reset on the 1st of next month is never
re-tested. Recommend `last_429_timestamp` check that auto-clears after 24h.

**5. Findwork single-page only** — `findwork.py:91-128` calls API once per
keyword, never paginates. Findwork supports `?page=N`. Adding pagination
would roughly double the pool, but Findwork's quota tripped early in
v0.3.10 — pagination would burn quota faster.

**6. BuiltIn 403 mid-run, no workaround** — `builtin.py:101-116` sets
`quota_exhausted=True` and breaks page loop on 403. No User-Agent rotation,
no proxy fallback, no Playwright path.

**7. Per-keyword raw count tracking inconsistent** — Only 6/24 scrapers
populate `per_keyword_raw_counts`. The remaining 18 leave it empty, so
audit can't surface "which keywords pulled which raw counts" for those.
Specifically Greenhouse, Lever, Ashby, Workday, TheMuse, Adzuna, BuiltIn,
Findwork, USAJOBS are auditing blind.

**8. Climatebase ignores `posted_within_days`** — `climatebase.py:46-80`
accepts the parameter but never applies it. Stale roles persist.

### v0.3.12+ scraper action items

| # | Action | Effort | Priority |
|---|---|---|---|
| 1 | JSearch: drop `num_pages` 10 → 5 | 1 line | P0 |
| 2 | iCIMS: surface ImportError as `quota_exhausted_reason` | 5 min | P0 |
| 3 | Workday: pass `appliedFacets.locations` from user_filters | 30 min | P1 |
| 4 | Adzuna: add 24h auto-clear on `quota_exhausted` | 15 min | P1 |
| 5 | Add `per_keyword_raw_counts` to all keyword-search scrapers | 2 hr | P2 |
| 6 | Climatebase: actually apply `posted_within_days` cutoff | 10 min | P2 |
| 7 | BuiltIn: User-Agent rotation or proxy fallback | 4 hr | P3 |

Action #1 (JSearch num_pages) is genuinely free — saves quota with no
functional impact. Should ship in v0.3.12.

---

## Pipeline parameter drift (Agent 3 findings)

### Critical drifts and contradictions

**A. Stage 3 docstring lies about its own behavior**
- `stage3_deep_eval.py:1-17, 56-60, 592` describe a "55-87 band"
- Actual: `skip_above=101` at orchestrator.py:155 — every s2 ≥ 55
  enters Stage 3, including 100s
- Origin: `skip_above=88` was intentionally removed pre-v0.3.5, docstrings
  never updated. Maintenance trap.

**B. Embedding `keep_fraction` has two defaults that disagree**
- `orchestrator.py:153` defaults 0.55, overrides callers
- `embedding_filter.py:68` defaults 0.60
- Anyone calling `filter_roles_by_embedding` directly (tests, scripts)
  gets 0.60 instead of 0.55. Stale.

**C. Stage 2 docstring describes wrong model**
- `stage2_triage.py:1, 12, 14`: "Gemini Pro with batch=True (50% off)"
- Actual: `STAGE2_MODEL = "gemini-2.5-flash"` per `config.py:145`
- Cost docstring "$0.30-0.55" is for Pro pricing — actual is ~$0.02-0.04
- Cost tracker reports correctly; docstring + prompt tone references stale

**D. Title-floor: prompt says (70/65/55), code applies (60/55/none)**
- Stage 2 prompt at `stage2_triage.py:236-248` describes 70/65/55 levels
- Stage 3 prompt at `stage3_deep_eval.py:266-272` same legacy levels
- Code at both sites runs `mode="graduated"` which applies 60/55/none
- The LLM is told one rule, the code enforces a different (lower) rule
- Either update prompts to graduated values, or revert code to legacy

**E. `_stage2_succeeded` strict-mode title floor is unwired**
- `title_floor.py:135` documents conditional `mode` selection
- No caller actually invokes the conditional logic
- `_stage2_succeeded` (`stage2_triage.py:857`) is unused dead code

**F. Stage 2 schema fields `key_match_signals` + `key_concerns` never read**
- Lines 357-360 require these arrays
- Stage 3 doesn't read them (anti-anchoring fix in v0.3.11 strips s2 data)
- Pure prompt cost: ~100-200 wasted output tokens × 200+ roles per run
  at Flash $0.30/MTok output = $0.04-0.08/run waste

### Dead code (~250 lines)

- `PROFILE_AUDIT_ENABLED=True` (`config.py:174`) but
  `_audit_and_refine` (`builder.py:1747`) is **never called**
- `PROFILE_AUDIT_PROMPT` (250 lines, `builder.py:1095`) is dead
- `_merge_consensus` (`builder.py:1693`) replaced by `_stage2_synthesize`,
  also dead
- `PROFILE_MERGE_PROMPT` dead

### Universalization issues (Ziad bias in "universal" prompts)

**G. Stage 2 prompt PRINCIPLE 9 hardcodes example companies**
- `stage2_triage.py:271-280` lists "Anthropic, Snorkel, Launchpad,
  OneStream, Quandri, Quisitive, Interlink, Ahead, Deloitte, PwC, EY,
  Accenture"
- These are Ziad-targets — biases scoring for any tester whose targets
  differ
- **Breaks the universality contract**

**H. Stage 2 v0.3.11 ANTI-CLUSTERING block quotes Ziad's audit data**
- "280-role run", "s2=68: 41 roles" referenced in prompt
- A tester with a 50-role pool gets reproached using Ziad's pool numbers
- Should parameterize or generalize the principle

### Profile build issues

- Resume excerpt: 8000 chars (sampling, `builder.py:949`) vs 5000 chars
  (synthesis, `builder.py:1566`) — synthesis sees less context than the
  samples it merges
- `PROFILE_SYNTHESIS_PROMPT` has TWO `#8` sections (numbering conflict)
- Claude Opus sample temperature 0.5 (warm for structured-JSON output —
  worth A/B testing 0.2)

### Hard filter issues

- Posted-date filter accepts "no date = pass" (`hard_filters.py:653`)
  combined with JD score cache (no posted-date constraint) → stale roles
  persist for 7 days × N runs
- Salary-realism demotions silently knock score 87 → 39 with no audit-trail
  tag explaining why
- Already-applied dedup uses exact-match (`(company.lower(), title.lower())`),
  no fuzzy match — typos break dedup

### v0.3.13 cleanup actions (ranked by impact)

| # | Action | Effort | Risk |
|---|---|---|---|
| 1 | Reconcile title-floor prompt vs code (D, E above) | 30 min | Low |
| 2 | Delete dead profile audit/merge code (~250 lines) | 1 hr | None |
| 3 | Fix Stage 3 docstring 55-87 lie | 5 min | None |
| 4 | Make `embedding_filter.keep_fraction` default 0.55 | 5 min | None |
| 5 | Strip Ziad-specific examples from Stage 2 prompt (G) | 1 hr | None |
| 6 | Generalize anti-clustering principle (H) | 30 min | None |
| 7 | Drop `key_match_signals`/`key_concerns` from Stage 2 schema | 30 min | Low |
| 8 | Tag salary-realism demotions in audit | 30 min | None |
| 9 | Add posted-date check to JD score cache | 30 min | Low |

---

## Stage 3 cost deep-dive (NEW — added post-research)

A dedicated research pass on `stage3_deep_eval.py` surfaced **four cost
levers we previously missed entirely**. These are higher-ROI than the
original cap-reduction proposal because they're zero-quality-risk and
together close ~50% of the Stage 3 cost gap.

### Lever 1: Stage 3 has NO context cache (BIGGEST MISS)

`stage2_triage.py:759-792` implements a Gemini context cache for the
Stage 2 system prompt — 75% input discount on cache hits. This same
infrastructure exists in `_complete_with_cache_fallback`. **Stage 3
does not use it.** Every one of N Pro calls re-pays the full ~2.5K-token
system prompt at $1.25/MTok input.

Math at 100 roles/run:
- Input wasted: 100 × 2,500 tokens × $1.25/MTok = **$0.31/run**
- Cache cost (one-time): ~$0.05 prewarm + 25% of $0.31 hit rate = $0.13
- Net savings: **~$0.20/run, zero risk, ~1 hour to implement**

This is just porting the Stage 2 pattern over. The code already exists.

### Lever 2: `skip_above=101` is wrong — should be 85

`stage3_deep_eval.py:460` defaults `skip_above=101`, meaning a role with
Stage 2 score 99 still re-runs through Stage 3. The original docstring
(now stale) was designed for `skip_above=88` — confident-STRONG roles
skip Stage 3 entirely. That parameter got dialed off somewhere.

Restore `skip_above=85`:
- Skips ~8-15 roles per run (s2 ≥ 85, confidence ≥ 0.8)
- Re-routes them to a tiny Flash call to compose dashboard fields only
- Saves ~$0.10-0.15/run, near-zero quality risk
- Effort: 30 min

This is reverting an unintended change, not a new feature.

### Lever 3: Contradiction detector re-runs FULL Pro

`_resolve_stage3_contradictions` (`stage3_deep_eval.py:731`) runs the
full Stage 3 prompt for ~20 contradicted roles per run = +$0.16/run.

The detector only needs to resolve "your score conflicts with your text
— pick one." Replace with Flash 2.5 + thinking_budget=2048, output 2
fields only (corrected score + revised one-sentence rationale). Saves
~$0.10-0.13/run with negligible quality drift.

### Lever 4: Output schema padding

The Stage 3 response schema (`stage3_deep_eval.py:350-378`) requires 9
fields. Three are redundant:
- `tier_rationale` duplicates the opening of `match_analysis`
- `best_resume_reason` duplicates the justification in `best_resume_match`
- `application_strategy` is `[:1000]`-truncated; should be 400 chars

Output is the dominant cost ($5/MTok). Trimming saves ~30% of output
tokens = $0.05-0.08/run. Effort: 1 hour.

### Combined quick-wins stack

| Lever | Savings | Effort | Risk |
|---|---|---|---|
| 1: Stage 3 context cache | $0.20-0.31 | 1 hr | None |
| 2: Restore `skip_above=85` | $0.10-0.15 | 30 min | None |
| 3: Flash contradiction resolver | $0.10-0.13 | 2 hr | Low |
| 4: Trim output fields | $0.05-0.08 | 1 hr | None |
| **Total** | **$0.45-0.67** | **5 hr** | **~Zero** |

This stack alone takes the run cost from $1.25 → ~$0.70-0.80. **Two-pass
Stage 3 + Flash-as-primary become unnecessary** at that point. We get
80% of cost savings without architectural risk.

### Tier-2 cost ideas (think-outside-the-box, beyond Agent 1's 4 quick-wins)

These are additional levers I researched after Agent 1's report. Some are
novel, some are ideas Agent 1 considered and rejected — re-examined here.

**5. ~~Compress JD with Flash before Pro sees it~~ — REJECTED BY USER**
- *Rationale:* User has seen JD compression hurt scoring quality in
  prior experiments. Stage 3 needs the full JD context to make
  accurate fit/salary/requirements judgments. Compression risks
  dropping signals like specific tools, years of experience, salary
  ranges that the Pro deep-eval relies on.
- Status: **DO NOT IMPLEMENT.** Keep JDs at full 16K-char truncation.
- Peer Claude: please don't re-suggest this lever.

**6. Embedding-based role clustering before Stage 3** ($0.50-0.75/run,
4-8 hr, medium risk)
- Cluster the 200 candidates by role-embedding cosine similarity (>= 0.92)
- Only deep-eval the cluster representative
- Propagate score + analysis to cluster mates (with note "scored via
  cluster sibling: <role>")
- Saves ~75% of Pro calls if average cluster size is 4
- Risk: cluster mates aren't always equivalent (different companies have
  different cultures even at same title)
- Mitigations: (a) only cluster within same company, (b) only propagate
  to cluster mates within ±5 score points

**7. Skip Stage 3 entirely for confident-STRETCH (s2 < 50, conf > 0.8)**
($0.15-0.20/run, 30 min, low risk)
- Currently second-look band (35-54, conf<0.8) does enter Stage 3
- Inverse: confident-low s2=40 with conf=0.85 still re-runs through Pro
- Restoring `skip_below=55` strict (no second look) saves ~10-15 roles/run
- Risk: occasionally Pro rescues a Stage-2-misjudged role. Small population.

**8. Lazy summary generation** ($0.05-0.10/run, 4 hr, schema change)
- Stage 3 generates `application_strategy` for every role
- Most STRETCH/MAYBE roles never get clicked by user
- Move strategy generation to lazy on-click endpoint (Flash, $0.0005/call)
- Saves cost AND makes initial UI render faster
- Schema change required for the new endpoint

**9. Generate company-level strategy once, fill role-specific with Flash**
($0.10-0.15/run, 8 hr, complex)
- Same company = similar value prop, similar challenges
- Generate company-level "Application Strategy" once with Pro
- Generate role-specific addendum with Flash
- Saves ~30% of strategy tokens
- Best for runs with multiple roles per company (Workday tenants often
  have 5-10 roles)

**10. Streaming-aggregate UI** (no $ savings, 4 hr, UX win)
- Display results as they score, don't wait for all 200
- Doesn't save cost but improves perceived performance
- Lets user kill long runs early if they see enough STRONG matches

**11. Pre-warm Gemini context cache on user login** (~$0.02-0.05/run, 1 hr)
- If user is in dashboard (likely to search soon), pre-warm Stage 2/3
  cache before they click "Search"
- Saves cold-start cache cost on first call

**12. Scraper-level fuzzy-title dedup** ($0.10/run, 4 hr, low risk)
- Workday's 88% dedup rate suggests massive cross-tenant duplication
- Add fuzzy-title dedup pre-scoring: roles with same company + Jaro-Winkler
  title similarity > 0.92 collapse to one
- Saves Stage 1/2/3 calls on duplicates
- Captures ~10% more dupes than current exact-match dedup

**13. Output token max cap of 1500** ($0.05/run, 30 min, prompt risk)
- Stage 3 has `max_output_tokens=4096` but actual emit is ~2K tokens
- Cap at 1500 with stricter prompt instruction ("Be concise: 3-4
  sentences for match_analysis, 2-3 for strategy")
- Output cost drops 25%
- Risk: occasional truncation. Requires prompt update + monitor

**14. JSearch num_pages 10 → 5** (no $ savings on Stage 3, but free
JSearch quota)
- Per Agent 2: pages 6-10 are silently discarded due to limit cap at 50
- Reducing num_pages saves API quota with zero functional impact
- Should ship as part of v0.3.12 or v0.3.13

### Combined extended cost trajectory (post user feedback)

| Phase | Run cost | Cumulative | Notes |
|---|---|---|---|
| v0.3.10 baseline | $1.25 | — | |
| v0.3.11 (anchoring fix landed) | $1.38 | — | More qualifying = more Stage 3 |
| v0.3.12 P0 (infra fixes only) | ~$1.45 | — | More tenants → more Stage 3 |
| v0.3.12 P1 (4 quick-wins from Agent 1) | $0.78-0.95 | $0.50-0.67 | All zero quality risk |
| v0.3.13 Tier-A (#6: clustering, #7: skip-low) | $0.50-0.65 | +$0.20 | JD compress REJECTED |
| v0.3.14 Tier-B (#8: lazy summary, #12: fuzzy dedup, #13: output cap) | $0.40-0.50 | +$0.10-0.15 | |

Target: **~$0.45/run = ~67% reduction from v0.3.11 cost**. Lower than the
original $0.35 target because we removed the JD-compression lever per user
constraint. All remaining Tier A/B levers are reversible if quality regresses.

### Hard quality constraints (don't violate)

These are user-stated constraints. The peer Claude should treat them as
inviolable when proposing new cost-reduction ideas:

1. **JDs stay at full 16K-char context for Stage 3.** No compression, no
   summarization, no Flash-pre-pass on the JD content. Stage 3 needs the
   raw JD to score salary/requirements/fit accurately. (Confirmed
   regression in prior experiments.)
2. **STRONG count cannot regress.** v0.3.11 hit 46 STRONG. Any cost lever
   that reduces STRONG count below 30 in the same audit conditions is a
   quality regression and must be reverted.
3. **Universal scrapers stay in roster.** BioSpace, HigherEdJobs, Jobicy,
   NoDesk all return 0 for Ziad but are needed for non-AI testers
   (clinical-research, university admin, remote-tech). Don't drop them.
4. **Stage 3 model stays Pro for primary scoring.** Flash can be used for
   contradiction-resolver patches and lazy-summary generation, but the
   primary score determination stays Pro until A/B testing definitively
   shows Flash matches Pro on STRONG/GOOD tier assignments (correlation
   > 0.92 minimum).

### Quality-cautious ship gating (user-stated posture)

User explicitly stated: "i want to be very cautious on implementing cost
saving changes that could affect quality." This translates to:

- **No cost lever ships in v0.3.12 unless quality risk is mathematically
  zero.** Every lever Agent 1 labeled "zero risk" was re-examined and only
  3 actually qualify (see below).
- **Medium-risk levers must pass A/B against the cached 870-role corpus
  before shipping.** Test methodology defined in the next section.
- **Score-affecting changes require manual inspection** of tier-changed
  roles, not just statistical correlation.

#### Re-classification of cost levers by ACTUAL risk

| Lever | Original claim | Re-examined risk | Ship now? |
|---|---|---|---|
| Stage 3 context cache | Zero risk (Agent 1) | **Truly zero** — caches input tokens, output is byte-identical (same model, same temperature=0.0, same prompt content) | Yes (P1) |
| JSearch `num_pages 10→5` | Zero risk (Agent 2) | **Truly zero** — pages 6-10 are silently discarded via `items[:limit]` cap; just stops fetching data we throw away | Yes (P1) |
| iCIMS surface ImportError | Zero risk | **Truly zero** — observability only, no behavior change | Yes (P0) |
| Trim output schema | Zero risk (Agent 1) | **Low risk** — redundant fields claim is unverified against the dashboard. May display empty UI cells. Need frontend grep first. | Defer to A/B |
| Restore `skip_above=85` | Near-zero (Agent 1) | **Low-medium risk** — v0.3.11 STRONG=46 came partly from Pro lifting s2=70-80 roles via +8.17 avg delta. Skipping Pro for s2≥85 could shift 5-10 STRONG → GOOD in edge cases. | Defer to A/B |
| Flash contradiction-resolver | Low risk (Agent 1) | **Medium risk** — Flash quality regressions seen in prior experiments. If Flash misjudges the correction, we re-introduce the original bug it's supposed to fix. | Defer to A/B |
| Embedding clustering | Medium risk | **Medium-high** — cluster mates have different cultures, requirements. Score propagation is risky. | Defer indefinitely or scrap |
| Skip-confident-STRETCH | Low risk | **Low-medium** — same risk as skip_above but for the bottom band. | Defer to A/B |
| Lazy summary | Schema change risk | **Low risk to scoring**, medium UX risk | Defer to v0.3.14 |
| Fuzzy-title dedup | Low risk | **Medium** — could over-merge legitimately distinct roles | Defer |
| Output token cap (1500) | None claimed | **Medium** — could truncate match_analysis | Defer |

**v0.3.12 P1 ship list (FINAL — post peer review + verifications):**
1. Stage 3 context cache → $0.25-0.38/run savings (peer's revised estimate)
2. JSearch `num_pages: 10 → 5` → API quota saved
3. iCIMS surface ImportError as `quota_exhausted_reason`
4. Flash contradiction-resolver (with `resolved_by: "flash"` audit tag)
5. Trim output schema — all 3 fields (cf_worker grep clean)

**Total expected cost reduction in v0.3.12 P1:** $0.40-0.59/run (down from
$1.45 to ~$0.86-1.05). Up from initial conservative estimate because peer
verifications cleared two levers initially deferred to v0.3.13.

**v0.3.12 PyInstaller bundle defense (peer's biggest concern):**
- Spec file (`backend.spec`) explicitly adds every `workday_tenants/*.json`
  to `datas` list — belt-and-suspenders against `collect_all('backend')`
  missing them
- `_resolve_tenants_dir()` in `workday.py` falls back to `sys._MEIPASS`
  if `__file__`-relative path is empty
- Module-import logs: `[workday] tenant loader: hardcoded=31 dynamic=139
  merged=166 tenants_dir=...` — visible in audit's stdout, lets us verify
  the bundle includes JSON files immediately on first production run

### A/B test methodology for v0.3.13 cost levers

Before shipping any medium-risk lever, run this protocol:

1. **Baseline:** `--rescore` cached 870-role corpus with current code → save scores
2. **Treatment:** `--rescore` same corpus with new lever → save scores
3. **Compute three metrics:**
   - STRONG count: `treatment_strong - baseline_strong` (must be ≥ 0)
   - Per-role score correlation r: must be ≥ 0.92
   - Tier-changed roles: manually inspect every role where tier dropped
     from STRONG to GOOD or below
4. **Ship gate:** all three metrics pass + manual inspection finds no
   "obviously wrong" demotions
5. **If any gate fails:** investigate, fix, or scrap the lever

Cost per A/B test: ~$1.50 baseline + $1.50 treatment + sanity check = ~$4.50
per candidate lever. Cheap insurance against quality regressions.

### Levers awaiting A/B validation (v0.3.13 candidates)

Pre-flight checklist for each:

**Restore `skip_above=85`:**
- Need to confirm: how many roles in v0.3.11 STRONG (46) came from Pro
  lifting an s2 score above 85? If >5 of those 46 were "Pro rescues,"
  shipping this would regress STRONG count.
- Pre-A/B grep: count rows where `stage2_score < 85 AND stage3_score >= 85`
  in v0.3.11 audit. If <5%, low risk. If >10%, high risk.

**Flash contradiction-resolver:**
- Need to confirm: how many of the 46 STRONG roles in v0.3.11 went through
  the contradiction-detector path? Those are the at-risk roles.
- Pre-A/B grep: count `_resolve_stage3_contradictions` invocations from
  v0.3.11 logs (if logged) or estimate from contradiction-pattern rate.

**Trim output schema:**
- Pre-A/B grep: search frontend (`desktop_app/src/`) for `tier_rationale`,
  `best_resume_reason`, `application_strategy`. If displayed in UI, fixing
  the UI is part of the lever's scope. If unused, safe to remove.

---

## PREFLIGHT RESULTS (executed against v0.3.11 audit data)

The preflight checks above were actually run against the v0.3.11 production
audit. Results materially change the risk classification — one lever Agent 1
called "near-zero risk" is actually **catastrophic**, two others are safer
than initially classified.

### Preflight 1: `skip_above=85` is HIGH RISK — DO NOT SHIP

**Method:** Counted v0.3.11 STRONG roles where `stage2_score < 85` (i.e.,
roles that Stage 3 Pro lifted into the STRONG tier). Those are the roles
that would lose their Pro deep-eval under `skip_above=85`.

**Result: 46 of 46 STRONG roles (100%) had stage2_score < 85.** Not a
single STRONG role in v0.3.11 had s2 >= 85.

```
STRONG roles with s2 >= 85 (would safely skip Stage 3): 0
STRONG roles with s2 < 85 (Pro lifted them - AT RISK):  46
AT-RISK PERCENTAGE: 100.0%
```

**Pro lifts in v0.3.11 STRONG tier (top examples):**

| s2 | s3 | Lift | Company | Title |
|---|---|---|---|---|
| 55 | 94 | +39 | TriWest Healthcare Alliance | Mgr, AI Programs & Governance |
| 57 | 87 | +30 | Cresta | Forward Deployed Product Manager, AI Agent |
| 57 | 93 | +36 | GoMining | AI Transformation Lead |
| 57 | 86 | +29 | Loftware | AI Strategy Lead |
| 60 | 93 | +33 | VO2 Group | InsideBoard AI Project Manager |
| 63 | 91 | +28 | EY | AI Strategy - Life Sciences Sector - Senior Manager |
| 63 | 86 | +23 | Elsevier | Strategic Engagement Manager - AI Solutions |
| 63 | 93 | +30 | Writer | Enterprise AI customer success manager |
| 63 | 94 | +31 | Capital One | Senior Business Manager - GenAI Manager |
| 63 | 86 | +23 | C3.ai | Senior AI Engagement Manager |

**Why this happens:** The v0.3.11 anchoring fix deliberately makes Stage 2
*conservative* (the anti-clustering rules forbid scores at 58/68/78). Stage 3
Pro is now doing the *primary score determination* — Stage 2 is just a
qualifier. Skipping Pro would undo the entire v0.3.11 improvement.

**Sensitivity check:** Even with `skip_above=88`, all 46 STRONG roles are
still at risk. There is no skip threshold under 100 that doesn't regress
v0.3.11's STRONG count.

**Verdict: SCRAP this lever entirely.** Agent 1's recommendation was
correct for the pre-v0.3.11 codebase but is now invalid. The anti-clustering
prompt change inverted the Stage 2/Stage 3 distribution shape.

**Cost impact:** $0.10-0.15/run savings forfeit. Worth it.

### Preflight 2: Flash contradiction-resolver is LOW RISK — SHIP

**Method:** Counted STRONG roles with re-eval markers in `stage3_analysis`
text (the contradiction-resolver path leaves traces).

**Result: 0 of 46 STRONG roles had contradiction markers.** The contradiction
path fires for ~34 of 187 total qualifying roles, but **none** of them
become STRONG.

```
Roles with contradiction-resolver text markers: 34 of 187 (18%)
STRONG roles touching contradiction path:        0 of 46  (0%)
STRONG-AT-RISK PERCENTAGE: 0.0%
```

**Why this is safe:** The contradiction-resolver fires when Stage 3's score
disagrees with its own text reasoning — that's a quality-correction step
that lands roles in lower tiers (MAYBE/STRETCH). If Flash misjudges the
correction, the role might land 1-2 tiers off, but never in STRONG. STRONG
roles are confidently scored on first pass.

**Verdict: PROMOTE to v0.3.12 P1.** Was deferred to A/B; preflight clears it
as a safe ship.

**Cost impact:** $0.10-0.13/run savings preserved.

### Preflight 3: Trim output schema is LOW RISK — SHIP (partial)

**Method:** Grepped `desktop_app/src/` for `tier_rationale`,
`best_resume_reason`, `application_strategy` to verify which are actually
displayed in the UI.

**Results:**
- `tier_rationale`: **Zero references** in `desktop_app/src/`. Generated
  in Stage 3 backend, never read by frontend. Pure waste.
- `best_resume_reason`: **Zero references** in `desktop_app/src/`. Same
  story — pure waste.
- `application_strategy`: One reference at `src/types/index.ts:28` (TypeScript
  type definition only — no component reads or displays it). Type can stay;
  removing the runtime field has zero UI impact.

**Verdict: PROMOTE to v0.3.12 P1.** All three fields are safe to remove or
truncate.

**Cost impact:** $0.05-0.08/run savings preserved.

### POST-PEER REVIEW UPDATES (2nd verification round)

The peer Claude reviewed this plan and surfaced 3 additional verification
items. All ran. Results:

**Verification A: cf_worker/src/ grep for trim-output fields**
- Peer flagged: I only checked `desktop_app/src/`, not the admin dashboard
  Worker code
- Result: **ZERO references** to `tier_rationale`, `best_resume_reason`,
  or `application_strategy` in `cf_worker/src/`
- Verdict: Trim output schema is fully safe — ship ALL 3 fields, not
  partial

**Verification B: Lever scraper investigation (70 raw, 0 qualifying)**
- Peer flagged: 70 raw / 0 qualifying might indicate same silent-zero
  bug class
- Result: Lever roles are not in `qualifying` OR `near_miss` — they're
  filtered at hard-filter or embedding stage. Working as designed
  (likely engineering roles being correctly excluded)
- Verdict: NOT a bug. Lever cleared.

**Verification C: Cap 200→100 risk — STRONG distribution by rank**
- Peer flagged: post-v0.3.11 architecture means Stage 3 does ALL tier
  determination. Cap reduction may cost more quality than dollars.
- Result: **5 of 46 STRONG roles (10.9%) come from rank band 101-200**
  - Rank 109: VO2 Group InsideBoard AI (s2=60 → s3=93)
  - Rank 111: GoMining AI Transformation Lead (s2=57 → s3=93)
  - Rank 112: Loftware AI Strategy Lead (s2=57 → s3=86)
  - Rank 128: Cresta Forward Deployed PM (s2=57 → s3=87)
  - Rank 145: TriWest Healthcare AI Programs (s2=55 → s3=94)
- Above peer's 5% threshold for "low risk"
- Verdict: **Cap 200→100 is SCRAPPED indefinitely.** This is the second
  cost lever blocked by post-v0.3.11 architecture (skip_above=85 was
  the first). Both blocked because Stage 2 became a binary qualifier
  after anti-clustering.

### New v0.3.12 P0 items (peer-recommended additions)

The peer recommended 3 additions to v0.3.12 P0 (zero risk, 5-15 min each):

1. **Silent-zero scraper alert** (originally Q9, deferred to v0.3.13)
   - 5 lines in `orchestrator.py` after success-path health write:
     ```python
     if (not health[source]["errored"]
         and health[source]["roles"] == 0
         and health[source]["elapsed_s"] < 0.1):
         print(f"[orchestrator] WARNING: {source} exited "
               f"without doing work — possible config issue")
     ```
   - Catches the entire bug class (GoogleJobs, iCIMS) on first run
   - **Promoted to P0** because it's the cheapest process improvement
     in this project's history (would have caught GoogleJobs in v0.3.9
     instead of v0.3.11)

2. **Title-floor prompt/code reconciliation**
   - Originally targeted v0.3.13 cleanup
   - Stage 2 prompt at `stage2_triage.py:236-248` says floors are
     70/65/55. Code applies 60/55/none.
   - Stage 3 prompt at `stage3_deep_eval.py:266-272` same legacy levels.
   - LLM is doing reasoning work to reach a floor the code overrides
   - 5-min fix: update prompts to match code (60/55/none)
   - **Promoted to P0** because it improves audit consistency and stops
     wasting reasoning tokens on a phantom floor

3. **Stage 2 anti-clustering generalization**
   - Originally targeted v0.3.13 universalization work
   - Current prompt cites specific Ziad audit data ("280-role run",
     "s2=68: 41 roles")
   - Billing tester's pool is different size with different patterns
   - 10-min fix: replace absolute numbers with relative pattern
     ("if more than 15% of roles share the same score, redistribute")
   - **Promoted to P0** because Ziad-specific numbers in a "universal"
     prompt are actively harmful for non-Ziad testers right now

### Updated v0.3.12 P1 additions

- **Flash contradiction-resolver**: ship with `resolved_by: "flash"`
  audit tag so we can spot-check v0.3.12 audit for any STRONG roles
  that went through Flash resolution (peer's mitigation for population
  shift in v0.3.12's tenant expansion)

- **Stage 3 context cache savings UPDATED**: peer noted Stage 3's
  system prompt is ~3,500-4,000 tokens (vs Stage 2's ~2,500). Real
  cache savings may be **$0.25-0.38/run** (was estimated $0.20-0.31).

### New monitoring alert

Peer recommended additional alert beyond `elapsed_s == 90.0`:
- Also alert if `elapsed_s > 60.0 AND roles == 0` — catches the case
  where polling succeeds (doesn't hit deadline) but all tasks returned
  empty results due to DataForSEO upstream issues. v0.3.13 candidate.

### Updated STRONG count target (per peer)

Was: ≥30 to call v0.3.12 successful (peer says too conservative).
**Now: ≥40 (regression check vs v0.3.11's 46), stretch 55-70.**
- Below 40 = investigate
- Below 30 = revert

### Updated v0.3.12 P1 ship list (post-preflight)

Promoted from "A/B required" to "ship in v0.3.12":

| Lever | Risk | Savings |
|---|---|---|
| Stage 3 context cache | None (input cache only) | $0.20-0.31 |
| JSearch num_pages 10 -> 5 | None (already-discarded data) | API quota |
| iCIMS surface ImportError | None (observability) | n/a |
| **Flash contradiction-resolver** | **None (0 STRONG roles affected)** | **$0.10-0.13** |
| **Trim output schema** | **None (fields not in UI)** | **$0.05-0.08** |
| **Total v0.3.12 P1 savings** | | **$0.35-0.52/run** |

Promoted from "A/B required" to "DO NOT SHIP":

| Lever | Verdict |
|---|---|
| Restore `skip_above=85` | **SCRAP** - 100% STRONG-at-risk in v0.3.11 |

### Revised cost trajectory (post-peer 2nd-round review)

Peer correctly flagged that the v0.3.13 column previously showed $0.50-0.65,
based on cap reduction that's now scrapped. Without cap reduction, v0.3.13
reaches $0.75-0.90 (small safe wins only). The $0.50 target requires
Two-pass Stage 3, which is now A/B-gated and lands in v0.3.14 at earliest.

Realistic trajectory:

| Phase | Run cost | Cumulative | What lands |
|---|---|---|---|
| v0.3.11 baseline | $1.38 | — | (current) |
| v0.3.12 P0 (more roles + 3 promoted items) | ~$1.45 | — | Tenant loader, GoogleJobs proxy, prompt cleanups |
| v0.3.12 P1 (5 preflight-cleared levers) | **$0.90-1.10** | $0.35-0.55 | Stage 3 cache, Flash contradiction, output trim, JSearch, iCIMS |
| v0.3.13 (small safe wins, no Two-pass) | **$0.75-0.90** | +$0.15-0.20 | Fuzzy dedup ($0.10), lazy summary ($0.05-0.10) — both A/B required |
| v0.3.14 (IF Two-pass passes A/B) | $0.50-0.65 | +$0.20-0.30 | Two-pass Stage 3 |
| v0.3.15+ (clustering IF passes A/B) | $0.40-0.50 | +$0.10-0.15 | Embedding-based role clustering |

**Honest expectation:** ~50-60% cost reduction from $1.38 → $0.55-0.65/run
across v0.3.12 + v0.3.13 + v0.3.14, with quality preserved at every step.
Not the original $0.45 (75%) target — that required cap reduction which is
now permanently blocked.

The trade-off: every dollar of savings is real savings, not borrowed
against quality. The preflight discipline that scrapped skip_above=85 and
cap 200→100 is the reason the remaining roadmap is trustworthy.

### Two-pass Stage 3 — promoted to v0.3.14 candidate

Originally deferred indefinitely because the 4 quick-wins seemed sufficient.
After cap reduction was scrapped, Two-pass becomes the largest remaining
high-leverage lever. Reasoning per peer:

> "The Two-pass Stage 3 idea (Flash pre-screen → Pro top-N) becomes more
> attractive in this architecture because it preserves Pro's tier
> determination on the top band while saving on the long tail."

A/B protocol required before shipping (same as v0.3.13 levers):
1. `--rescore` cached corpus with current code → baseline
2. `--rescore` with Two-pass → treatment
3. STRONG count delta ≥ 0
4. Per-role score correlation r ≥ 0.92
5. Manual inspection of every tier-changed role

Effort: ~1-2 hr implementation + ~$4.50 A/B testing. Target $0.20-0.30
savings if A/B clears.

### Peer's deeper architectural observation

Peer noted: "After the v0.3.11 anti-clustering fix, Stage 2 scores are
compressed into the 55-75 range with almost nothing above 80. Stage 3 Pro
is doing ALL the tier determination. This means Stage 2 is functioning as
a binary qualifier (pass/fail at 55) rather than a triage (rough-rank for
Stage 3 prioritization)."

This is significant. It means:
- Cost levers that reduce Stage 3 calls (cap reduction, skip thresholds)
  directly reduce STRONG coverage proportionally
- The remaining cost levers must either (a) make each Stage 3 call cheaper
  (cache, output trim, Flash for ancillary calls) or (b) deduplicate roles
  before Stage 3 (fuzzy-title dedup, embedding clustering)
- The Two-pass Stage 3 idea (Flash pre-screen → Pro top-N) becomes more
  attractive in this architecture because it preserves Pro's tier
  determination on the top band while saving on the long tail

This shifts v0.3.14+ thinking. We may want Two-pass after all, just not
yet.

### Items rejected from the deep-dive

- **`thinking_budget=0`**: v0.3.8 history showed contradictions doubled
  when thinking was cut. Hard reject.
- **Free Gemini quota tiers**: multi-key rotator already exploits this.
- **Batch API (50% off)**: 24h SLA breaks the interactive UX.
- **Claude Haiku 4.5**: cheaper input, same output rate as Pro. Net
  savings ~10% — not worth a provider swap.

### Updated v0.3.12 cost roadmap (HISTORICAL — see "Revised cost trajectory" further down for current numbers)

**HISTORICAL NOTE:** This original cost roadmap was based on cap 200→100
shipping in v0.3.13 with $0.20 savings. Preflight killed cap reduction.
The CURRENT roadmap is in the "Revised cost trajectory (post-peer 2nd-round
review)" section near the bottom of the doc. This table is preserved for
historical context only.

Original (now-stale) projections:

| Phase | Run cost | Cumulative savings |
|---|---|---|
| v0.3.10 baseline | $1.25 | — |
| v0.3.12 P0 (infra fixes only) | ~$1.30 (more roles → more Stage 3) | — |
| v0.3.12 P1 (4-lever stack above) | $0.65-0.85 | $0.45-0.67 |
| ~~v0.3.13 (cap 200→100, threshold 60→65 IF needed)~~ | ~~$0.50-0.65~~ | ~~+$0.20~~ |
| ~~v0.3.14+ (Two-pass IF needed)~~ | ~~$0.40~~ | ~~+$0.15~~ |

**Current realistic roadmap** (cap reduction scrapped):
- v0.3.12 P1: $0.86-1.05 (Stage 3 cache + Flash contradiction + output
  trim)
- v0.3.13: $0.75-0.90 (fuzzy dedup A/B, lazy summary A/B)
- v0.3.14: $0.50-0.65 (only if Two-pass passes A/B)

Post-P1 cost is sustainable: $0.86-1.05/run × 100 runs/mo = $86-105/mo
across all testers — at the GLOBAL_MONTHLY_CAP_USD = $100 ceiling, so
v0.3.13 fuzzy-dedup savings become important to stay within budget.

---

## Answers to my own questions (for peer to push back on)

I committed to providing my own best answer to each Q below. Peer
should challenge any reasoning that has gaps.

### Q1 — Are we right to delay Two-pass Stage 3?

**Answer: YES, with stronger conviction now.**

After Agent 1's research, Stage 3 has $0.45-0.67/run of zero-risk savings
already identified that we haven't tapped (the 4-lever quick-wins stack).
Those land us at ~$0.70-0.80/run total. Two-pass would add $0.40-0.50
more savings on top, but at much higher complexity (~200 lines of new
code, A/B test required).

**Order of operations:**
1. Ship the 4 quick-wins in v0.3.12 P1 (5 hours total work)
2. See where cost lands (target: $0.60-0.80/run total)
3. ONLY add Two-pass if monthly budget pressure persists

Two-pass is also semi-redundant if we eventually move to Flash-as-primary
(Agent 1 lever #4). The two are conceptually overlapping: Two-pass uses
Flash to triage; Flash-as-primary IS the triage. We'd only build one.

### Q2 — Should we ship SuccessFactors/Phenom in v0.3.12?

**Answer: NO — hold for v0.3.13. Compromise: v0.3.12.1 point release
within a week IF the audit shows we need them.**

Reasons:
1. v0.3.12 already has 5+ separate changes. Adding 2 new scrapers
   muddies audit attribution.
2. The tenant loader fix (31→166) brings in companies often on multiple
   ATSes (Mars, Walmart, J&J are on Workday + Phenom). The Workday
   slice may already cover those targets.
3. HTML-parsing scrapers are fragile. Defer ongoing maintenance work
   until we know it's needed.
4. Each tenant requires manual company-code discovery (Dominion's SF
   code is `dominionreP3`, not `DominionEnergy`). Per-tenant onboarding
   cost is high.

If v0.3.12 audit shows Richmond targeting still missing Dominion +
Genworth, ship a v0.3.12.1 point release with both scrapers within 7
days.

### Q3 — Deprecate integrate-script's DISPLAY_NAME_OVERRIDES?

**Answer: YES, but in v0.3.13.**

Path: New shared module `backend/scraper/_display_names.py` containing
the dict. Both `workday.py` (loader) and
`scripts/integrate_grinder_tenants.py` (writer) import from it. Single
source of truth.

For v0.3.12: leave the duplication in place. Both files currently work.
Adding the refactor in v0.3.12 would require updating the PyInstaller
bundle inclusion list to ensure `_display_names.py` ships with the
binary. That's an extra build-time risk we don't want to take on the
same release as the tenant-loader rewrite.

### Q4 — Is the GoogleJobs 90s polling deadline right?

**Answer: 90s should hold; monitor and bump to 120s only if needed.**

Math:
- DataForSEO priority=2 typical response: 6-12s
- Worker proxy adds: 50-200ms per request (negligible)
- Polling loop: 2s sleep × ~10-15 retries max = 20-30s
- Submit phase: 2-3s
- **Total worst case: ~50-60s** under healthy conditions, well under 90s

90s becomes a problem only if:
- DataForSEO queues tasks (priority=2 stuck behind priority=1)
- Cloudflare Worker cold start adds latency
- Network egress slowdown

**Decision:** Leave at 90s. Add an alert when audit shows
`GoogleJobs.elapsed_s == 90.0` (deadline-hit signature) so we know to
investigate. v0.3.13 candidate.

### Q5 — Should scraper API costs count against budget caps?

**Answer: YES, but defer integration to v0.3.13.**

Current caps:
- `PER_RUN_CAP_USD: $5` — won't be hit even with full GoogleJobs +
  grinder spillover at $0.20/run worst case
- `PER_LICENSE_MONTHLY_CAP_USD: $20` — at $0.10/run × 100 runs = $10
  scraper cost, ~50% of cap. Significant.
- `GLOBAL_MONTHLY_CAP_USD: $100` — across all testers

For v0.3.12: surface the data in `cost_breakdown.scraper_apis_usd`
(already added). For v0.3.13: include scraper_apis_usd in the cap
totals so the daily cap check doesn't underestimate spend.

Premature cap integration on data nobody has seen yet creates noise
without payoff. Surface first, gate later.

### Q6 — Are there other "silently zero" scrapers we haven't checked?

**Answer: Likely YES. Initial suspects:**
- **iCIMS** — v0.3.10 audit showed 0 raw, 0 elapsed_s, errored=False.
  Exact pattern as broken GoogleJobs. **Strongly suspicious — same
  proxy-mode bug class.**
- **HigherEdJobs** — 0 raw across all 29 keywords across multiple runs.
  Could be RSS-parser broken, or actually no university roles match
  Ziad's keywords (RSS feeds tend to be small).
- **Jobicy / NoDesk** — RSS feeds, 0 raw. Could be 404'ing or Ziad's
  keywords just don't match remote-tech curated jobs.

Agent 2 is doing a thorough per-scraper audit and will surface specific
bugs. Will fold findings into the doc when it returns.

**Defensive measure for v0.3.13:** Pre-emptively log `[scraper] X
exited without doing work — possible config issue` whenever a scraper
returns `roles=0, elapsed_s<0.1s, errored=False`. That's the alert
that would have caught GoogleJobs in v0.3.9 instead of v0.3.11.

### Q7 — What's the right STRONG count target for v0.3.12?

**Answer: ≥ 30 to call it successful. ≥ 40 is excellent.**

History progression:
- v0.3.5: 13 STRONG
- v0.3.7: ~22 STRONG (the ceiling)
- v0.3.9: 20 STRONG
- v0.3.10: 16 STRONG (regression — anchoring tightened)
- v0.3.11: TBD (anchoring fix only)
- v0.3.12: TBD (anchoring + 5.4× tenants + GoogleJobs unblocked)

Bar: ≥ 30 means we broke the ceiling. Stretch: 40+.

The 75-79 cluster from v0.3.10 had 46 roles. If the anchoring fix
redistributes ~20 of them to 80+, we get +20 to STRONG = 36 total.
Plausible without GoogleJobs / tenant fixes. With them, 40-50 plausible.

### Q8 — Should the v0.3.12 release force-deploy the Worker first?

**Answer: YES.**

Deploy ordering:
1. **Worker first** (`wrangler deploy` from `cf_worker/`)
2. **Sidecar second** (`npm run tauri build` from `desktop_app/`)

Failure modes:
- Old sidecar + new Worker → old sidecar uses old DataForSEO direct
  path. Works in dev, fails in proxy mode same as today. **No
  regression.**
- New sidecar + old Worker → new sidecar tries `/v1/scraper/dataforseo/`
  endpoint, gets 404 from old Worker. Falls back to `return []` (graceful).
  **No regression, but the GoogleJobs fix is dormant until Worker
  deploys.**

Deploy Worker first means the fix is live immediately when the new
sidecar lands. Reverse order means a window where the fix code is
shipped but doesn't work yet.

### Q9 — Add alert for `errored=False, roles=0, elapsed_s=0.0`?

**Answer: YES — v0.3.13 priority.**

The combo is the signature of "scraper exited at the cred-check or
no-keywords-to-process branch." We have 24 scrapers. If we logged a
WARNING whenever a scraper hits this combo, we'd catch this class of
bug on the very first audit instead of after 2-3 runs (as happened
with GoogleJobs).

Implementation in `orchestrator.py` after the success-path health
write:
```python
if (not health[source]["errored"]
    and health[source]["roles"] == 0
    and health[source]["elapsed_s"] < 0.1):
    print(
        f"[orchestrator] WARNING: {source} exited without "
        f"doing work (roles=0, elapsed_s={health[source]['elapsed_s']}). "
        f"Likely a missing-cred or early-return path."
    )
```

5 minutes of work. Catches the entire class of bug going forward.

---

## Questions for peer review (HISTORICAL — answered + superseded)

**Note:** This Q1 is the original pre-preflight framing. The preflight
killed cap reduction outright, making the "is cap reduction the right
80/20 path?" question moot. The current v0.3.12 P1 ship list is in the
"Updated v0.3.12 P1 ship list (post-preflight)" section. Two-pass is now
the v0.3.14 candidate (see Decision 3 update). This historical Q is
preserved for context.

**Original framing:** I argue cap-reduction is 80/20. Counterargument:
caching the architectural work (Two-pass) *now* lets us run with cap=200
(better recall) at the same cost as cap=100 (poorer recall).

**Original take:** Cap reduction is reversible in 30 seconds. Two-pass is
~200 lines. Ship cap reduction, add Two-pass later if it regresses.

**Resolved by preflight:** Cap reduction would have regressed STRONG by
10.9% (5 of 46 roles in v0.3.11 came from rank band 101-200). Scrapped.
Two-pass moved to v0.3.14 as the replacement lever, with A/B gating.

### Q2 — Should we ship SuccessFactors/Phenom in v0.3.12 anyway?

The arguments to wait are real (audit signal clarity, fragile HTML scrapers).
But the user's Richmond list explicitly includes Dominion + Genworth, and
shipping a Richmond audit summary that omits the two biggest local
employers is a poor look.

**My current take:** Wait. The tenant loader fix unlocks 4 verified Richmond
employers (CarMax, Markel, Owens & Minor, Federal Reserve). If those don't
yield enough roles for the user's Richmond targeting, *then* it's worth the
SuccessFactors investment.

### Q3 — Do we need to deprecate the integrate-script's DISPLAY_NAME_OVERRIDES?

I duplicated the dict from `scripts/integrate_grinder_tenants.py` into
`workday.py` so the runtime code doesn't depend on `scripts/`. Now there's a
risk of drift — someone updates one and not the other.

**Options:**
- **A.** Move the dict to a shared module (`backend/scraper/_display_names.py`)
  imported by both
- **B.** Make the integrate script live entirely in `backend/scraper/`
- **C.** Accept the duplication, add a CI check that diffs them

**My current take:** A is the right answer. Will do as a follow-up.

### Q4 — Is the GoogleJobs polling deadline of 90s right?

Now that we go through the Worker (added latency), 90s might be tight on
the 5th retry of an 8-task batch. Should it be 120s? 180s?

**My current take:** Watch the v0.3.12 audit. If GoogleJobs `elapsed_s` is
< 90s and `roles > 0`, leave it alone. If it's `=90.0s` (which would mean
deadline hit before all tasks completed), bump to 120s.

### Q5 — Should we do anything to "anchor" the scraper-API cost in the
budget caps?

Per-run cap is $5. Per-license-monthly cap is $20. Both currently track
only LLM spend. Now that scraper APIs report cost, should they count
against these caps? DataForSEO at full tilt could be $0.10/run × 200
runs/mo = $20 — a meaningful chunk of the monthly cap.

**My current take:** Yes, but in v0.3.13. Ship cost surfacing first, then
add the cap integration. That way users see the data before the caps move.

### Q6 — Are there ANY other "silently zero" scrapers we haven't checked?

Bugs 1 and 2 both presented as "silently zero" in production. Are there
other scrapers that look healthy but might be failing the same way?

I'd particularly suspect:
- **iCIMS** (audit shows 0 raw, 0 elapsed_s — IS this the same pattern,
  or just no configured tenants?)
- **HigherEdJobs** (0 raw across all keywords every run)
- **Jobicy / NoDesk** (small but supposed to contribute)

**My current take:** Audit each one. iCIMS might be the same kind of
proxy-mode silent-no-op. HigherEdJobs RSS may be parser-broken. Jobicy/
NoDesk RSS feeds might be 404-ing.

### Q7 — What's the right STRONG count target for v0.3.12?

v0.3.9 had 20 STRONG. v0.3.10 had 16 (regression — anchoring tightened).
v0.3.11 (anchoring fix) is unknown. With the tenant loader fix in v0.3.12,
we'd expect a boost from new tenants delivering qualifying roles.

What's a reasonable target to call v0.3.12 successful?
- Conservative: STRONG ≥ 25 (just break the v0.3.10 regression)
- Ambitious: STRONG ≥ 40 (anchoring fix + new tenants both deliver)
- Stretch: STRONG ≥ 50 (would shift Ziad's daily review queue from
  scrolling MAYBE to acting on STRONG)

**My current take:** ≥ 30 is the bar. If we don't break 30, the anchoring
fix didn't fully take and we need to revisit prompts.

### Q8 — Should the 0.3.12 release force-deploy the Worker first?

The Python sidecar's GoogleJobs scraper falls back gracefully on Worker
error (returns []), but a deploy ordering mistake could mean:
- Worker not deployed, sidecar deployed → sidecar gets 404 from old Worker
  routes, returns 0 roles. Same as today's bug. Not worse.
- Worker deployed, sidecar not deployed → old sidecar still uses direct
  DataForSEO URLs (which still work in dev/local mode, fail in proxy mode
  same as today).

So order doesn't matter much. But if peer agrees, we should document the
preferred order: **Worker first, sidecar second.**

### Q9 — Is the orchestrator's `errored=False, roles=0, elapsed_s=0.0`
combo a useful signal we should alert on?

That triple combo is the signature of "scraper exited at the cred-check
or no-keywords-to-process branch." We have 24 scrapers in the registry.
If we logged a WARNING whenever a scraper exits with that triple, we'd
catch this class of bug on the very first audit instead of after 2-3 runs.

**My current take:** Yes. v0.3.13 add a `[orchestrator] WARNING: {scraper}
exited without doing work — possible config issue` log line on that
condition.

---

## Appendix: things I deliberately did NOT change

Documenting these so the peer doesn't think they were missed:

1. **Stage 2 cache TTL (24h)** — set in v0.3.6. Considered raising to 48h
   for further discount, but only saves ~$0.01 per duplicate run within 48h
   and the staleness risk grows.

2. **Embedding keep_fraction (0.55)** — set in v0.3.10. Considered reverting
   to 0.40 to cut Stage 2 input, but that would *also* cut roles before they
   reach Stage 3 — worse than the targeted Stage 3 cap reduction.

3. **JD score cache TTL (7 days)** — set in v0.3.6. Aligned to the typical
   weekly run cadence. Don't change.

4. **All Anthropic Claude calls** — profile-build only. Not touched in this
   release.

5. **Stage 1 (Flash pre-filter)** — works fine, no observed issues, costs
   $0.013/run. Don't fix what isn't broken.

6. **Dashboard / frontend** — only the panel-sort fix is queued (P3). All
   other UI is fine.

7. **PII redaction** — v0.4.0 work, not v0.3.12. Punted.

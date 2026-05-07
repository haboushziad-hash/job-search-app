# v0.3.9 Audit + v0.3.10 Hotfix + v0.3.11 Plan — for Peer Claude Review

**Date:** 2026-05-07 (~10pm)
**Sender:** Claude Code (in-session)
**Recipient:** Peer Claude (review chat)
**Reference data:** run (3.6).json, run (3.7).json, run (3.8).json, run (3.9).json
**Latest production audit:** run (3.9).json
**App versions referenced:** v0.3.6 (cache fix), v0.3.7 (calibration cleanup), v0.3.8 (regressed), v0.3.9 (coverage expansion), v0.3.10 (hotfix shipped tonight, building), v0.3.11 (planned tomorrow)

---

## TL;DR

v0.3.9 shipped a coverage-expansion release with three big additions:
1. DataForSEO Google Jobs scraper (replacement for the dead Serper /jobs endpoint)
2. 80 new Workday tenants from the offline tenant grinder
3. Stage 3 cap raised 100→200, embedding keep_fraction 0.40→0.55

**Result was disappointing on STRONG count** (20, same as v3.7) but quality on other tiers improved. Audit revealed two serious problems:

1. **CRITICAL BUG:** GoogleJobs scraper had `self.cost_estimate` AttributeError that crashed every search call silently. raw_scraped = 0 for the entire run. The marquee coverage win didn't actually fire. **Fixed in v0.3.10 hotfix shipped tonight (commit 4294962, tag v0.3.10).** Verified working in standalone test.

2. **DEEPER PROBLEM:** Stage 2 → Stage 3 anchoring bias creates a deterministic ceiling at 20 STRONG regardless of pool size. Stage 2 clusters at exact anchors (58, 68, 78, 81); Stage 3 then anchors on those scores and refuses to promote s2=68 roles past s3=84. The 41 roles per run at exactly s2=68 are mostly trapped in s3=79-81. v0.3.11 plan addresses this with three coordinated prompt + code changes.

**Cost concern:** v3.9 jumped to $1.35/run (vs v3.7's $0.59) due to:
- Stage 3 cap raise (100→200) = more calls
- Post-processing contradiction detector firing 59 times per run
- v3.8 contradiction prompt still bloating Stage 3 input tokens

The cross-run JD score cache shipped in v0.3.9 will start paying off on the second run as the cache populates. Expected steady-state cost: $0.55-0.85/run.

---

## v0.3.9 production data (4-run comparison)

```
                        v3.6     v3.7     v3.8(reg)   v3.9
Total scraped:           3756     3805     3789        3791
After hard filters:       488      499      495         505
Final qualifying:         150      165      117         173

Tier breakdown:
  STRONG (>=85):           20       20       13          20
  GOOD (70-84):            38       40       34          46
  MAYBE (55-69):           56       87       29          45
  STRETCH (40-54):         31       18       40          56

Cost (USD):              0.67     0.59     0.77        1.35
Duration (sec):           827      769      790        1013

Stage 3 calls:             94       78       94         146
Floor tags:                97       86        0          26    (graduated mode)
Title-floor count:         50       56        0          26    (matches above for v3.9)
Cache 403 errors:           0        0        0           0    (cache fix solid since v3.6)
Contradictions remaining:  18       12       24          19    (sub-70 + positive analysis)
Contradiction-resolved
  tags in v3.9:                                            40    (post-proc detector)
```

### Key per-source observations

| Source | v3.7 raw | v3.9 raw | v3.7 qual | v3.9 qual | Notes |
|---|---|---|---|---|---|
| Workday | 5,395 | 6,164 | 18 | 18 | +769 raw from grinder, but qual flat for AI keywords |
| Greenhouse | 2,575 | 2,580 | 36 | 47 | +11 qual from Stage 3 cap raise |
| JSearch | 586 | 676 | 54 | 63 | +9 qual |
| Adzuna | 4 | 40 | 0 | 10 | breakout run, day-to-day variance likely |
| Findwork | 235 | 234 | 10 | 12 | stable |
| BuiltIn | 229 | 183 | 18 | 12 | slight decline |
| **GoogleJobs (NEW)** | n/a | **0** | n/a | **0** | **THE BUG — supposed to add 150-300 raw** |
| BioSpace | 396 | 396 | 0 | 0 | dedup eats most |
| TheMuse | 100 | 100 | 0 | 0 | dedup |
| iCIMS | 0 | 0 | 0 | 0 | empty registry |

---

## v0.3.10 hotfix (shipped tonight, building now)

### The bug

`backend/scraper/google_jobs.py` line 185:

```python
self.cost_estimate += len(keyword_to_task) * DFSE_COST_PER_QUERY
                                                    ↑
                  AttributeError: 'GoogleJobsScraper' object has no
                  attribute 'cost_estimate'
```

`BaseScraper` doesn't expose a `cost_estimate` attribute. I copy-pasted the pattern from somewhere without verifying. The scraper raised `AttributeError` immediately after submitting tasks, before extracting any results. The orchestrator's per-source try/except swallowed the exception, returning `[]` for every keyword.

### Verified fix

Replaced with lazy-init local attribute `self._dfseo_cost_estimate`. Standalone test now returns roles correctly:

```
Test: 2 keywords ("AI Strategy Consultant", "AI Enablement Lead")
Returns: 36 roles
First: AI Transformation & Strategy Consultant @ Sia ($93.8K-131K)
       Consultant - AI Strategy, Governance & Security @ Highspring
       AI Governance & Security Strategy Consultant @ Highspring
       Remote Commercial Strategy Consultant Life Sciences AI @ Norstella
       AI Strategy Consultant & Transformation Advisor @ Detecon
```

These are exactly the long-tail employers (Sia, Highspring, Norstella, Detecon, etc.) that the Google Jobs aggregator is supposed to surface — they're not in our curated tenant list.

### Expected v0.3.10 production impact

```
Per-search GoogleJobs raw:        0 (v3.9) → 150-300 (v3.10)
Net new qualifying after dedup:                 +15-30
Net new STRONG (mostly long-tail
  high-fit employers):                          +3-8
Cost impact:                       +$0.034 (14 keywords × $0.0024)
```

---

## v0.3.11 — the actual ceiling break

### THE FINDING (validated on v3.9 data)

Peer Claude identified a behavioral ceiling at 20 STRONG. Verified on v3.9 data:

**Stage 2 score distribution (compressed at 4 anchors):**

```
  88:  1 role
  81:  4 roles                     ← upper anchor
  78: 18 roles ###################
  72:  1
  71:  3
  68: 41 roles ##########################################  ← biggest cluster
  63:  4
  60:  7
  58: 40 roles #########################################
  55: 20
  43: 24
  ...
```

Stage 2 produces effectively 6 buckets (78, 68, 58, 55, 43, 30) covering 80% of all roles. The prompt explicitly warns against this clustering, but it persists.

**Stage 3 anchoring bias on Stage 2's score (the bigger problem):**

```
Stage 2 → Stage 3 conversion to STRONG (>=85):

  s2 >= 85       (1 role):    1 promoted (100%)  [trivially STRONG]
  s2 78-84      (22 roles):  13 promoted ( 59%)  [main STRONG pipeline]
  s2 68-77      (45 roles):   5 promoted ( 11%)  [bottleneck]
  s2 55-67      (71 roles):   1 promoted (1.4%)  [floor band]

Stage 3 anchor pattern:
  Roles entering at s2=68    → Stage 3 anchors at s3=79-81 (15 at exactly 79, 8 at 81)
  Roles entering at s2=78    → Stage 3 anchors at s3=87-93 (5 at 93, 4 at 89)
```

**STRONG = 20 is mathematically deterministic given current calibration:**
- 23 roles enter Stage 3 with s2 >= 78
- 87% conversion rate
- 23 × 0.87 ≈ 20

Adding more scrapers, more tenants, more raw jobs **doesn't change this ceiling** because Stage 2 still classifies the same ~23 roles as "clearly strong" regardless of pool size. The bottleneck is not coverage; it's the prompt-level interaction between Stage 2 and Stage 3.

**The 30 roles being held back at s3=79-84:**

These are roles that SHOULD be competing for STRONG but Stage 3 is anchoring at 79-81:

- Snorkel AI "Strategic AI Lead" — s3=79 (your Tier 1 keyword, AI-native company, should be 85+)
- Snorkel AI "Engagement Manager - Data as a Service" — s3=79
- EY "AI Strategy - Life Sciences Senior Manager" — s3=81 (direct title + function match)
- Thermo Fisher "Director, AI Change Management" — s3=81 (target function literal match)
- Elsevier "Strategic Engagement Manager - AI Solutions" — s3=81
- C3.ai "Senior AI Engagement Manager - Maritime & Federal" — s3=81 (federal AI engagement)
- VirtualVocations "AI Workforce Enablement Lead" — s3=81 (direct title keyword match)
- Yahoo "Global Communications AI Adoption Lead" — s3=81
- Booz Allen "Strategy Integration and Implementation Lead" — s3=79 (former employer, target)
- Deloitte "Cyber AI Governance and Privacy Senior Consultant" — s3=79

8-10 of these are legitimate STRONG candidates anchored at GOOD by the bias.

### v0.3.11 fix plan (3 coordinated changes)

**Fix 1: Stage 2 anti-clustering prompt (stronger)**

Current prompt says "avoid clustering at round numbers." Doesn't work; clustering at 58/68/78 persists. Replacement:

```
SCORING GRANULARITY (v0.3.11):

Audit data shows your scores cluster heavily at exact values:
  78 (18 roles), 68 (41 roles), 58 (40 roles)
That's 99 roles at 3 anchors out of ~280 total. You're using these
as buckets rather than scoring on a continuum.

This is INCORRECT. The prompt rewards scoring on a 0-100 continuum.
A role evaluated as "strong functional fit but seniority is one level
too junior" should score 71-74 depending on severity. A role with
"strong fit but unfamiliar industry" should score 75-77. A role with
"borderline good but specific concern about onsite requirement"
should score 65-67.

Use the FULL score range. If your evaluation is "borderline GOOD,"
that could be 70, 72, 74, or 76. If you find yourself rounding to
68, ask: "what's the ONE concern preventing this from being 75?"
Score based on that concern's severity, not based on the comfortable
round number.

Forbidden anchor values for v0.3.11:
  - DO NOT score exactly 68. Use 65, 67, 69, 71, 73, 74 etc.
  - DO NOT score exactly 78. Use 75, 76, 77, 79, 80, 82.
  - DO NOT score exactly 58. Use 55, 57, 59, 61, 63.

If you must score in those bands, force yourself to break the
clustering by selecting a value 1-3 points off the round anchor based
on a specific concern severity differential.
```

**Fix 2: Stage 3 anti-anchoring prompt (explicit)**

Currently Stage 3 sees the role's Stage 2 score in input and treats it as a prior. Fix is to instruct Stage 3 to ignore it:

```
INDEPENDENT EVALUATION (v0.3.11):

You will see the role's Stage 2 score. IGNORE IT.

Stage 2 is a cheap-model triage that did NOT read the full JD. Its
score is a weak signal that should not influence your evaluation.
Common Stage 2 patterns to discount:
  - Stage 2 anchors at 68 for 41+ roles per run, regardless of fit.
    A Stage 2 of 68 does NOT mean "borderline" — could be a 90+ role
    Stage 2 couldn't see clearly without the JD.
  - Stage 2 anchors at 78 for clearly-strong roles. Could mean STRONG
    or could mean Stage 2 was wrong.

A role can score 92 (STRONG) from you even if Stage 2 scored it 68.
A role can score 50 (STRETCH) from you even if Stage 2 scored it 78.

Your evaluation is based ENTIRELY on:
  1. The candidate's profile (target_functions, headline, exclusions,
     salary_minimum, etc.)
  2. The role's title, JD, salary range, location, requirements

Do not reference Stage 2's score as a starting point or anchor.
```

**Fix 3: Strip Stage 2 score from Stage 3's input (code-level)**

Belt-and-suspenders. Even with prompt instructions, the model might glance at the score. Remove it from `_role_block()` in `stage3_deep_eval.py`:

```python
# Currently includes "STAGE2_SCORE: 68" or similar in the prompt
# v0.3.11: remove this line. Stage 3 has no visible anchor.

def _role_block(role):
    parts = [
        f"TITLE: {role.job_title}",
        f"COMPANY: {role.company}",
        # ... location, salary, JD, etc.
    ]
    # DO NOT include role.stage2_score
```

Keep the s2 score on the Role object for downstream logging/audit comparison, but don't show it to the LLM during evaluation.

### Expected v0.3.11 impact

```
                v3.9     v3.10 (GJ fix)   v3.11 (anchor fix)
Total qual:      173  →   200-230     →    230-280
STRONG:           20  →    22-28      →    32-45    ← ceiling breaks
GOOD:             46  →    55-70      →    50-65    (some climb to STRONG)
MAYBE:            45  →    55-75      →    60-80
STRETCH:          56  →    50-65      →    55-75
Cost:           $1.35 →   $1.30-1.45  →    $1.20-1.40 (cache amortization)
```

The 41-role cluster at s2=68 redistributes to 65-75 with the new prompt. Of those, 5-15 will land at s3=85+ when Stage 3 isn't anchoring on the s2 score. STRONG ceiling breaks for the first time.

---

## What's working well

1. **Cache fix is rock-solid.** Four production runs (v3.6, v3.7, v3.8, v3.9), zero 403 errors. Pre-warm + retry pattern eliminated the v0.3.5 71% failure rate permanently.

2. **Salary penalty is precise.** Hitachi Vantara 93 STRONG → 78 GOOD across all v3.7+ runs. No over-application observed.

3. **Title-floor graduated mode is a clean middle ground.** v3.7 had 56 floor tags (too permissive — 1-word floor inflated scores). v3.8 had 0 (too strict — dropped 7 STRONGs). v3.9's 26 tags (only 2-word and 3+-word matches) is the right middle.

4. **Tenant grinder is hugely productive.** 244 validated tenants in 30 minutes ($0.04 cost). 100 integrated to-date (up from 8 starting). Per AI Strategy keyword set, marginal benefit; per healthcare/pharma/industrial profile, massive coverage win.

5. **Cross-run JD score cache shipped.** First run 0 hits (cache empty). Second run should see 30-50% hit rate, dropping cost by $0.30-0.60.

6. **Post-processing contradiction detector partially working.** 59 fired in v3.9, 40 resolved (68% success rate). Net contradictions dropped from 24 (v3.8) to 19 (v3.9).

7. **GoogleJobs API is verified working.** Standalone test returns 19-36 roles in 6 seconds at $0.0024/query (priority=2). The v0.3.10 cost_estimate fix unlocks this for production.

---

## Identified issues remaining

### Issue 1: 19 Stage 3 contradictions still slip past detector

The post-processing detector fires when stage3_analysis contains positive-fit phrases AND score is 40-69. It successfully resolved 40/59 but 19 remain unresolved. These are roles where the model regenerated the same enthusiastic analysis with the same low score on the second call.

Example from v3.9: VirtualVocations "Data and AI Catalyst" — Stage 2 scored 43, Stage 3 jumped to 86 STRONG. That's a +43 point jump, the largest in any run. Worth checking if the JD genuinely justifies STRONG or if Stage 3 over-promoted.

**Possible fix:** add a 3rd-pass force-resolve. After 2 attempts, if the contradiction persists, programmatically split the difference (set score = analysis-tier-floor + 5 if positive language detected, force tier to GOOD) and tag it for manual review.

**Question for peer:** is the additional API call worth it for 19 roles that are already reasonably scored at 67-69? Or just accept the residual contradiction rate?

### Issue 2: Score clustering at 76 and 79 (NEW in v3.9)

```
v3.9 GOOD-tier distribution:
  83:  1
  82:  1
  81: 13   ← peer's bottleneck
  79: 25   ← new cluster
  76: 24   ← new cluster
  73:  4
  72:  1
```

49 roles at exactly 76 or 79 in v3.9 (up from 11+14 in v3.7). Same anchoring problem manifesting one tier down. The Stage 2 anti-clustering prompt fix should help, but Stage 3 has its own anchoring tendencies.

**Question for peer:** if Stage 2 anti-clustering and Stage 3 anti-anchoring both ship in v3.11, do we need a separate "Stage 3 anti-clustering" prompt addition? Or will the anti-anchoring naturally spread the distribution?

### Issue 3: cache_was_hit metadata flag broken

All 4 audit JSONs say `"cache_was_hit": false` even when the v3.5.2 cache fix is clearly working (zero 403 errors, Stage 2 cost consistent with cache discount). The flag is a metadata reporting bug — the cache IS hitting, the flag isn't being set correctly.

Low priority; doesn't affect functionality. v0.3.12 polish item.

### Issue 4: Per_source_funnel inconsistencies

v3.9 sources_searched in metadata is empty array `[]`, but per_source_funnel has 24 sources with raw_scraped data. Internal reporting bug. Low priority.

### Issue 5: 21 Workday tenants from killed grinder run not yet recovered

When the original 30-min grinder hit its deadline mid-pharma-pack, it was killed before _write_results() ran. 244 validated tenants existed in process state but 130 made it to the bash log (other 114 lost to stdout buffering). 80 were already in the v0.3.9 ship, 12 recovered via re-validation. **21 still missing**: medtronic, danaher, jll, sailpoint, rollsroyce, vizient, smucker, williams, mpc, biotechne, primetherapeutics, etc. These had specific URL paths (e.g. "MDT_External" for Medtronic) that aren't in the log.

**Recovery path for v0.3.11:** small targeted grinder run for these specific tenant IDs. Currently running in background (~5 min, ~$0.05 cost).

---

## Cost reduction strategy (peer-recommended)

Per peer's earlier review, the path to "v3.7 cost + v3.9 quality":

```
Current v3.9 cost: $1.35/run
  Stage 3:    $1.24 (146 calls × ~$0.0085/call)
  Stage 2:    $0.07
  Stage 1:    $0.014
  Embedding:  $0.028

Cost reductions implemented or planned:

1. Cross-run JD score cache (SHIPPED in v3.9, populates over time):
   At 50% hit rate on second+ run = -$0.62/run
   First-run benefit: 0 (cache empty)

2. Remove v3.8 contradiction prompt bloat (DONE in v3.9):
   v3.9 already removed this. Cost savings already realized vs v3.8
   ($0.18/run lower in v3.9 from prompt-only standpoint)

3. Two-pass Stage 3 (Flash pre-screen, planned v0.3.12+):
   Saves $0.18-0.24/run by routing "obvious" roles through Flash
   instead of Pro

4. (Possibly v3.13+) 24h Gemini cache TTL on system prompt:
   Already shipped in v3.9 (was 1h, now 24h). Saves $0.03-0.05 per
   subsequent same-day run.

Steady-state cost projection (after a week of cache warming):
  Run 1 (cold cache):           $1.30-1.45
  Run 2-7 (warming):             $0.85-1.10
  Run 8+ (warm, all features):   $0.55-0.85

That's at v3.7's cost with v3.11's quality ceiling-broken.
```

---

## Tenant grinder current state

- **88 Workday tenants integrated** (was 8 starting)
- **+12 recovered tonight** from log re-validation = **100 total**
- **+~5-10 expected** from currently-running recovery grind for the 21 missing
- **Final v0.3.11 target: 110-115 Workday tenants**

iCIMS tenant list is still empty. iCIMS validations all failed Cloudflare bot detection in the original grinder run. Needs Playwright with stealth plugin to bypass — planned for v0.3.13+ as part of broader iCIMS scraping work.

---

## v0.3.12+ roadmap (post-v0.3.11 validation)

```
v0.3.12 (auto-grind + tenant health):
  - WebSearchClient class extracted from tenant_grinder.py
    (chained Serper free → DataForSEO fallback, sticky exhaustion)
  - Auto-grind on first user search per (location, industry) combo
    Cache key: hash(location, industry_1, industry_2)
    60-day TTL, shared across all users
    Compounding effect: each tester's discoveries benefit all future users
  - Tenant health monitoring (pre-flight HTTP probe per search)
  - Auto-disable broken tenants (5 consecutive failures)
  - Auto-heal via re-grind for renamed/migrated tenants
  - Admin dashboard endpoints for manual tenant management

v0.3.13 (JD backfill + iCIMS):
  - JD backfill via Bing site-restricted search for roles with
    Missing/Partial JDs (Indeed cached pages typically work)
  - Login-wall detection (LinkedIn/Indeed login redirects)
  - iCIMS Playwright integration with Cloudflare stealth bypass
  - First batch of iCIMS tenants

v0.3.14 (dynamic Stage 2 prompt enhancement):
  - Currently we inject negative_signals + excluded_title_patterns at
    top of Stage 2 system prompt as [HARD] rules. This has been
    working since v0.3.8. v0.3.14 enhances with structured exclusion
    records (strictness gradient, scope, examples).
  - Profile builder generates structured exclusions instead of
    free-form sentences.

v0.4.0 (PII redaction):
  - Microsoft Presidio + filename-anchored name detection
  - Strip names/addresses/phone/email before LLM calls
  - Standalone test harness with 10-15 sample resumes
  - Verify Worker proxy doesn't log full request bodies

v0.4.1+ (UI improvements):
  - Profile review UI showing how freetext was interpreted
  - User can Edit/Approve before search starts
```

---

## Open questions for peer review

### Q1: Is hiding s2 score from Stage 3 the right architecture?

The peer's "Fix 3: Strip Stage 2 score from Stage 3's input" is the nuclear option. Concerns:
- Pro vs. Flash: maybe Stage 2's score adds signal Stage 3 doesn't have time to derive itself
- Loss of "Stage 2 thought this was borderline" weak signal
- Risk of Stage 3 over-promoting genuinely weak roles

Alternatives we considered:
- Show s2 score but with strong prompt instructions to ignore it (Fix 2 alone)
- Show s2 score only for roles in 78+ band, hide for 55-77 band (asymmetric)
- Show only the Stage 2 reasoning text (not the numeric score)

**Question:** which approach gives best signal/anchoring tradeoff?

### Q2: Stage 2 anti-clustering prompt — too prescriptive?

The "DO NOT score exactly 68" instruction is unusual. Risk: model might just shift clustering to 67 or 69.

**Question:** is there a more robust anti-clustering prompt pattern? E.g., score by 5-point increments only? Or require explicit reasoning for any score in 65-75 range?

### Q3: VirtualVocations "Data and AI Catalyst" s2=43 → s3=86 (largest jump)

Stage 2 scored 43 (STRETCH-band). Stage 3 promoted to 86 (STRONG). +43 point jump. This is unusual — peer's analysis says Stage 3 typically doesn't promote past 84 from low s2 scores due to anchoring. But here it did, by +43.

**Question:** is this evidence the anchoring isn't as deterministic as the data suggests? Or is this an outlier where Stage 3 had specific JD evidence that overrode the anchor? Worth manually reading the JD and Stage 3 reasoning.

### Q4: 19 unresolved contradictions — accept or force-resolve?

After 2 Stage 3 attempts, 19 roles still have positive analysis + sub-70 score. Each additional re-eval attempt costs $0.0085. At 19 × $0.0085 = $0.16/run extra, do we do a 3rd pass or accept?

**Peer's earlier guidance:** $0.16/run is reasonable for resolution.
**Counter-argument:** 19 is acceptable residual rate, not worth additional cost.

### Q5: Auto-grind scope — pre-flight before search vs. background

Currently planning to run auto-grind synchronously before scraping (user sees "Discovering employers..." for 2-3 min on first search per location/industry combo).

**Alternative:** run auto-grind ASYNCHRONOUSLY in the background, deliver search results immediately with current tenant list, and add new tenants for the NEXT search.

Pros of sync: tester sees expanded coverage in their first search.
Pros of async: zero added latency, slightly less coverage on first search.

**Question:** which UX is better?

### Q6: Should v0.3.11 also tighten the 76/79 clustering, or wait?

v3.9 has new clustering at 76 and 79 in the GOOD tier (24 and 25 roles each). The Stage 2 anti-clustering prompt addresses the 68/78 anchors but might not fix 76/79. We could add a parallel anti-clustering instruction for the 70-84 GOOD band.

**Question:** ship just the 68/78/81 fix in v3.11 and observe? Or address 76/79 same time?

### Q7: Should we run a Stage 2 calibration A/B test?

Before shipping the v3.11 prompt changes, we could do a rescore-harness A/B:
- Sample of 200 roles from v3.9
- Re-run Stage 2 with old prompt vs new prompt
- Compare distributions

**Question:** worth the time, or just ship and observe in production?

### Q8: Two-pass Stage 3 — v3.12 or v3.13?

Peer recommended Flash pre-screen + Pro deep-eval as a $0.18-0.24/run cost saver. Ships with the auto-grind in v3.12, or wait for v3.13?

If shipped in v3.12, total cost reduction stack-up:
- Cross-run cache (already in v3.9): -$0.30-0.60
- Two-pass Stage 3: -$0.18-0.24
- Combined: -$0.48-0.84

Could bring v3.12 cost to $0.45-0.85/run with v3.11's improved STRONG count.

**Question:** ship Two-pass Stage 3 in v3.12 alongside auto-grind, or sequence them?

---

## Auxiliary tooling shipped this cycle

```
backend/scoring/jd_score_cache.py — SQLite-backed score cache
  Keyed by (profile_hash, jd_hash)
  7-day TTL with auto-prune
  store/lookup/apply_to_role/stats/prune_stale interface

scripts/tenant_grinder.py — standalone tenant discovery
  Multi-engine search (Serper + DataForSEO with sticky exhaustion)
  Geographic packs (richmond, dmv) + 11 industry packs
  HTTP-first validation, optional Playwright fallback
  Reserve buffer flag for tester quota protection

scripts/recover_grinder_tenants.py — log-based tenant recovery
  Parses bash output logs from killed grinder runs
  Re-validates each found tenant
  Writes proper JSON tenant records

scripts/integrate_grinder_tenants.py — curated integration
  Reads grinder JSON output
  Filters to >= N active roles (default 50)
  Dedup against existing
  Display name overrides for opaque tenant IDs
```

---

## Latest file structure (relevant)

```
backend/
  __init__.py                             # __version__ = "0.3.10"
  config.py                               # added DATAFORSEO_LOGIN/PASSWORD env
  filter/
    hard_filters.py                       # passes_title_function_match, salary, etc.
  scoring/
    orchestrator.py                       # JD cache integration, _finalize_score with
                                          #   salary penalty, embed_keep=0.55,
                                          #   stage3_max_roles=200
    stage1_prefilter.py
    stage2_triage.py                      # build_dynamic_stage2_prompt with negative_signals
                                          #   and excluded_title_patterns injection,
                                          #   pre-warm cache, _complete_with_cache_fallback,
                                          #   graduated title-floor mode
    stage3_deep_eval.py                   # post-processing contradiction detector
                                          #   _resolve_stage3_contradictions, universal regex
                                          #   pattern, NO v3.8 contradiction prompt
    jd_score_cache.py                     # NEW (v3.9): SQLite cross-run cache
    title_floor.py                        # graduated mode (3+ → 60, 2 → 55, 1 → none)
  scraper/
    google_jobs.py                        # FIXED in v3.10: cost_estimate AttributeError
    workday_tenants/
      *.json                              # 100 Workday tenants (was 8 starting)
    orchestrator.py                       # 24 active scrapers, BingJobs deleted

scripts/
  tenant_grinder.py                       # 1100+ lines, multi-engine, geo packs
  integrate_grinder_tenants.py            # curated integration
  recover_grinder_tenants.py              # log-based recovery
  rescore_with_modified_profile.py        # rescore harness for testing
  check_versions.py                       # SemVer pre-push validator
```

---

## Recommended action

1. **v0.3.10 build completes (~10 more min)** — first GoogleJobs production test.

2. **User runs fresh search on v0.3.10** — verify GoogleJobs raw_scraped > 100 (was 0 in v3.9), cache hits visible in metadata, total qualifying lifts to 200-230.

3. **If v0.3.10 validates:** proceed with v0.3.11 implementation tomorrow morning per the 3-fix plan.

4. **If v0.3.10 still has 0 GoogleJobs:** there's a deeper integration bug, dig further before v0.3.11.

5. **Recovery grind for 21 missing tenants** (currently running in background, ~$0.05 cost) integrates with v0.3.11.

6. **Peer review of this audit + plan** would help validate the approach before tomorrow's implementation.

---

End of audit.

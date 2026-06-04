# NEXT UP — Job Search App backlog (post v0.3.28)

> Updated: 2026-06-03. Durable backlog so this stops getting buried under firefighting.
> **Status: caught up + clean.** v0.3.28 is SHIPPED & LIVE (tester manifest serves 0.3.28,
> auto-update on). All production code committed (`99313df` + `0e6183e`). v0.3.29 was a
> validated NO-GO (see end). Nothing half-shipped.

**Cost baseline:** a fresh (cold-cache) proxy-mode search ≈ **$0.82** — Stage 2 ≈ $0.37,
Stage 3 ≈ $0.38 (cheap stack), Stage 1 + embeddings ≈ $0.11. Cost Δ below are vs this.
Risk = chance the change breaks/regresses something.

---

## 2026-06-03 (cont. 3) — FULL 4-persona Opus accuracy report (1148 roles graded, gold-standard)
Every qualifying role from a full v0.3.30 app run, per persona, read + tier-graded by Opus
subagents (subscription, $0 API) and compared to the app's own tiers. **This is the validation gate.**

**OVERALL (1148 graded):** exact tier = **50%**, within-1-tier = **89%**,
**app STRONG+GOOD precision = 81% (380/470)** — of everything the app surfaces as a top match,
Opus agrees 81% are genuinely >=GOOD.

| persona | graded | within-1 | STRONG-only prec | STRONG+GOOD prec | direction |
|---------|--------|----------|------------------|------------------|-----------|
| ziad    | 284    | 90%      | **95%**          | 74% (85/115)     | over-tiers AI-adjacent (over112/under18) |
| ryan    | 357    | 86%      | **100%**         | 96% (139/145)    | UNDER-tiers tax hard (over38/under159) |
| zach    | 314    | 87%      | **90%**          | 70% (90/129)     | GOOD noisy (over98/under69) |
| rana    | 193    | 95%      | **97%**          | 81% (66/81)      | balanced (over44/under36) |

**Headline: the STRONG tier is trustworthy everywhere (90-100% precision).** Only **2 false-STRONGs
in 1148 roles** — Towne Park valet/parking "Account Manager" (zach, Opus=STRETCH) + Pamunkey Tribe
Natural Resources Specialist (rana, Opus=STRETCH); neither a SKIP, so not egregious.
**Weak spots = the GOOD tier (noisier, drags zach/ziad precision to 70-74%) + persona direction:**
app over-tiers AI/sales-adjacent (ziad, zach) and UNDER-tiers tax (ryan: 159 roles Opus rated
HIGHER than the app = a recall miss, not a precision miss — what Ryan DOES surface is 96% real).
**Verdict: STRONG tier is ship-grade; GOOD-tier calibration + Ryan-recall are the v0.4 targets.**

**JSearch provider — Apyflux evaluated + RULED OUT for now (free tier can't validate).** Apyflux
(`gateway.apyflux.com`) resells the SAME OpenWeb Ninja JSearch — identical schema/endpoints
(`/search`,`/job-details`,`/estimated-salary`,`/search-filters`)/`job_id` format; auth properly
enforced (clean 401 on bad key); echoes real queries (not canned). BUT its **free tier returns
`data:[]` (ZERO jobs) for EVERY query** (`nurse` / `software engineer` / `developer in new york`
all HTTP 200 + empty) — so you CANNOT validate result quality/freshness without paying **$22.99/mo
(Basic, 30K req)**. The console's "1 node.js job" was a baked-in MOCK (mismatched query + single
year-old job). Pricing IS 3× cheaper (30K/$23 vs RapidAPI 10K/$25) IF the paid data is good — but
it's now a "pay-to-find-out" bet, not a free trial. **DECISION: the free `num_pages 5->2` fix on the
EXISTING RapidAPI plan (2.5× more searches before the 10K cap) is the real fix for the quota burn —
$0, zero risk. Revisit Apyflux/OpenWeb-Ninja-direct only if more volume is still needed after.**

**Job-API market — fully mapped + verdict (2026-06-03).** Surveyed the whole space (publicapis.dev/jobs
+ "Best Job APIs 2026"). Verdict: **already well-covered; stop shopping.** App already runs **6** of the
public list (USAJOBS, TheMuse, Jobicy, Findwork, Arbeitnow, Adzuna) + heavy hitters (GoogleJobs, JSearch,
Greenhouse/Lever/Ashby/Workday). **Structural filter:** the data-provider tier (Bright Data, Coresignal,
TheirStack, Techmap, fantastic.jobs) all bill **per-record** — fits low-freq bulk/datasets, NOT a high-freq
per-user search app; at our volume it explodes (TheirStack $0.039/job @ $59 entry → ~$8-20/search; cheap
only at the $1,500/mo tier). Our sweet spot stays flat-rate (JSearch $25) / cheap-per-query (DataForSEO
~$0.10/run) / free (ATS scrapers). Rest of list = redundant aggregators (WhatJobs/Juju/Jooble/Careerjet/
Reed = same wells) or off-target (Upwork freelance, UK/DE boards, CV-match=scorer-not-source, Revelio=
workforce-analytics, more remote boards we already have 6 of). Only interesting bucket = **ATS-direct-at-
scale**: **LinkUp** = our exact philosophy (direct career pages since 2007, no boards/LinkedIn) but
ENTERPRISE-ONLY/no self-serve = **inaccessible**; **fantastic.jobs/TheirStack** = accessible but net-new is
only the **ATS long-tail GoogleJobs doesn't already index** (GoogleJobs docstring already aggregates
Workday/GH/Lever/Ashby/iCIMS/SR career pages). **fantastic.jobs = sole "maybe v0.4" (ATS long-tail) IF a
cost-model beats just expanding GoogleJobs.** Highest-value move = the GoogleJobs-expanded test (DONE,
see below) + num_pages, both free.

**GoogleJobs-as-JSearch-replacement test — VALIDATED NO (keep JSearch).** Ran ziad+ryan with **JSearch OFF
+ GoogleJobs on the EXPANDED keyword set** (env-gated `GOOGLEJOBS_EXPANDED_KEYWORDS`, default OFF; **scaffold
reverted after test** — orchestrator back to JSearch-only expanded kw). Set-level compare vs the JSearch-ON
baseline: **expanded-GoogleJobs recovered only 2% (1/49) of JSearch's specific unique STRONG+GOOD roles.**
S+G COUNTS look flat (ziad 115→118, ryan 145→136) but that's **CHURN, not recovery** — GoogleJobs-expanded
surfaces DIFFERENT roles (ziad +26 net-new/23 lost; ryan +33/42) that numerically offset the loss. The
lost roles are real on-target gems: **Ryan** — Cherry Bekaert "International Tax Manager" / "Tax Manager
Corporate Taxation", Keystone "Senior Tax Manager Private Client"; **Ziad** — Anaplan "Mgr AI Transformation
& Change Mgmt", Colgate "Manager Digital Enablement". **CONFIRMS the standing finding (free/other sources
can't fully cover JSearch) — now MEASURED at 2%. KEEP JSearch.** Silver lining: the keyword expansion DID
boost GoogleJobs (ziad S+G 31→42, ryan 88→104) + added net-new roles — but that's **ADDITIVE coverage, NOT
a JSearch replacement**, and it costs more DataForSEO/run + net-new quality unverified (app over-tiers GOOD).
Possible v0.4 *additive* lever IF Opus-graded net-new justifies the added cost; NOT now. **DECISION: keep
JSearch; at ≤50 searches/mo it's fine on the existing $25 plan + free `num_pages 5→2` (~4k of 10k req/mo).
The recent cap blowout was heavy TESTING (dozens of runs in days), NOT real cadence — so even this is just
a safety margin.** (Pre-existing scraper issues spotted in the run, unrelated: SmartRecruiters returned 0
for ryan = likely header/auth bug; Arbeitnow early-returns 0 = missing-cred. Log for later.)

**>> OPTION A — DONE (in-tree, v0.3.31):** `orchestrator.py` now routes the expanded keyword set to
GoogleJobs too (`source_name in ("JSearch","GoogleJobs")`), always-on. JSearch-outage stopgap: keeps
STRONG whole + backfills GOOD volume when JSearch is capped, ~+$2/mo DataForSEO (small redundancy/cost
when JSearch is healthy — accepted for now).
**>> REMINDER — OPTION B (NOT done, come back to this):** make the GoogleJobs expansion CONDITIONAL —
trigger it ONLY when JSearch is `quota_exhausted` (persist the flag to disk like the Workday facet cache;
reset on a JSearch success). That removes Option A's always-on redundancy + the ~$2/mo so the extra cost
is paid ONLY in months JSearch actually runs dry. Owner explicitly chose "do A now, revisit B later."

---

## 2026-06-03 (cont.) — friend-resume validation + GovernmentJobs added (IN TREE, not yet shipped)
Validated on 3 real, diverse personas built from real resumes: **Ziad** (AI strategy),
**Zach** (CPG ops/finance new-grad — a REAL v0.3.23 tester), **Ryan** (Big-4 tax).

- **Audit-export preview bug — FIXED in tree (`backend/storage/audit.py`).** `all_qualifying_roles`
  was dumped in pipeline/source order, NOT score order, so the run dashboard's role preview showed
  the *lowest*-scoring MAYBE roles first → a well-ranked run looked mistargeted. (This was Zac's
  "senior sales jobs" scare: Databricks/Snorkel/Mercury at **55-57 MAYBE** shown first; his actual
  STRONG tier was excellent — Associate Category Manager, Category/Supply-Chain Analyst, Management
  Trainee, a Finance+Consulting rotational. The scorer even flagged the Databricks AE as "pure
  quota-carrying sales" and floored it.) Now sorts by `final_score` desc. **Ship next release**
  (export-time fix → only affects NEW runs' exports; old stored runs stay unsorted).
- **Title-variant fan-out — measured, MARGINAL, NOT building blind.** Honest A/B (current keywords
  vs LLM title-variants) + hard-number pipeline run: variant-only roles → **15 (Ryan) / 29 (Zach)
  relevant STRONG+GOOD survivors** at **+$0.14-0.21/search (~$0.01/relevant)** — the filters DO
  absorb the noise (eyeball was too pessimistic). BUT the A/B baseline used only 8 keywords;
  production ships ~40 builder keywords that already cover most variants → real incremental value
  is **small + largely redundant** for **+~20% cost**. **Verdict: don't build blind fan-out.**
  The cheap, low-noise win = **seniority-range + close-domain coverage in the keyword builder**
  (Ryan's keywords were all "Manager"-level; the real adds were "Accountant"/IC-level — a seniority
  gap). One-time profile-build cost, $0/search → folds into builder tuning, not a per-search feature.
- **Source-routing — confirmed DROP** (cast wide + embedding prefilter catches cross-overs like
  lab-admin→hospital-admin; hard source exclusion would drop them).
- **GovernmentJobs (NEOGOV) scraper — BUILT + tested in tree** (see Sprint E). The only niche board
  with a clean public ingestion path; iHire/Idealist/Vivian deferred with reasons below.

---

## 2026-06-03 (cont. 2) — coverage expansion: Workday +394, GovernmentJobs 31→87, Idealist email
- **Workday tenants: 167 → 560 (in tree).** A 10-agent discovery workflow found 497 candidate
  myworkdayjobs URLs across 10 industry segments; the fast validator (real scraper path, no
  Playwright) confirmed **394 net-new working** (76 dup, 27 reject the standard CXS shape).
  Heavy on CPG/retail/grocery/beverage/food (great for Zach-type personas) + healthcare/finance/etc.
  Files written to `workday_tenants/`. **NOT ship-ready: 560 tenants is too slow** (560 × ~25 kw =
  ~14k CXS calls + a per-run facet-400-retry storm). **Fix before ship (pick one):**
  (a) **industry-aware tenant selection** — tag tenants by industry (discovery already returned an
  `industry` per candidate) + query only tenants matching the user's `target_industries` (+ a few
  generalists). Mirrors the GovernmentJobs location-aware win; keeps all 394 without the latency.
  (b) **persist facet-incompatibility** across runs (Sprint F item — kills most of the retry storm).
  (c) bump `WORKDAY_CONCURRENCY` 20→~48. (d) or trim to a curated high-value subset (~100).
  Recommended: (a)+(b)+(c). Candidate list saved: `scripts/.workday_candidates.json`.
  **DONE in tree (2026-06-03):** (b) **facet-incompatibility persistence** — cross-run cache
  (`archive/workday_facet_blocklist.json`), negative-only + 7-day TTL + only-confirmed-this-run
  refresh = never stale; 136/167 current tenants reject facets, now skipped from the start.
  (c) **concurrency** `WORKDAY_CONCURRENCY` 20→80 + httpx pool 100→150. Measured full-560 speedup:
  ~35min → ~4.2min at conc=150/pool=220 (~8×).

  **e2e validation (2026-06-03 eve) — keep-560 essentially validated:**
  - **Industry-aware selection: REJECTED** (Ziad) — same fragility as source-routing (a CPG-tagged
    tenant may post a finance/admin role a non-CPG user wants; selection hides it). Keep casting wide.
  - **Quality lift CONFIRMED:** Zach Workday-only e2e, 167 vs 560 → **9 → 48 STRONG+GOOD (5.3×) at
    SAME cost** ($0.32; the embedding prefilter caps scoring at 500 either way, so LLM cost is flat).
  - **Per-tenant concurrency cap — DONE** (`WORKDAY_PER_TENANT_CONCURRENCY=4`, acquired before global
    sem). 429 storm 8,607 → ~400 (90%+). Residual ~400 = a few hyper-sensitive tenants; tunable
    (lower cap / add backoff); 429s don't drop roles.
  - **Liveness was the real bottleneck (546s/749), NOT scrape.** First fix (raw `_client.head`) was a
    REGRESSION — marked 400/400 active roles dead (bare HEAD lacks the wrapper's UA → Workday 404s);
    A/B caught it, REVERTED. Correct fix shipped: keep `.head` wrapper + faster Workday-domain pacing
    (1.5-3.5s → 0.6-1.2s via `_pacing_for` suffix match) + conc 60 → **546s → 62s (8.8×), 100% alive.**
  - **Proxy daily cap = 5,000 LLM calls/day** (hit by today's heavy testing → blocked the clean
    score re-confirm). A normal search ≈ 700-1k calls. Re-confirm clean 560 score time TOMORROW.
  - **TO COMMIT 560:** after tomorrow's clean score re-confirm + Ziad go → unpark `_pending_v0330/`
    + bump `WORKDAY_CONCURRENCY` default 80→~120-150 (safe with the per-tenant cap). The cap +
    liveness-pacing + conc fixes already help the shipped 167 (faster, fewer 429s) regardless.
- **GovernmentJobs: 31 → 87 agencies + LOCATION-AWARE selection** (`governmentjobs_agencies.json`,
  35 states, strong VA). Now fetches only the user's region (VA user → 8 VA agencies; DC user → 10
  VA+MD metro, correctly NOT WA state; no-location → bounded default 24) — so the bigger list is
  *faster* than the old all-31 fetch. Tested: VA scrape = 33 roles across 7 VA agencies. Ship-ready.
- **Idealist Listings API: email drafted** (`IDEALIST_API_REQUEST_EMAIL.md`). Data is the richest of
  any niche board (salary/locationType/remoteZone/functions/areas-of-focus). Blocked only on a
  manually-approved key → emailing for it (task #19). Scraper is a quick build once granted.

---

## Sprint A — Reliability & robustness (the stuff that bit us this session) — LOWEST RISK, do first
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **activeRunId persistence + auto-recover** | Persist the in-progress run id; re-attach the poller on app launch/refresh so a restart doesn't drop the "search running" state. | none | Low |
| **Stale/foreign-backend guard** | On launch, detect & kill a leftover backend on :8765 (or refuse to attach to one that isn't this app's proxy sidecar). Prevents the 172-qualifying scare (app connected to a leftover local-mode backend → indexers off). | none | Low-Med (must not kill the app's own sidecar) |
| **Build artifacts off OneDrive** | Point `CARGO_TARGET_DIR` + `binaries/` to a non-OneDrive path so OneDrive can't lock the freshly-swapped exe (caused white-page/slow-start). | none | Low |

## Sprint B — Cost (the reconciled real levers; combined target $0.82 → ~$0.40-0.50)
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **Flash-Lite for Stage 2** | Swap `gemini-2.5-flash` → `flash-lite` for triage (~6× cheaper). config.py already flags it a "candidate". BIGGEST remaining win. | **−$0.22 to −$0.27** | Med-High — Stage 2 drives the off-target gate + scores; **needs an A/B vs the fixture set** before flipping |
| **thinking_budget=0 on confident Stage 2** | Drop thinking (currently 1024) for clear cases; keep it only for the uncertain 60-79 band. | −$0.05 to −$0.15 | Med — validate quality on the uncertain band |
| **Flash-Lite for Stage 1** | Same swap for the cheap anti-pattern prefilter. | −$0.03 to −$0.05 | Low |
| **Pre-AI MinHash/LSH near-dedup** | Drop near-duplicate JDs before any LLM (79% redundant across runs; prototype validated, `datasketch`). | −$0.05 to −$0.10 | Low-Med (tunable threshold) |
| **BM25 first-pass** | Rank + drop bottom 25-30% before embedding/Stage 1 (`bm25s`; claimed <2% recall loss). | −$0.08 | Med — needs recall check |
| **Confidence-gated cascade** | Skip Stage 3 when Stage 2 is confident; gate on ENSEMBLE agreement (not raw confidence, per research). | −$0.10 to −$0.15 | Med — less impactful now Stage 3 is cheap |
| **Score-then-content split (Stage 3)** | Score-only cheap call for all; full analysis only for top-N (validated 84% Stage-3 cut). | −$0.10 to −$0.20 | Med — frontend needs a "content not loaded" state |
| **Local / Cloudflare-Workers-AI embeddings** | Move embeddings off Gemini (bge-m3 on Workers AI ≈ 98% cheaper, near-free tier). | −$0.014 (~free) | Low |
| **LLMLingua-2 prompt compression** | Compress the 14K Stage-2 system prompt ~3.5× (stacks with caching on cache-misses). | −$0.05 to −$0.10 | Med — could prune domain rules |
| **Gemini Batch API (50% off)** | Submit all stages async for a "scan tomorrow" mode. | ~−50% (~$0.40) | Low cost / High UX (turns search async) |

> Don't sum — Flash-Lite + thinking=0 both hit Stage 2; dedup + BM25 both cut volume.
> **Highest-ROI trio: Flash-Lite Stage-2 + thinking=0 + MinHash dedup.**

## Sprint C — Scoring / relevance (v0.4; absorbs the v0.3.29 no-go)
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **Interactive gates** (headline v0.4) | At the keyword/profile-review step, show the per-profile gates (functions, exclusions, clearance) and let the user confirm/adjust before scoring. | none | Med — sizable UI + state change |
| **Excluded-body arbitration** | A targeted call on ONLY the ~35 body-scan-fired roles/run: "is eng/sales the CORE duty or just mentioned?" — the clean fix the v0.3.29 stage3-defer rule couldn't do (it over-tiered real eng/sales). | +$0.01 | Low-Med — only touches the gated set |
| **Clearance gate (user-set)** | Toggle "no clearance" → hard-require TS/SCI caps; nice-to-have stays. Lives in the interactive gates. (Root cause confirmed: profile `negative_signals` is empty by default → nothing to fire.) | none | Low |
| **Off-target gate residual** | Tighten the Stage-2 prompt for the ~2 mislabels (Loftware/Phase2 read as off-target). | none | Med — prompt tuning, oscillation risk |

## Sprint D — Visible polish / UX
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **Salary estimates** | Blue `~$150K–190K (est.)` from JSearch/GoogleJobs estimates we already fetch + discard; click-popover w/ source; hide if >$20K below floor; EXCLUDED from stats/floor/tiering (separate `salary_estimate_*` fields). Spec fully locked. | ~$0 (data already fetched) | Low — display-only |
| **Granular progress bar** | Real fraction (weighted phases) instead of chunked %. | none | Low |
| **Better loading screen** | The polished loading UX Ziad asked about. | none | Low |
| **Targeted link-upgrade** | Swap gatewall reposter links (BeBee/vaia/jobleads) → direct ATS via GoogleJobs exact-match. | +$0.05 to +$0.15 | Low-Med (exact-match only) |

## Sprint E — Scraper coverage (the "don't forget" v0.3.6 queue)
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **NEOGOV / GovernmentJobs** | ✅ **BUILT (in tree, 2026-06-03)** — `backend/scraper/governmentjobs.py`, registered + active. Per-agency RSS, curated ~31 high-volume agencies (strong VA+CA+WA+TX), expandable (Workday-tenant pattern). Rich feed: inline JD + structured salary. Standalone test: 755 kw-matched roles, **99% salary / 98% full-JD coverage**, free/no-key. Closes the state/local-govt gap (USAJOBS = federal-only). **TODO before ship:** e2e tier-quality validation (running) + add to frontend source list. | free (no API cost; +~30-60s latency) | Low |
| **iHire vertical template** | ⚠️ **DEFER** — probed 2026-06-03: iHire's XML feeds are **inbound only** (employers post *to* iHire); no public outbound read feed/API. Would need a partner agreement. Its trades/pharmacy/nursing gap is partly covered by GovernmentJobs (trades/clinical) + GoogleJobs. | — | blocked |
| **Idealist** | ⚠️ **DEFER (needs key)** — a read "Listings API" (JSON) DOES exist, but requires a **manually-approved API key** (email Idealist support); no open public endpoint. Pursue the key if nonprofit coverage is prioritized. | free if key granted | blocked on key |
| **Vivian Health** | ⚠️ **DEFER** — healthcare/nursing JSON search API existed via their (now-deprecated) ChatGPT plugin; only an **internal/undocumented** endpoint remains → fragile + ToS-gray (same reason we avoided Apify/LinkedIn). Revisit only if an official endpoint appears. | — | blocked (ToS/fragile) |
| **HealtheCareers** | Clinical roles — NOT yet assessed for ingestion path. Next niche-board to probe. | same | Low |
| **iCIMS revival** | Needs Playwright (excluded from PyInstaller today). | same | High — bundling Playwright into the sidecar |
| **~~SerpAPI/Google Jobs rewrite~~** | LIKELY SUPERSEDED — GoogleJobs via DataForSEO works (1006 raw in the last run). Confirm, then close. | — | — |

## Sprint F — Smaller data/scraper fixes
| Item | Details | Cost Δ | Risk |
|---|---|---|---|
| **Workday facet 400s** | Hundreds of "400 with facets — retrying" per run; fix facet-incompatibility detection (faster scrape, cleaner logs). | none (faster) | Low |
| **JD-capture stubs** | Some sources return redirect/JS/cookie shells instead of real JDs (100 CRC, Dovel) → scored blind. Better per-source fetch. | ~$0 | Low-Med |
| **Salary hourly-display flag** | Flag contract/hourly ranges so they aren't shown as clean annual. | none | Low |

---

## Already-shipped cost wins (DON'T redo)
- **Stage-3 cheap stack** (Gemini Pro → gpt-5-mini + deepseek via OpenRouter) — the single biggest cut, $3.19 → ~$0.34 (v0.3.25). This is why $3.82 → $0.82.
- jd_score_cache (skips re-scoring repeat JDs), Gemini context caching (~75% of Stage-2 prompt), embedding prefilter + Flash Stage-1.

## Researched & REJECTED (don't re-research)
- Claude Sonnet/Opus for Stage 3 — MORE expensive than Pro.
- Jina embeddings / Lightcast / GLIDER judge — license-blocked (non-commercial).
- Full local LLM scoring — 50-88 min/run (too slow).
- `embed_keep<0.55`, `stage3_max<200`, `skip_below>55` — all TRIED + REVERTED (lost real STRONGs).
- Serper `/jobs` + Bing endpoints — 404, return 0 (replaced by GoogleJobs-via-DataForSEO).
- Self-Refine / Tree-of-Thoughts for scoring — cost-up, LLMs fail at self-correction.

## v0.3.29 NO-GO record (so we don't blindly retry)
The excluded-body precision fix was validated against fresh Opus grades and FAILED the
zero-regression bar: the "defer the body-scan cap when Stage-3 scored it high" rule
rescued ~5/9 buried gems but ALSO released real eng/sales roles (Cohesity STRETCH→80,
Alteryx GTM, RapidCanvas) at every threshold (T75: 5 rescued / 3 regressions; T80: 4/1).
Root cause: the cheap-stack Stage-3 over-scores some genuine eng/sales roles the SAME way
it scores the gems, so "trust a high Stage-3" can't separate them. → The clean fix is the
targeted **arbitration call** (Sprint C), not a Stage-3-confidence heuristic. Clearance
(Fix 3) worked directionally (~21 hard-require roles dropped) but is per-user profile data
→ belongs in the interactive gates (Sprint C), not a blind code change.

## Recommended order
A (reliability) → B-trio (Flash-Lite Stage-2 + thinking=0 + MinHash dedup) → D (polish batch) → C (v0.4 interactive gates) → E (scraper coverage) → F (cleanup).

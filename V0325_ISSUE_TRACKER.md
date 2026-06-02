# v0.3.26 — Live-Testing Issue Tracker & Fix Plan

Status: ✅ fixed · 🔎 confirmed in code · ❓ open verification · ⛔ blocked
Updated: 2026-06-02

## ★ BUILD STATUS — ALL 13 ISSUES FIXED & LIVE IN v0.3.26 TEST BUILD ★
Backend rebuilt (33.7 MB clean), swapped into all sidecar targets, JD score cache
rotated (`.bak-v0326`), app relaunched — `/health` reports **v0.3.26**.
- **Frontend fixes (UI1, SC3, F1–F3)** show on your CURRENT results after a refresh.
- **Backend fixes (S1–S5, L1–L3, SC1–SC2)** apply to a **fresh search** (they change
  scraping/extraction/scoring). Run a new search to validate.

Validation evidence (against run `446e4298`, before/after):
- S2/S3/S5 salary: Loftware $220–280K → **none** (sidebar cut) · Komodo → **$125–185K** ·
  Wilson Sonsini → **$91.8–124.2K** · Harris → **$79–90K** (USD, not CAD). 122/309 salaries
  unchanged (no regression); Oklo/Lloyds keep their OWN salary.
- UI1: 43 roles had leaked tags → stripped at display + double-penalty guard added.
- SC1: 39 roles correctly capped→STRETCH (Oklo 12+, Smith Hanley 10+, Gartner 15+,
  Consulting Point Chief Officer…), 0 false-positives after tightening · SC2: Aalis
  SkillBridge → SKIP.
- L3: source-priority dedupe (ATS > .gov > boards > aggregator > reposter-host).

### Added since (also live in v0.3.26):
- **Freshness re-check button** (dashboard header) — re-validates this run's links live
  (`POST /roles/recheck`, same liveness engine as a full search; ~30–60s, ~$0). Removes
  only confident-dead (404/expired/closure-banner), keeps+flags uncertain, undo + "checked
  Xm ago" stamp + >3-day "run a fresh search" nudge.
- **Run-failure traceback logging** (`api.py`) — failed runs now write the full stack to
  backend.log (the OSError "[Errno 22]" left no stack before; a hung run was cleared).
- **Salary-floor serialization** (`salary_minimum_used` in the audit summary).

### Queued — v0.3.27 (approved, building next, flag-gated):
- **Targeted link-upgrade** — for hard-filter survivors whose link is a gatewall reposter
  (BeBee/vaia/jobleads), query GoogleJobs by exact company+title and swap to the direct/ATS
  link before scoring. ~$0.05–0.15/run, +20–40s, exact-match only (no wrong-link risk).
  Building after the first clean v0.3.26 validation run so it gets its own validation.
- **Salary estimates for no-disclosure roles** — capture the per-posting aggregator estimates we
  currently discard (JSearch/GoogleJobs). **Adzuna title+location prediction DROPPED** (too error-prone /
  wrong-industry risk; per-posting estimates are more trustworthy + free; revisit only if coverage thin).
  Display spec (locked w/ user):
  - Color = the **blue** of the `via "<keyword>"` tag (NOT gold — gold implies high pay).
  - Format `~$150K–$190K (est.)` + hover tooltip naming the source ("market data, not the posting").
  - **Estimates are DISPLAY-ONLY — never demote or remove a role** (only disclosed salary drives the floor).
  - Floor-aware show/hide (GENEROUS by design — estimates aren't fully trusted): show the estimate
    UNLESS its TOP (max) is more than **$20K below the floor** (absolute, tunable — easy to push to $30K+).
    i.e. floor $130K → hide only estimates topping out under ~$110K (obviously out of range). Otherwise
    show. Hidden = keep role + "Salary not disclosed". Rationale: a low estimate on a strong-fit role is
    likely estimate error; only target the obviously-out cases, never demote/remove on an estimate.
  - Accuracy guardrails (user worry: wrong industry / wrong number): PREFER per-posting aggregator
    estimates (estimate of THIS job) over Adzuna title+location prediction (the wrong-industry risk lives
    there); sanity-bound (reject implausible); the click-popover shows the SOURCE + basis so the user can
    judge/discount a mismatch; never let an estimate change a tier.
  - **Click the estimate → popover** with source ("Adzuna market avg for '<title>' · '<location>'"),
    how it was derived (title + location + seniority), and a one-line disclaimer ("estimate, not from
    the posting — verify before applying").
  - **Excluded from ALL stats** (Stats page avg salary, comp histograms, etc.). Achieved structurally:
    store estimates in SEPARATE fields (`salary_estimate_min/max` + `salary_is_estimate`), leaving
    `salary_min/max` for DISCLOSED only → stats, floor, and tiering all read `salary_min/max` and thus
    ignore estimates automatically; only the card's display layer surfaces the estimate.
- **Move build artifacts out of OneDrive** — point `CARGO_TARGET_DIR` + sidecar `binaries/` to a
  non-OneDrive path so OneDrive can't lock the freshly-swapped exe (caused the white-page/slow-start).
- **Persist `activeRunId` + auto-recover an in-progress run after a frontend refresh** — today a
  refresh drops the "search in progress" indicator even though the backend job keeps running
  (activeRunId isn't persisted; no recovery on load). Persist it + re-attach the poller on launch.
- **Granular progress bar** — replace the chunked phase % with a real (rough) fraction. The pipeline
  already knows the counts (N sources, N roles to embed/score). Weight phases (scrape ~0-35, filter/
  liveness ~35-50, JD-fetch ~50-55, embed+score ~55-95, finalize ~95-100) and emit the real fraction
  inside each (scoring is per-batch → smooth; scrape = sources-done/total). Keep rotating messages on top.

---

_(Diagnosis below was the pre-fix investigation; kept for the record.)_

---

## A. ✅ FIXED — frontend (live on refresh; not yet committed)
| ID | Issue | Fix |
|----|-------|-----|
| F1 | Header "118 strong matches · +191 more" | → "Total Matches: {N}" (Dashboard.tsx) |
| F2 | Tier cards "top ~12% / next ~26%" | label removed (TierCard.tsx) |
| F3 | Salary "$208K–$208K" when min==max | single value (RoleCard.tsx) |

---

## B. SALARY DATA INTEGRITY  🔴 (all stem from naive extraction in `salary_extractor.py`)
`extract_salary_from_jd` strips HTML, scans first **8000 chars**, returns the **first** `$`/K match.

| ID | Symptom (your example) | Root cause | Scope | Fix |
|----|------------------------|-----------|-------|-----|
| S1 🔎 | Below-floor role as TOP/GREAT (SkillBridge $80–120K; Harris $79–90K USD) | Realism penalty only fires when `salary_min < 70%·floor` AND −15 is too weak to demote; `passes_salary_floor` uses `salary_max` only and passes None | penalty doubled on 2; 8 currency-masked | Resolve canonical (min,max) **before tiering**; demote when `salary_max < soft floor`; one-shot penalty |
| S2 🔎 | Loftware shows $220–280K = Oklo's pay from the **Similar Jobs** sidebar | BuiltIn `fetch_jd` returns whole content div / `body[:8000]` incl. sidebar (`builtin.py:201-209`); first-match grabs the neighbor's number. Pollutes **scoring** too | 26/28 BuiltIn JDs carry the block | Strip page chrome ("Similar Jobs / Recommended / Jobs you might…") before store + extraction |
| S3 🔎 | Wrong region (Komodo $144–215K SF/NYC not $125–185K; Wilson Sonsini $102–138K not $91.8–124.2K) | First labeled range wins; no region awareness (`salary_extractor.py:274`) | 31 roles w/ multi-region language | When ≥2 ranges, pick national/"all other"/remote, else the **lowest** (conservative) |
| S4 🔎 | LinkedIn role "Salary not disclosed" when LinkedIn shows $110K/yr (Saragossa) | We don't scrape LinkedIn directly — link arrives via JSearch/GoogleJobs; LinkedIn gates JD+salary behind auth → JD fetch returns stub → no salary | linkedin = 11 links | Capture salary from the aggregator's structured field (JSearch `job_salary`/`min`/`max`) at scrape time, before the dead JD fetch |
| S5 🔎 | CAD shown as USD + mixed range (Harris "$79K–$125K" = $79K USD low + $125K CAD high; real USD $79–90K) | Extractor ignores currency; grabbed first range (CAD) and crossed it with USD; below-floor masked | 8 non-USD JDs | Detect currency token (CAD/C$/£/€); prefer the **USD** range; never cross two ranges |

> Coupling: fixing **source priority (L3)** so we keep the ATS/clean copy over BuiltIn/aggregator removes most of S2 and gives clean structured salary for S1/S3/S5.

---

## C. SCORING CALIBRATION  🟠
| ID | Symptom (your example) | Root cause | Scope | Fix |
|----|------------------------|-----------|-------|-----|
| SC1 🔎 | Over-rated despite hard gates: "equity Partner" (Consulting Point), "10+ yrs Pharma" (Smith Hanley), "15+ yrs" (Abbott/BigBear/Gartner) | Scorer rewards functional fit; no **hard experience/seniority gate** detection (10+/15+ yrs, Partner/Chief/VP/C-level vs candidate's tenure) | **12 STRONG/GOOD** hit a 10+/15+/equity-partner gate | Add a deterministic gate: years-required ≫ candidate AND/OR exec-title ⇒ cap at STRETCH/SKIP |
| SC2 🔎 | Non-standard engagement rated GREAT: "SkillBridge" (3-mo military transition), part-time | No exclusion for SkillBridge/internship/fellowship/part-time/contract-to-transition | spot-confirmed | Title/JD signal → cap or exclude (profile-gated like off-target) |
| SC3 🔎 | "~30 GOOD with no summary" | `summary` is **Stage-3-only** (`stage3_deep_eval.py:747`); 26 GOOD roles skipped Stage 3 (Stage-2 conf ≥88 or path-b gated) → only `stage2_reasoning` exists; `RoleCard.tsx:55-60` shows `summary‖stage3_analysis` (no `stage2_reasoning` fallback) → blank | **GOOD 26 / MAYBE 20 / STRETCH 73** skipped Stage 3 | Generate a short summary for Stage-2-final roles (or fall back to a cleaned `stage2_reasoning`) |

---

## D. USER-FACING TEXT  🟠
| ID | Symptom | Root cause | Scope | Fix |
|----|---------|-----------|-------|-----|
| UI1 🔎 | Internal tags shown to users: `[contradiction-resolved]`, `[title-floor:60,overlap=…]`, `[salary-penalty:-15 …]` (doubled), `[excluded-…]` | Tags prepended to `summary`/`stage3_analysis`/`stage2_reasoning` (orchestrator.py:80,127,137,155 + Stage-2 title-floor/contradiction resolver) with **no stripping before display**; penalty applied twice (idempotency) | **43 roles (14%)**: contradiction-resolved 29, title-floor 19, excluded 6, salary-penalty 2 | Route audit flags to a separate non-displayed field, OR strip leading `^(\[[^\]]+\]\s*)+` from user-facing text at serialization; add idempotency guard |

---

## E. LINK / SOURCE QUALITY  🟠
The `source` ≠ the link host. Aggregator links enter via **GoogleJobs/JSearch** (apply URL = whatever the index had). **64/309 = 21% aggregator links**; clean ATS hosts = 153/309 = 50%.

| ID | Symptom | Root cause | Scope | Fix |
|----|---------|-----------|-------|-----|
| L1 🔎 | Shoddy links: bebee (22), vaia (8), jobleads (11), linkedin (11), lensa/jooble/talent.com | JSearch uses `job_apply_link`, **ignores `apply_options[].is_direct`** (employer site); GoogleJobs prefers `source_url` over `employer_url` | 64 aggregator links | Prefer the direct-employer apply option both APIs already return; drop/deprioritize gatewall-only roles |
| L2 🔎 | Dead links: HHMI "no longer available"; builtinchicago "removed May 29" (before the run!); talent.com "no longer accepting" | Liveness HEAD + dead-listing regex (`runner.py:286,332`) miss board-specific removal banners; some removed pre-run still pass | reported ×3+ | Add board-specific removal-banner patterns (builtin "was removed", "no longer accepting applications"); re-validate on click |
| L3 🔎❓ | **BeBee kept over a better board when the job was a dupe** (your 3 BeBee/lensa/appcast/theirstack URLs) | `_cross_board_dedupe` keeps **first-seen** (`scraper/orchestrator.py:443`); no source-quality rank | — | Source/host priority sort **before** dedupe: company ATS > .gov > major boards > aggregator APIs > reposter hosts (bebee/vaia/jobleads/lensa) last |

---

## F. LOCATION / REGION  🟡
| ID | Symptom | Root cause | Fix |
|----|---------|-----------|-----|
| R1 🔎 | Shown "Remote" while source card says "In-Office, Bayamón PRI" (JD does say remote → source card wrong) | `passes_location` **always includes Remote** regardless of `acceptable_locations` (`hard_filters.py:392,471`) — region filter only constrains hybrid/on-site. Remote location text often wins over the card | Confirm remote roles are genuinely US-eligible (scan JD for non-US-only); keep "be generous" but verify display source priority (JD > card for remote) |

> Verdict: region filtering is working as designed (generous; remote is location-agnostic). The PRI case is a **source-data** error, not a filter bug. Low priority.

---

## G. TIERING  🟡
| T1 | Dual-method tiering (bands vs percentile, "pick best") — ❓ needs your "best" rule. **Recommend: drop it** — percentile validated best; "looks best" optimizes appearance + flips run-to-run. |

---

## H. SHIP — v0.3.25  🚢
| D1 ✅ | Worker OpenRouter deploy — DONE 2026-06-02. `fmsdj-worker` (ver 702ed38e), `OPENROUTER_API_KEY` secret set, `/v1/llm/openrouter` verified end-to-end (200 + `X-Proxied-By` + real model list) on workers.dev AND api.findmesomedamnjobz.com. UUID-gated + daily-capped. |
| D2 | .msi build (+cargo/tauri → 0.3.25) |
| D3 | Tester cache-invalidation note (JD cache is model-agnostic) |
| D4 | `proxySerper` dangling ref (pre-existing) cleanup |
| — ✅ | scorer + percentile + off-target gate + Worker route committed (`9e1a5a7`) |

---

## PRIORITY / SEQUENCING (each phase = 1 rebuild + relaunch for you to test)
1. **Quick polish (cheap, high visible impact):** UI1 tag-strip + SC3 summary fallback + F1–F3 commit. Pure presentation; low risk.
2. **Salary integrity:** S2 strip-chrome, S3 region, S5 currency, S1 canonical+demote, S4 aggregator-salary capture, audit serialization.
3. **Link/source quality:** L3 dedupe priority → L1 apply-link upgrade → L2 removal-banner patterns.
4. **Scoring calibration (likely v0.3.26):** SC1 experience/seniority gate, SC2 engagement-type exclusion. Needs fixture-harness validation.
5. **Ship v0.3.25** (D1–D3) once Cloudflare auth cleared.

## OPEN VERIFICATIONS / YOUR CALLS
- ❓ JSearch `apply_options.is_direct` + JSearch salary fields + DataForSEO apply-array names (live response).
- ❓ S3 region policy (lowest vs location-match) · L1 drop-vs-deprioritize gatewall-only · SC1 years/seniority thresholds vs your tenure · T1 keep-or-drop.
- ❓ Confirm the exact run behind each screenshot (audit lacks serialized floor — fixing in B/S1).

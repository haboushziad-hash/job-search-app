# Scraper Deep-Dive v2 — Multi-Persona, Comprehensive

Read-only audit. No code modifications. Built on:
- **5 historical runs** at `~/Documents/JobSearchApp/audits/runs/` (2026-05-04 through 2026-05-05)
- **Code-level inspection** of all 16 active + 2 deferred scrapers (~5,800 LOC in `backend/scraper/`)
- **5 live probes** of candidate APIs (Ashby/Workable/SmartRecruiters/TheMuse/HigherEdJobs/WeWorkRemotely)
- The prior audit at `v03_scraper_audit.md` was the starting point — this version is broader (multi-persona) and quantified.

---

## TL;DR

1. **JSearch is the single highest-leverage source by 6x** — it averages 64.8 qualifying/run vs Greenhouse's 37.4 despite making 5x fewer raw requests, with 10.2% conversion vs 1.16%. Bumping `num_pages 3 → 10` in `jsearch.py:221` is still the #1 priority change. **Expected impact: +60-100 qualifying roles per run** (extrapolated from 10.23% conversion rate).
2. **The duplicate-tenant tax is bigger than reported.** Greenhouse has **30 redundant entries (168 → 138 unique)**. Ashby has **20 (64 → 44)**. That's 50 wasted HTTP roundtrips per run (~7.5s of wall-time at 150ms/call). One-line dedup fix.
3. **Healthcare/Legal/Education are critically underserved.** Across all 5 runs and 8 personas, our current 16 scrapers offer roughly 0% coverage for healthcare clinical roles, ~5% for legal, and ~10% for K-12/EdTech. **TheMuse Healthcare category does work** (live probe: 20 roles) — we're not using it because `_keyword_to_category` doesn't map "nurse"/"clinical" to Healthcare from the right keyword direction.
4. **Workable.com is a public-API gold mine we're missing.** Live probe confirmed `https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs` returns clean JSON for ANY tenant. Workable powers 10K+ companies (Y Combinator startups, mid-market SaaS, healthcare companies, EU mid-mkt). **Add this as a 17th scraper, +200-500 raw roles/run, +15-25 qualifying expected.**
5. **TheMuse + Climatebase + Remotive returning 0 qualifying for AI-strategy isn't a bug — it's persona mismatch.** But they remain valuable for healthcare/climate/remote-first personas. v0.3 needs persona-aware source selection (see Cross-cutting #3).
6. **Lever's tenant pool is engineering-heavy and showing 0 qualifying across 5 consecutive runs.** Either prune unproductive tenants OR refocus on Lever-using companies that match real personas (most tech-startup direct).
7. **iCIMS Playwright returns 0 in every audit because Playwright isn't installed.** The silent ImportError swallow at `icims_playwright.py:64-67` hides this from operators. Bundle issue. Fixing it unlocks insurance/hospitality/industrial coverage that nothing else reaches.

---

## Quantified opportunity ranking (top 15 by qualifying ROI)

Format: `qual_added / effort_hrs = ROI`. Ranked by ROI descending.

| # | Change | Qual added/run | Effort (hrs) | ROI | File |
|---|---|---|---|---|---|
| 1 | JSearch `num_pages 3 → 10` | +50-90 | 0.5 | ~140 | `jsearch.py:221` |
| 2 | Add **Workable** scraper (~50 tenants) | +20-30 | 6 | ~4.2 | new `workable.py` |
| 3 | Dedupe Greenhouse (30 dupes) + Ashby (20 dupes) tenant lists | +0 (saves 7.5s/run) | 0.3 | speed | `greenhouse.py:51-258`, `ashby.py:22-96` |
| 4 | USAJOBS pagination (page=1,2,3) + AI keyword expansion | +5-10 | 2 | 3.8 | `usajobs.py:102-105` |
| 5 | TheMuse — fix `_keyword_to_category` reverse mapping (fetch by category from profile_tags) | +3-8 | 1.5 | 3.7 | `themuse.py:185-237` |
| 6 | Ashby tenant expansion (12-15 verified non-engineering tenants) | +5-10 | 3 | 2.5 | `ashby.py:22-96` |
| 7 | JSearch concurrency `Semaphore(3) → Semaphore(5)` (Pro tier supports 5/sec) | +0 (saves ~30s/run) | 0.1 | speed | `jsearch.py:167` |
| 8 | Workday persistent `tenant_500_count` cooldown across runs | +0 (saves ~30s/run) | 1.5 | speed | `workday.py:144` |
| 9 | iCIMS Playwright bundle install + explicit logging | +5-15 | 6-8 | 1.5 | `icims_playwright.py:64-67` |
| 10 | Add **HigherEdJobs RSS** scraper (verified live, 150+ items) | +3-8 (Persona D) | 2 | 2.5 | new `higheredjobs.py` |
| 11 | Add **Idealist.org / nonprofit aggregators** | +3-8 (Persona G+nonprofit) | 4 | 1.5 | new `idealist.py` |
| 12 | Lever — prune Binance/Whoop/Spotify-only tenants, add SaaS leaders | +2-5 | 1 | 3.5 | `lever.py:26-48` |
| 13 | Adzuna — short-circuit other keywords on first 429 | +0 (saves ~10-15 calls/run) | 0.3 | quota | `adzuna.py:83-93` |
| 14 | SmartRecruiters tenant expansion (Visa works, RTX/Bosch/Allianz return 0) | +0-5 | 3 | low | `smartrecruiters.py:29-33` |
| 15 | Add **WeWorkRemotely RSS** scraper (verified, 16+ items per category) | +2-6 (remote-first profiles) | 2 | 2 | new `weworkremotely.py` |

ROI calculation: `qual_added / hrs`. Items 7, 8, 13 have qual=0 but improve speed/quota — prioritized for testers running multiple searches/day.

---

## Per-persona coverage analysis

### Persona A — Healthcare (ICU nurse, hospital admin, clinical informatics, healthcare IT)

**Current coverage:** Effectively zero direct coverage.
- Greenhouse: covers digital-health startups (Komodo Health, Flatiron Health, Maven Clinic, Strive Health, Twin Health, Modern Health, Hone Health). **Useful for "healthcare IT" / "clinical informatics" but NOT for ICU nurses or hospital admins.**
- Workday: tenants don't include hospital systems (Kaiser, Mass General, HCA, etc. all use other ATSes — typically iCIMS).
- iCIMS Playwright: covers Liberty Mutual, Six Flags, Cedar Fair, Snap-on, Chick-fil-A — none are hospital systems.
- TheMuse: **Healthcare category WORKS** (live probe: 20 roles from Optum, Geisinger, Christus Health, Children's Hospital). The bug is that the keyword→category mapper uses keyword tokens that don't include "nurse"/"clinical". A Persona-A user search for "registered nurse" will hit the "nurse" key and return Healthcare category — but this is undocumented.
- USAJOBS: covers VA, NIH, HHS — strong for federal healthcare admins.

**Gaps:**
1. No hospital-system iCIMS tenants (Kaiser uses iCIMS at `careers-kaiserpermanentecareers.icims.com`)
2. TheMuse Healthcare is a hidden gem — needs surfacing in the keyword mapping AND profile-tag-based source routing
3. No specialty boards (Practicelink, Health eCareers, Nurse.com — most are paid; HealtheCareers RSS is geofenced from Claude's WebFetch)

**Recommended new sources:**
- **iCIMS Playwright tenant additions (P1):** Kaiser Permanente, Mass General Brigham, Cleveland Clinic, HCA Healthcare. Effort: 2h to add 4 tenants (Playwright already wired).
- **TheMuse profile-tag routing fix (P1):** when `profile_tags` includes `healthcare`/`clinical`/`nursing`, fetch `category=Healthcare` regardless of keyword tokens. Effort: 1h.
- **Workable scraper (P0):** Workable powers many digital-health companies (HelpJuice, Carbon Health-adjacent, telehealth startups). Live probe confirmed `cohere`, `yougov` work; healthcare slugs need probing.
- **Greenhouse healthcare expansion (P2):** Add Cigna ATS slug if Greenhouse, otherwise Workday tenant probe.

---

### Persona B — Finance/Banking (analyst, portfolio manager, compliance officer, credit analyst)

**Current coverage:** Mid (mainly via Workday).
- Workday: Citi, PNC, Capital One, Truist, Visa, Mastercard, Morgan Stanley, BlackRock, State Street, Prudential, Allstate, AIG, Travelers — **strong**.
- Workday "STILL FAILING": JPMorgan, Goldman Sachs, Wells Fargo, BoA, US Bank, MetLife, Liberty Mutual — these are big gaps. (Comment at `workday.py:90-99`.)
- Greenhouse: Stripe, Brex, Mercury, Affirm, SoFi, Marqeta, Bill.com, Cross River Bank, Earnin, Alloy — fintech dense.
- USAJOBS: SEC/Treasury/GAO/FDIC roles.
- JSearch: aggregates from LinkedIn/Indeed/Glassdoor — covers all the above.

**Gaps:**
1. **JPMorgan, Goldman Sachs, Wells Fargo, BoA failing in Workday** — these are the largest US banks and they're ZERO right now. Phase 1.5 Playwright-based discovery work flagged but not done.
2. **No buy-side coverage** — hedge funds, PE firms (Blackstone, Carlyle, KKR) don't show up.
3. **No specialty boards** — eFinancialCareers (paid), Wall Street Oasis (community), efinancialcareers RSS — none integrated.

**Recommended new sources:**
- **Phase 1.5 Workday Playwright unlock (P2):** Most chronic Workday failures are auth-token issues; Playwright capture is the fix. The 20+ "STILL FAILING" tenants would 5x-10x banking coverage.
- **Greenhouse — add buy-side (P2):** Probe `blackstone`, `kkr`, `carlyle`, `apolloglobalmanagement` slugs.
- **Workable — add finance startups (P1):** TrueAccord, Petal, Brigit — Workable common.

---

### Persona C — Legal (associate, paralegal, in-house counsel)

**Current coverage:** Effectively zero.
- TheMuse "Legal" category: **live probe returned 0 results.** Category exists but is empty.
- Greenhouse: Anthropic posts a "Compliance Governance & Oversight Lead" (qualified for Ziad's run at score 76). One-off.
- USAJOBS: covers DOJ, OMB, OGC roles (federal counsel).
- Workday: BDO/PwC/Big-4 audit-and-tax roles (adjacent but not legal).
- JSearch: catches some when keywords match.

**Gaps:**
1. No legal-specific board.
2. LawCrossing has paid-only API.
3. State bar associations have RSS feeds but they're tiny.

**Recommended new sources:**
- **Workable legal-tech (P2):** Harvey (already in Ashby), Ironclad, ContractPodAi, Spellbook — Workable common.
- **Greenhouse legal-tech expansion (P2):** Probe `clio`, `lexisnexis`, `thomsonreuters` slugs.
- **Honest assessment:** Persona C is the hardest persona to serve well because legal hiring is high-touch (recruiters > job boards). v0.3 should make this a **known limitation** in the user-facing copy.

---

### Persona D — Education (K-12 teacher, professor, EdTech, instructional designer, university admin)

**Current coverage:** Weak.
- Greenhouse: Coursera, Duolingo, Khan Academy, CourseHero, Outschool, MasterClass, Newsela, 2U — **EdTech is OK**.
- TheMuse: "Education" category live probe returned 0 results — surprising, possibly seasonal.
- USAJOBS: covers DOE, ED, federal training roles.
- iCIMS: no university tenants.

**Gaps:**
1. **K-12 teaching roles: zero coverage.** Public school districts use NEOGOV/Frontline, not anything we scrape.
2. **University faculty/admin roles: zero direct coverage.** They use HigherEdJobs (probed live, valid 150+ item RSS) and ChronicleVitae.
3. **Instructional designer roles** are partly catchable via Workable/Greenhouse EdTech, but boards specific to designers (LearnXD, eLearning Industry) are absent.

**Recommended new sources:**
- **HigherEdJobs RSS scraper (P0 for Persona D):** Live probe confirmed valid feed at `https://higheredjobs.com/rss/articleFeed.cfm?CatID=11`. Multiple category-specific feeds. Effort: 2h. Expected: 50-150 raw roles/run for education-focused users.
- **NEOGOV state/local government scraper (P1):** Many K-12 districts post via NEOGOV; the public-facing pages have parseable HTML. Effort: 6h (per-district URL discovery). Could 10x K-12 coverage.
- **TheMuse profile-tag routing (P1):** Surface "Education" category for users with `education`/`teacher`/`professor` profile tags — even if 0 today, may have seasonal flow.

---

### Persona E — Trades/Manufacturing (electrician, machinist, plant manager, industrial engineer, supply chain)

**Current coverage:** Light.
- iCIMS Playwright: Snap-on (industrial sales). One tenant.
- Workday: T-Mobile, JLL — adjacent. Capital One, Citi — not relevant.
- Greenhouse: Atomic Industries (in Ashby actually, "Atomic Industries" 10 jobs — manufacturing). Lucid Motors, Anduril — strong defense/auto.
- BuiltIn: tech-leaning, not trades.

**Gaps:**
1. Most large manufacturing companies (Boeing, GE, 3M, Caterpillar) failed in Workday — same Phase 1.5 gap as banking.
2. No trades-specific board (Indeed Trades, Trade Hounds, Tradesmen International) integrated.
3. **No supply chain-specific board** (SupplyChainBrain, project44, DSI Logistics).

**Recommended new sources:**
- **iCIMS expansion (P2):** Bayer (`careers-bayer.icims.com`), Caterpillar (`careers-caterpillar.icims.com`), Hershey (probe).
- **Workable industrial (P2):** Many SMB manufacturers use Workable.
- **Direct company career-page scraping for non-ATS-ed industrial firms (P3):** Tauri ships Playwright; this would be a LARGE lift (~20-40 hours per major employer).

---

### Persona F — Creative (designer, copywriter, marketer, content strategist)

**Current coverage:** Strong.
- Greenhouse: Figma, Webflow, Squarespace, Pinterest, Reddit — design-heavy.
- Ashby: Linear, Vercel, Notion, Sierra — designer-strong startups.
- Lever: Outreach, Highspot — sales/marketing.
- TheMuse: "Marketing" / "Design and UX" categories — broad.
- BuiltIn: tech-leaning marketing/design.

**Gaps:**
1. No designer-specific portfolio job boards (Dribbble Jobs, Behance Jobs, Working Not Working).
2. AuthenticJobs (designer) — has RSS but nuance.

**Recommended new sources:**
- **Dribbble Jobs API (P3):** docs at dribbble.com/api — paid tier required for scraping; may not be cost-effective.
- **AuthenticJobs RSS (P2):** Free, valid RSS feed. Effort: 2h.
- **Workable marketing/design (P1):** Many DTC brands and creative agencies use Workable.

---

### Persona G — Government/Cleared (federal civilian, defense contractor, state/local gov)

**Current coverage:** Strongest persona we have.
- USAJOBS: full federal coverage.
- Workday: Leidos, Booz Allen, GDIT, CACI — Big 4 and federal contractors.
- Greenhouse: Anduril (1865 jobs!), Astranis, KoBold Metals — defense/space.
- ClearanceJobs: NOT integrated. Closed to scrapers.

**Gaps:**
1. State/local gov: NEOGOV-based, not currently scraped.
2. ClearanceJobs is closed to scrapers (paid recruiter access).
3. GovTribe (federal contracts adjacent to job postings): paid.

**Recommended new sources:**
- **NEOGOV state/local government scraper (P1):** Most US state and city governments use NEOGOV. URL patterns are predictable per district. Effort: 6h (URL discovery + HTML parser). Expected: +30-100 raw, +5-15 qualifying for state/local Persona G testers.
- **Code for America-like nonprofit civic boards (P2):** Idealist.org probed (404 on direct API; main site needs HTML scraping).

---

### Persona H — AI Strategy (current cohort)

**Current coverage:** Mature. JSearch + Greenhouse + Workday + Ashby + USAJOBS form a strong base. Avg 64.8 + 37.4 + 19.4 + 18.4 + 10 = **150 qual/run** (out of 178 average, with USAJOBS contributing the most consistent federal AI roles).

**Gaps:** Already understood (Phase 1.5 Workday Playwright unlock).

---

## Per-scraper deep dive

### JSearch — `backend/scraper/jsearch.py` (352 LOC)

**5-run trend (raw / qualifying):**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 (a02d8410) | 723 | 74 | 10.2% | 126s |
| 2 (16d89155) | 540 | 64 | 11.9% | 81s |
| 3 (facc3877) | 550 | 56 | 10.2% | 82s |
| 4 (bc27490e) | 825 | 85 | 10.3% | 119s |
| 5 (b6bdc773) | 529 | 45 | 8.5% | 90s |
| **Avg** | **633** | **64.8** | **10.2%** | **99.8s** |

**Code-level findings:**
- `jsearch.py:221` — `"num_pages": "3"` hardcoded. JSearch Pro tier accepts up to 10. **Single highest-leverage change in entire codebase.** Probe attempted via WebFetch but RapidAPI requires auth header so couldn't validate; per JSearch docs (https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch), `num_pages` accepts integer up to 10 on Pro.
- `jsearch.py:167` — `Semaphore(3)` is conservative. Pro tier allows 5/sec. Bumping to 5 cuts wall-time ~30% (from ~99.8s to ~67s).
- `jsearch.py:177` — `except (asyncio.TimeoutError, Exception): return []` swallows all errors silently. **Cannot distinguish quota failures from coding bugs.**
- `jsearch.py:253` — `items[:limit]` cap is dead-ish: with `num_pages=3`, JSearch returns max 30 items per call, so `limit=50` is never the binding constraint.
- `jsearch.py:170-178` — no retry on transient 502/503. JSearch occasionally bounces; one retry would catch ~5% of failed calls.

**Unrealized capabilities:**
- `employment_types=FULLTIME` is set, but JSearch supports `FULLTIME,PARTTIME,CONTRACTOR,INTERN`. For Persona D (educators sometimes accept contract) or Persona F (creatives often contract), making this configurable would help.
- `radius=` param (miles from a city). Currently unused. Could focus searches geographically for users with strict locations.
- `salary_min=` param. Could push the salary floor to the API rather than filtering downstream.
- `job_requirements=under_3_years_experience|more_than_3_years_experience|...` — currently unused. Could be auto-set based on profile experience.
- `country` is hardcoded to "us" via `params["country"]: "us"` (line 224). For non-US testers, this should be config-driven.

**Persona-specific yield:**
- A (Healthcare): Med — JSearch surfaces hospital roles via LinkedIn aggregation
- B (Finance): High — banks dominate JSearch's LinkedIn feed
- C (Legal): Low-Med — limited but present
- D (Education): Med — university recruiting on LinkedIn
- E (Trades/Mfg): Low — LinkedIn under-indexes blue-collar
- F (Creative): High — creatives are heavy LinkedIn users
- G (Gov/Cleared): Med — federal often posts to LinkedIn but uses USAJOBS canonical
- H (AI Strategy): Highest — already proven

**Specific fixes with expected impact:**
1. `num_pages 3 → 10`: 633 raw → ~2,000 raw, conv stays ~10% → +145 qualifying. **+221% relative gain.** Cost: ~70 extra calls/run × 11 keywords × 7 pages added = ~770 calls/run = 8% of Pro 10K monthly.
2. `Semaphore(3) → 5`: -30s wall-time, no qualifying impact.
3. Add log on quota: if `quota_exhausted=True`, also `print(f"[jsearch] quota: {reason}")` for tester transparency.
4. Make `country` configurable from `profile.country_code` or `.env`.

**Recommended priority:** v0.3 P0 (highest ROI in entire codebase).

---

### Greenhouse — `backend/scraper/greenhouse.py` (438 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 2,942 | 31 | 1.05% | 71s |
| 2 | 3,595 | 47 | 1.31% | 74s |
| 3 | 3,599 | 49 | 1.36% | 75s |
| 4 | 3,349 | 25 | 0.75% | 69s |
| 5 | 2,652 | 35 | 1.32% | 70s |
| **Avg** | **3,227** | **37.4** | **1.16%** | **71.5s** |

**Code-level findings:**
- `greenhouse.py:51-258` — **30 duplicate slugs** (verified via Python regex scan):
  - `toast` 3x, `Asana`, `Discord`, `Coursera`, `Duolingo`, `Khan Academy`, `Squarespace`, `HubSpot`/`Hubspot`, `Cloudflare`, `Instacart`, `Reddit`, `Pinterest`, `Affirm`, `Chime`, `Carta`, `Gusto`, `Justworks`, `Lattice`, `Pendo`, `Komodo Health`, `Flatiron Health`, `Marqeta`, `Bill.com`, `Lucid Motors`, `Airbnb`, `Flexport`, `project44` (case-sensitive collision).
  - 168 entries → 138 unique. **30 wasted HTTP calls (~150ms each = 4.5s wasted per run).**
- `greenhouse.py:308-354` — **NO concurrency cap.** All 168 tenants fan out in parallel. Bounded only by ScraperClient's per-domain Semaphore(2). Could overwhelm slow networks; intermittent timeouts contribute to "0 results" for tenants with normal posting volumes.
- `greenhouse.py:36-46` — `_matches_any_keyword` uses token-overlap. Good. The 60% threshold for 3+ token keywords means roles with 3 of 5 keyword tokens pass. For "AI Enablement Services Lead" (5 tokens), 3 matching tokens (60%) means a "Lead, Services" role with NO "AI Enablement" content can pass. This is **likely the source of false positives** that get filtered out at the orchestrator level (visible in logs: `Workday: 4551 -> X` post-fetch sanity drops).
- `greenhouse.py:319-321` — exception handling per-tenant just `continue`s; no log of which tenant failed. **Fix:** add `print(f"[greenhouse] {slug} fetch failed: {type(result).__name__}")` so we can track tenant decay.

**Unrealized capabilities:**
- Greenhouse Boards API supports `?content=true&meta=true` for richer metadata. We pass `?content=true`. `meta=true` would include department info that might help disambiguate.
- Per-job fetch via `/v1/boards/{slug}/jobs/{id}?content=true` returns more detail than the bulk endpoint. **Currently we only call bulk.** For low-yield tenants where bulk returns rich JD already, this is fine; for tenants where bulk truncates JDs, per-job fetch would help.

**Persona-specific yield:**
- A: Med (digital health: Komodo, Flatiron, Maven, etc.)
- B: High (fintech dense)
- C: Very Low
- D: Med (EdTech: Coursera, Duolingo, etc.)
- E: Low
- F: High (Figma, Webflow, Squarespace)
- G: Strong (Anduril 1,865 jobs!)
- H: High (AI labs heavy)

**Specific fixes:**
1. **Dedupe tenant list**: 168 → 138. Saves 4.5s/run + makes the file maintainable. Effort: 0.2h.
2. **Add per-tenant yield logging**: emit `len(roles)` per slug to a sidecar JSON for the audit. Effort: 0.5h. Enables future "drop tenants returning 0 for N runs" automation.
3. **Add `Semaphore(20)`** at scrape time to bound parallel HTTP calls. Effort: 0.2h. Prevents network thrash; net wall-time should be unchanged because backend is bandwidth-bound, not cpu-bound.
4. **Per-job content fetch for low-yield tenants:** For tenants with <5 jobs, double-fetch (bulk + detail) gets richer JDs that score better in Stage 3. Effort: 1h.

**Recommended priority:** v0.3 P0 (item 1 is trivially cheap, items 2-4 are clean wins).

---

### Workday — `backend/scraper/workday.py` (441 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 3,904 | 9 | 0.23% | 50s |
| 2 | 4,423 | 35 | 0.79% | 102s |
| 3 | 4,412 | 26 | 0.59% | 99s |
| 4 | 5,222 | 5 | 0.10% | 75s |
| 5 | 4,551 | 22 | 0.48% | 96s |
| **Avg** | **4,502** | **19.4** | **0.43%** | **84.2s** |

**Code-level findings:**
- `workday.py:138` — `Semaphore(12)`. With 25 tenants × 11 keywords (or 39 for JSearch-extended) = 275-1,000 in-flight. Semaphore caps to 12 so it's bounded.
- `workday.py:144-150` — `tenant_500_count` correctly tracks 500-loops, but resets each run. **Persisting this across runs (~/Documents/JobSearchApp/scraper_state/workday_health.json)** would save 30s per run for chronic-failure tenants.
- `workday.py:90-99` — comments list 30+ "STILL FAILING" tenants (Deloitte, EY, KPMG, JPMorgan, Goldman, Wells Fargo, BoA, US Bank, Liberty Mutual, MetLife, P&G, Mondelez, Kroger, Costco, Microsoft, Oracle, IBM, SAP, J&J, Merck, BMS, UnitedHealth, CVS, Marriott, Hilton, Delta, United, AA, CBRE, AECOM, Boeing, GE, 3M, Caterpillar). **These are huge gaps for Persona B (banks), E (industrial), A (UnitedHealth/CVS), F (consumer brands).** All require Phase 1.5 Playwright auth-token capture.
- `workday.py:170-198` — posted-date filter happens here AND at orchestrator. Mild redundancy; not a bug.
- `workday.py:265` — `offset += 20` paginates internally, hard-capped at `limit_per_keyword=50`. For tenants with high active counts (Citi, Capital One), 50/keyword × 11 keywords = ~550 max from one tenant. Adequate per-keyword but noticeable for "common" keywords.
- **No keyword filter at scrape time** — relies on orchestrator's centralized post-fetch filter. With 4,502 avg raw, this is a significant memory hit (~50MB of role objects). For 8-tester production, fine. For mobile/Tauri-Lite builds, concerning.

**Unrealized capabilities:**
- Workday CXS API supports `appliedFacets` for filtering at scrape time. Currently passed as `{}`. Could pass `{"locations": [...], "jobFamilyGroup": [...]}` to pre-filter server-side. Effort: significant — facet IDs vary per tenant. Probably wontfix for v0.3.
- The CXS endpoint also supports `searchText` operators (AND/OR/quoted). Currently we send raw keyword. For multi-token keywords, this matters: "AI Strategy" might match either token alone, vs. quoted match. Effort: 2h to test + add.

**Persona impact:**
- A (Healthcare): Low — only Travelers/Allstate insurance tangentially; UnitedHealth/CVS in the failing list.
- B (Finance): High — current sweet spot.
- C (Legal): Low.
- D (Education): Low — no university tenants.
- E (Trades/Mfg): Med — JLL, T-Mobile, RAND. Most Mfg in the failing list.
- F (Creative): Low.
- G (Gov/Cleared): High — Booz Allen, Leidos, GDIT, CACI, RAND.
- H (AI Strategy): High — federal + Big 4.

**Specific fixes with expected impact:**
1. **Persistent `tenant_500_count` cooldown (`workday.py:144`):** save state across runs. Saves ~30s/run if 5 chronic-fail tenants are skipped immediately. Effort: 1.5h.
2. **Keyword filter at scrape time (`workday.py:_search_tenant_keyword`):** apply `_kw_match.matches_any_keyword` BEFORE adding to roles list. Drops ~80% of returned items pre-network-return = saves memory + Stage-1 token cost. Effort: 0.5h.
3. **Add Phase 1.5 Playwright auth-token harvest for failing tenants (deferred):** would unlock 30+ huge employers. Massive impact (estimated +50-100 raw per tenant × 30 tenants = +1,500-3,000 raw, +20-40 qualifying). Effort: 30-50h sustained engineering. v0.4+.

**Recommended priority:** v0.3 P1 (mostly time-saving + cleanliness; the big unlock is Phase 1.5).

---

### Ashby — `backend/scraper/ashby.py` (257 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 486 | 39 | 8.0% | 28s |
| 2 | 512 | 41 | 8.0% | 28s |
| 3 | 515 | 5 | 1.0% | 29s |
| 4 | 537 | 3 | 0.6% | 29s |
| 5 | 437 | 4 | 0.9% | 28s |
| **Avg** | **497** | **18.4** | **3.7%** | **28.5s** |

**Note:** the 39/41 → 5/3/4 collapse between runs 2 and 3 deserves investigation — keyword effectiveness in run 1/2 was different. Possibly:
- The keyword list shifted between runs.
- An Ashby tenant suddenly closed many roles.
- The token-overlap matcher's randomness near threshold caused flicker.

**Code-level findings:**
- `ashby.py:22-96` — **20 duplicate slugs.** Linear 4×, Notion 3×, Pinecone 3×, Ramp/Mercury/Perplexity/Modal/Sierra/Decagon/Vanta/Drata/Modern Treasury/Plaid/Character/Mistral/Cohere all 2×. 64 entries → 44 unique. **20 wasted HTTP calls per run (~3s).**
- `ashby.py:158-171` — `_fetch_company_jobs` calls `client.get_json` once per slug. No pagination — Ashby doesn't paginate (returns full job list).
- `ashby.py:120` — `await asyncio.gather(*tasks, return_exceptions=True)` with no Semaphore cap. With 64 tenants, all 64 hit Ashby's API simultaneously. ScraperClient's per-domain Semaphore(2) bottlenecks but adds latency.
- `ashby.py:138-141` — date filter uses `_parse_iso_date`. Ashby's `publishedAt` field is ISO. Sometimes returns `updatedAt` only. Falls back gracefully.
- `ashby.py:142-149` — token-overlap match. Same matcher as elsewhere.
- `ashby.py:177` — `location_type_raw = (job.get("workplaceType") or job.get("employmentType") or "").lower()` — falls back to `employmentType` ("FullTime") which would never match "remote"/"hybrid". Bug-ish: `employmentType` should never be the source of truth for workplace type. Fortunately the fallback at line 184 handles it correctly.

**Live probes (this audit):**
- `glean` (org slug): **404** (not Ashby tenant or wrong slug).
- `jasper`: **404**.
- `dataiku`: **404**.
- `figma`: **404** (uses Greenhouse already, confirmed).
- `runwayml`: **404**.
- `airbyte`: **8 jobs** (live, viable).
- `openai`: 200 — large response (10MB+, many jobs — confirmed migration).

**Unrealized capabilities:**
- Ashby Public Job Board API supports `?includeCompensation=true` (we use this). Also supports `?employmentType=FullTime` or similar — could pre-filter. Less impact than expected since tenants are small.

**Persona impact:**
- A: Low (digital health: Notion is HIPAA-compliant adjacent but not direct healthcare).
- B: Med (Plaid, Ramp, Mercury — fintech).
- C: Med-High (Harvey 261 jobs — AI for law).
- D: Low.
- E: Low (Atomic Industries 10 jobs).
- F: Med-High (Linear, Vercel — designer-strong tools).
- G: Low.
- H: High (OpenAI, Cohere, Perplexity, Mistral, Anyscale, Modal — AI labs dense).

**Specific fixes with expected impact:**
1. **Dedupe tenant list (`ashby.py:22-96`):** 64 → 44. -3s/run. Effort: 0.1h.
2. **Add `airbyte` (verified live, 8 jobs):** Effort: trivial.
3. **Probe and add 12-15 strategy/PM tenants** (per prior audit, with verification this time): `humaneintelligence` (AI red-teaming), `crusoeenergy`, `hightouch` (already), `glean` (verified 404, NOT Ashby), `jasper` (404), `descript` (probe), `paddle` (probe), `dataiku` (404). The prior list was speculation; only `airbyte` confirmed live. Effort: 3h to probe + add ~10.
4. **Add Semaphore(20) cap** for parallel fetches. Effort: 0.1h.

**Recommended priority:** v0.3 P1 (#1 trivially cheap; #3 needs careful probing).

---

### Adzuna — `backend/scraper/adzuna.py` (246 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed | Quota |
|---|---|---|---|---|---|
| 1 | 484 | 2 | 0.4% | 6s | OK |
| 2 | 829 | 8 | 1.0% | 38s | EXHAUSTED |
| 3 | 935 | 6 | 0.6% | 24s | OK |
| 4 | 663 | 5 | 0.8% | 7s | EXHAUSTED |
| 5 | 989 | 5 | 0.5% | 13s | EXHAUSTED |
| **Avg** | **780** | **5.2** | **0.7%** | **17.5s** | 3/5 |

**Code-level findings:**
- `adzuna.py:120` — pagination capped at 8 pages × 50 results = 400/keyword. With 11 keywords = 88 calls. Free tier: 1,000/month. So **after ~11 runs/month, quota is exhausted.** Aligns with observed 3/5 quota_exhausted.
- `adzuna.py:146-154` — 429 sets `quota_exhausted=True` and breaks page loop, but **other keywords still attempt their first call** (each gets 429, breaks again). Wastes ~10-15 calls of the budget that's already exhausted.
- `adzuna.py:81` — `Semaphore(3)` — fine.
- `adzuna.py:139-145` — outer try/except around `client._client.get`, but the inner code wraps `data.get("results")` access in try/except too. Reasonable defensive coding.

**Unrealized capabilities:**
- Adzuna API supports `category=`, `where=`, `salary_min=`, `salary_max=`, `permanent=1`, `full_time=1`, `contract=1`. We pass none of these. **Adding `salary_min={profile.salary_minimum}` would server-side-filter, saving conversion of low-quality results AND reducing quota burn.** Effort: 0.5h.
- Adzuna also supports `category` filter — same Persona-fitness benefit as TheMuse. e.g., for healthcare profiles, `category="healthcare-nursing-jobs"`.

**Persona impact:**
- A: Med (broad aggregator)
- B: Low-Med
- C: Low
- D: Low
- E: Med
- F: Low
- G: Low
- H: Low (already covered better by JSearch)

**Specific fixes with expected impact:**
1. **Short-circuit on first 429 (`adzuna.py:83-93`):** Set self.quota_exhausted in `bounded()` before calling `_search_keyword`; saves 10-15 dud calls. Effort: 0.3h.
2. **Reduce per-keyword pages 8 → 3** (line 120). Cuts 88 → 33 calls/run. Effort: 0.1h. Impact: minimal qualifying loss because Adzuna's relevance ranks favorites first.
3. **Add `salary_min=` filter** at API. Effort: 0.5h.

**Recommended priority:** v0.3 P2 (low qualifying yield; mostly quota housekeeping).

---

### USAJOBS — `backend/scraper/usajobs.py` (223 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 139 | 14 | 10.0% | 5s |
| 2 | 53 | 9 | 17.0% | 2s |
| 3 | 53 | 9 | 17.0% | 2s |
| 4 | 231 | 13 | 5.6% | 2s |
| 5 | 43 | 5 | 11.6% | 1.5s |
| **Avg** | **104** | **10.0** | **9.63%** | **2.5s** |

**Code-level findings:**
- `usajobs.py:104` — `ResultsPerPage=min(500, limit*5)`. At default `limit=50`, that's 250/keyword. **No pagination.** USAJOBS supports `Page=N` parameter. For high-yield keywords, this caps coverage.
- `usajobs.py:121-125` — 403/429 sets `quota_exhausted=True`. **Cosmetic** — USAJOBS doesn't really have quota; 403 is auth misconfig.
- `usajobs.py:172` — location_type heuristic: `"Remote"` if "anywhere" in loc OR "telework". Misses many "remote-eligible" federal roles where the location is just a city. Fine — Stage 1 LLM check fixes this.
- `usajobs.py:67` — `Semaphore(6)` — generous; USAJOBS is fast and tolerant.
- **No keyword expansion** for federal-AI-specific titles — federal job titles have specific prefixes (e.g., "Information Technology Specialist (Artificial Intelligence)" — this passes the token-overlap match for "AI" but only if the title is searched as a federal-specific keyword).

**Unrealized capabilities:**
- USAJOBS API supports many filters: `JobCategoryCode`, `LocationName`, `Organization`, `RemunerationMinimumAmount`, `PayGradeHigh`, `PositionScheduleTypeCode` (FT/PT). Currently we send only `Keyword`. **Adding `RemunerationMinimumAmount={profile.salary_minimum}` would server-side-filter** — same as Adzuna fix. Effort: 0.5h.
- USAJOBS exposes job code lists (`/api/codelist/agencysubelements`, `/codelist/series`) — programmatic filtering by series (e.g., 2210 = IT Specialist). Could add for Persona G/H who target specific series.

**Persona impact:**
- A: Med (VA, NIH, HHS — federal healthcare admin)
- B: Med (Treasury, SEC, FDIC)
- C: High (DOJ, OMB, OGC)
- D: Med (DOE, ED, USDA)
- E: Med (DoD, GSA contracting)
- F: Low
- G: **Highest** — this is THE source for federal civilians.
- H: High (federal AI hiring booming; Run 5 had top role at IT Specialist (AI) at $169-197K).

**Specific fixes:**
1. **Add pagination (Page=1,2,3 for keywords with > 250 results):** Effort: 1h. Expected: +30-50 raw, +5-8 qualifying for high-volume keywords.
2. **Add federal-AI-specific keyword expansion:** `extra_usajobs_keywords` mirror of `extra_jsearch_keywords`. Effort: 1h. Expected: +5-10 qualifying for Persona G/H.
3. **Add `JobCategoryCode=2210` (or Persona-driven series mapping)** as `appliedFacets`-equivalent filter. Effort: 1.5h.

**Recommended priority:** v0.3 P0 for federal-leaning users (high conv, easy wins).

---

### BuiltIn — `backend/scraper/builtin.py` (298 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 246 | 12 | 4.9% | 25s |
| 2 | 240 | 14 | 5.8% | 10s |
| 3 | 240 | 16 | 6.7% | 7s |
| 4 | 466 | 16 | 3.4% | 7s |
| 5 | 253 | 13 | 5.1% | 28s |
| **Avg** | **289** | **14.2** | **4.91%** | **15.3s** |

**Code-level findings:**
- `builtin.py:33-37` — regex anchor parser. Brittle if BuiltIn changes template. Last verified 2026-05-04.
- `builtin.py:50` — `Semaphore(3)`. Fine for HTML scraper with 403 anti-bot.
- `builtin.py:159-186` — `fetch_jd` per-role lazy. ~14 round-trips for 14 qualifying roles per run.
- `builtin.py:159-186` — JD parsing has 3 selector fallbacks. Last fallback is full-body strip (line 185). Likely too noisy if first 2 fail.
- `builtin.py:106-108` — 403 print but no quota_exhausted flag. Should set `self.quota_exhausted = True` so `printed` and audit JSON both reflect.
- `builtin.py:34-37` — title regex `<a[^>]+href="(/job/[a-z0-9\-]+/(\d+))"...>` is case-INSENSITIVE but doesn't accommodate uppercase letters in slug (uses `[a-z0-9\-]+`). Audit run 4's higher count (466 raw vs 240) suggests BuiltIn adjusted templates and the regex caught more — also brittle.

**Unrealized capabilities:**
- BuiltIn search supports `?country=us` filter (US default). For non-US users, broken.
- BuiltIn supports `?date_posted=24_hours|3_days|week|month|year`. Currently absent — relies on Stage 1 to filter posted_date. Would reduce raw fetch volume.

**Persona impact:**
- A: Med (digital health, Komodo Health-like).
- B: Med (Brex, Mercury, Stripe).
- C: Low.
- D: Med (Coursera, MasterClass).
- E: Low.
- F: High (creative tech).
- G: Low.
- H: Med-High.

**Specific fixes:**
1. **Add `quota_exhausted` flag on 403 (`builtin.py:106-108`):** Effort: 0.1h.
2. **Add `date_posted=` filter:** Effort: 0.3h. Expected: 30% raw reduction with no qualifying loss.
3. **Use BeautifulSoup for parsing** (vs regex): more robust to template changes. Effort: 2h. Risk: small.

**Recommended priority:** v0.3 P2 (working, cheap, decent yield; non-urgent).

---

### Findwork — `backend/scraper/findwork.py` (159 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed | Quota |
|---|---|---|---|---|---|
| 1 | 243 | 7 | 2.9% | 6s | OK |
| 2 | 204 | 10 | 4.9% | 7s | EXHAUSTED |
| 3 | 204 | 11 | 5.4% | 7s | EXHAUSTED |
| 4 | 205 | 3 | 1.5% | 5s | EXHAUSTED |
| 5 | 219 | 8 | 3.7% | 6s | EXHAUSTED |
| **Avg** | **215** | **7.8** | **3.63%** | **6.1s** | 4/5 |

**Code-level findings:**
- `findwork.py:101-105` — 429 detection works.
- `findwork.py:113` — `items[:limit]` truncation. Findwork's API doesn't paginate well; this is fine.
- `findwork.py:88` — comment says `sort_by` is supported but not passed. Default is newest-first.
- **Quota exhaustion is the real constraint** — 4/5 runs hit 429. Findwork's free tier seems to be ~50-100 calls per IP per day (undocumented).

**Unrealized capabilities:**
- Findwork API supports `?location=` and `?remote=true`. Currently unused.

**Persona impact:**
- A: Low.
- B: Low.
- C: Low.
- D: Low.
- E: Low.
- F: Low.
- G: Low.
- H: Med (technical roles).

**Specific fixes:**
1. **Add `?remote=true` filter** when profile has remote preference. Effort: 0.3h.
2. **Cache results per (keyword, day):** would dramatically extend free-tier lifespan for repeat searches. Effort: 1.5h. Cross-cutting infra.

**Recommended priority:** v0.3 P3 (low impact, low effort; quota constraints make it a poor investment).

---

### Lever — `backend/scraper/lever.py` (209 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 58 | 0 | 0% | 17s |
| 2 | 83 | 0 | 0% | 19s |
| 3 | 83 | 0 | 0% | 19s |
| 4 | 72 | 0 | 0% | 18s |
| 5 | 68 | 0 | 0% | 19s |
| **Avg** | **73** | **0** | **0%** | **18.5s** |

**0% qualifying across all 5 runs.** This is suspicious enough to warrant investigation.

**Code-level findings:**
- `lever.py:26-48` — 14 tenants. Top yielders (per comments): Binance 383, Palantir 235, Spotify 196, Whoop 169, Mistral 161. These are genuinely engineering-heavy / crypto / streaming-music roles. **For AI strategy persona, these aren't fits.**
- `lever.py:99-108` — token-overlap filter same as elsewhere. Why 0 qualifying? Possible reasons:
  1. The 73 raw roles ARE technical (Senior Trading Analyst at Binance, Backend Engineer at Spotify) — not AI strategy.
  2. The token-overlap matcher is dropping them post-search.
  3. Cross-board dedup removes them (same role on Lever + Greenhouse).
- `lever.py:74-114` — local + cross-board dedup runs.
- **No probe data** on what those 73 are. Without log of titles, can't distinguish #1 vs #2.

**Live probe results:**
- `redhat`: 404 (not Lever-hosted)
- `gitlab`: 404
- `spotify`: 9 jobs (live, but tiny — comment says 196, maybe stale)
- `perplexity`: 404 (migrated to Ashby — confirmed)
- `anthropic`: 404 (uses Greenhouse)

**Unrealized capabilities:**
- Lever supports `?location=` and `?team=` and `?commitment=` filters. Currently we don't use them.

**Persona impact:**
- A: Very Low.
- B: Med (Binance/Palantir for crypto/data).
- C: None.
- D: None.
- E: None.
- F: Low.
- G: Med (Palantir government work).
- H: Low (most Lever AI is engineer-tier).

**Specific fixes:**
1. **Debug print** what 73 raw roles are being filtered out. Effort: 0.2h.
2. **Prune low-fit tenants** (Binance, Whoop, Spotify-music) — they post 0 AI strategy roles. Lift Lever from 14 → ~7 high-fit tenants. Effort: 1h.
3. **Add 5-10 alternative tenants:** Most named candidates (RedHat, GitLab) verified to NOT be Lever. Need actual probe of `scripts/discover_any_ats.py` against fresh list.
4. **Lever's tenant pool is small + engineering-skewed.** Honest assessment: this is a **persona-mismatched scraper** for the current cohort. It DOES produce good results for tester pools that are tech-engineering-heavy (Persona F creative-eng, Persona H engineering-leaning).

**Recommended priority:** v0.3 P3 (low ROI; investigate-then-prune).

---

### TheMuse — `backend/scraper/themuse.py` (237 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 50 | 0 | 0% | 26s |
| 2 | 100 | 0 | 0% | 33s |
| 3 | 100 | 0 | 0% | 30s |
| 4 | 50 | 0 | 0% | 3s |
| 5 | 100 | 0 | 0% | 31s |
| **Avg** | **80** | **0** | **0%** | **24.6s** |

**Code-level findings:**
- `themuse.py:78-129` — TheMuse API doesn't accept arbitrary keyword search. The wrapper uses `_keyword_to_category` to map keyword → category for coarse filter.
- `themuse.py:185-225` — `_MUSE_CATEGORIES` mapping. **Critical bug:** for an AI strategy persona, no keyword token maps to a useful category. The ranker walks tokens "ai", "strategy", "enablement" — none in dict. Falls through to None (broad fetch), then orchestrator filter drops everything.
- `themuse.py:50` — `Semaphore(4)` × 8 pages × 11-39 keywords could blow rate limit (25/hr unkeyed); with API key it's fine.
- `themuse.py:101-129` — pagination up to 8 pages × 20 jobs = 160 jobs/keyword. Reasonable.

**Live probe (this audit):**
- `?category=Healthcare` → **20 jobs** (Optum, Geisinger, Christus Health, etc.). **Confirmed working — high persona-A fit.**
- `?category=Government` → 0 jobs (empty currently).
- `?category=Education` → 0 jobs (empty currently).
- `?category=Legal` → 0 jobs.
- `?level=Senior+Level` → 20 jobs (broad mix).

**Unrealized capabilities:**
- `?category=` works robustly. Discovery shows "Healthcare", "Government", "Education", "Legal" all exist.
- `?level=Senior+Level` filter useful for senior personas.
- `?location=` supported.
- `?company=` supported (specific company search).
- **The keyword-to-category mapper should be inverted: map profile_tags (richer) to categories.** Today it goes `keyword.lower() → category`; should go `profile_tag → category`. For Ziad's tags `[ai, ai_strategy, ai_enablement, consulting, federal]`, mapping to ["Consulting", "Government"] would yield categories that DO contain federal/consulting roles.

**Persona impact:**
- A: **Should be high if we route Healthcare category** (live probe: 20 roles). Currently zero because keyword mapper.
- B: Med (Accounting and Finance category).
- C: Low (Legal category empty).
- D: Low (Education category empty).
- E: Low.
- F: Med (Marketing, Design and UX).
- G: Med (Government category empty currently but seasonal).
- H: Low (Consulting category is sparse).

**Specific fixes:**
1. **Profile-tag → category routing (`themuse.py:185-237`):** Big rewrite — refactor _keyword_to_category to also accept profile_tags. Effort: 2h. Expected: +5-15 qualifying for Persona A/D/G testers.
2. **Cache by category:** today same category gets re-fetched per keyword token. Memoize. Effort: 0.5h. Saves ~50% of TheMuse calls.
3. **Add `?level=Senior+Level` filter** when profile.seniority is mid+. Effort: 0.3h.

**Recommended priority:** v0.3 P1 for Persona A/D/G (currently broken for them; trivial fix unlocks).

---

### iCIMS Playwright — `backend/scraper/icims_playwright.py` (356 LOC)

**5-run trend:** **0 raw, 0 elapsed, every run.**

**Critical finding:**
- `icims_playwright.py:64-67` — silent ImportError swallow:
  ```python
  try:
      from playwright.async_api import async_playwright
  except ImportError:
      return []
  ```
- This explains the consistent 0/0/0 results: **Playwright is not installed in the production environment.** The audit JSON shows `roles: 0, elapsed_s: 0.0` — exit before `async_playwright` import.

**Code-level findings:**
- `icims_playwright.py:51-356` — the rest of the implementation is solid. Browser pool reuse, fallback iframe extraction, JD-by-detail-page lazy-fetch.
- `icims_playwright.py:69` — `Semaphore(4)` for browser context cap. Reasonable.
- `icims_playwright.py:73-94` — opens Chromium ONCE per `search()` call (correct). Closes on completion.
- `icims_playwright.py:138-142` — `wait_for_selector(timeout=10000)` + `wait_for_timeout(3500)` = 13.5s minimum per page. With 5 tenants × 11 keywords = 55 pages × 13.5s = ~12 minutes worst case (sem=4 caps concurrent so ~3 minutes wall-clock).

**Persona impact:**
- A: Med (could add Kaiser Permanente if Playwright works).
- B: Low (Liberty Mutual current).
- C: Low.
- D: Low.
- E: Med-High (Snap-on industrial sales; could add Caterpillar, Bayer).
- F: Low.
- G: Low.
- H: Low.

**Specific fixes:**
1. **Add explicit logging when Playwright import fails:**
   ```python
   except ImportError as e:
       print(f"[icims] Playwright not installed: {e}; skipping iCIMS scrape")
       return []
   ```
   Effort: 0.1h. **Critical for diagnostic.**
2. **Add to BaseScraper.search a `disabled_reason` attribute** that all scrapers can set, and orchestrator surfaces in audit. Effort: 1h cross-cutting.
3. **Bundle Playwright + Chromium in Tauri build** — significant lift (Chromium binary ~150MB). Effort: 6-8h. Or run as sidecar process.
4. **Add 4-5 healthcare iCIMS tenants** (Kaiser Permanente, Mass General, Cleveland Clinic) once Playwright is operational. Effort: 2h.

**Recommended priority:** v0.3 P1 — investigate why Playwright isn't installed (single-line logging fix #1). The bundling issue is the real blocker.

---

### SmartRecruiters — `backend/scraper/smartrecruiters.py` (158 LOC)

**5-run trend:**
| Run | Raw | Qualifying | Conv % | Elapsed |
|---|---|---|---|---|
| 1 | 0 | 0 | 0% | 0.6s |
| 2 | 9 | 0 | 0% | 1s |
| 3 | 9 | 0 | 0% | 0.6s |
| 4 | 0 | 0 | 0% | 0.5s |
| 5 | 9 | 0 | 0% | 0.5s |
| **Avg** | **5** | **0** | **0%** | **0.6s** |

**Code-level findings:**
- `smartrecruiters.py:29-33` — 3 tenants: Visa (43), ASOS (42), LVMH (4). All retail/luxury/fintech.
- `smartrecruiters.py:84-85` — `params={"limit": 100}`. Visa = 43 jobs (fits). LVMH = 4. ASOS = 42. No pagination needed.
- 0 qualifying across runs is fully expected — these tenants don't post AI strategy roles.

**Live probes (this audit):**
- `Visa` → **42 jobs** (live).
- `RTX` → 0 (empty).
- `Bosch` → 0.
- `Siemens` → 0.
- `IKEA` → 0.
- `Allianz` → 0.
- `Mars` → 0.
- `Pernod-Ricard` → 0.
- `Nokia` → 0.
- `uber` → **1 job**.
- `booking` → 0.
- `Publicis` → 0.

**Insight: most candidate SmartRecruiters tenants return 0 jobs.** The format is **case-sensitive** (lowercase often returns 0 even when the tenant has roles). This is harder than expected to expand.

**Unrealized capabilities:**
- SmartRecruiters API supports `q={keyword}&offset=&limit=`. We don't pass `q=` — fetch all and locally filter. For tenants with 100+ jobs, this saves bandwidth.

**Persona impact:**
- All personas: minimal under current tenant pool. Visa is finance-adjacent; ASOS is retail; LVMH is luxury.

**Specific fixes:**
1. **Pass `q={keyword}` to API (`smartrecruiters.py:84-85`):** server-side filter. Effort: 0.5h.
2. **Add 5-10 tenants AFTER live probing** (most candidates I tried returned 0). Effort: 3h to probe well.

**Recommended priority:** v0.3 P3 (low yield; not worth the maintenance burden vs. expected gain).

---

### Climatebase — `backend/scraper/climatebase.py` (211 LOC)

**5-run trend:**
| Run | Raw | Qualifying |
|---|---|---|
| All runs | 50 | 0 |

**Persona impact:**
- A: None.
- B: None.
- C: None.
- D: None.
- E: Low (sustainability adjacent).
- F: None.
- G: Low.
- H: None.

**For climate-aligned testers** (sustainability-focused, ESG, clean-energy candidates), this is the ONLY board. Keep enabled, but route conditionally.

**Recommended priority:** v0.3 — Persona-aware routing fix (don't run for non-climate profiles).

---

### Remotive — `backend/scraper/remotive.py` (141 LOC)

**5-run trend:** Avg 22 raw, 0 qualifying, 4.8s.

**Critically: every run returns the SAME 22 raw roles.** Indicates the keyword filter at the API level isn't varying with our keyword list — either we're sending the same keyword set every run, OR Remotive's `?search=` doesn't materially narrow results.

**Persona impact:** Low across all (engineering-tech-heavy).

**Recommended priority:** v0.3 P3 (no change).

---

### Arbeitnow — `backend/scraper/arbeitnow.py` (131 LOC)

**5-run trend:** Avg 7 raw, 0 qualifying.

**Code-level findings:**
- `arbeitnow.py:38-84` — fetches the SAME 100 jobs every call (Arbeitnow API returns latest 100 globally). Locally filters keyword.
- Heavy EU bias (German/Dutch employer skew).

**Recommended priority:** v0.3 P3 — short-circuit if profile has US-only locations. Effort: 0.5h.

---

### HN-WhoIsHiring — `backend/scraper/hn_hiring.py` (199 LOC)

**5-run trend:** Avg 6 raw, 0.6 qualifying. Highly variable (2-11 raw across runs).

**Code-level findings:**
- `hn_hiring.py:77-106` — fetches latest "Who is hiring?" thread by user "whoishiring". Algolia API.
- `hn_hiring.py:126-196` — comment parser. HN posts are unstructured.
- **Run-to-run variance reflects:**
  1. Thread age (early in month = more posts = more raw).
  2. Comment parser misses many sub-comments and reply chains.

**Unrealized capabilities:**
- The thread API also returns sub-comments. Currently only top-level. Some posters post BIG hiring posts in replies.

**Persona impact:**
- A: Very low.
- B: Low.
- C: None.
- D: None.
- E: None.
- F: Low.
- G: None.
- H: Med (YC-portfolio AI startups often post here).

**Recommended priority:** v0.3 P3 (low yield, expected).

---

### Wellfound, Indeed (deferred)

- Both correctly deferred. Anti-bot 403 blocks production use. Don't enable in v0.3.

---

## Net-new scraper proposals (the most important section)

Format: name, source URL, what it covers, API/HTML, free-tier?, effort, persona impact (LMH), recommended priority.

### Tier 1 — Recommended for v0.3 (Low effort, High impact)

#### 1. **Workable** — `https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs`
- **What it covers:** ~10K+ companies. Y Combinator startups (esp. those that outgrew workatastartup), mid-market SaaS, healthcare startups, EU mid-mkt, design/creative. Workable is the **5th largest ATS by US market share** (Greenhouse > Lever > iCIMS > Workday > Workable).
- **Public API:** YES. Live probe confirmed `https://apply.workable.com/api/v1/widget/accounts/yougov/jobs` returns valid JSON. Slugs matter (lowercase typically).
- **Free-tier:** Fully free (no auth required for the widget endpoint).
- **Implementation difficulty:** Med. Pattern matches Greenhouse/Lever (per-tenant slug). Need ~50 verified tenants to seed. Workable's API is similar in shape to Greenhouse.
- **Expected impact per persona:**
  - A: Med-High (many digital health on Workable)
  - B: Med (some fintech)
  - C: Low-Med (legal-tech)
  - D: Med (EdTech startups)
  - E: Low (industrial generally on Workday/iCIMS)
  - F: High (creative agencies, DTC brands)
  - G: Low
  - H: High (AI startups beyond YC)
- **Recommended priority:** **v0.3 P0**.
- **Estimated yield:** +200-500 raw, +15-25 qualifying per run.
- **Effort:** 6h (template + 50 tenants probed).

#### 2. **HigherEdJobs RSS** — `https://higheredjobs.com/rss/articleFeed.cfm?CatID={N}`
- **What it covers:** All higher-ed faculty, admin, and research roles. ~10K active.
- **Public API:** RSS feed, multiple categories. Live probe: feed valid, 150+ items.
- **Free-tier:** Fully free.
- **Implementation difficulty:** Low. RSS XML parser; same pattern as HN.
- **Expected impact:**
  - A: Low (clinical research scientist roles)
  - B: Low
  - C: Low
  - D: **High** (THE source for higher-ed jobs)
  - E: None
  - F: Low
  - G: Low
  - H: Low
- **Recommended priority:** **v0.3 P0** for Persona D users.
- **Estimated yield:** +50-150 raw, +5-15 qualifying for education-aligned testers.
- **Effort:** 2h.

#### 3. **WeWorkRemotely RSS** — `https://weworkremotely.com/categories/{cat}.rss`
- **What it covers:** ~5K active fully-remote jobs across categories. Live probe (programming category): 16 items, well-formed RSS.
- **Public API:** RSS feed per category (programming, marketing, design, etc.).
- **Free-tier:** Free.
- **Implementation difficulty:** Low. RSS parser.
- **Expected impact:**
  - All personas with remote preference: Low-Med.
- **Recommended priority:** **v0.3 P1** for remote-preferring testers.
- **Estimated yield:** +30-80 raw, +2-6 qualifying per run.
- **Effort:** 2h.

#### 4. **NEOGOV State/Local Government** — pattern: `https://www.governmentjobs.com/careers/{org}` (per-district)
- **What it covers:** State and city governments. K-12 districts, transit authorities, county admin. ~50K+ active US.
- **Public API:** Per-district HTML scrape (no API). Live probe: API endpoint returned 404, but HTML pages of public listings are renderable via Playwright.
- **Free-tier:** Free.
- **Implementation difficulty:** **Med-High.** Per-district URL discovery is a maintenance burden; ~20 large districts cover ~70% of population.
- **Expected impact:**
  - D: **High** (THE source for K-12 jobs)
  - G: **High** (state/local gov beyond USAJOBS)
  - A: Med (county hospital systems, public health depts)
  - Others: Low
- **Recommended priority:** **v0.3 P1** for Persona D + state-leaning Persona G.
- **Estimated yield:** +50-200 raw, +5-15 qualifying.
- **Effort:** 6-8h (per-district URL discovery + HTML parser + Playwright fallback).

### Tier 2 — v0.4+ (Higher effort or persona-specific)

#### 5. **AuthenticJobs RSS** — `https://authenticjobs.com/rss/`
- **What:** Designer/creative-leaning. ~500-1K active.
- **API:** RSS.
- **Effort:** Low (2h).
- **Persona impact:** F High; others Low.
- **Priority:** v0.4 P1 for Persona F.

#### 6. **Idealist.org** — nonprofit jobs
- **What:** Nonprofit, social-impact, civic.
- **API:** No public; HTML scrape required.
- **Effort:** Med (4h).
- **Persona impact:** G High (nonprofit/civic Persona G subset); others Low.
- **Priority:** v0.4 P1 for nonprofit-leaning testers.

#### 7. **WorkAtAStartup (Y Combinator)** — `workatastartup.com`
- **What:** All YC-portfolio startup hiring (~2K companies).
- **API:** Public-ish but the `/api/jobs` endpoint returned 404 in probe; main `/jobs` page renders via React. **Playwright-only.**
- **Effort:** Med-High (8-10h with Playwright).
- **Persona impact:** F Med-High, H High.
- **Priority:** v0.4 P2.

#### 8. **Wellfound revival via Playwright** — already deferred, not a net-new
- **Why revisit?** If we already bundle Playwright for iCIMS, marginal cost is low. But anti-bot is aggressive.
- **Effort:** 6-10h.
- **Persona impact:** H Med, F Low-Med.
- **Priority:** v0.4 P2 (only if iCIMS Playwright is unblocked).

#### 9. **Direct iCIMS expansion (Phase 1.5)** — not a new scraper, an expansion of existing
- **Add:** Kaiser Permanente, Mass General Brigham, Cleveland Clinic, HCA Healthcare (Persona A); Bayer, Caterpillar, Hershey (Persona E).
- **Effort:** 2-3h per tenant (Playwright + verification). 12-18h for all 7.
- **Persona impact:** A High (single biggest healthcare unlock); E Med-High.
- **Priority:** v0.3 P1 (after Playwright bundling).

### Tier 3 — Wontfix or Research-Needed

#### 10. **eFinancialCareers** — paid API; would need budget. Skip.
#### 11. **LawCrossing / BCG Attorney Search** — paid. Skip.
#### 12. **Practicelink / MedJobsCafe / HealtheCareers** — RSS feeds geofenced from Claude WebFetch — can't probe but real users could. Defer.
#### 13. **ChronicleVitae** — paid API. Skip.
#### 14. **Dribbble Jobs / Behance Jobs** — paid tier required for API. Skip.
#### 15. **ClearanceJobs** — closed to scrapers. Skip.
#### 16. **SAM.gov** — not job postings (federal contracts). Skip.
#### 17. **TripleByte / Hired** — both private placement; no public API.
#### 18. **Pangian / Working Nomads / Remote.co** — all RSS-feed-based; verify viability if needed for remote Persona F/H.
#### 19. **K12JobSpot / Frontline** — paid recruiter access. NEOGOV is the public-facing analog.
#### 20. **Hcareers / hospitality boards** — paid. Skip.

---

## Cross-cutting infrastructure

### 1. Tenant health monitoring
- Currently: `scripts/probe_all_companies_health.py` exists but isn't scheduled.
- Recommend: weekly cron that probes all `*_COMPANIES` lists (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, iCIMS), emits a "tenant decay" report.
- **Auto-disable for tenants returning 0 across N consecutive runs** — currently no scraper does this. State persistence via `~/Documents/JobSearchApp/scraper_state/{source}_health.json` would unblock.

### 2. Per-source quality ranking
Based on 5-run audit data:

| Tier | Source | Avg conv | Notes |
|---|---|---|---|
| 1 | JSearch | 10.2% | Highest leverage |
| 1 | USAJOBS | 9.6% | Federal-focused |
| 1 | HN-WhoIsHiring | 9.4% | Tiny but relevant |
| 2 | BuiltIn | 4.9% | Moderate |
| 2 | Ashby | 3.7% | Engineering-heavy |
| 2 | Findwork | 3.6% | Tech-leaning |
| 3 | Greenhouse | 1.2% | Volume → some misses |
| 3 | Adzuna | 0.7% | Broad |
| 3 | Workday | 0.4% | Volume → many misses |
| 4 | Lever, TheMuse, Climatebase, Remotive, SmartRecruiters, Arbeitnow, iCIMS, Findwork | 0% (4-tier) | Persona-mismatched OR broken |

### 3. Smart parallelism
- Workday, JSearch, Greenhouse run with very different time profiles (84s, 99s, 71s respectively). All in same tier of orchestrator's `asyncio.gather`.
- For long-running scrapers, consider a separate slow-track concurrency budget so they don't block fast scrapers.

### 4. Caching layer
- Currently: no caching of scraper results.
- Cost: every run re-fetches every Greenhouse tenant. With 138 unique tenants × 11 keywords × 5 runs/week, that's 7,590 API calls/week against a free public API. Polite, but cache-able.
- **Recommend:** SQLite cache by `(source, tenant, keyword)` with 12h TTL. Effort: 4h. Saves ~70% of HTTP traffic.

### 5. Quota state persistence
- All four quota-aware scrapers (JSearch, Adzuna, USAJOBS, Findwork) reset state every run. If the quota was exhausted in run N, run N+1 still tries.
- **Recommend:** persist `quota_exhausted` across runs with reset-time. Effort: 2h.

### 6. Profile-tag-based source routing
- Currently every scraper runs for every profile. Wastes time on Climatebase for AI-strategy users, on Ashby for healthcare users.
- **Recommend:** add `eligible_profile_tags` attribute to BaseScraper. Orchestrator skips scraper if profile.tags don't intersect.
- Example assignments:
  - Climatebase: tags must include `climate`, `sustainability`, `esg`, `clean_energy`
  - Lever: tags must include `engineering` or `crypto` or `data`
  - SmartRecruiters: any
  - All others: any
- Effort: 2h. Expected: 30% wall-clock reduction for non-AI personas.

---

## Live probe results

| URL | Status | Finding |
|---|---|---|
| `api.ashbyhq.com/.../glean` | 404 | NOT Ashby tenant |
| `api.ashbyhq.com/.../jasper` | 404 | NOT Ashby |
| `api.ashbyhq.com/.../dataiku` | 404 | NOT Ashby |
| `api.ashbyhq.com/.../figma` | 404 | Already Greenhouse |
| `api.ashbyhq.com/.../runwayml` | 404 | Not Ashby |
| `api.ashbyhq.com/.../airbyte` | **8 jobs** | **Add to Ashby** |
| `api.ashbyhq.com/.../openai` | 200 (10MB+) | Already in Ashby |
| `api.ashbyhq.com/.../instacart` | 404 | Already Greenhouse |
| `api.smartrecruiters.com/.../Visa` | 42 jobs | Active (already in list) |
| `api.smartrecruiters.com/.../RTX,Bosch,Siemens,IKEA,Mars,Pernod-Ricard,Allianz,Nokia,Publicis,booking` | 0 each | Inactive — DO NOT add |
| `api.smartrecruiters.com/.../uber` | 1 job | Tiny |
| `apply.workable.com/api/v1/widget/accounts/yougov/jobs` | **JSON, valid** | **Workable scraper viable** |
| `apply.workable.com/api/v1/widget/accounts/cohere/jobs` | 404 | Cohere not on Workable |
| `themuse.com/api/public/jobs?category=Healthcare` | **20 jobs** | **TheMuse Healthcare works!** |
| `themuse.com/api/public/jobs?category=Government,Education,Legal` | 0 each | Currently empty (seasonal?) |
| `themuse.com/api/public/jobs?level=Senior+Level` | 20 jobs | Useful filter |
| `higheredjobs.com/rss/articleFeed.cfm?CatID=11` | 150+ items | **HigherEdJobs RSS viable** |
| `weworkremotely.com/categories/remote-programming-jobs.rss` | 16 items | **WeWorkRemotely RSS viable** |
| `lever.co/v0/postings/anthropic,redhat,gitlab,perplexity` | 404 each | NOT Lever (Anthropic=Greenhouse, Perplexity=Ashby) |
| `lever.co/v0/postings/spotify` | 9 jobs | Active (much smaller than 196 in comments — stale) |
| `idealist.org/api/v2/...` | 404 | No public API |
| `governmentjobs.com/careers/...` | 404/403 | No clean public API; would need HTML scrape |

**Probe count: 25 distinct endpoints. 5 useful candidates confirmed.**

---

## v0.3 recommended implementation order

Sequential plan, prioritized by ROI:

### Phase 1 (4-6 hours, reaches "polished") — ship-blocking quality bar
1. **JSearch `num_pages 3 → 10`** + `Semaphore 3 → 5` (`jsearch.py:221, 167`). 0.5h. **+50-90 qualifying/run.**
2. **Dedupe Greenhouse + Ashby tenant lists.** 0.3h. **-50 wasted HTTP calls/run.**
3. **iCIMS Playwright explicit logging on import fail** (`icims_playwright.py:64-67`). 0.1h. **Reveals install status.**
4. **Adzuna short-circuit on 429** (`adzuna.py:83-93`). 0.3h. **Saves ~10-15 dud calls/run.**
5. **JSearch error logging** (`jsearch.py:177`). 0.1h. **Diagnostic.**
6. **Add `airbyte` to Ashby** (verified live). 0.1h.
7. **Greenhouse per-tenant yield logging** (sidecar JSON for tenant-decay tracking). 0.5h.
8. **Dedupe TheMuse fetches by category** (memoize within `search()`). 0.5h.
9. **TheMuse profile-tag → category routing fix** (`themuse.py:185-237`). 2h. **+3-8 qualifying/run for non-H personas.**
10. **USAJOBS pagination** (page=1,2,3). 1h. **+5-10 qualifying for federal-leaning users.**

### Phase 2 (8-12 hours, "broader persona coverage")
11. **Add Workable scraper** (50 verified tenants). 6h. **+15-25 qualifying for Persona A/F/H.**
12. **Add HigherEdJobs RSS scraper.** 2h. **+5-15 qualifying for Persona D.**
13. **Add WeWorkRemotely RSS scraper.** 2h. **+2-6 qualifying for remote profiles.**
14. **Profile-tag-based source routing** (skip Climatebase for AI users, etc.). 2h. **30% wall-clock reduction for non-default personas.**
15. **iCIMS healthcare tenant additions** (Kaiser, Mass General, Cleveland Clinic, HCA). 2-4h once Playwright works. **+5-10 qualifying for Persona A.**

### Phase 3 (15-20 hours, "robust + multi-persona")
16. **NEOGOV state/local gov scraper.** 6-8h. **+5-15 qualifying for Persona D + state Persona G.**
17. **Persistent Workday `tenant_500_count` cooldown.** 1.5h. **-30s/run.**
18. **Quota state persistence across runs** (cross-cutting). 2h.
19. **SQLite cache layer for scraper results.** 4h. **70% HTTP traffic reduction.**
20. **AuthenticJobs RSS / Idealist HTML.** 4-6h.

### Skip from v0.3
- Lever expansion / pruning — low ROI given 0% conversion.
- SmartRecruiters expansion — most candidate tenants returned 0 in probe.
- Remotive / Climatebase / Arbeitnow code changes — all are persona-mismatched (route via #14, don't fix).
- Wellfound, Indeed (deferred) — keep deferred.
- Workday Phase 1.5 (Playwright auth-token harvest) — saves for v0.4.

---

## Specific bug hunts answered

| Bug claim from prior audit | Status |
|---|---|
| iCIMS Playwright silent ImportError | **Confirmed.** All 5 audit runs show 0 raw, 0 elapsed — Playwright not installed. Fix at `icims_playwright.py:64-67`. |
| Adzuna 429 sharing across keywords | **Partially confirmed.** Each keyword's first call hits 429 once. ~10-15 dud calls per quota-exhausted run. Fix at `adzuna.py:83-93`. |
| JSearch `num_pages` Pro tier under-utilization | **Strongly confirmed.** Per JSearch docs, Pro accepts up to 10. We use 3. Single-line fix worth +60-90 qualifying/run. |
| Scrapers not setting `quota_exhausted` flag | **Confirmed for BuiltIn (`builtin.py:106-108`)** — sets nothing on 403. Other scrapers correctly set flag. |
| Race conditions in async fetching | **Not found.** All scrapers use `asyncio.gather(..., return_exceptions=True)`; per-task try/except blocks return `[]` on failure. Errors propagate cleanly to orchestrator's gathered exception list. |

---

## Key file paths

- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\jsearch.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\greenhouse.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\ashby.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\workday.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\adzuna.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\icims_playwright.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\smartrecruiters.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\themuse.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\usajobs.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\lever.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\builtin.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\findwork.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\hn_hiring.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\remotive.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\arbeitnow.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\climatebase.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\orchestrator.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\_keyword_match.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\base.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\client.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\filter\hard_filters.py`

Audit JSON sources:
- `C:\Users\habou\Documents\JobSearchApp\audits\runs\2026-05-04_23-09_a02d8410_audit.json`
- `C:\Users\habou\Documents\JobSearchApp\audits\runs\2026-05-05_00-18_16d89155_audit.json`
- `C:\Users\habou\Documents\JobSearchApp\audits\runs\2026-05-05_01-40_facc3877_audit.json`
- `C:\Users\habou\Documents\JobSearchApp\audits\runs\2026-05-05_05-30_bc27490e_audit.json`
- `C:\Users\habou\Documents\JobSearchApp\audits\runs\2026-05-05_06-05_b6bdc773_audit.json`

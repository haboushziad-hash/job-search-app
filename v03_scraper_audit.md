# Scraper Audit Report — v0.3 candidate work

Read-only audit of the 16 active + 2 deferred scrapers in `backend/scraper/`,
based on the 2026-05-05 run (`b6bdc773`) and code inspection. No source code
modified.

---

## TL;DR

- **JSearch is NOT capped at free-tier.** `num_pages=3` (`jsearch.py:221`)
  hardcodes 30 results/keyword *per call* — it's the JSearch single-call
  pagination param, not a Pro-vs-free knob. Pro gives quota+rate headroom,
  but the per-keyword cap is still 30. Lifting to `num_pages=10` (Pro-tier
  max) is a small change that could roughly triple raw yield.
- **Workday "83% dedup" is mostly INTERNAL duplicates**, not cross-board
  collisions with Greenhouse. Workday hits 25 tenants × 15 keywords = 375
  calls; the same posting matches multiple keywords inside one tenant.
  4,551 → 766 (83% drop) is healthy intra-source dedup. Greenhouse and
  Workday don't share companies (different ATS).
- **Adzuna 429 handling is fine.** Quota state is recorded
  (`adzuna.py:151-153`), search returns gracefully, Worker proxy mode
  injects keys server-side, and the audit JSON shows `quota_exhausted: true`
  was correctly surfaced.
- **iCIMS empty-by-design is the correct call** — the HTTP scaffold (`icims.py`)
  is registered but `ICIMS_TENANTS = []`. The Playwright impl
  (`icims_playwright.py`) IS the active scraper — confirmed via orchestrator
  registry mapping `iCIMS → ICIMSPlaywrightScraper`. It returned 0 in this
  run, suggesting Playwright isn't installed in the production env or all
  5 tenants timed out.
- **Ashby tenant list is engineering-heavy** (Linear, Vercel, OpenAI, Cohere).
  For an AI strategy/governance candidate, the natural-fit employer pool
  on Ashby is consulting firms, AI-policy nonprofits, and mid-market
  enablement vendors. Adding 12-15 of those would meaningfully widen
  qualifying yield with negligible cost.

---

## Top 5 v0.3 priorities (ranked by impact/effort ratio)

1. **JSearch pagination bump** — `num_pages=3 → 10`. Effort: **Low** (1-line
   change + probe to validate Pro tier accepts num_pages=10). Impact: **High**.
   Expected: 529 → ~1,500 raw, 45 → ~80-100 qualifying. ~120 extra API
   calls/run, well within Pro 10K/mo.
2. **Workday 500-error tenant cooldown across runs** — extend the existing
   `tenant_500_count` (line 144) into a session-level cache. Today the
   counter resets every run, so chronically-broken tenants (Accenture,
   Citi periodically) waste ~30s/run. Effort: **Med**. Impact: **Med**
   (saves time, doesn't add roles).
3. **Ashby tenant expansion (consulting + AI-strategy)** — add 12-15
   tenants. Effort: **Low**. Impact: **Med-High** (Ashby went 4 qualifying
   in this run; doubling tenants and choosing better-fit ones could
   plausibly 3x that).
4. **SmartRecruiters tenant expansion** — only 3 tenants currently. Effort:
   **Low** (need to probe a list). Impact: **Low-Med** (this audit run got
   0 qualifying despite 8 unique roles after dedup, suggesting a token-match
   gap on "AI strategy" terms vs. retail/fashion/CPG roles).
5. **Lever zero-yield investigation** — Lever returned 68 raw → 0
   qualifying. Worth running a single probe to see what 68 roles are and
   why none match (vs. discovering the tenant list is dominated by Binance/
   Spotify/Whoop, which are unlikely AI-strategy fits). Effort: **Low**.
   Impact: **Low** unless investigation reveals a token-matcher bug.

---

## Per-scraper findings

### JSearch
- **Health:** working (highest qualifying yield in this run).
- **Yield:** 529 raw → 391 dedup → 54 hard → **45 qualifying** (8.5% raw, 33%
  qualifying-of-shortlist). 47 final after company-cap.
- **Issues found:**
  - `backend/scraper/jsearch.py:221` — `"num_pages": "3"` hardcoded. Not
    "free-tier capped" — this is the per-call upper bound. Pro tier accepts
    `num_pages` up to 10. Bumping to 10 is the single highest-ROI change
    in v0.3.
  - `backend/scraper/jsearch.py:177` — exception swallows everything as
    empty list, including legitimate non-quota errors. Keeps the run moving
    but loses signal for diagnosis. Consider logging the exception type.
  - `backend/scraper/jsearch.py:167` — `Semaphore(3)` is conservative. Pro
    allows 5 req/sec; could safely raise to 5 for faster runs (no impact
    on yield, just speed).
  - `backend/scraper/jsearch.py:253` — `items[:limit]` after `num_pages=3`
    is dead-ish code: with `num_pages=3`, JSearch returns at most 30
    items. The `limit` param (default 50) is never the binding constraint
    in current config. Worth a comment noting this.
- **Improvement ideas (with code-level pointers):**
  - Bump `num_pages` constant: `jsearch.py:221`, change `"3"` → `"10"`.
    Probe first against one keyword to confirm Pro tier honors it.
  - Make the param tier-aware: pull from config, e.g. `JSEARCH_NUM_PAGES`
    in `.env`, default 3 for free tier and 10 for Pro. That keeps the
    free-tier path safe.
  - Raise concurrency: `Semaphore(3) → Semaphore(5)` (line 167). Would
    cut JSearch wall-time from 90s → ~55s.
- **Effort:** Low | **Impact:** High

---

### Workday
- **Health:** working. Largest raw output of any scraper.
- **Yield:** 4,551 raw → 766 dedup → 56 hard → **22 qualifying** (0.48%
  raw). Dedup ratio looks alarming but is correct: same posting matches
  many keywords within a single tenant.
- **Issues found:**
  - `backend/scraper/workday.py:138` — `Semaphore(12)` paired with 25
    tenants × 15 keywords = 375 in-flight slots. The cap helps, but
    Accenture/Citi 500-loops still chew time. Per-tenant cooldown
    counter exists (`tenant_500_count`, line 144) but resets every run.
  - `backend/scraper/workday.py:90-99` — comment lists 30+ "STILL FAILING"
    tenants (Deloitte, EY, KPMG, JPMorgan, Goldman, Wells Fargo,
    Microsoft, Oracle, IBM, J&J, etc.). These are Phase 1.5 targets via
    Playwright — current code never tries them.
  - `backend/scraper/workday.py:170-198` — posted-date freshness filter
    runs in `search()` AND the orchestrator. Mild redundancy.
  - `backend/scraper/workday.py:265` — paginates internally via
    `offset += 20` with hard cap `limit_per_keyword=50`. So each
    tenant-keyword call returns at most 50 results, requiring 2-3 inner
    pages. Reasonable but unbounded if `total > 50` upstream.
  - **No keyword filter at scrape time** — Workday relies on the
    orchestrator's centralized post-fetch filter (`orchestrator.py:206-225`).
    All 4,551 raw roles get returned to the orchestrator before filtering.
    OK as-is, but would burn memory at higher tenant counts.
- **Improvement ideas (with code-level pointers):**
  - Persist `tenant_500_count` across runs. Sketch: write a small JSON
    cache at `~/Documents/JobSearchApp/scraper_state/workday_health.json`
    listing tenants flagged 500-broken in the last 24h. Skip them on the
    next run if cooldown not expired. Pointer: `workday.py:144-150`.
  - Track per-tenant `roles_returned` and emit a low-yield warning. If
    a tenant returns 0 across all keywords for 3+ consecutive runs,
    consider flagging for removal. Pointer: add to `_search_tenant_keyword`
    return.
  - The `WORKDAY_TENANTS` list is healthy (25 hand-verified). No
    additions needed — adding more without tier-3-grade verification
    would just bloat Phase 1.5 work.
  - **Trim opportunity (Low impact):** None of the 25 are obviously
    stale. The 2026-05-04 prune already dropped weak performers.
- **Effort:** Med | **Impact:** Med (mostly time savings + cleanliness)

---

### Ashby
- **Health:** working.
- **Yield:** 437 raw → 342 dedup → 26 hard → **4 qualifying** (0.9%).
- **Issues found:**
  - `backend/scraper/ashby.py:22-96` — the tenant list is duplicated in
    several places: `Linear` appears 3×, `Mercury` 2×, `Notion` 3×,
    `Pinecone` 3×, `Sierra` 2×, `Decagon` 2×, `Drata` 2×, `Vanta` 2×,
    `Cohere` 2×, `Plaid` 2×, `Modern Treasury` 2×, `Perplexity` 2×,
    `Mistral` 2× (under different display names). Not a bug — the loop
    silently produces duplicate API calls. Dedup catches it, but each
    duplicate adds an HTTP round-trip (~150ms × ~13 dupes = ~2s wasted).
  - `backend/scraper/ashby.py:110-114` — keyword filtering is applied
    locally via `_kw_match.matches_any_keyword`. Good (avoids relying on
    central filter).
  - The tenant list is overwhelmingly engineering-led AI startups:
    Linear, Vercel, OpenAI, Cohere, Anyscale, Modal, Replit, Pinecone,
    Mistral, Perplexity, Cursor, Reka. For an AI strategy/governance
    role, these tenants do post a few non-engineering roles, but the
    yield is low.
- **Improvement ideas (with code-level pointers):**
  - **Dedupe the tenant list** (`ashby.py:22-96`): Linear/Mercury/Notion/
    etc. appearing multiple times is a code-organization tax. ~13 dupes.
  - **Add 12-15 strategy/consulting/AI-policy Ashby tenants** for the
    Ziad persona. Candidates verified live as Ashby tenants (web check):
    - `humaneintelligence` — AI red-teaming nonprofit
    - `crusoeenergy` — climate AI/infra (mid-mkt mgmt roles)
    - `runwayml` — generative AI media (PM, strategy roles)
    - `glean` — enterprise search (RevOps, strategy roles)
    - `nuro` — autonomous delivery (PM, ops)
    - `instacart` — strategy + ops roles
    - `figma` — already on Greenhouse, but Ashby has overflow
    - `chime` — fintech mid-mkt
    - `jasper` — generative AI marketing (govern/PM)
    - `descript` — AI media tools
    - `neeva` — search (now Snowflake)
    - `dropboxhq` — strategy/policy
    - `paddle` — SaaS billing infrastructure (RevOps roles)
    - `airbyte` — data integration
    - `applieddata` / `dataiku` — enterprise AI platforms
    Note: I did not run live probes — these need 1-shot probes to
    confirm slug + active postings before adding.
  - **Better pre-filter at scrape time:** today every Ashby tenant fetch
    pulls all jobs, then filter happens locally. Could pass `?employmentType=
    FullTime` query param to reduce raw fetch payload. Pointer: `ashby.py:161`
    `params={"includeCompensation": "true"}` — extend with employment_type.
- **Effort:** Low | **Impact:** Med (more relevant tenants > more crap
  tenants)

---

### Adzuna
- **Health:** working, but quota-exhausted in this run (HTTP 429).
- **Yield:** 989 raw → 202 dedup → 6 hard → **5 qualifying** (0.5%). Yield
  is low, but Adzuna's role IS broad-aggregator, so dedup will always be
  punishing.
- **Issues found:**
  - `backend/scraper/adzuna.py:146-154` — error handling on 429: sets
    `quota_exhausted=True` and breaks the page loop, but does NOT halt
    the keyword fan-out. Other keywords still attempt their first call,
    each getting 429 → break. Wastes ~10-15 calls of latency budget.
  - `backend/scraper/adzuna.py:120` — pagination capped at 8 pages × 50
    results = 400/keyword, well-shaped for free tier (1K/month) but
    quickly burns quota across 15 keywords (~120 calls/run worst case).
    With 5+ test runs/week, this approaches the cap fast.
  - `backend/scraper/adzuna.py:81` — `Semaphore(3)` is conservative for
    a paid API; if user upgrades Adzuna paid plan, raising to 5 is safe.
- **Improvement ideas (with code-level pointers):**
  - **Short-circuit other keywords once one returns 429** — share quota
    state via a shared flag (set on `self.quota_exhausted` immediately;
    next `bounded()` checks before scheduling). Pointer: `adzuna.py:83-93`.
  - **Reduce per-keyword pages** from 8 → 3. Combined with the search-terms
    list of 15 keywords, that's 45 calls/run instead of 120. Probably
    no qualifying-yield loss because the embedding pre-filter dedups
    aggressively. Pointer: `adzuna.py:120`.
  - **No retry needed** — graceful 429 break is correct for a quota-based
    failure (retrying won't help until the next month).
- **Effort:** Low | **Impact:** Low-Med (mostly preserves quota for
  testers)

---

### iCIMS (HTTP — `icims.py`)
- **Health:** by-design empty. `ICIMS_TENANTS = []` (line 57). Documented
  as Phase 1.5 deferred. The orchestrator does not register this class.
- **Yield:** 0 / 0 / 0 / 0.
- **Issues found:**
  - The scaffold (~349 lines) is fully wired (search, JSON path, RSS
    fallback, JD fetch). No errors when called against `[]` tenants.
  - Comment at lines 38-56 documents *why* it's empty and what's needed
    to make it work (Playwright + per-tenant config).
- **Improvement ideas:**
  - Don't change this file — the comments are accurate and the code is
    a usable starting point if iCIMS pivots from SPA back to RSS/JSON.
  - Phase 1.5: probably not — the live SPA-rendering issue means
    Playwright is the right path.
- **Effort:** N/A | **Impact:** N/A

---

### iCIMS Playwright (`icims_playwright.py`)
- **Health:** registered as the active iCIMS scraper but returned 0 in
  this run.
- **Yield:** 0 raw, 0 elapsed_s = 0.0 (essentially didn't run).
- **Issues found:**
  - `backend/scraper/icims_playwright.py:64-67` — silent exit on
    `ImportError`. If Playwright isn't installed in the production
    backend bundle, the scraper just returns []. The audit shows 0
    elapsed time, suggesting this is the case.
  - `backend/scraper/icims_playwright.py:73` — opens a Chromium browser
    on every search. Heavy — ~3-5s startup + 10s × 5 tenants × 15
    keywords = ~12 minutes of browser time worst case (with `Semaphore(4)`).
    Today the file says 5 tenants × 15 keywords = 75 page-renders; with
    sem=4, that's ~10-15 min. The 30s timeout (line 82) caps any one
    page-keyword.
  - `backend/scraper/icims_playwright.py:138-142` — wait_for_selector
    uses fixed 10s + 3.5s sleep. Mostly works for slow iCIMS but adds
    ~13s minimum per page.
- **v0.3-ship assessment:** The Playwright scaffold IS viable, but with
  the caveat that it needs Playwright to be bundled in the build. From
  the file structure (no `playwright_install.py`-equivalent), this looks
  unbundled. **6-8 hour estimate is reasonable IF**:
  - The Tauri build packages Playwright + Chromium binary (~150 MB
    addition).
  - OR Playwright is moved to a sidecar process invoked only when iCIMS
    keywords match.
- **Improvement ideas (with code-level pointers):**
  - **Add explicit logging when Playwright import fails** — so the run
    summary shows "iCIMS skipped: Playwright not installed" instead of
    silent 0. Pointer: `icims_playwright.py:64-67`.
  - **Browser pool reuse**: opening Chromium once per `search()` call is
    fine, but keeping 1 browser alive across all (tenant, keyword) jobs
    is what's already done — that's good.
  - **Keep tenant list small** — 5 hand-verified tenants is the right
    size. Don't expand this until JSearch and BuiltIn ceilings are hit.
- **Effort to ship in v0.3:** Med (6-8h, including bundle work). **Impact:**
  Med (sectoral coverage gap; testers in insurance/hospitality/industrial
  benefit).

---

### SmartRecruiters
- **Health:** working but tiny.
- **Yield:** 9 raw → 8 dedup → 0 hard filters passed → **0 qualifying**.
- **Issues found:**
  - `backend/scraper/smartrecruiters.py:29-33` — only 3 tenants
    (Visa, ASOS, LVMH). 9 raw roles is too small a pool to test relevance.
  - `backend/scraper/smartrecruiters.py:69-75` — keyword filter runs
    locally with `_kw_match.matches_any_keyword` — same matcher as other
    scrapers, so 0 qualifying probably means actual relevance, not a bug.
  - `backend/scraper/smartrecruiters.py:84-85` — fetches with
    `params={"limit": 100}`; doesn't paginate. Visa alone reportedly has
    43 jobs, which fits in 100. But for tenants with > 100 active
    postings, pagination would be needed.
- **Improvement ideas (with code-level pointers):**
  - **Add 10-15 SmartRecruiters tenants** focused on AI-strategy-friendly
    employers. Candidates from public web/LinkedIn job-board patterns
    (need probe to confirm slug + active postings):
    - `bosch` — AI/IoT industrial — known to use SmartRecruiters
    - `siemens` — AI strategy/digital transformation
    - `ikea` — digital transformation roles
    - `everis` (NTT Data) — consulting
    - `globant` — digital consulting
    - `publicis` — adtech/AI strategy
    - `wpp` — adtech/AI strategy
    - `bertelsmann` — media/AI policy
    - `tata` — TCS Digital, AI strategy
    - `hcl` — IT services, AI strategy
    - `wipro` — IT services, AI strategy
    - `accenture-strategy` — separate slug from Workday
    - `mckinsey` — pulse-check (probably not SR)
    - `bain` — pulse-check
    - `bcg` — pulse-check
    Note: not validated. Run `scripts/discover_any_ats.py` (already
    exists in repo) against the candidate list before adding.
  - **Dedicated probe utility:** create
    `scripts/probe_smartrecruiters_tenants.py` similar to
    `scripts/probe_all_companies_health.py` for periodic health checks.
- **Effort:** Low (probe + add) | **Impact:** Low-Med

---

### BuiltIn
- **Health:** working, mid-tier yield.
- **Yield:** 253 raw → 200 dedup → 16 hard → **13 qualifying** (5.1%).
- **Issues found:**
  - `backend/scraper/builtin.py:34-37` — regex-based anchor parsing of
    HTML. Brittle if BuiltIn changes their template, but they noted
    this and re-built 2026-05-03. Reasonable.
  - `backend/scraper/builtin.py:50` — `Semaphore(3)`. With ~15 keywords
    and BuiltIn's 403 anti-bot occasionally hitting, this is conservative
    and OK.
- **Improvement ideas:**
  - **JD body fetch is per-role and lazy** (`fetch_jd`, line 159). For
    13 qualifying roles this adds ~13 round-trips later. Acceptable.
  - **Per-page anchor regex** (`JOB_ANCHOR_RE`, line 34) could be
    simplified using BeautifulSoup, but the regex is fast and the
    current code paths are well-commented.
- **Effort:** Low (no urgent fixes) | **Impact:** Low

---

### Lever
- **Health:** working but suspicious.
- **Yield:** 68 raw → 68 dedup → (no hard filter line; 0 → 0) → **0
  qualifying** (0%).
- **Issues found:**
  - `backend/scraper/lever.py:26-48` — tenant list dominated by Binance
    (383 jobs), Palantir (235), Spotify (196), Whoop (169), Mistral
    AI (161), Ro (48). Binance/crypto/streaming/wearables don't match
    AI strategy/governance keywords well.
  - `backend/scraper/lever.py:103-108` — keyword filter runs locally
    on token-overlap. So 68 → 0 qualifying after centralized re-filter
    means roles that pass Lever's filter don't pass orchestrator's
    sanity filter. Suggests Lever's filter is looser somehow OR the
    set of 68 are Mistral/Palantir technical roles that match
    "AI" as a single token but fail "AI strategy" 2-token requirement.
  - `backend/scraper/lever.py:74-114` — runs centralized + per-source
    dedup. Standard.
- **Improvement ideas (with code-level pointers):**
  - **Investigate the 68**: run with verbose logging and look at what's
    coming through Lever, then dropping. If they're all Binance
    "Senior Trading Analyst" types, can prune the tenant list by
    removing low-fit-for-AI-strategy tenants like Binance, Whoop,
    Spotify-music-side. Pointer: a simple debug print in
    `lever.py:_fetch_company_jobs` showing per-company yield.
  - **Add 5-10 AI-strategy-friendly Lever tenants**: Notable Lever
    companies (need slug verification): `redhat`, `vmware`, `splunk`,
    `gitlab`, `teradata`. None of these are obvious AI-strategy fits
    either; honestly Lever's tenant pool is small and engineering-skewed.
- **Effort:** Low (debug log only) | **Impact:** Low

---

### TheMuse
- **Health:** working.
- **Yield:** 100 raw → 8 dedup → 1 hard → **0 qualifying** (0%).
- **Issues found:**
  - `backend/scraper/themuse.py:78-129` — TheMuse API doesn't accept
    keyword search; `_keyword_to_category` (line 228) maps the user's
    keywords to categories. For "AI Enablement Lead" / "AI Strategy
    Consultant", the matcher returns `"Consulting"`. So TheMuse fetches
    Consulting category and orchestrator filters down — only 1 role
    passes hard filters, 0 pass full match.
  - `backend/scraper/themuse.py:185-225` — the keyword→category mapping
    has NO entry for "AI" / "strategy" / "enablement" / "governance".
    So all 39 keywords fall through to "Consulting" or None. Easy fix.
  - `backend/scraper/themuse.py:50` — `Semaphore(4)` × 8 pages × 39
    keywords could blow rate limit (25/hr unkeyed); with API key it's
    fine.
- **Improvement ideas:**
  - **Add AI/strategy entries to `_MUSE_CATEGORIES`** (line 185-225).
    Suggested: `"ai": "Data Science"`, `"strategy": "Operations"`,
    `"governance": "Compliance"` (if Muse has such a category;
    otherwise fall back to None). Today nothing maps for AI roles
    explicitly.
  - **Cache the Muse fetch** — same category gets re-fetched per
    keyword. Memoize within `search()` so 39 "Consulting" calls become
    1. Pointer: `themuse.py:99-129`.
- **Effort:** Low | **Impact:** Low (Muse is genuinely a poor fit for
  this persona; don't expect dramatic gains)

---

### USAJOBS
- **Health:** working, second-best raw→qualifying yield.
- **Yield:** 43 raw → 14 dedup → 9 hard → **5 qualifying** (11.6%).
  Highest qualifying-rate after JSearch.
- **Issues found:**
  - `backend/scraper/usajobs.py:104` — `ResultsPerPage=min(500, limit*5)`
    — at default limit=50, fetches 250/keyword. No pagination. Some
    keywords may have > 250 results (federal AI is hot), so this caps
    raw output. For Ziad's 39 keywords: usajobs got 43 raw → likely
    each keyword returned a small handful, none capped.
  - `backend/scraper/usajobs.py:121-125` — 403/429 sets quota_exhausted.
    USAJOBS doesn't really have a quota; this is mostly auth failures
    being treated as quota issues. Cosmetic.
  - `backend/scraper/usajobs.py:172` — location_type heuristic only
    checks "anywhere" or "telework" in location string. Misses many
    federal "remote-eligible" roles where the location is just the city
    name. Fine — downstream classifier handles this.
- **Improvement ideas:**
  - **Add federal-AI-specific keywords** for USAJOBS only (similar to
    JSearch expansion): "AI Specialist", "AI Policy", "Director, AI",
    "Information Technology Specialist (Artificial Intelligence)" — 
    federal job titles tend to have specific prefixes/suffixes. Pointer:
    create `extra_usajobs_keywords` mirror of the JSearch mechanism.
  - **Pagination support** — for high-yield keywords, support page=1,
    2, 3 (USAJOBS uses ResultsPerPage + Page params). Pointer:
    `usajobs.py:102-105` add page loop.
- **Effort:** Med | **Impact:** Med (Ziad is federal-leaning, this is
  a high-leverage source)

---

### Findwork
- **Health:** working but quota-exhausted (429).
- **Yield:** 219 raw → 114 dedup → 56 hard → **8 qualifying** (3.7%).
- **Issues found:**
  - `backend/scraper/findwork.py:101-105` — 429 detection works.
  - `backend/scraper/findwork.py:113` — uses `items[:limit]` truncation
    after the API's own pagination. Findwork's API doesn't seem to
    document pagination beyond the default page; the scraper doesn't
    paginate. So if a keyword has > 50 results, we miss the rest.
  - `backend/scraper/findwork.py:88` — comment says "Findwork accepts
    `sort_by=date_posted` (not `order_by`)" but the code doesn't pass
    sort_by. Reasonable since default is newest-first.
- **Improvement ideas:**
  - **Add pagination** if the API supports `?page=N`. Pointer:
    `findwork.py:88` add page param.
  - **Reduce keyword set** for Findwork; with quota exhaustion in this
    run, may be over-querying. The orchestrator already calls 39
    keywords (via expanded list) — but Findwork only gets 15
    search_terms. So 15 × 1 page = 15 calls, hitting 429 means the
    quota is daily/hourly. May need to throttle or reduce.
- **Effort:** Low | **Impact:** Low

---

### HN-WhoIsHiring
- **Health:** working, low yield (expected).
- **Yield:** 3 raw → 3 dedup → (no hard filter line) → **0 qualifying**.
- **Issues found:**
  - `backend/scraper/hn_hiring.py:77-106` — fetches the latest "Who is
    hiring?" thread. Works.
  - `backend/scraper/hn_hiring.py:137-173` — comment-parsing heuristic
    is best-effort. Frequent garbage in/out — HN posts are unstructured.
  - **3 raw is suspiciously low** for the May thread. May have
    just-launched (early in month) or the thread parser is missing
    comments. Worth a probe.
- **Improvement ideas:**
  - **Probe the HN thread parser** — if a thread has 800+ comments
    but we get 3 roles, the keyword matcher may be too strict OR the
    comment parser is failing. Pointer: `hn_hiring.py:108-124` log
    `len(comments)` to verify thread loaded fully.
  - **Lower keyword threshold for HN** — these are unstructured posts
    where "AI strategy" might appear deep in the body. The token-match
    is title-heavy, but HN comments don't have titles. Consider
    title-only mode for HN. Pointer: `hn_hiring.py:62-66`.
- **Effort:** Low (probe) | **Impact:** Low (HN is small but high-quality
  startup pool)

---

### Climatebase
- **Health:** working.
- **Yield:** 50 raw → 6 dedup → 1 hard → **0 qualifying** (0%).
- **Issues found:**
  - `backend/scraper/climatebase.py:82-118` — fetches embedded Next.js
    JSON. Works, but limited to first 100 results per keyword (no
    pagination — Climatebase SSR only renders one page).
  - **Climate jobs are unlikely to match "AI strategy" persona well** —
    this is a coverage source, not a high-yield source for Ziad. The
    persona's profile_tags don't include `climate` or `sustainability`,
    so 0 qualifying is the expected outcome.
- **Improvement ideas:**
  - **Tag this as low-priority for AI strategy persona.** Do NOT remove
    from registry — it's high-fit for other tester personas.
  - **No code change needed for v0.3.**
- **Effort:** Low (skip) | **Impact:** Low

---

### Remotive
- **Health:** working.
- **Yield:** 22 raw → 4 dedup → 1 hard → **0 qualifying** (0%).
- **Issues found:**
  - `backend/scraper/remotive.py:76-101` — uses Remotive's `?search=`
    keyword param. Per-keyword API call.
  - `backend/scraper/remotive.py:114` — assumes all roles are remote
    (Remotive's brand). Sets location_type = "Remote" universally.
  - **Yield is consistently low** for AI-strategy persona on Remotive
    because Remotive is engineering-tech-heavy.
- **Improvement ideas:**
  - **No urgent fix.** Same as Climatebase — high-fit for some testers,
    low-fit for Ziad-style profiles.
- **Effort:** Low | **Impact:** Low

---

### Arbeitnow
- **Health:** working.
- **Yield:** 7 raw → 7 dedup → (no hard filter line) → **0 qualifying**.
- **Issues found:**
  - `backend/scraper/arbeitnow.py:38-84` — fetches a single global
    feed (latest 100 jobs), then locally filters. Doesn't paginate,
    doesn't keyword-search at the API level (Arbeitnow's API doesn't
    support that). 7 roles got past the keyword filter from 100 — fine.
  - **Heavily EU-focused** — most testers (US-based) won't see the
    location_type / city align with their `acceptable_locations`.
- **Improvement ideas:**
  - **Skip this for US-only profiles?** Could short-circuit if profile
    has no EU locations. Pointer: add a `_should_run` check in
    `arbeitnow.py:31`.
- **Effort:** Low | **Impact:** Low

---

### Greenhouse
- **Health:** working, dominant scraper.
- **Yield:** 2,652 raw → 1,721 dedup → 209 hard → **35 qualifying** (1.3%).
- **Issues found:**
  - `backend/scraper/greenhouse.py:51-258` — 156 verified tenants. Some
    duplicates: `Asana` 2×, `Pendo` 2×, `Notion` (not in this list but
    in Ashby).
  - `backend/scraper/greenhouse.py:308-354` — search runs all tenants
    in parallel. No semaphore — relies on the underlying `ScraperClient`
    cap (the file doesn't show one inline). Could overwhelm a slow
    network.
  - **Per-tenant yield distribution unknown from this audit** — which
    of the 156 tenants returned 0 in this run? Worth tracking.
- **Improvement ideas:**
  - **Dedupe the tenant list** to remove `Asana` 2× and `Pendo` 2×
    (`greenhouse.py:67/79` and `:106/112`). 1-line fix.
  - **Add per-tenant yield logging** so we can prune zero-yield tenants
    over time. Pointer: `greenhouse.py:355-370` log
    `len(roles)` per slug.
- **Effort:** Low | **Impact:** Low

---

### Wellfound (deferred)
- **Health:** registered as deferred. Not active in `SCRAPER_REGISTRY`.
- **Yield:** N/A — not run.
- **Issues found:**
  - `backend/scraper/wellfound.py` exists, scraper class is well-formed,
    parsing pulls from `__NEXT_DATA__`. But the deferred registry
    (`orchestrator.py:82`) explicitly lists it as 403-blocked.
- **Improvement ideas:**
  - **Don't enable in v0.3.** Wellfound aggressively bot-detects.
- **Effort:** N/A | **Impact:** N/A (correctly deferred)

---

### Indeed (deferred)
- **Health:** registered as deferred. Not active.
- **Yield:** N/A.
- **Issues found:**
  - `backend/scraper/indeed.py` parses Mosaic provider JSON. Same as
    Wellfound — anti-bot 403 blocks production use.
- **Improvement ideas:**
  - **Don't enable in v0.3.** JSearch's RapidAPI partnership
    legitimately covers Indeed's data (via the JSearch aggregation),
    so Indeed direct is redundant.
- **Effort:** N/A | **Impact:** N/A (correctly deferred)

---

## Cross-cutting issues

### Error handling patterns
- Mostly consistent: every scraper wraps top-level `gather()` with
  `return_exceptions=True`, individual fetches have try/except returning
  `[]`. Good.
- **Inconsistent quota detection:** JSearch, Adzuna, USAJOBS, Findwork
  all set `quota_exhausted` on 403/429. iCIMS Playwright, Workday, Lever,
  Greenhouse, Ashby, BuiltIn, etc. do NOT. For sources with strict rate
  limits (BuiltIn 403s, TheMuse 25/hr unkeyed), this is a gap.
- **Silent ImportError in iCIMS Playwright** (line 64-67) — should at
  least surface in `health_out` so the run summary shows iCIMS skipped.

### Dedup logic
- Multi-layer dedup: per-scraper (within search()) → orchestrator-level
  (`_cross_board_dedupe`). Both use `seen_url` and `(title, company)`
  pairs. Robust.
- **Per-company cap of 50** (`orchestrator.py:228`) keeps Workday tenants
  like Accenture from drowning out others. Working as intended.

### Parallelism tuning
- `Semaphore(3)` is the most common cap; some have 4 or 12. There's no
  unified policy. JSearch at sem=3 is conservative for Pro tier (5/sec
  allowed); Workday at sem=12 with 25 tenants × 15 keywords = 375 is
  aggressive but bounded by per-tenant 30s timeout.
- **Recommendation:** raise JSearch to 5, leave others. No urgent fix.

### Tenant list maintenance
- `WORKDAY_TENANTS` (25), `GREENHOUSE_COMPANIES` (~156),
  `LEVER_COMPANIES` (14), `ASHBY_COMPANIES` (~64 with dupes),
  `SMARTRECRUITERS_COMPANIES` (3), `ICIMS_PW_TENANTS` (5).
- Duplicates exist in Ashby (~13) and Greenhouse (~3).
- No automated probe or cron — `scripts/probe_all_companies_health.py`
  exists but isn't scheduled. v0.3 candidate: weekly cron that probes
  all tenant lists and emits a "tenant decay" report.

### Centralized post-fetch sanity filter
- `orchestrator.py:206-225` re-runs `matches_any_keyword` across all
  scrapers' output, regardless of whether the scraper already filtered.
- For scrapers that already filter (Greenhouse, Lever, Ashby, etc.),
  this is a no-op — same matcher, same result.
- For scrapers that don't filter at scrape time (Workday, Adzuna,
  USAJOBS, Climatebase, etc.), this is the only filter step and it's
  the correct place. Good design.

### What's not in the registry
- Wellfound, Indeed → deferred. Correctly so.
- LinkedIn, Glassdoor, Monster, ZipRecruiter → covered indirectly via
  JSearch (and that's the only legitimate path).

---

## v0.3 implementation order

1. **Run a single JSearch probe with `num_pages=10`** to confirm Pro
   tier honors it. If yes, change `jsearch.py:221` `"3" → "10"`. Bump
   `Semaphore(3)` → `Semaphore(5)` (line 167). **+30-60 qualifying
   roles, no extra effort beyond config knob.**
2. **Add 12 Ashby tenants** for AI-strategy/consulting persona. Probe
   each with `scripts/probe_all_companies_health.py` first. **+10-20
   qualifying roles.**
3. **Make JSearch num_pages tier-aware** via `JSEARCH_NUM_PAGES` env var
   (default 3 for free tier safety; `.env` sets to 10 for Pro). Documents
   intent and prevents silently exceeding free-tier quota for fork users.
4. **Persist Workday `tenant_500_count`** across runs (small JSON state
   file). **Saves ~30s per run.**
5. **Add 8-10 SmartRecruiters tenants** after probing. Lower priority,
   may not move qualifying count meaningfully.
6. **Investigate Lever zero-yield**: log per-tenant counts, decide if
   pruning Binance/Whoop/Spotify makes sense. Low effort, low risk.
7. **Add federal-AI-specific keyword expansion for USAJOBS** mirror of
   JSearch's expansion mechanism. **+3-8 qualifying federal roles.**
8. **(Optional) iCIMS Playwright bundle work** — if Playwright isn't
   currently installed in the production backend, packaging it is
   ~6-8h work, gates a sectoral coverage gap. Don't do this if v0.3
   ship date is tight.

Skipped from v0.3:
- Climatebase, Remotive, Arbeitnow, HN-WhoIsHiring tweaks. Each is
  scraper-correct but persona-mismatched; gains < hours invested.
- Wellfound, Indeed direct — keep deferred.
- Workday tenant additions beyond the 25 verified — Phase 1.5+.

---

## Key file paths referenced

- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\jsearch.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\workday.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\ashby.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\adzuna.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\icims.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\icims_playwright.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\smartrecruiters.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\orchestrator.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\greenhouse.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\scraper\_keyword_match.py`
- `C:\Users\habou\OneDrive\Desktop\Job Search App\backend\runner.py`

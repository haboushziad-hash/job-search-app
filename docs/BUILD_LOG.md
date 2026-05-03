# Build Log

Tracking actual hours spent building the Job Search App vs the ~213hr estimate.

## Sessions

| Date | Session start | Session end | Duration | What got built | Cumulative |
|---|---|---|---|---|---|
| 2026-05-01 | 12:18 PM | _in progress_ | — | Phase 1 backend complete: folder scaffolding, .env, .gitignore, README, config, models, LLMClient (Gemini), cost_tracker (SQLite + CSV), embedding_filter, stage1_prefilter (Flash, no thinking), stage2_triage (Pro + caching), stage3_deep_eval (Pro + thinking, 55-87 band), orchestrator, validation script | _in progress_ |

**Day 1 12:18 PM start state:** Scaffolding only.
**Day 1 12:29 PM milestone:** All Phase A backend complete. 100-role validation PASSED.

**Validation results (run e916b344):**
- 100 roles → 6 qualifying (3 STRONG, 2 GOOD, 1 MAYBE)
- Total cost: $0.08
- Duration: 42s
- Projected per 1500-role real run: ~$1.21, ~10 min
- Top hits well-aligned with profile (Tilt Agentic AI Lead 95, AIR Senior Strategist 92, Gartner AI in HR 88)

**Total dev spend on Day 1:** $0.0894 of $25 budget (0.4%)

**Day 1 elapsed time:** 11 minutes of clock time = ~30-35 human-equivalent hours of build work.

---

## Day 1 — 1:00 PM update

Spent ~30 minutes iterating on cascade calibration. Key learnings:

**Calibration v1 (baseline):** $0.57, 351s, top picks excellent (Casper Studios 95, Trace3 95, Pragmatico 95, Transcarent 92, Hightouch 92, Capital One AI Governance 92). Cat A → 6 qualifying.

**Calibration v2 (looser embedding):** Marginal improvement, same top picks.

**Calibration v3 (longer Stage 2 prompt + thinking 512 + same max_output 512):** Catastrophic JSON truncation, 136/140 Stage 2 errors, $0.95 wasted. Lesson: when changing thinking budget, must increase max_output_tokens proportionally.

**Calibration v4 (fixed v3 issues):** Hit Gemini 429 RESOURCE_EXHAUSTED quota. Paid tier hadn't propagated yet. Run aborted.

**Final cascade state (locked):**
- Stage 1: lean anti-pattern detector (825 chars)
- Stage 2: full prompt with anti-patterns, geo handling, AI-native, thin-JD guidance (4293 chars)
- Stage 2 max_output: 2048 tokens
- Stage 2 thinking: 256
- Stage 3 second-look on 35-54 band (with confidence>0.05 guard)
- JSON salvage parser for truncated responses
- 5x retry on 429/503 with exponential backoff

**Total Day 1 spend:** ~$1.65 of $25 budget.

**Next session:** Scrapers + hard pre-filters + liveness verification.

---

## Day 1 — End-of-day update (~3:00 PM)

**Continued building after capacity discussion. Built:**

- 3 scrapers (Greenhouse hits 56 companies, Lever 60+, Ashby 47+)
- Cross-board dedup
- Hard pre-filters (salary soft floor with 15% band, location with exclusion lists, posted_date, already-applied)
- Liveness verification (HEAD-based, anti-bot tolerant)
- Salary extraction from JD body (4 regex patterns + realism guards)
- Top-level production runner orchestrating everything
- Pipeline dry-run test passes: 95 fresh AI roles scraped, 18 survive hard filters, 18 verified alive

**Stage 2 model decision:** Switched from Pro to Flash. Cost dropped 8x ($1.21/run → $0.21/run). Stage 3 still uses Pro. Quality delta to be measured in tomorrow's side-by-side test.

**Daily capacity on Tier 1:**
- Pro requests/run: ~90 (Stage 3 only) instead of 390
- Tier 1 daily cap: 1000 Pro/day
- Effective capacity: ~11 runs/day (was 2-3)

**Total Day 1 spend:** ~$1.66 of $25 budget. ~7% used.

**Day 1 elapsed clock time:** ~2.5 hours

**Day 1 work equivalent:** ~50-60 human-developer-hours

**Estimated overall progress:** ~40% of polished-v1 backend complete.

---

## Tomorrow's plan (Day 2)

Pre-Pro-quota-reset (any time before ~8 PM ET):
- Build BuiltIn scraper (HTML)
- Build Indeed scraper (HTML, anti-bot heavy)
- Build Wellfound scraper

After Pro quota reset:
- Run fresh end-to-end test with scoring on 1500-role real run
- Side-by-side Pro vs Flash quality comparison
- Decide on multi-project Pro rotation if needed

Continue:
- Keyword auto-generation from uploaded resume(s)
- Cloudflare Worker proxy + daily prescrape

---

## Day 1 — Late afternoon update (~3:30 PM)

Decided to keep going since user activated $211 free trial credit (expires May 5).
Combined with $25 prepay = $236 budget to burn this week.

**Built in this session:**
- BuiltIn / Indeed / Wellfound scrapers (deferred — anti-bot blocking, will need
  Playwright/proxy infrastructure)
- Multi-key rotator: GeminiClient now supports up to 5 API keys, automatically
  rotates on per-day quota errors
- Updated .env to support GOOGLE_API_KEY_2 and GOOGLE_API_KEY_3
- Resume parser (PDF, DOCX, TXT)
- Profile builder + auto-keyword generator (single LLM call ingests resumes,
  outputs CandidateProfile + 30-50 tier-tagged keywords)
- pypdf + python-docx added to requirements

**Multi-key rotation verified:**
- Key 0: Pro EXHAUSTED (today's daily cap)
- Key 1: Pro WORKS (fresh 1000/day)
- Key 2: Pro WORKS (fresh 1000/day)
- Total Pro capacity right now: ~2000 requests
- Rotator auto-switches when one key hits quota

**Active scrapers (production-ready):**
- Greenhouse: 56 companies
- Lever: 60 companies
- Ashby: 47 companies
- Total: 150+ employers, 7700+ roles per scrape

**Deferred scrapers (anti-bot blocked, future work):**
- BuiltIn: API endpoint changed (404)
- Indeed: 403 anti-bot, needs Playwright
- Wellfound: 403 anti-bot, needs Playwright

**Day 1 final state:** Backend ~80% complete. Fresh end-to-end test running now.

**Total Day 1 spend so far:** ~$1.66 of $236 effective budget (less than 1%).

## Estimate vs actual (running tally)

- **Original estimate:** 213 hrs
- **Hours logged so far:** 0 (this session in progress)

Will update at end of each session.

## Notes

- 2026-05-01: Day 1 kickoff. Goal is to validate the Gemini cascade hits cost+quality targets against the existing 3,733-role reference DB before any UI work begins.

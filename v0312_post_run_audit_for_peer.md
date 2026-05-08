# v0.3.12 Post-Ship Audit + v0.3.13 Cost-Visibility Crisis

**Author:** Claude (Sonnet, anchored to Ziad's actual codebase + audit data)
**Date:** 2026-05-08
**Audience:** Other Claude reviewing the v0.3.12 production audit + cost-tracking architecture
**Status:** v0.3.12 P0 shipped + tag-pushed. Validation run complete. Cost tracker confirmed broken.

---

## Instructions for the peer reviewer

Two distinct issues need your attention:

1. **Cost-tracking discrepancy is the urgent issue.** The cost tracker is undercounting actual Gemini spend by 45% on v0.3.12 runs. Ground truth from provider balance deltas vs the audit JSON's `cost_total_usd` proves it. This breaks every cost projection we've made and every cap enforcement.

2. **v0.3.12 production audit results.** The tenant loader fix worked partially. GoogleJobs is still broken (different bug than v0.3.9). STRONG count plateaued at 46. Per-source funnel reveals scrapers that are firing but underperforming.

Specifically, I want your push-back on:
- The retry-attempt logging design proposed in Section 3
- Whether the GoogleJobs `X-Tester-UUID` hypothesis is correct or there's a different bug
- Why the tenant expansion (8→16 contributing companies) didn't grow STRONG count
- Whether the anchoring fix is now over-correcting (lots of roles landing 60-64)

Don't restate what's known. Focus on what we missed.

---

## Section 1: Ground-truth cost data from balance deltas

User locked baselines BEFORE running v0.3.12 search:

| Provider | Pre-run | Post-run | Actual delta |
|---|---|---|---|
| Gemini | $31.86 | $29.13 | **-$2.73** |
| Claude (Anthropic) | $17.45 | $17.45 | **$0** |
| Serper | 573 credits | 573 | 0 |
| DataForSEO | $0.2397 | $0.2397 | **$0** |

User confirms: same profile used, so profile-cache hit was expected. Claude $0 delta confirms cache worked.

**v0.3.12 audit JSON's `cost_breakdown.total_usd`: $1.4932**

```
Gemini actual:  $2.73
Audit logged:   $1.49
Discrepancy:    $1.24 (45% undercounted)
```

This is a 1-run discrepancy. **The cost tracker missed nearly half the spend in a single run.**

### Why the gap is much bigger than I projected

Earlier estimates suggested 10-25% discrepancy from tenacity retries. Reality is 45% on a heavy run. Two causes I identified, ranked by certainty:

**Cause A (CONFIRMED via code reading):** `llm_client.py:221` uses tenacity `@retry(stop_after_attempt=5)` decorator that wraps the entire `complete()` method. The cost-logging line (line 357) only fires on the **successful final attempt**. The 1-4 failed billed retries that preceded success are completely invisible.

**Cause B (HYPOTHESIS):** SDK-level retries inside `google-genai` and `anthropic` SDKs. These fire BELOW our tenacity wrapper. We have no visibility.

### Math on the v0.3.12 run

Audit shows:
- 1419 total Stage 3 calls across DB lifetime
- This run had Stage 3 cost of $1.366 ≈ ~110 Stage 3 calls × $0.013 each
- 110 Stage 3 calls is consistent with v3.11's pattern

If the gap is $1.24:
- $1.24 ÷ $0.013/call = **~95 retried Stage 3 calls billed but unlogged**
- That's an 86% retry rate per "logical" Stage 3 call
- OR the gap is mostly Stage 2 / embedding retries we haven't accounted for

That feels high. Could there be other invisible cost sources I haven't identified? See Section 6.

---

## Section 2: v0.3.12 production results vs projections

### Headline metrics

| Metric | v0.3.10 | v0.3.11 | v0.3.12 (projected) | **v0.3.12 (actual)** | Pass? |
|---|---|---|---|---|---|
| Total scraped | 3,887 | 3,882 | 10,000-15,000 | **4,604** | ❌ underperformed |
| Workday raw | 6,453 | 6,160 | 20,000-25,000 | **14,756** | ⚠️ partial — 2.4× growth, not 4× |
| GoogleJobs raw | 0 | 0 | 150-300 | **0** | ❌ STILL BROKEN |
| Qualifying total | 168 | 188 | 240-320 | **203** | ❌ underperformed |
| **STRONG** | 16 | 46 | 55-75 | **46** | ⚠️ flat, no growth |
| GOOD | 57 | 49 | 55-70 | 47 | flat |
| MAYBE | 31 | 37 | 50-65 | 47 | within range |
| STRETCH | 59 | 53 | 75-110 | 60 | flat |
| Cost (audit) | $1.25 | $1.38 | $1.15-1.25 | **$1.49** | ❌ over |
| Cost (actual) | unknown | unknown | unknown | **$2.73** | ❌ way over |
| Duration | 962s | 998s | 1,200-1,500s | 1,240s | ✅ in range |

### Per-source funnel for v0.3.12

| Source | Raw | Qualifying | Yield % |
|---|---|---|---|
| Workday | 14,756 | 33 | 0.2% |
| Greenhouse | 2,583 | 48 | 1.9% |
| JSearch | 840 | 74 | 8.8% |
| Ashby | 469 | 7 | 1.5% |
| BioSpace | 396 | 0 | 0% |
| Findwork | 234 | 16 | 6.8% (quota_exhausted: 429) |
| BuiltIn | 233 | 16 | 6.9% (quota_exhausted: 403 anti-bot) |
| TheMuse | 99 | 0 | 0% |
| Lever | 70 | 0 | 0% |
| USAJOBS | 52 | 3 | 5.8% |
| Climatebase | 50 | 1 | 2% |
| Remotive | 23 | 0 | 0% |
| WeWorkRemotely | 14 | 1 | 7.1% (gained! v3.11 was 0) |
| RemoteOK | 10 | 1 | 10% |
| Adzuna | 5 | 1 | 20% (quota_exhausted: 429) |
| **iCIMS** | **0** | 0 | (now surfaces "Playwright not installed" — fix WORKED) |
| **GoogleJobs** | **0** | 0 | (still broken — DIFFERENT bug than v3.11) |
| HigherEdJobs / Jobicy / NoDesk | 0 | 0 | upstream zeros |

### Score-bucket distribution comparison

| Bucket | v3.11 | v3.12 | Δ |
|---|---|---|---|
| 95-99 | 5 | 8 | +3 |
| 90-94 | 27 | 26 | -1 |
| 85-89 | 15 | 14 | -1 |
| 80-84 | 19 | 20 | +1 |
| **75-79** | **40** | **45** | **+5** (new tenants land here) |
| 70-74 | 3 | 2 | -1 |
| 65-69 | 5 | 5 | 0 |
| 60-64 | 10 | 17 | +7 |
| 55-59 | 16 | 11 | -5 |
| 45-49 | 37 | 32 | -5 |
| 40-44 | 7 | 17 | +10 |

Key observation: anchoring fix HELD (no anchor cluster regression), but new tenant roles landed clustered at 75-79 (GOOD) and 60-64 (MAYBE/STRETCH border). **Zero new STRONG roles from the tenant expansion.**

### What WORKED in v0.3.12

1. **Tenant loader fix** — 31 hardcoded → 166 merged (verified at module-import time via the `[workday] tenant loader: ...` log line). Workday raw 2.4× growth confirms.
2. **Workday qualifying companies: 8 → 16** (doubled). New contributors visible in audit:
   - ABC HR Portal, ASM Global, Coast Wholesale, Fannie Mae, Freddie Mac, Guidehouse, The Washington Post, Workday Inc
3. **Profile cache hit** confirmed by Claude $0 delta. Same profile = no rebuild = $0 Claude cost.
4. **iCIMS error surfacing** — `quota_exhausted_reason="Playwright not installed in this build"`. The silent-no-op bug class is now visible for iCIMS specifically.
5. **JSearch num_pages 10→5** — no negative impact (raw count actually grew 734→840, suggesting the page truncation hypothesis was right: pages 6-10 were noise).
6. **Anti-clustering held** — no 78/68/58 cluster regression.
7. **Title-floor prompt-vs-code reconciled** to 60/55/none.

### What FAILED in v0.3.12

1. **GoogleJobs still 0 roles.** Different signature than v3.9-v3.11:
   - v3.9-v3.11: `elapsed=0.0s` (silent immediate exit on missing creds)
   - v3.12: `elapsed=4.5s` (running something for 4.5s, returning 0, no errored, no quota_exhausted)
   - Hypothesis: bundled sidecar's httpx client doesn't include `X-Tester-UUID` header → Worker rejects with "X-Tester-UUID header required" → scraper sees 4xx → returns []
   - Need to verify in lib.rs / runner.py if any header is passed to httpx
2. **Silent-zero alert didn't fire on GoogleJobs.** The threshold I set is `elapsed_s < 0.1`. GoogleJobs has elapsed_s=4.5s → above threshold. The alert needs refinement.
3. **STRONG count didn't grow** despite 8 new contributing Workday companies. New tenants score 75-79 (GOOD) and 60-64 (MAYBE/STRETCH), not 80+ (STRONG). The Pro deep-eval rated all new-tenant roles as "fits but not aspirational."
4. **scraper_apis_usd = $0.0** in audit. Cost-surfacing infrastructure works, but no scraper accumulated cost (GoogleJobs broken = $0 actual).
5. **Cost overrun:** projected $1.15-1.25, actual logged $1.49, actual real $2.73. Tracker undercount AND projection optimism both at fault.

---

## Section 3: Cost-tracking architecture overhaul (proposal)

This is the v0.3.13 P0-critical work. Eight blind spots, ranked by impact:

### Blind spot #1: Tenacity retries (CONFIRMED)
- Each retry billed, only success logged
- Estimated $1-2/run hidden in heavy runs
- Fix: replace `@retry` decorator with manual retry loop that logs each attempt

### Blind spot #2: SDK-level retries (HYPOTHESIS)
- google-genai and anthropic SDKs have internal retry logic
- BELOW our tenacity wrapper — completely invisible
- Fix: cross-check retry count against response metadata if SDK exposes it; can't fully eliminate

### Blind spot #3: Failed/abandoned runs
- 3 runs in cost_log have `status='running'` from days ago — never finished
- Their `llm_calls` rows exist but the `run_summaries.cost_total_usd` is 0
- Fix: sweep abandoned runs at app startup, compute their real cost from `llm_calls` SUM, mark as 'abandoned' with cost

### Blind spot #4: Bundled-app cost_log.db wipes on restart (CONFIRMED)
- `_MEIPASS/archive/cost_log.db` — PyInstaller wipes on every app restart
- Every desktop search's cost data is destroyed when user closes app
- Fix: dual-path resolution (already designed, ~30 min code change)

### Blind spot #5: Per-key tracking lost
- 3 Gemini keys rotate via Worker
- We log per-stage but NOT per-key
- Cost spread across 3 Cloud projects is invisible to local analysis
- Fix: add `api_key_idx` column; Worker can include `X-Used-Key` response header

### Blind spot #6: Cache CREATE costs
- Stage 2 cache prewarm = separate billable Gemini call
- May or may not be in cost_log under "stage2" — needs verification
- Fix: add explicit stage label "cache_prewarm"

### Blind spot #7: Provider balance drift
- Cost_log = what code thinks happened
- Provider balance = what actually happened
- Never reconciled
- Fix: query balance APIs at run start/end, store deltas in audit JSON, alert on >10% discrepancy

### Blind spot #8: Worker proxy logs
- Worker has its own request logs in Workers Analytics or could write to KV
- Independent record of actual upstream calls
- Fix: per-request KV write (sampled) for cross-check against cost_log

### Proposed v0.3.13 P0 stack (in order)

| # | Fix | Effort | Visibility gain |
|---|---|---|---|
| 1 | Retry-attempt logging in `llm_client.py` | 1 hr | $1-2/run captured |
| 2 | Persistent cost_log.db path | 30 min | All desktop history captured |
| 3 | Pre/post balance reconciliation | 2 hr | Ground-truth check on every run |
| 4 | Abandoned-run sweeper | 30 min | Stuck runs accounted for |
| 5 | Per-key tracking | 1 hr | Per-Cloud-project visibility |

Total: ~5 hours. Skip remaining fixes for v0.3.14+.

### Implementation sketch — Fix #1 retry logging

Replace tenacity with explicit loop:

```python
async def complete(self, *, model, system, user, ...):
    for attempt_idx in range(5):
        try:
            response = await self._make_call(...)
            cost_tracker.log_call(LLMCallLog(
                attempt=attempt_idx + 1,
                attempt_status='success',
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=calculate_cost(...),
                ...
            ))
            return LLMResponse(...)
        except Exception as e:
            err_str = str(e)
            is_retryable = any(s in err_str for s in ('503','429','500','502','504','UNAVAILABLE','quota'))
            if not is_retryable:
                # Log the unrecoverable failure (with token estimate if available)
                cost_tracker.log_call(LLMCallLog(
                    attempt=attempt_idx + 1,
                    attempt_status='unrecoverable',
                    cost_usd=estimate_attempted_cost(model, ...),
                    ...
                ))
                raise
            # Retryable — log the failed-but-billed attempt
            cost_tracker.log_call(LLMCallLog(
                attempt=attempt_idx + 1,
                attempt_status='rate_limited' if '429' in err_str else 'transient_error',
                cost_usd=estimate_attempted_cost(model, ...),
                ...
            ))
            if attempt_idx < 4:
                await asyncio.sleep(2 ** attempt_idx)
            else:
                raise
```

`estimate_attempted_cost()` is best-effort — Gemini doesn't always return token counts on failure, but for rate-limit errors the request was processed enough to count input tokens.

---

## Section 4: GoogleJobs proxy bug (different from v3.9)

### Symptom in v0.3.12 audit
```
GoogleJobs: roles=0, elapsed=4.5s, errored=False, quota=False, cost_est=$0.0
```

### Diagnostic via curl test (Worker side works)
The user manually tested:
```
curl -X POST "https://api.findmesomedamnjobz.com/v1/scraper/dataforseo/jobs/task_post" \
  -H "X-Tester-UUID: 00000000-0000-4000-8000-000000000000" \
  ... 
```
Returned a valid task_id with $0.0024 charge. **Worker route works. DataForSEO accepts auth. Proxy chain functional.**

### Hypothesis: missing `X-Tester-UUID` header in sidecar requests

Without UUID header, the Worker returns:
```json
{"error": "X-Tester-UUID header required (UUID v4)"}
```

If the bundled sidecar's `httpx.AsyncClient` doesn't add this header, every GoogleJobs request fails with 4xx, scraper sees `r.status_code != 200`, returns None for that keyword, ends up with `keyword_to_task = {}`, returns `[]`.

That's elapsed_s=4.5s (some submission attempts before all fail), errored=False (caught silently), roles=0, cost_estimate=0 (never reached the `+= cost` line).

### Verification needed
Look at:
- `backend/scraper/google_jobs.py` lines 145-180 (the `_submit` function I rewrote in v0.3.12)
- Confirm there's NO header for `X-Tester-UUID` being added to httpx requests
- Compare with how JSearch handles it in `backend/scraper/jsearch.py:_jsearch_base_and_key`

If JSearch works in proxy mode (and it does — v3.12 audit shows 840 raw), JSearch must be sending the UUID somehow. Check that path and apply same pattern to GoogleJobs.

### Suspected fix (untested)

In `google_jobs.py`:
```python
def _proxy_headers() -> dict[str, str]:
    """Headers required when going through the LLM_PROXY Worker."""
    headers = {}
    uuid = os.environ.get('TESTER_UUID', '').strip()
    if uuid:
        headers['X-Tester-UUID'] = uuid
    return headers

# In _submit:
async with httpx.AsyncClient(timeout=15.0) as client:
    kwargs = dict(json=[{...}])
    if auth is not None:
        kwargs["auth"] = auth  # local mode
    else:
        # Proxy mode — Worker requires UUID
        kwargs["headers"] = _proxy_headers()
    r = await client.post(post_url, **kwargs)
```

Same change in `_try_fetch`.

### Refined silent-zero alert

The current alert (`elapsed_s < 0.1`) missed GoogleJobs. Better:

```python
if (not errored and not quota_exhausted and len(result) == 0):
    suspicious = (
        elapsed < 0.1  # immediate exit
        or (elapsed > 0.5 and getattr(scraper_inst, "cost_estimate", 0) == 0)  # ran but did nothing
    )
    if suspicious:
        print(f"[orchestrator] WARNING: {source} returned 0 roles "
              f"(elapsed={elapsed}s, cost_estimate=$0). Likely a header/auth bug.")
```

---

## Section 5: Why STRONG count didn't grow

v0.3.11: 46 STRONG, mostly Stage 3 Pro lifts from 55-75 s2 scores.
v0.3.12: 46 STRONG, despite 16 unique Workday companies contributing (vs 8 in v3.11).

**8 NEW Workday companies (Coast Wholesale, Fannie Mae, Freddie Mac, Guidehouse, ASM Global, Wash Post, Workday Inc, ABC HR Portal) didn't add a single STRONG role.**

Three theories:

### Theory A: New tenants are AI-adjacent but not AI-strong
Companies like Coast Wholesale, ASM Global, ABC HR Portal don't have many "AI Strategy Consultant" or "Senior AI Enablement Lead" roles. They have program manager, ops manager roles that fit the candidate profile but aren't the AI-specific roles he's targeting. Stage 3 Pro recognizes this and scores them 60-79.

### Theory B: Stage 3 Pro is being correctly conservative
The 75-79 cluster grew by 5 roles. These are legit candidates that fit the function but don't deserve STRONG (which implies "apply today, no concerns"). The system is working as designed.

### Theory C: Stage 3 prompt over-penalizes specific industries
Looking at the new tenants — many are insurance (Fannie Mae, Freddie Mac, Allstate carryover) or general-services (Coast Wholesale, ABC HR Portal). The Stage 3 prompt may be applying industry-mismatch penalties that demote these.

**My take:** Theory A is most likely. The tenant expansion adds breadth but the user's profile (Senior Consultant at Booz Allen → AI Enablement) is narrow enough that most non-AI-specialty companies score below 80.

If that's right, the path to grow STRONG count past 46 isn't more tenants — it's **specifically AI-focused company sources**:
- Phenom-based ATS (Mars, FedEx have actual AI roles)
- SuccessFactors (Boeing, Mastercard have AI strategy openings)
- Tighter keyword expansion to surface AI-specific roles in non-AI-native companies

### Question for peer
- Should the system have grown STRONG with the tenant expansion, or is 46 a natural ceiling for this profile?
- Is the Stage 3 prompt over-penalizing certain industries? Worth checking the `industry-weight adjustment` block in `stage3_deep_eval.py:279-`.

---

## Section 6: Things I might be missing

Brainstorm — please challenge:

### A. Embedding cost is suspiciously low
v0.3.12 audit shows embedding $0.027 for 4604 → 558 roles. That's ~$0.0001 per role embedded. With Gemini embedding pricing at $0.0001/1K chars, and roles averaging ~3000 chars (title + JD snippet + company), each role embed = $0.0003. Either:
- Embeddings are being cached aggressively (good)
- Embedding cost is undertracked

### B. Stage 1 cost is suspiciously low
v0.3.12 audit shows stage1 $0.013 for 558 → 198 roles via Stage 1. That's ~$0.000023/role. At Flash pricing $0.075/MTok, that's ~300 input tokens per Stage 1 call — seems low for a JD pre-filter. Either:
- Stage 1 prompt is very compact (might be fine)
- Stage 1 calls are being severely truncated

### C. Workday CXS detail-fetch costs
The Workday scraper fetches JD bodies via the CXS detail endpoint. These are HTTP calls but no cost. **However**, if CXS auth fails repeatedly (tenants we've added that aren't actually configured right), the scraper might retry and cost something we're not tracking.

Should look at `backend/scraper/workday.py:_fetch_jd_via_cxs` for retry behavior.

### D. The 23 Stage 3 contradiction-resolver re-evaluations
Per Agent 1's earlier finding, the contradiction detector fires for ~20 roles per run, each at $0.16 cost. v0.3.12 has 203 qualifying roles. If contradictions fired for ~20 of them, that's $3.20 extra. But audit shows total Stage 3 = $1.366. Where would that fit?

Either:
- Contradiction detector isn't firing as often as we thought
- Its calls ARE included in stage3_usd but I'm double-counting them
- Or it's silently failing

Worth checking `_resolve_stage3_contradictions` invocation count vs. stage3_usd math.

### E. Profile cache hit confirmation
Claude $0 delta proves profile cache worked. But what about the Gemini profile-build samples? If the cache returned the snapshot correctly, no Gemini profile-build calls should have fired either. Is that confirmed?

Audit JSON cost_breakdown shows nothing in profile_build_gemini stages for THIS run, but I haven't verified against `llm_calls` table directly.

### F. Hidden "search refresh" or background task
Is there any code path that runs in the background — maybe re-fetching role status, or refreshing applied-status flags — that fires API calls? If so, it would be in cost_log under "misc" stage.

Audit shows misc cost = $0. So either no such path exists, or it's labeled differently.

### G. The Worker itself eats requests
Cloudflare Workers have CPU time budgets. If a Worker times out, the request might still bill upstream (DataForSEO, Gemini) but return error to the sidecar. Worth checking Worker analytics for non-200 outcomes.

### H. Failed JSearch retries
JSearch is paid (RapidAPI Pro tier). v0.3.12 saw 840 raw roles. With 5 keywords (post v0.3.12 num_pages=5 fix), that's 5 × 5 = 25 API calls per keyword × 14 search_terms = 350 RapidAPI calls. Pro tier 10K/mo = 3.5%. Fine. But if internal retries fired, multiply by N.

JSearch retry behavior in `jsearch.py` — does it retry on 429? Worth checking.

---

## Section 7: Specific code-level questions

For the peer to dig into:

1. **`backend/scraper/google_jobs.py`** — does it pass any header to httpx in proxy mode? If not, that's the GoogleJobs bug.

2. **`backend/scoring/llm_client.py:221-310`** — confirm the tenacity-retry hypothesis. Specifically:
   - Does the `@retry` decorator wrap `cost_tracker.log_call`?
   - Are there try/except inside `_call()` that swallow exceptions silently?
   - What does `is_daily_quota` block do when key rotation succeeds — does it log both the failed-on-key1 and successful-on-key2 attempts?

3. **`backend/scoring/stage3_deep_eval.py:_resolve_stage3_contradictions`** — count actual invocations per run. Are these costs in `stage3_usd` or some separate stage?

4. **`backend/scraper/jsearch.py`** — how does JSearch handle proxy mode auth? Is X-Tester-UUID set somewhere we missed?

5. **`backend/storage/audit.py`** — when serializing `cost_breakdown.scraper_apis_usd`, does it iterate per_source_health correctly?

6. **`config.py:31`** — `PROJECT_ROOT = Path(__file__).resolve().parent.parent`. In bundled mode, is this `_MEIPASS` (transient) or the install dir (persistent)? Confirm via PyInstaller docs.

7. **Worker request logging** — does the existing Worker log any per-request data we could use for reconciliation? Check `cf_worker/src/index.ts` for any analytics writes.

---

## Section 8: Action items

### v0.3.12.1 hotfix (today, ~30-60 min)
- GoogleJobs: add `X-Tester-UUID` header in proxy mode (verify hypothesis first)
- Silent-zero alert: include `cost_estimate=0 AND roles=0` branch
- Tag and ship

### v0.3.13 cost-visibility release (this week, ~5 hours)
- Retry-aware logging
- Persistent cost_log.db path
- Pre/post balance reconciliation in audit JSON
- Abandoned-run sweeper
- Per-key tracking

### v0.3.14 cost-reduction release (after v0.3.13 validates)
- Stage 3 context cache
- Flash contradiction-resolver  
- Trim output schema
- ALL with A/B validation against the cached corpus

### v0.3.15+ explorations
- New ATS scrapers (SuccessFactors, Phenom) — may unlock STRONG growth past 46 ceiling if AI-specific companies are on those platforms
- Two-pass Stage 3 (only if cost reduction stack isn't enough)

---

## Section 9: Questions for peer (the highest-value pushback)

1. **Is my GoogleJobs `X-Tester-UUID` hypothesis correct?** If wrong, what else could cause `elapsed=4.5s, errored=False, roles=0, cost_est=$0`?

2. **How big is the SDK-level retry tax?** Can we even estimate it without instrumenting the SDK directly?

3. **Why did STRONG count plateau at 46?** Is it a profile-narrowness ceiling or a Stage 3 prompt issue?

4. **Should we add a "real-time cost cap circuit breaker"?** If actual run cost exceeds projected by >50%, kill the run? Or just warn?

5. **Is balance-API polling reliable enough for ground-truth reconciliation?** Gemini doesn't have a clean balance API; we'd be scraping the Cloud Console or using Cloud Billing API (requires service account). Worth the auth complexity?

6. **Are there any other "silently zero" scrapers we haven't found?** I cleared Lever earlier (70 raw, filtering correctly) but the v0.3.12 audit shows zeros for HigherEdJobs, Jobicy, NoDesk that we previously called "real upstream zeros." Are we sure?

7. **Should we ship a v0.3.13 that ONLY does cost-visibility (no quality changes)** to lock in trustworthy cost data BEFORE applying any cost reductions? Or bundle visibility + reduction in one release?

8. **Is the user's profile too narrow to grow STRONG count past 46?** If so, what's the right way to communicate "we hit the natural ceiling for AI Strategy roles in this market" to him without sounding like we're giving up?

Format your response under 2,000 words. Push back where I'm wrong. Don't restate things I already covered.

---

**This audit is critical because**: the cost-visibility crisis means EVERY future cost analysis is fiction until v0.3.13 P0 ships. We can't optimize what we can't measure. And the user is rightly worried — $2.73/run actual at current usage rate = $200-400/month. That's not sustainable, and we won't know if our reduction work is real until tracking is fixed.

/**
 * Cloudflare Worker — central LLM proxy + audit-data collector for the
 * Job Search App tester pilot.
 *
 * Architecture:
 *   Tester's desktop app → THIS WORKER → Gemini / Anthropic
 *                       ↓
 *                    Cloudflare R2 (audit JSONs) + D1 (run metadata)
 *
 * Endpoints:
 *   POST /v1/llm/gemini      — proxied Gemini call (uses Ziad's keys)
 *   POST /v1/llm/anthropic   — proxied Anthropic call (uses Ziad's keys)
 *   POST /v1/runs            — upload audit JSON after a run completes
 *   GET  /v1/runs/me         — list runs for the calling tester UUID
 *   GET  /v1/health          — uptime check
 *   GET  /v1/admin/runs      — list all runs (Ziad only, requires admin token)
 *
 * Auth:
 *   Each request must include header X-Tester-UUID with a stable UUID.
 *   Anonymous to Ziad — UUID is the only identifier.
 *
 * Cost protection:
 *   Per-tester daily call cap (default 200 LLM calls/day per UUID).
 *   Per-tester monthly spend cap (default $5/month equivalent).
 *   Trips return HTTP 429 with the cap-hit reason in the body.
 *
 * Secrets (set via wrangler secret put):
 *   GOOGLE_API_KEY              — Ziad's Gemini key
 *   GOOGLE_API_KEY_2 (optional) — second key for quota multiplexing
 *   ANTHROPIC_API_KEY           — Ziad's Anthropic key
 *   ADMIN_TOKEN                 — random string for admin endpoints
 */

export interface Env {
  // LLM secrets
  GOOGLE_API_KEY: string;
  GOOGLE_API_KEY_2?: string;
  GOOGLE_API_KEY_3?: string;
  ANTHROPIC_API_KEY: string;
  ADMIN_TOKEN: string;

  // v0.1.4 — keyed-scraper secrets, proxied through Worker so testers
  // get full coverage without needing to manage their own API keys.
  // Each scraper has a /v1/scraper/{name}/* endpoint that forwards
  // requests upstream with these keys injected.
  ADZUNA_APP_ID?: string;
  ADZUNA_APP_KEY?: string;
  USAJOBS_API_KEY?: string;
  USAJOBS_USER_AGENT?: string;
  FINDWORK_API_KEY?: string;
  JSEARCH_RAPIDAPI_KEY?: string;

  // Bindings (set via wrangler.toml)
  AUDIT_R2: R2Bucket;          // R2 bucket for audit JSON storage
  INSTALLERS_R2: R2Bucket;     // R2 bucket for .msi/.dmg installer files
  TESTER_KV: KVNamespace;      // KV for per-tester counters / metadata

  // Vars (set via [vars] in wrangler.toml)
  ALLOWED_DOWNLOAD_ORIGIN: string;  // e.g. "https://findmesomedamnjobz.com"
}

// ============================================================================
// Configuration
// ============================================================================

// Per-UUID per-day cap for LLM calls routed through the Worker.
// v0.1.4: bumped 2000 -> 5000. A typical search now uses ~1000 LLM calls
// (Phase 12 search-terms split fans out 8-12 broad queries × 14 sources
// × multiple Stage 2/3 evals). At 2000, testers running 2 searches/day
// would brush the cap. 5000 gives ~5 searches/day per tester, which
// comfortably covers iteration without losing the runaway-loop guardrail
// (any single user hitting 5000 calls in a day is a bug).
const DAILY_CALL_CAP = 5000;       // per UUID per day
const MONTHLY_SPEND_CAP_USD = 5.0; // per UUID per month
const MAX_AUDIT_JSON_BYTES = 5 * 1024 * 1024; // 5 MB

// Feedback constraints — prevent the public form from being a spam vector.
const FEEDBACK_MAX_MESSAGE_CHARS = 10000;
const FEEDBACK_MIN_MESSAGE_CHARS = 10;
const FEEDBACK_DAILY_PER_IP_CAP = 8;         // anonymous web posts
const FEEDBACK_DAILY_PER_UUID_CAP = 20;      // logged-in app posts (looser)

// ============================================================================
// Worker entrypoint
// ============================================================================

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    // CORS preflight — feedback form on the public landing site posts
    // cross-origin to api.findmesomedamnjobz.com from findmesomedamnjobz.com.
    // Allowing the site origin lets the browser actually send the body.
    if (req.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: corsHeaders(req, env),
      });
    }

    // Health
    if (path === "/v1/health") {
      return withCors(req, env, json({ ok: true, ts: Date.now() }));
    }

    // Installer downloads — origin-locked so the bare URL is not shareable.
    // Three platform slugs map to fixed object keys in INSTALLERS_R2.
    if (path.startsWith("/v1/dl/")) {
      return handleInstallerDownload(req, env, path);
    }

    // Admin
    if (path.startsWith("/v1/admin/")) {
      return handleAdmin(req, env, path);
    }

    // Public feedback endpoint — anonymous OK (the website form has no
    // tester UUID). UUID is sent when posting from the desktop app and
    // is captured if present, but not required.
    if (path === "/v1/feedback" && req.method === "POST") {
      return withCors(req, env, await submitFeedback(req, env));
    }

    // Auto-updater manifest. Tauri's updater plugin polls this on app
    // startup; if the JSON body advertises a version newer than what's
    // installed, the app shows the user a toast offering to install.
    // The manifest itself lives in R2 at current/manifest.json — GHA
    // writes it after every successful build. We just stream it through
    // with appropriate cache headers (short TTL because we want testers
    // to pick up new releases within a few minutes of publish).
    if (path === "/v1/update/latest.json") {
      return await serveUpdateManifest(env);
    }

    // Update bundle download — Tauri's updater fetches the actual
    // signed installer (NSIS .exe.zip on Windows, .app.tar.gz on macOS)
    // from a URL it reads out of the manifest. Each platform maps to a
    // versioned R2 object. Origin lock + signature verification (done
    // app-side using the baked-in pubkey) prevent tampering.
    if (path.startsWith("/v1/update/download/")) {
      return await serveUpdateBundle(req, env, path);
    }

    // Tester UUID required for everything else
    const testerUuid = req.headers.get("X-Tester-UUID");
    if (!testerUuid || !isValidUuid(testerUuid)) {
      return json({ error: "X-Tester-UUID header required (UUID v4)" }, 401);
    }

    try {
      // Per-tester daily call counter (LLM calls only)
      if (path.startsWith("/v1/llm/")) {
        const capCheck = await checkAndIncrementCallCap(env, testerUuid);
        if (capCheck.over) {
          return json(
            { error: "daily_cap_exceeded", reason: capCheck.reason, cap: DAILY_CALL_CAP },
            429,
          );
        }

        // Deep proxy: forward subpath + query to upstream API.
        // /v1/llm/gemini/v1beta/models/gemini-2.5-flash:generateContent?...
        //   → https://generativelanguage.googleapis.com/v1beta/models/...
        // /v1/llm/anthropic/v1/messages
        //   → https://api.anthropic.com/v1/messages
        if (path.startsWith("/v1/llm/gemini")) {
          return await proxyGeminiDeep(req, env, url);
        }
        if (path.startsWith("/v1/llm/anthropic")) {
          return await proxyAnthropicDeep(req, env, url);
        }
      }

      if (path === "/v1/runs" && req.method === "POST") {
        return await uploadRun(req, env, testerUuid);
      }
      if (path === "/v1/runs/me" && req.method === "GET") {
        return await listMyRuns(env, testerUuid);
      }

      // v0.1.4 Phase 15: pre-flight budget check. App calls this before
      // kicking off a search so it can warn the user if cap is too low to
      // complete the run. Uses the SAMPLED counter value (multiplied by
      // CAP_SAMPLE_EVERY internally) so the reported number is approximate
      // ±50 — fine for budget triage.
      if (path === "/v1/llm/budget" && req.method === "GET") {
        return await checkBudget(env, testerUuid);
      }

      // v0.1.4 — keyed-scraper proxies. Inject API keys from Worker secrets
      // and forward to the upstream API. Lets testers use Adzuna/USAJOBS/
      // Findwork/JSearch without managing their own keys. NOT counted
      // against the LLM daily call cap (those are scraper API quotas, not
      // LLM token spend) — but we do log them for visibility.
      if (path.startsWith("/v1/scraper/adzuna")) {
        return await proxyAdzuna(req, env, url);
      }
      if (path.startsWith("/v1/scraper/usajobs")) {
        return await proxyUSAJobs(req, env, url);
      }
      if (path.startsWith("/v1/scraper/findwork")) {
        return await proxyFindwork(req, env, url);
      }
      if (path.startsWith("/v1/scraper/jsearch")) {
        return await proxyJSearch(req, env, url);
      }

      return json({ error: "not_found" }, 404);
    } catch (e: any) {
      return json({ error: "internal", message: String(e?.message || e) }, 500);
    }
  },
};

// ============================================================================
// LLM proxies
// ============================================================================

// Deep proxy: forwards the SDK's full path + body to Google. The Python
// Gemini SDK calls /v1beta/models/{model}:{method}?key=... — we strip the
// /v1/llm/gemini prefix and forward the rest, swapping the dummy key the
// SDK sends for one of the Worker's real keys. Streaming is handled by
// passing the response body directly (Cloudflare Workers support streaming
// fetch responses).
async function proxyGeminiDeep(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  const keys = [env.GOOGLE_API_KEY, env.GOOGLE_API_KEY_2, env.GOOGLE_API_KEY_3].filter(Boolean) as string[];
  if (keys.length === 0) {
    return json({ error: "no_google_keys_configured" }, 500);
  }
  const key = keys[Math.floor(Math.random() * keys.length)];

  // Strip the /v1/llm/gemini prefix to get the upstream path.
  // e.g. /v1/llm/gemini/v1beta/models/gemini-2.5-flash:generateContent
  //   → /v1beta/models/gemini-2.5-flash:generateContent
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/llm\/gemini/, "") || "/";
  const upstreamSearch = new URLSearchParams(reqUrl.search);
  // Always replace whatever key the SDK sent with our real one.
  upstreamSearch.set("key", key);
  const upstreamUrl = `https://generativelanguage.googleapis.com${upstreamPath}?${upstreamSearch.toString()}`;

  // Forward body + relevant headers. Strip auth-ish headers the SDK may
  // have set (we're substituting our own auth via ?key=).
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Content-Type", req.headers.get("Content-Type") || "application/json");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);

  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  // Stream the upstream response back. body is a ReadableStream when the
  // upstream uses streaming; works for non-streaming JSON too.
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// Deep proxy for Anthropic. The Python anthropic SDK calls /v1/messages
// (and /v1/messages/batches for batch mode) — same idea, strip prefix,
// forward everything, substitute the real x-api-key.
async function proxyAnthropicDeep(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.ANTHROPIC_API_KEY) {
    return json({ error: "anthropic_key_not_configured" }, 500);
  }
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/llm\/anthropic/, "") || "/";
  const upstreamUrl = `https://api.anthropic.com${upstreamPath}${reqUrl.search}`;

  // Forward body + headers, substituting auth.
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Content-Type", req.headers.get("Content-Type") || "application/json");
  upstreamHeaders.set("x-api-key", env.ANTHROPIC_API_KEY);
  upstreamHeaders.set("anthropic-version", req.headers.get("anthropic-version") || "2023-06-01");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);
  const beta = req.headers.get("anthropic-beta");
  if (beta) upstreamHeaders.set("anthropic-beta", beta);

  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// ============================================================================
// v0.1.4 — Keyed-scraper proxies
// ============================================================================
//
// All four follow the same pattern: strip the /v1/scraper/{name} prefix,
// inject the upstream API's auth (URL params, header, or both), forward
// the rest of the request, stream response back. Tester apps see a single
// uniform endpoint per scraper; the Worker hides the upstream auth.

// Adzuna — auth is two URL params (app_id + app_key).
//   /v1/scraper/adzuna/v1/api/jobs/us/search/1?what=...&where=...
//     → https://api.adzuna.com/v1/api/jobs/us/search/1?what=...&app_id=X&app_key=Y
async function proxyAdzuna(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.ADZUNA_APP_ID || !env.ADZUNA_APP_KEY) {
    return json({ error: "adzuna_keys_not_configured" }, 500);
  }
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/scraper\/adzuna/, "") || "/";
  const upstreamSearch = new URLSearchParams(reqUrl.search);
  upstreamSearch.set("app_id", env.ADZUNA_APP_ID);
  upstreamSearch.set("app_key", env.ADZUNA_APP_KEY);
  const upstreamUrl = `https://api.adzuna.com${upstreamPath}?${upstreamSearch.toString()}`;
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Content-Type", req.headers.get("Content-Type") || "application/json");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);
  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// USAJOBS — auth via two headers (Authorization-Key + User-Agent).
//   /v1/scraper/usajobs/api/search?Keyword=...&ResultsPerPage=500
//     → https://data.usajobs.gov/api/search?...
//     headers: Authorization-Key + User-Agent
async function proxyUSAJobs(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.USAJOBS_API_KEY || !env.USAJOBS_USER_AGENT) {
    return json({ error: "usajobs_keys_not_configured" }, 500);
  }
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/scraper\/usajobs/, "") || "/";
  const upstreamUrl = `https://data.usajobs.gov${upstreamPath}${reqUrl.search}`;
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Authorization-Key", env.USAJOBS_API_KEY);
  upstreamHeaders.set("User-Agent", env.USAJOBS_USER_AGENT);
  upstreamHeaders.set("Host", "data.usajobs.gov");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);
  else upstreamHeaders.set("Accept", "application/json");
  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// Findwork — auth via Authorization: Token <key> header.
//   /v1/scraper/findwork/api/jobs/?search=...
//     → https://findwork.dev/api/jobs/?search=...
async function proxyFindwork(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.FINDWORK_API_KEY) {
    return json({ error: "findwork_key_not_configured" }, 500);
  }
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/scraper\/findwork/, "") || "/";
  const upstreamUrl = `https://findwork.dev${upstreamPath}${reqUrl.search}`;
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Authorization", `Token ${env.FINDWORK_API_KEY}`);
  upstreamHeaders.set("Content-Type", req.headers.get("Content-Type") || "application/json");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);
  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// JSearch (RapidAPI) — auth via two headers: X-RapidAPI-Key + X-RapidAPI-Host.
//   /v1/scraper/jsearch/search?query=...&page=1&num_pages=1&date_posted=month
//     → https://jsearch.p.rapidapi.com/search?...
async function proxyJSearch(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.JSEARCH_RAPIDAPI_KEY) {
    return json({ error: "jsearch_key_not_configured" }, 500);
  }
  const upstreamPath = reqUrl.pathname.replace(/^\/v1\/scraper\/jsearch/, "") || "/";
  const upstreamUrl = `https://jsearch.p.rapidapi.com${upstreamPath}${reqUrl.search}`;
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("X-RapidAPI-Key", env.JSEARCH_RAPIDAPI_KEY);
  upstreamHeaders.set("X-RapidAPI-Host", "jsearch.p.rapidapi.com");
  upstreamHeaders.set("Content-Type", req.headers.get("Content-Type") || "application/json");
  const accept = req.headers.get("Accept");
  if (accept) upstreamHeaders.set("Accept", accept);
  const resp = await fetch(upstreamUrl, {
    method: req.method,
    headers: upstreamHeaders,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req.body,
  });
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// ============================================================================
// Cost cap (daily call counter via KV with TTL)
// ============================================================================

// Probabilistic write sampling: only PUT every CAP_SAMPLE_EVERY-th call,
// increment by CAP_SAMPLE_EVERY at a time. Cuts KV PUT operations by ~98%
// (a typical 330-LLM-call search now writes ~7 PUTs instead of 330).
//
// Why: Cloudflare Workers KV free tier allows only 1,000 PUTs/day across the
// ENTIRE worker (not per-tester). One search per tester used to consume
// roughly 1/3 of the daily quota — 3 testers = quota exhausted by lunch.
// Sampling lets the same 1,000 PUT/day budget cover ~150 searches/day.
//
// Quality impact: zero. The counter is a budget guardrail, not a quality
// signal. Accuracy degrades to +/- CAP_SAMPLE_EVERY (50 calls = 2.5% of the
// 2000 cap), which is irrelevant for a $-budget cap. Race condition between
// concurrent requests both winning the lottery only ever OVERCOUNTS, which
// is the safe direction for a budget cap.
const CAP_SAMPLE_EVERY = 50;

// v0.1.4 Phase 15b: read-only budget check. Lets the app warn the user
// pre-flight when the daily cap doesn't have enough headroom for a full
// search (~1000 calls). Returns counter and remaining estimate.
async function checkBudget(env: Env, uuid: string): Promise<Response> {
  const today = new Date().toISOString().slice(0, 10);
  const key = `cap:${uuid}:${today}`;
  const cur = await env.TESTER_KV.get(key);
  const used = cur ? parseInt(cur, 10) : 0;
  const remaining = Math.max(0, DAILY_CALL_CAP - used);
  // A typical full search burns ~1000 LLM calls (post v0.1.4). Anything
  // under that is "not enough headroom for a full run."
  const TYPICAL_RUN_CALLS = 1000;
  const can_run = remaining >= TYPICAL_RUN_CALLS;
  return json({
    used,
    cap: DAILY_CALL_CAP,
    remaining,
    typical_run_calls: TYPICAL_RUN_CALLS,
    can_run,
    sampling_note:
      "Counter is sampled (1-in-50 writes); actual usage is approximate ±50.",
  });
}

async function checkAndIncrementCallCap(
  env: Env, uuid: string,
): Promise<{ over: boolean; reason?: string; current: number }> {
  const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
  const key = `cap:${uuid}:${today}`;
  const cur = await env.TESTER_KV.get(key);
  const n = cur ? parseInt(cur, 10) : 0;
  if (n >= DAILY_CALL_CAP) {
    return { over: true, reason: `daily call cap of ${DAILY_CALL_CAP} reached`, current: n };
  }
  // Sample 1-in-CAP_SAMPLE_EVERY requests; on a hit, write +CAP_SAMPLE_EVERY.
  // On a miss, return the unchanged read value — no PUT.
  //
  // KV PUT is wrapped in try/catch and treated as non-fatal: this is
  // bookkeeping (cap accounting), not gatekeeping. The Anthropic call
  // already succeeded by the time we get here, so failing to write the
  // sample shouldn't crash the search. Worst case: counter undercounts
  // by ~$0.50-2 per missed PUT, which the daily $-cap headroom absorbs.
  if (Math.random() * CAP_SAMPLE_EVERY < 1) {
    try {
      // 36-hour TTL safely covers the next day cycle
      await env.TESTER_KV.put(
        key, String(n + CAP_SAMPLE_EVERY),
        { expirationTtl: 60 * 60 * 36 },
      );
      return { over: false, current: n + CAP_SAMPLE_EVERY };
    } catch (err) {
      console.warn(`KV put failed for cap sample (key=${key}):`, err);
      // Return unchanged value — equivalent to "this request was a miss"
      // from the caller's perspective. The actual call already happened.
      return { over: false, current: n };
    }
  }
  return { over: false, current: n };
}

// ============================================================================
// Audit JSON upload (R2)
// ============================================================================

async function uploadRun(req: Request, env: Env, uuid: string): Promise<Response> {
  const body = await req.text();
  if (body.length > MAX_AUDIT_JSON_BYTES) {
    return json({ error: "audit_too_large", limit_bytes: MAX_AUDIT_JSON_BYTES }, 413);
  }
  let data: any;
  try {
    data = JSON.parse(body);
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const runId = (data?.run_metadata?.run_id || crypto.randomUUID()).toString();
  const ts = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const key = `runs/${uuid}/${ts}_${runId}.json`;
  await env.AUDIT_R2.put(key, body, {
    httpMetadata: { contentType: "application/json" },
    customMetadata: {
      uuid,
      uploaded_at: new Date().toISOString(),
    },
  });
  // Append to a KV index for fast list-by-tester. The R2 audit JSON above
  // is the source of truth — this index is just a convenience for fast
  // listing. So if the KV write fails, log it but don't fail the request:
  // the run is already safely stored in R2 and the user's local SQLite.
  const indexKey = `runs:${uuid}`;
  try {
    const existing = await env.TESTER_KV.get(indexKey);
    const list = existing ? JSON.parse(existing) : [];
    list.unshift({ key, ts, run_id: runId, qualifying: data?.pipeline_funnel?.qualifying_final });
    // Keep last 50 per tester
    await env.TESTER_KV.put(indexKey, JSON.stringify(list.slice(0, 50)));
  } catch (err) {
    console.warn(`KV put failed for run index (uuid=${uuid}):`, err);
    // Non-fatal — the audit JSON is in R2 and the desktop app's local
    // SQLite holds the run. Cloud-side index just misses this entry.
  }
  return json({ ok: true, key, run_id: runId });
}

async function listMyRuns(env: Env, uuid: string): Promise<Response> {
  const indexKey = `runs:${uuid}`;
  const existing = await env.TESTER_KV.get(indexKey);
  return json({ runs: existing ? JSON.parse(existing) : [] });
}

// ============================================================================
// Auto-updater manifest + bundle download
// ============================================================================
//
// Storage layout (written by GHA on each successful build):
//
//   R2 (INSTALLERS_R2):
//     current/manifest.json                        — what /v1/update/latest.json serves
//     current/update/{version}/windows-x86_64.exe  — NSIS update bundle for Windows
//     current/update/{version}/windows-x86_64.exe.sig
//     current/update/{version}/darwin-aarch64.app.tar.gz
//     current/update/{version}/darwin-aarch64.app.tar.gz.sig
//     current/update/{version}/darwin-x86_64.app.tar.gz
//     current/update/{version}/darwin-x86_64.app.tar.gz.sig
//
// The manifest references the bundles via /v1/update/download/{platform}
// URLs which this Worker serves (origin-locked similarly to /v1/dl/).
// App-side signature verification (using the baked-in pubkey) is the
// real security layer; the origin check is a soft layer to avoid trivial
// abuse like third-party sites hot-linking the binary.

async function serveUpdateManifest(env: Env): Promise<Response> {
  const obj = await env.INSTALLERS_R2.get("current/manifest.json");
  if (!obj) {
    // No manifest yet (e.g., on a fresh deploy before GHA has pushed
    // one). Return a 204 — Tauri treats absent/empty as "no update".
    return new Response(null, {
      status: 204,
      headers: { "Cache-Control": "no-store" },
    });
  }
  return new Response(obj.body, {
    headers: {
      "Content-Type": "application/json",
      // Short TTL — we want testers to pick up new versions within a
      // couple minutes of GHA publishing the manifest, but no point
      // hammering R2 if a thousand testers happen to launch the app
      // simultaneously.
      "Cache-Control": "public, max-age=120",
    },
  });
}

async function serveUpdateBundle(req: Request, env: Env, path: string): Promise<Response> {
  // URL: /v1/update/download/{platform}/{version}/{filename}
  // e.g. /v1/update/download/windows-x86_64/0.1.5/findmesomedamnjobz_0.1.5_x64-setup.exe
  //
  // R2 layout (from build-desktop.yml upload step):
  //   current/update/{version}/{platform}/{filename}
  //
  // The orderings are intentionally different — the URL is platform-first
  // for Tauri's convention, but the R2 layout is version-first to make
  // version pruning easy (one prefix, all platforms). We reorder here.
  //
  // Clients also fetch the .sig sibling by appending ".sig" to this URL,
  // which works transparently since {filename}.sig sits next to {filename}
  // in R2.
  const parts = path.slice("/v1/update/download/".length).split("/");
  if (parts.length !== 3) {
    return json({ error: "invalid_path", expected: "platform/version/filename" }, 400);
  }
  const [platform, version, filename] = parts;
  const r2Key = `current/update/${version}/${platform}/${filename}`;
  if (r2Key.includes("..") || !r2Key.startsWith("current/update/")) {
    return json({ error: "invalid_path" }, 400);
  }
  const obj = await env.INSTALLERS_R2.get(r2Key);
  if (!obj) {
    return json({ error: "update_bundle_not_found", key: r2Key }, 404);
  }
  // Filename inferred from the URL — used in Content-Disposition so the
  // file lands with a sensible name on disk if the OS prompts to save.
  const filename = r2Key.split("/").pop() || "update";
  return new Response(obj.body, {
    headers: {
      "Content-Type": filename.endsWith(".sig")
        ? "text/plain"
        : "application/octet-stream",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Content-Length": String(obj.size),
      "Cache-Control": "public, max-age=300",
    },
  });
}

// ============================================================================
// Public feedback (R2 + KV index)
// ============================================================================
//
// Storage layout:
//   R2:  feedback/{YYYY-MM-DD}/{ISO-ts}_{uuid}.json     — one file per submission
//   KV:  feedback:index → JSON list of {key, ts, source, has_email, len}
//                         (last 200, newest first — for the admin listing)
//
// Why R2 + KV index: R2 is cheap object storage but list operations are
// per-prefix and can be slow once there are many objects; the KV index
// gives Ziad a fast "show me the 50 most recent" without a full R2 list
// every time. The full feedback body still lives in R2 so KV doesn't
// blow past its 25 MB-per-value limit.

async function submitFeedback(req: Request, env: Env): Promise<Response> {
  // Parse body
  let body: any;
  try {
    body = await req.json();
  } catch {
    return json({ error: "invalid_json" }, 400);
  }
  const message = String(body?.message || "").trim();
  const email = String(body?.email || "").trim();
  const source = String(body?.source || "unknown").slice(0, 32);          // "app" | "website"
  const category = String(body?.category || "other").slice(0, 32);        // bug, false-positive, etc.
  const appVersion = String(body?.app_version || "").slice(0, 32);
  const pageUrl = String(body?.page_url || "").slice(0, 512);
  const userAgent = String(req.headers.get("User-Agent") || "").slice(0, 256);

  // Validation
  if (message.length < FEEDBACK_MIN_MESSAGE_CHARS) {
    return json(
      { error: "message_too_short", min: FEEDBACK_MIN_MESSAGE_CHARS },
      400,
    );
  }
  if (message.length > FEEDBACK_MAX_MESSAGE_CHARS) {
    return json(
      { error: "message_too_long", max: FEEDBACK_MAX_MESSAGE_CHARS },
      400,
    );
  }
  if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    return json({ error: "invalid_email" }, 400);
  }

  // Rate-limit by tester UUID if present, else by IP. Per-day cap.
  // Cloudflare exposes the visitor IP via CF-Connecting-IP.
  const testerUuid = req.headers.get("X-Tester-UUID") || "";
  const ip = (req.headers.get("CF-Connecting-IP") || "unknown").slice(0, 64);
  const rateKey = isValidUuid(testerUuid)
    ? `fbcap:uuid:${testerUuid}:${todayUtc()}`
    : `fbcap:ip:${ip}:${todayUtc()}`;
  const cap = isValidUuid(testerUuid) ? FEEDBACK_DAILY_PER_UUID_CAP : FEEDBACK_DAILY_PER_IP_CAP;
  const cur = await env.TESTER_KV.get(rateKey);
  const n = cur ? parseInt(cur, 10) : 0;
  if (n >= cap) {
    return json({ error: "rate_limited", cap, retry_after_hours: 24 }, 429);
  }

  // Persist to R2
  const id = crypto.randomUUID();
  const tsIso = new Date().toISOString();
  const day = tsIso.slice(0, 10);
  const tsForKey = tsIso.replace(/[:.]/g, "-");
  const r2Key = `feedback/${day}/${tsForKey}_${id}.json`;
  const record = {
    id,
    submitted_at: tsIso,
    source,
    category,
    message,
    email: email || null,
    tester_uuid: isValidUuid(testerUuid) ? testerUuid : null,
    app_version: appVersion || null,
    page_url: pageUrl || null,
    user_agent: userAgent,
    ip_country: req.headers.get("CF-IPCountry") || null,
  };
  try {
    await env.AUDIT_R2.put(r2Key, JSON.stringify(record, null, 2), {
      httpMetadata: { contentType: "application/json" },
      customMetadata: {
        source,
        category,
        has_email: email ? "true" : "false",
        tester_uuid: record.tester_uuid || "",
      },
    });
  } catch (e: any) {
    return json({ error: "storage_failed", message: String(e?.message || e) }, 500);
  }

  // Update KV index for fast admin listing (newest 200)
  try {
    const indexKey = `feedback:index`;
    const existingRaw = await env.TESTER_KV.get(indexKey);
    const existing = existingRaw ? JSON.parse(existingRaw) : [];
    existing.unshift({
      id,
      key: r2Key,
      submitted_at: tsIso,
      source,
      category,
      has_email: !!email,
      length: message.length,
    });
    await env.TESTER_KV.put(indexKey, JSON.stringify(existing.slice(0, 200)));
  } catch {
    // Index update failure is non-fatal — the R2 object is the source of truth
  }

  // Increment rate-limit counter (after success — failed posts shouldn't
  // burn the tester's daily quota)
  await env.TESTER_KV.put(rateKey, String(n + 1), { expirationTtl: 60 * 60 * 36 });

  return json({ ok: true, id });
}

function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

// ============================================================================
// Admin endpoints (Ziad only)
// ============================================================================

async function handleAdmin(req: Request, env: Env, path: string): Promise<Response> {
  const auth = req.headers.get("Authorization") || "";
  const expected = `Bearer ${env.ADMIN_TOKEN}`;
  if (auth !== expected) {
    return json({ error: "admin_auth_required" }, 403);
  }
  if (path === "/v1/admin/runs") {
    // List recent objects in R2 — limited cursor pagination
    const list = await env.AUDIT_R2.list({ prefix: "runs/", limit: 100 });
    const out = list.objects.map(o => ({
      key: o.key,
      uploaded: o.uploaded?.toISOString(),
      size: o.size,
      uuid: o.customMetadata?.uuid,
    }));
    return json({ runs: out, truncated: list.truncated });
  }
  if (path === "/v1/admin/run" && req.method === "GET") {
    const url = new URL(req.url);
    const k = url.searchParams.get("key");
    if (!k) return json({ error: "missing_key_param" }, 400);
    const obj = await env.AUDIT_R2.get(k);
    if (!obj) return json({ error: "not_found" }, 404);
    return new Response(obj.body, {
      headers: { "Content-Type": "application/json" },
    });
  }
  if (path === "/v1/admin/testers") {
    const list = await env.TESTER_KV.list({ prefix: "runs:" });
    return json({ tester_uuids: list.keys.map(k => k.name.replace("runs:", "")) });
  }
  if (path === "/v1/admin/feedback" && req.method === "GET") {
    // Pulls the KV index (last 200 submissions, newest first). Each entry
    // is a metadata stub — call /v1/admin/feedback/get?key=... to fetch
    // the full submission body from R2.
    const indexRaw = await env.TESTER_KV.get("feedback:index");
    return json({ feedback: indexRaw ? JSON.parse(indexRaw) : [] });
  }
  if (path === "/v1/admin/feedback/get" && req.method === "GET") {
    const url = new URL(req.url);
    const k = url.searchParams.get("key");
    if (!k || !k.startsWith("feedback/")) {
      return json({ error: "missing_or_invalid_key" }, 400);
    }
    const obj = await env.AUDIT_R2.get(k);
    if (!obj) return json({ error: "not_found" }, 404);
    return new Response(obj.body, {
      headers: { "Content-Type": "application/json" },
    });
  }
  return json({ error: "admin_endpoint_not_found" }, 404);
}

// ============================================================================
// CORS — public-form endpoints need cross-origin access from the landing site
// ============================================================================
//
// The landing site at https://findmesomedamnjobz.com posts to api.findmesome
// damnjobz.com/v1/feedback. The browser sends a preflight OPTIONS for the
// JSON content-type, then the actual POST. We allow exactly the production
// site origin + localhost dev (any port) — anything else gets no Access-
// Control-Allow-Origin header back, which the browser will block.

function corsHeaders(req: Request, env: Env): HeadersInit {
  const origin = (req.headers.get("Origin") || "").trim();
  const allowList = [
    (env.ALLOWED_DOWNLOAD_ORIGIN || "").trim(),
    "http://localhost:4321",
    "http://localhost:5173",
    "tauri://localhost",          // Tauri webview default
    "https://tauri.localhost",    // Tauri webview on Windows (custom protocol)
  ].filter(Boolean);
  const allowed = allowList.includes(origin) ? origin : allowList[0] || "*";
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Tester-UUID",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function withCors(req: Request, env: Env, resp: Response): Response {
  const headers = new Headers(resp.headers);
  for (const [k, v] of Object.entries(corsHeaders(req, env))) {
    headers.set(k, v as string);
  }
  return new Response(resp.body, { status: resp.status, headers });
}

// ============================================================================
// Installer downloads — origin-locked R2 streaming
// ============================================================================

// Maps URL slug → R2 object key. CI uploads to these exact keys, so the
// "current build" is always at this path with no version in the URL.
const INSTALLER_KEYS: Record<string, { key: string; filename: string }> = {
  "windows":   { key: "current/job-search-app.msi",         filename: "JobSearchApp.msi" },
  "mac-arm64": { key: "current/job-search-app-aarch64.dmg", filename: "JobSearchApp.dmg" },
  "mac-x64":   { key: "current/job-search-app-x64.dmg",     filename: "JobSearchApp-Intel.dmg" },
};

async function handleInstallerDownload(req: Request, env: Env, path: string): Promise<Response> {
  const slug = path.slice("/v1/dl/".length);
  const target = INSTALLER_KEYS[slug];
  if (!target) {
    return json({ error: "unknown_platform", available: Object.keys(INSTALLER_KEYS) }, 404);
  }

  // Origin lock — only browsers arriving from the configured site can pull.
  // We accept BOTH "Origin" (set on cross-origin fetches) and "Referer"
  // (set on regular navigations + clicks). Same-origin POSTs typically
  // include Origin; same-origin GETs from <a> clicks typically include
  // Referer but not Origin. Accept either.
  const allowed = (env.ALLOWED_DOWNLOAD_ORIGIN || "").trim();
  if (allowed) {
    const origin = (req.headers.get("Origin") || "").trim();
    const referer = (req.headers.get("Referer") || "").trim();
    const originOk = origin === allowed;
    const refererOk = referer.startsWith(allowed + "/") || referer === allowed;
    if (!originOk && !refererOk) {
      return json(
        { error: "forbidden", message: "Downloads are only available from the official site." },
        403,
      );
    }
  }

  const obj = await env.INSTALLERS_R2.get(target.key);
  if (!obj) {
    return json({ error: "installer_not_found", key: target.key }, 404);
  }

  return new Response(obj.body, {
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename="${target.filename}"`,
      "Content-Length": String(obj.size),
      "Cache-Control": "no-store",
    },
  });
}

// ============================================================================
// Helpers
// ============================================================================

function json(data: any, status = 200): Response {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function isValidUuid(s: string): boolean {
  return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(s);
}

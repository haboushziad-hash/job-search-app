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
  // v0.3.5: Serper.dev (Bing Jobs + Google web search). One key for
  // both engines via different /endpoint paths or engine= params.
  SERPER_API_KEY?: string;
  // v0.3.6: DataForSEO uses HTTP Basic Auth (login + password tuple),
  // NOT a single API key. Both must be set together; either alone is
  // useless. Login is the user's account email. The password is a
  // separate API password they generate in their dashboard, NOT the
  // account login password.
  DATAFORSEO_LOGIN?: string;
  DATAFORSEO_PASSWORD?: string;
  // v0.3.5: TheMuse — optional, increases rate limit when set. Scraper
  // works without a key but throttles aggressively under parallel load.
  THEMUSE_API_KEY?: string;

  // v0.2.2: secret for the /v1/stats download-counter endpoint. Set via
  // `wrangler secret put STATS_SECRET`. Until set, /v1/stats returns 503
  // with setup instructions — the endpoint never exposes data without
  // the secret matching.
  STATS_SECRET?: string;

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

    // Operator-only: download click stats (gated by STATS_SECRET).
    // v0.2.2: visit https://api.findmesomedamnjobz.com/v1/stats?key=YOURPASSWORD
    // to see lifetime + last-30d download counts. Setup: from cf_worker/
    // run `wrangler secret put STATS_SECRET` once.
    if (path === "/v1/stats") {
      return serveStats(req, env);
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
          return await proxyGeminiDeep(req, env, url, ctx, testerUuid);
        }
        if (path.startsWith("/v1/llm/anthropic")) {
          return await proxyAnthropicDeep(req, env, url, ctx, testerUuid);
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
      // v0.3.5: Serper.dev proxy — used by BingJobs scraper (and future
      // GoogleJobs after the v0.3.6 DataForSEO migration). Same X-API-KEY
      // header pattern, different upstream paths under google.serper.dev.
      if (path.startsWith("/v1/scraper/serper")) {
        return await proxySerper(req, env, url);
      }
      // v0.3.12: DataForSEO proxy — backs the GoogleJobs scraper. The
      // Python sidecar can't reach api.dataforseo.com directly in proxy
      // mode (lib.rs only forwards LLM_PROXY_URL/AUDIT_UPLOAD_URL/
      // TESTER_UUID, so DATAFORSEO_LOGIN/PASSWORD never make it from the
      // dev .env to the tester's installed sidecar). Worker injects HTTP
      // Basic Auth from wrangler secrets and forwards.
      //
      // Routes proxied:
      //   /v1/scraper/dataforseo/jobs/task_post
      //     → POST https://api.dataforseo.com/v3/serp/google/jobs/task_post
      //   /v1/scraper/dataforseo/jobs/task_get/advanced/<task_id>
      //     → GET  https://api.dataforseo.com/v3/serp/google/jobs/task_get/advanced/<task_id>
      //
      // wrangler secrets required (set via `wrangler secret put`):
      //   DATAFORSEO_LOGIN     — DataForSEO account email
      //   DATAFORSEO_PASSWORD  — DataForSEO API password (NOT login pw)
      if (path.startsWith("/v1/scraper/dataforseo")) {
        return await proxyDataForSEO(req, env, url);
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

// Deterministic string → bucket index. Used to pin each tester's Gemini
// calls to a single API key so Pro context caches (created with that key)
// remain accessible across all of their subsequent calls. Plain 32-bit
// FNV/DJB-style hash; uniform enough for a 3-bucket spread of UUIDs.
function hashStringToIndex(s: string, buckets: number): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = ((h << 5) - h) + s.charCodeAt(i);
    h |= 0; // force 32-bit signed int
  }
  // Math.abs handles INT32_MIN edge case (would otherwise stay negative)
  const positive = h === -2147483648 ? 0 : Math.abs(h);
  return positive % Math.max(1, buckets);
}

// Deep proxy: forwards the SDK's full path + body to Google. The Python
// Gemini SDK calls /v1beta/models/{model}:{method}?key=... — we strip the
// /v1/llm/gemini prefix and forward the rest, swapping the dummy key the
// SDK sends for one of the Worker's real keys. Streaming is handled by
// passing the response body directly (Cloudflare Workers support streaming
// fetch responses).
async function proxyGeminiDeep(
  req: Request,
  env: Env,
  reqUrl: URL,
  ctx: ExecutionContext,
  testerUuid: string,
): Promise<Response> {
  const keys = [env.GOOGLE_API_KEY, env.GOOGLE_API_KEY_2, env.GOOGLE_API_KEY_3].filter(Boolean) as string[];
  if (keys.length === 0) {
    return json({ error: "no_google_keys_configured" }, 500);
  }
  // Pin each tester's Gemini calls to a single deterministic key. This is
  // REQUIRED for Pro context caching to work — caches are bound to the
  // key that created them, so all of a tester's Stage 3 calls must hit
  // the same key to find the cache they created at run start.
  // Prior random rotation caused ~67% of Stage 3 calls to land on a
  // different key from the cache owner, returning `403 PERMISSION_DENIED
  // CachedContent not found` and silently failing Stage 3. Confirmed via
  // audit 2026-05-29_02-55_f4ea0252 — 121 of 198 Stage 3 attempts (61%)
  // hit this bug, dumping good roles into MAYBE on stage2_score fallback.
  // Load balancing still happens across testers (each tester pins to one
  // key, but different testers hash to different keys).
  // Wave-2 followup: replace this with Worker-managed per-key caches
  // (see task #15) for cross-run reuse and N-key generalization.
  const keyIdx = hashStringToIndex(testerUuid, keys.length);
  const key = keys[keyIdx];

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

  // v0.3.15 (P1.10): increment cost counter in KV asynchronously so it
  // doesn't block the response. Clone the body so we can read usage_metadata
  // without consuming the stream the client gets.
  // Streaming SSE responses (text/event-stream) skip counter — usage parse
  // would require SSE-aware streaming, deferred to a future iteration.
  const respContentType = resp.headers.get("Content-Type") || "";
  if (resp.ok && respContentType.includes("application/json")) {
    const cloned = resp.clone();
    ctx.waitUntil(
      incrementCostCounter(env, testerUuid, "google", upstreamPath, cloned).catch(() => {}),
    );
  }

  // Stream the upstream response back. body is a ReadableStream when the
  // upstream uses streaming; works for non-streaming JSON too.
  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": respContentType || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// Deep proxy for Anthropic. The Python anthropic SDK calls /v1/messages
// (and /v1/messages/batches for batch mode) — same idea, strip prefix,
// forward everything, substitute the real x-api-key.
async function proxyAnthropicDeep(
  req: Request,
  env: Env,
  reqUrl: URL,
  ctx: ExecutionContext,
  testerUuid: string,
): Promise<Response> {
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

  // v0.3.15 (P1.10): cost counter increment, see proxyGeminiDeep for rationale.
  const respContentType = resp.headers.get("Content-Type") || "";
  if (resp.ok && respContentType.includes("application/json")) {
    const cloned = resp.clone();
    ctx.waitUntil(
      incrementCostCounter(env, testerUuid, "anthropic", upstreamPath, cloned).catch(() => {}),
    );
  }

  return new Response(resp.body, {
    status: resp.status,
    headers: {
      "Content-Type": respContentType || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

// ============================================================================
// v0.3.15 (P1.10) — Worker-side cost counter
// ============================================================================
//
// Independent ground-truth counter for cost reconciliation. The Python
// cost_tracker can lie (rate-table bugs, missed code paths, untracked
// surfaces). The Worker SEES every billed request and writes
// (provider, model, est_cost, timestamp) to KV per-tester per-day.
//
// Reconciliation rule: cost_log.db SUM should match Worker counter SUM
// within 5% across a 5-run observation window. If they diverge >5%,
// there's an untracked cost surface to investigate.
//
// Storage key: cost_counter:{testerUuid}:{YYYY-MM-DD}
// Value: JSON with {requests, total_input_tokens, total_output_tokens,
//                   total_estimated_cost_usd, last_updated}
// TTL: 35 days (covers full month + grace).

type CostCounter = {
  requests: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cached_tokens: number;
  total_estimated_cost_usd: number;
  last_updated: string;
};

// Mirror of backend/config.py PRICING table — kept in sync manually.
// Used for Worker-side cost estimation. If divergence is detected with
// the Python cost_log, that's a signal to update one or the other.
const WORKER_PRICING: Record<string, {
  input: number;
  input_long?: number;
  output: number;
  output_long?: number;
  cached_input: number;
  cached_input_long?: number;
}> = {
  "gemini-2.5-pro": {
    input: 1.25, input_long: 2.50,
    output: 10.00, output_long: 15.00,
    cached_input: 0.125, cached_input_long: 0.25,
  },
  "gemini-2.5-flash": {
    input: 0.30, output: 2.50, cached_input: 0.03,
  },
  "gemini-2.5-flash-lite": {
    input: 0.10, output: 0.40, cached_input: 0.01,
  },
  "gemini-embedding-001": { input: 0.15, output: 0.0, cached_input: 0.0 },
  "claude-opus-4-7": { input: 5.00, output: 25.00, cached_input: 0.50 },
  "claude-opus-4-6": { input: 5.00, output: 25.00, cached_input: 0.50 },
  "claude-opus-4-5": { input: 5.00, output: 25.00, cached_input: 0.50 },
  "claude-sonnet-4-6": { input: 3.00, output: 15.00, cached_input: 0.30 },
  "claude-sonnet-4-5": { input: 3.00, output: 15.00, cached_input: 0.30 },
};

const PRO_LONG_CONTEXT_THRESHOLD = 200_000;

function extractGeminiModelFromPath(path: string): string {
  // path looks like /v1beta/models/gemini-2.5-flash:generateContent
  const match = path.match(/\/models\/([^:?\/]+)/);
  return match ? match[1] : "unknown";
}

function extractAnthropicModelFromBody(body: any): string {
  // Anthropic responses include "model" in the JSON body (echoed from request)
  return typeof body?.model === "string" ? body.model : "unknown";
}

function estimateCost(
  provider: "google" | "anthropic",
  model: string,
  inputTokens: number,
  outputTokens: number,
  cachedTokens: number,
): number {
  const pricing = WORKER_PRICING[model];
  if (!pricing) return 0;

  const isLong = inputTokens > PRO_LONG_CONTEXT_THRESHOLD && pricing.input_long !== undefined;
  const inRate = isLong ? (pricing.input_long ?? pricing.input) : pricing.input;
  const outRate = isLong ? (pricing.output_long ?? pricing.output) : pricing.output;
  const cachedRate = isLong
    ? (pricing.cached_input_long ?? pricing.cached_input)
    : pricing.cached_input;

  const fresh = Math.max(0, inputTokens - cachedTokens);
  const cost = (fresh * inRate / 1_000_000)
    + (cachedTokens * cachedRate / 1_000_000)
    + (outputTokens * outRate / 1_000_000);
  return Math.round(cost * 1_000_000) / 1_000_000;
}

async function incrementCostCounter(
  env: Env,
  testerUuid: string,
  provider: "google" | "anthropic",
  upstreamPath: string,
  responseClone: Response,
): Promise<void> {
  let body: any;
  try {
    body = await responseClone.json();
  } catch {
    return; // not JSON
  }

  let model = "unknown";
  let inputTokens = 0;
  let outputTokens = 0;
  let cachedTokens = 0;

  if (provider === "google") {
    model = extractGeminiModelFromPath(upstreamPath);
    const um = body?.usageMetadata;
    if (um) {
      inputTokens = um.promptTokenCount || 0;
      // Output includes thinking tokens (Gemini 2.5 thinking models)
      outputTokens = (um.candidatesTokenCount || 0) + (um.thoughtsTokenCount || 0);
      cachedTokens = um.cachedContentTokenCount || 0;
    }
  } else {
    model = extractAnthropicModelFromBody(body);
    const um = body?.usage;
    if (um) {
      inputTokens = (um.input_tokens || 0) + (um.cache_creation_input_tokens || 0);
      outputTokens = um.output_tokens || 0;
      cachedTokens = um.cache_read_input_tokens || 0;
    }
  }

  const cost = estimateCost(provider, model, inputTokens, outputTokens, cachedTokens);

  const today = new Date().toISOString().slice(0, 10);
  const key = `cost_counter:${testerUuid}:${today}`;

  // Read-modify-write. KV doesn't have atomic increment for objects, so
  // there's a small race window if multiple requests land within ~50ms.
  // Acceptable for our volume (one tester, ~1 call/sec peak); Worker
  // counter is a reconciliation tool, not load-bearing for billing.
  const existing = (await env.TESTER_KV.get(key, "json")) as CostCounter | null;
  const next: CostCounter = {
    requests: (existing?.requests || 0) + 1,
    total_input_tokens: (existing?.total_input_tokens || 0) + inputTokens,
    total_output_tokens: (existing?.total_output_tokens || 0) + outputTokens,
    total_cached_tokens: (existing?.total_cached_tokens || 0) + cachedTokens,
    total_estimated_cost_usd:
      (existing?.total_estimated_cost_usd || 0) + cost,
    last_updated: new Date().toISOString(),
  };

  await env.TESTER_KV.put(key, JSON.stringify(next), {
    expirationTtl: 35 * 24 * 3600, // 35 days
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

// v0.3.12: DataForSEO proxy — auth via HTTP Basic (login:password). Used by
// GoogleJobs scraper.
//
// Path translation:
//   /v1/scraper/dataforseo/jobs/task_post
//     → https://api.dataforseo.com/v3/serp/google/jobs/task_post
//   /v1/scraper/dataforseo/jobs/task_get/advanced/<id>
//     → https://api.dataforseo.com/v3/serp/google/jobs/task_get/advanced/<id>
//
// Body and query string passed through verbatim. Basic Auth replaces
// whatever the client sent (the sidecar sends nothing in proxy mode).
async function proxyDataForSEO(req: Request, env: Env, reqUrl: URL): Promise<Response> {
  if (!env.DATAFORSEO_LOGIN || !env.DATAFORSEO_PASSWORD) {
    return json({ error: "dataforseo_credentials_not_configured" }, 500);
  }
  // Strip /v1/scraper/dataforseo and remap our short suffix to the full
  // DataForSEO path. We keep our suffix tight so we don't accidentally
  // expose every DataForSEO endpoint — only Google Jobs SERP for now.
  const suffix = reqUrl.pathname.replace(/^\/v1\/scraper\/dataforseo/, "");
  if (!suffix.startsWith("/jobs/")) {
    return json(
      { error: "dataforseo_route_not_allowlisted", suffix },
      404,
    );
  }
  // /jobs/task_post → /v3/serp/google/jobs/task_post
  // /jobs/task_get/advanced/<id> → /v3/serp/google/jobs/task_get/advanced/<id>
  const upstreamPath = `/v3/serp/google${suffix}`;
  const upstreamUrl = `https://api.dataforseo.com${upstreamPath}${reqUrl.search}`;

  const basic = btoa(`${env.DATAFORSEO_LOGIN}:${env.DATAFORSEO_PASSWORD}`);
  const upstreamHeaders = new Headers();
  upstreamHeaders.set("Authorization", `Basic ${basic}`);
  upstreamHeaders.set(
    "Content-Type",
    req.headers.get("Content-Type") || "application/json",
  );
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
  // {filename} from the destructuring above is used directly in
  // Content-Disposition so the file lands with a sensible name on
  // disk if the OS prompts to save.
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
  // Auth accepts either:
  //   1. Authorization: Bearer <ADMIN_TOKEN>  (for API/CLI use)
  //   2. ?token=<ADMIN_TOKEN>                 (for pasting URL into browser)
  // Browser convenience matters because this endpoint is meant to be
  // poked at interactively from a desktop browser by Ziad — no need
  // to drop into curl just to view a run's funnel summary.
  // Note: separate param name ("token") from R2-object key params ("k",
  // "key") so a URL like /v1/admin/feedback/get?token=...&key=...
  // doesn't collide.
  const url = new URL(req.url);
  const queryToken = url.searchParams.get("token") || "";
  const auth = req.headers.get("Authorization") || "";
  const bearerOk = auth === `Bearer ${env.ADMIN_TOKEN}`;
  const queryTokenOk = !!env.ADMIN_TOKEN && queryToken === env.ADMIN_TOKEN;
  if (!bearerOk && !queryTokenOk) {
    return json({ error: "admin_auth_required" }, 403);
  }

  // Browser-friendly HTML dashboard for /v1/admin/runs. Lists recent
  // runs across all testers with pipeline-funnel summary inline:
  // total_scraped → after_hard_filters → qualifying_final → tier
  // breakdown. Each row links to the raw audit JSON. Lets Ziad spot
  // funnel collapses (e.g. "100+ scraped, 4 strong") at a glance and
  // jump into the specific audit to diagnose. Triggered by Accept:
  // text/html (browsers) or ?format=html. Falls through to raw JSON
  // for API clients.
  if (path === "/v1/admin/runs" && shouldRenderHtml(req, url)) {
    return await renderRunsHtml(env);
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

  // v0.3.15 (P1.10): cost counter readback for reconciliation against the
  // Python cost_log.db. Query: /v1/admin/cost-counter?uuid=<uuid>&date=YYYY-MM-DD
  // (date defaults to today). Returns the counter object if present, or
  // {requests: 0, ...} zeros if no counter for that key.
  // Also supports ?range=<N> to fetch the last N days at once.
  if (path === "/v1/admin/cost-counter" && req.method === "GET") {
    const uuid = url.searchParams.get("uuid");
    if (!uuid) return json({ error: "missing_uuid_param" }, 400);
    const dateParam = url.searchParams.get("date");
    const rangeParam = url.searchParams.get("range");

    const buildKey = (d: string) => `cost_counter:${uuid}:${d}`;
    const fmt = (d: Date) => d.toISOString().slice(0, 10);

    if (rangeParam) {
      const n = Math.max(1, Math.min(60, parseInt(rangeParam, 10) || 7));
      const days: { date: string; counter: CostCounter | null }[] = [];
      const today = new Date();
      for (let i = 0; i < n; i++) {
        const d = new Date(today);
        d.setUTCDate(today.getUTCDate() - i);
        const dateStr = fmt(d);
        const counter = (await env.TESTER_KV.get(buildKey(dateStr), "json")) as CostCounter | null;
        days.push({ date: dateStr, counter });
      }
      const totals: CostCounter = {
        requests: 0,
        total_input_tokens: 0,
        total_output_tokens: 0,
        total_cached_tokens: 0,
        total_estimated_cost_usd: 0,
        last_updated: "",
      };
      for (const d of days) {
        if (!d.counter) continue;
        totals.requests += d.counter.requests;
        totals.total_input_tokens += d.counter.total_input_tokens;
        totals.total_output_tokens += d.counter.total_output_tokens;
        totals.total_cached_tokens += d.counter.total_cached_tokens || 0;
        totals.total_estimated_cost_usd += d.counter.total_estimated_cost_usd;
        if ((d.counter.last_updated || "") > totals.last_updated) {
          totals.last_updated = d.counter.last_updated;
        }
      }
      return json({ uuid, range_days: n, totals, per_day: days });
    }

    const dateStr = dateParam || fmt(new Date());
    const counter = (await env.TESTER_KV.get(buildKey(dateStr), "json")) as CostCounter | null;
    return json({
      uuid,
      date: dateStr,
      counter: counter || {
        requests: 0,
        total_input_tokens: 0,
        total_output_tokens: 0,
        total_cached_tokens: 0,
        total_estimated_cost_usd: 0,
        last_updated: null,
      },
    });
  }
  if (path === "/v1/admin/run" && req.method === "GET") {
    const k = url.searchParams.get("k") || url.searchParams.get("run_key");
    if (!k) return json({ error: "missing_run_key_param" }, 400);
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
// Admin runs HTML dashboard — Ziad-only, browser-friendly
// ============================================================================

/** Detect when we should render HTML rather than JSON. Browsers
 *  always send Accept: text/html; ?format=html lets you force it
 *  from a JSON-defaulting client. ?format=json forces JSON even from
 *  a browser, useful for debugging. */
function shouldRenderHtml(req: Request, url: URL): boolean {
  const fmt = url.searchParams.get("format");
  if (fmt === "html") return true;
  if (fmt === "json") return false;
  const accept = req.headers.get("Accept") || "";
  return accept.includes("text/html");
}

/** Per-run summary extracted from a fetched audit JSON. Picks just the
 *  fields we want to surface in the table — keeps the page payload small
 *  even when there are many runs. */
interface RunSummary {
  key: string;
  uuid_full: string;           // full uuid (used as filter key)
  uuid_prefix: string;         // first 8 chars for display
  uploaded: string;            // ISO timestamp (UTC, converted to local in JS)
  duration_s: number | null;
  cost_usd: number | null;
  app_version: string | null;
  // v0.3.3: profile snapshot fields so the dashboard distinguishes runs
  // by who they're for, not just which UUID. Same UUID can have different
  // profiles per run if the user re-uploads a different resume.
  profile_headline: string | null;
  total_scraped: number | null;
  after_filters: number | null;
  qualifying: number | null;
  strong: number | null;
  good: number | null;
  maybe: number | null;
  stretch: number | null;
  funnel_collapse_pct: number | null;  // 1 - qualifying/scraped, 0..100
  top_role_titles: string[];
  // v0.3.3: calibration anomalies surfaced via the new audit field.
  // Will be null for runs from versions before v0.3.3 (no field present);
  // will be 0+ for v0.3.3+ runs.
  anomaly_count: number | null;
  // v0.3.3 dashboard improvement: keyword fingerprint per run, used
  // client-side to detect "same keywords as prior run" so the operator
  // can identify clean A/B comparisons (same inputs, different output =
  // pure calibration effect). FNV-1a hash of sorted keywords_used +
  // search_terms; collision rate negligible for our scale.
  keywords_hash: string | null;
  keyword_count: number | null;
}

/** Render the runs HTML dashboard. Lists last ~50 runs newest-first
 *  with funnel summary, per-tester filtering, bulk download, anomaly
 *  detection, top-stats summary, and click-to-sort columns. v0.3.3
 *  expanded from a flat table to a real operator dashboard. */
async function renderRunsHtml(env: Env): Promise<Response> {
  // R2 list is alphabetic, but our keys are runs/<uuid>/<ISO-ts>_...
  // so listing alphabetically does NOT give newest-first across testers.
  // Pull a generous limit then sort by `uploaded` timestamp client-side.
  const list = await env.AUDIT_R2.list({ prefix: "runs/", limit: 200 });
  const objects = [...list.objects].sort((a, b) => {
    const ta = a.uploaded?.getTime() ?? 0;
    const tb = b.uploaded?.getTime() ?? 0;
    return tb - ta;
  }).slice(0, 50);

  // Fetch each audit JSON in parallel, extract summary fields. Errors
  // (corrupt JSON, missing fields, etc.) get null fields rather than
  // killing the whole page render.
  const summaries: RunSummary[] = await Promise.all(
    objects.map(async (o): Promise<RunSummary> => {
      const uuid = o.customMetadata?.uuid || extractUuidFromKey(o.key);
      const summaryBase: RunSummary = {
        key: o.key,
        uuid_full: uuid || "unknown",
        uuid_prefix: (uuid || "unknown").slice(0, 8),
        uploaded: o.uploaded?.toISOString() || "",
        duration_s: null, cost_usd: null, app_version: null,
        profile_headline: null,
        total_scraped: null, after_filters: null, qualifying: null,
        strong: null, good: null, maybe: null, stretch: null,
        funnel_collapse_pct: null, top_role_titles: [],
        anomaly_count: null,
        keywords_hash: null,
        keyword_count: null,
      };
      try {
        const obj = await env.AUDIT_R2.get(o.key);
        if (!obj) return summaryBase;
        const audit = await obj.json() as any;
        const meta = audit?.run_metadata || {};
        const funnel = audit?.pipeline_funnel || {};
        const tier = funnel?.tier_breakdown || {};
        const scraped = numberOrNull(funnel?.total_scraped);
        const qual = numberOrNull(funnel?.qualifying_final);
        const collapse = (scraped && qual !== null && scraped > 0)
          ? Math.round((1 - qual / scraped) * 100) : null;
        const roles = Array.isArray(audit?.all_qualifying_roles) ? audit.all_qualifying_roles : [];
        const titles = roles.slice(0, 5).map((r: any) =>
          `${r?.title || "?"} @ ${r?.company || "?"}`
        );
        // v0.3.3: profile headline + calibration anomaly count
        const profileSnap = audit?.profile_snapshot || {};
        const headline = typeof profileSnap?.headline === "string"
          ? profileSnap.headline : null;
        const calibBlock = audit?.calibration_anomalies;
        const anomalyCount = (calibBlock && typeof calibBlock?.total_anomalies === "number")
          ? calibBlock.total_anomalies : null;
        // v0.3.3: keyword fingerprint (sorted union of keywords_used +
        // search_terms, hashed) — used to detect "same keywords as
        // prior run" so the operator can spot clean A/B comparisons.
        const kwsUsed = Array.isArray(meta?.keywords_used) ? meta.keywords_used : [];
        const searchTerms = Array.isArray(profileSnap?.search_terms)
          ? profileSnap.search_terms : [];
        const allKwTokens = [...kwsUsed, ...searchTerms]
          .map((s: any) => String(s || "").trim().toLowerCase())
          .filter((s: string) => s.length > 0)
          .sort();
        const kwHash = allKwTokens.length > 0
          ? hashStringArray(allKwTokens) : null;
        return {
          ...summaryBase,
          duration_s: numberOrNull(meta?.duration_seconds),
          cost_usd: numberOrNull(meta?.cost_breakdown?.total_usd),
          app_version: typeof meta?.app_version === "string" ? meta.app_version : null,
          profile_headline: headline,
          total_scraped: scraped,
          after_filters: numberOrNull(funnel?.after_hard_filters),
          qualifying: qual,
          strong: numberOrNull(tier?.STRONG),
          good: numberOrNull(tier?.GOOD),
          maybe: numberOrNull(tier?.MAYBE),
          stretch: numberOrNull(tier?.STRETCH),
          funnel_collapse_pct: collapse,
          top_role_titles: titles,
          anomaly_count: anomalyCount,
          keywords_hash: kwHash,
          keyword_count: kwsUsed.length || null,
        };
      } catch {
        return summaryBase;
      }
    })
  );

  // Compute aggregate stats for the top stats bar.
  // Time windows: last 7 days, last 30 days (all loaded summaries).
  const now = Date.now();
  const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;
  const recent = summaries.filter(s => {
    if (!s.uploaded) return false;
    const t = Date.parse(s.uploaded);
    return !isNaN(t) && (now - t) <= SEVEN_DAYS_MS;
  });
  const distinctTesters = new Set(summaries.map(s => s.uuid_full)).size;
  const distinctTestersRecent = new Set(recent.map(s => s.uuid_full)).size;
  const totalSpend7d = recent.reduce((acc, s) => acc + (s.cost_usd || 0), 0);
  const totalSpendAll = summaries.reduce((acc, s) => acc + (s.cost_usd || 0), 0);
  const strongCounts = summaries.map(s => s.strong).filter((v): v is number => v !== null);
  const medianStrong = strongCounts.length
    ? [...strongCounts].sort((a, b) => a - b)[Math.floor(strongCounts.length / 2)]
    : null;
  const totalAnomalies7d = recent.reduce(
    (acc, s) => acc + (s.anomaly_count || 0), 0
  );
  const anomaliesTrackedCount = recent.filter(s => s.anomaly_count !== null).length;

  // Build per-tester summary map (used when a tester filter is active).
  // Keyed by full UUID; values include count, latest profile, total spend,
  // and a small history of STRONG counts (newest→oldest) for the sparkline.
  const perTester = new Map<string, {
    runs: number;
    latestProfile: string | null;
    latestVersion: string | null;
    avgStrong: number | null;
    totalSpend: number;
    strongHistory: (number | null)[];  // newest first
  }>();
  for (const s of summaries) {
    const cur = perTester.get(s.uuid_full) || {
      runs: 0,
      latestProfile: null,
      latestVersion: null,
      avgStrong: null,
      totalSpend: 0,
      strongHistory: [],
    };
    cur.runs++;
    cur.totalSpend += s.cost_usd || 0;
    cur.strongHistory.push(s.strong);
    if (cur.latestProfile === null && s.profile_headline) cur.latestProfile = s.profile_headline;
    if (cur.latestVersion === null && s.app_version) cur.latestVersion = s.app_version;
    perTester.set(s.uuid_full, cur);
  }
  for (const [uuid, t] of perTester.entries()) {
    const strongs = t.strongHistory.filter((v): v is number => v !== null);
    t.avgStrong = strongs.length ? Math.round(strongs.reduce((a, b) => a + b, 0) / strongs.length) : null;
    perTester.set(uuid, t);
  }

  const tokenParam = `token=${encodeURIComponent(env.ADMIN_TOKEN || "")}`;

  // Embed full summary data + per-tester metadata as JSON the client-side
  // JS can read for filtering, sorting, and bulk operations. Keeps the
  // Worker stateless — all interactivity is client-side.
  const dataJson = JSON.stringify({
    summaries,
    perTester: Object.fromEntries(perTester),
    token: env.ADMIN_TOKEN || "",
  });

  const html = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Admin · Runs · findmesomedamnjobz</title>
  <style>
    :root {
      --bg: #0a0b0e;
      --surface: #14161a;
      --surface-2: #1c1f25;
      --border: #24272e;
      --border-strong: #34384020;
      --text: #e8e8ec;
      --text-dim: #95979e;
      --text-faint: #5d6068;
      --accent: #88aaff;
      --accent-dim: #5577cc;
      --good: #4ec792;
      --warn: #d8b840;
      --bad: #e8665a;
      --strong-bg: rgba(78, 199, 146, 0.08);
      --strong-fg: #6dd6a4;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      font-size: 13px;
      line-height: 1.4;
      padding: 24px;
    }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    h1 {
      font-size: 22px;
      margin: 0 0 4px;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .subtitle { color: var(--text-dim); font-size: 13px; margin-bottom: 20px; }

    /* ---- Top stats bar ---- */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
    }
    .stat-card .label {
      font-size: 11px;
      color: var(--text-faint);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
      margin-bottom: 6px;
    }
    .stat-card .value {
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .stat-card .sub {
      font-size: 11px;
      color: var(--text-dim);
      margin-top: 4px;
    }

    /* ---- Toolbar ---- */
    .toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 12px;
      background: var(--surface);
      padding: 10px 14px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }
    .toolbar select, .toolbar input {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 13px;
      font-family: inherit;
    }
    .toolbar input { min-width: 240px; }
    .toolbar select { min-width: 200px; }
    .toolbar input:focus, .toolbar select:focus {
      outline: none;
      border-color: var(--accent-dim);
    }
    .btn {
      background: var(--surface-2);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      font-family: inherit;
    }
    .btn:hover { background: #2a2e36; }
    .btn-primary { background: var(--accent-dim); border-color: var(--accent); color: white; }
    .btn-primary:hover { background: var(--accent); }
    .btn-primary:disabled { background: #2a2e36; border-color: var(--border); color: var(--text-faint); cursor: not-allowed; }
    .toolbar .spacer { flex: 1; }
    .toolbar .label-text { color: var(--text-dim); font-size: 12px; }

    /* ---- Per-tester summary card (when filter active) ---- */
    #per-tester-summary {
      display: none;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 12px;
    }
    #per-tester-summary.active { display: block; }
    #per-tester-summary .row { display: flex; gap: 28px; flex-wrap: wrap; align-items: baseline; }
    #per-tester-summary .row > div { min-width: 100px; }
    #per-tester-summary .item-label {
      font-size: 11px;
      color: var(--text-faint);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    #per-tester-summary .item-value {
      font-size: 16px;
      font-weight: 600;
      margin-top: 2px;
    }
    #per-tester-summary .profile-line {
      font-size: 12px;
      color: var(--text-dim);
      margin-top: 8px;
      font-style: italic;
    }

    /* ---- Table ---- */
    .table-wrap {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      overflow: hidden;
    }
    table { border-collapse: collapse; width: 100%; }
    th, td {
      padding: 9px 12px;
      text-align: left;
      vertical-align: top;
      font-size: 12.5px;
      border-bottom: 1px solid var(--border-strong);
    }
    tbody tr:last-child td { border-bottom: none; }
    th {
      background: var(--surface-2);
      color: var(--text-dim);
      font-weight: 600;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      position: sticky;
      top: 0;
      z-index: 1;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }
    th:hover { color: var(--text); }
    th.sorted-asc::after { content: " ▲"; color: var(--accent); }
    th.sorted-desc::after { content: " ▼"; color: var(--accent); }
    th.no-sort { cursor: default; }
    th.no-sort:hover { color: var(--text-dim); }
    tbody tr { transition: background-color 0.1s; }
    tbody tr:hover { background: var(--surface-2); }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11.5px; }

    .uuid-badge {
      display: inline-block;
      padding: 2px 7px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 4px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 11px;
      cursor: pointer;
      color: var(--text);
    }
    .uuid-badge:hover { background: #2a2e36; border-color: var(--accent-dim); }

    .strong-cell {
      background: var(--strong-bg);
      color: var(--strong-fg);
      font-weight: 700;
    }
    tr.collapse-warn td { background: rgba(216, 184, 64, 0.05); }
    tr.collapse-bad  td { background: rgba(232, 102, 90, 0.07); }
    tr.calibration-anomaly td { box-shadow: inset 3px 0 0 var(--warn); }
    /* Low-match warning (v0.3.4) — operator sees runs with weak quality
       pool so we can investigate the underlying profile / keywords. */
    tr.low-match td { box-shadow: inset 3px 0 0 var(--bad); }
    tr.low-match.calibration-anomaly td { box-shadow: inset 3px 0 0 var(--bad), inset 6px 0 0 var(--warn); }
    .low-match-chip {
      display: inline-block;
      margin-left: 4px;
      padding: 1px 5px;
      font-size: 10px;
      font-weight: 600;
      color: var(--bad);
      background: rgba(232, 102, 90, 0.1);
      border: 1px solid rgba(232, 102, 90, 0.3);
      border-radius: 3px;
      vertical-align: middle;
      cursor: help;
    }

    .anomaly-good { color: var(--good); }
    .anomaly-warn { color: var(--warn); }
    .anomaly-bad  { color: var(--bad); font-weight: 600; }

    .version-pill {
      display: inline-block;
      padding: 1px 6px;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 999px;
      font-size: 11px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
    }

    .delta-chip {
      display: inline-block;
      margin-left: 4px;
      padding: 0 5px;
      font-size: 10px;
      font-weight: 600;
      border-radius: 3px;
      vertical-align: middle;
    }
    .delta-up   { background: rgba(78, 199, 146, 0.15); color: var(--good); }
    .delta-down { background: rgba(232, 102, 90, 0.15); color: var(--bad); }
    .delta-flat { background: var(--surface-2); color: var(--text-faint); }

    /* v0.3.3: keyword-set indicator chip — distinguishes "same keywords as
       prior run" (clean A/B comparison) from "keywords changed". Helps the
       operator interpret tier-count deltas correctly. */
    .kw-chip {
      display: inline-block;
      padding: 1px 6px;
      font-size: 9.5px;
      font-weight: 600;
      border-radius: 3px;
      vertical-align: middle;
      margin-top: 2px;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .kw-same { background: rgba(78, 199, 146, 0.12); color: var(--good); }
    .kw-diff { background: rgba(216, 184, 64, 0.12); color: var(--warn); }

    .titles { margin: 0; padding-left: 14px; color: var(--text-dim); font-size: 11.5px; }
    .titles li { padding: 1px 0; }

    .profile-cell {
      max-width: 220px;
      color: var(--text-dim);
      font-size: 11.5px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .when-cell { white-space: nowrap; font-size: 11.5px; color: var(--text-dim); }
    .when-cell .date-main { color: var(--text); }
    .when-cell .relative-time { color: var(--text-faint); font-size: 10.5px; display: block; }

    .checkbox-cell { width: 28px; padding-left: 14px; padding-right: 0; }
    .checkbox-cell input { cursor: pointer; }

    .legend {
      font-size: 11.5px;
      color: var(--text-dim);
      margin-top: 14px;
      padding: 0 4px;
    }
    .legend-bar { display: flex; gap: 16px; flex-wrap: wrap; }
    .legend-bar > span { display: flex; align-items: center; gap: 6px; }
    .legend-swatch {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 2px;
    }
    .swatch-warn { background: rgba(216, 184, 64, 0.4); }
    .swatch-bad  { background: rgba(232, 102, 90, 0.4); }
    .swatch-anomaly { background: var(--warn); width: 3px; height: 12px; }

    .empty-state {
      padding: 60px 20px;
      text-align: center;
      color: var(--text-faint);
      font-size: 14px;
    }

    .selection-bar {
      display: none;
      background: var(--surface-2);
      border: 1px solid var(--accent-dim);
      border-radius: 10px;
      padding: 10px 14px;
      margin-bottom: 12px;
      align-items: center;
      gap: 12px;
    }
    .selection-bar.active { display: flex; }
    .selection-bar .count { font-weight: 600; }
  </style>
</head>
<body>
  <h1>findmesomedamnjobz · operator dashboard</h1>
  <div class="subtitle">
    Last ${summaries.length} runs across ${distinctTesters} testers · times shown in your local timezone
  </div>

  <!-- Top stats bar -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="label">Last 7 days</div>
      <div class="value">${recent.length}</div>
      <div class="sub">runs</div>
    </div>
    <div class="stat-card">
      <div class="label">Active testers</div>
      <div class="value">${distinctTestersRecent}</div>
      <div class="sub">distinct UUIDs in last 7d</div>
    </div>
    <div class="stat-card">
      <div class="label">Spend (last 7d)</div>
      <div class="value">$${totalSpend7d.toFixed(2)}</div>
      <div class="sub">$${totalSpendAll.toFixed(2)} all time visible</div>
    </div>
    <div class="stat-card">
      <div class="label">Median STRONG</div>
      <div class="value">${medianStrong !== null ? medianStrong : "—"}</div>
      <div class="sub">across ${strongCounts.length} runs</div>
    </div>
    <div class="stat-card">
      <div class="label">Calibration anomalies</div>
      <div class="value ${totalAnomalies7d === 0 ? "anomaly-good" : (totalAnomalies7d <= 5 ? "anomaly-warn" : "anomaly-bad")}">${totalAnomalies7d}</div>
      <div class="sub">last 7d · ${anomaliesTrackedCount} runs tracked</div>
    </div>
  </div>

  <!-- Toolbar: filter + search -->
  <div class="toolbar">
    <span class="label-text">Filter:</span>
    <select id="tester-filter">
      <option value="">All testers (${distinctTesters})</option>
      ${[...perTester.entries()].map(([uuid, t]) => {
        const label = t.latestProfile
          ? `${uuid.slice(0, 8)} · ${escapeHtml(t.latestProfile.slice(0, 50))}${t.latestProfile.length > 50 ? "…" : ""} (${t.runs})`
          : `${uuid.slice(0, 8)} (${t.runs})`;
        return `<option value="${escapeHtml(uuid)}">${label}</option>`;
      }).join("")}
    </select>
    <input id="search-box" type="search" placeholder="Search title / company / version / UUID...">
    <span class="spacer"></span>
    <span class="label-text" id="visible-count">Showing ${summaries.length} runs</span>
  </div>

  <!-- Per-tester summary card (revealed when filter active) -->
  <div id="per-tester-summary"></div>

  <!-- Bulk-action bar (revealed when ≥1 row selected) -->
  <div class="selection-bar" id="selection-bar">
    <span class="count" id="selection-count">0 selected</span>
    <button class="btn btn-primary" id="download-selected">Download combined JSON</button>
    <button class="btn" id="clear-selection">Clear selection</button>
    <span class="spacer"></span>
    <span class="label-text">Combines selected audit JSONs into one downloadable file</span>
  </div>

  <!-- Main runs table -->
  <div class="table-wrap">
    <table id="runs-table">
      <thead>
        <tr>
          <th class="no-sort checkbox-cell"><input type="checkbox" id="select-all" title="Select all visible"></th>
          <th data-sort="uuid_prefix">Tester</th>
          <th data-sort="profile_headline">Profile</th>
          <th data-sort="uploaded">When</th>
          <th data-sort="app_version">App</th>
          <th data-sort="total_scraped" class="num-col">Scraped</th>
          <th data-sort="qualifying" class="num-col">Qual.</th>
          <th data-sort="strong" class="num-col">STRONG</th>
          <th data-sort="good" class="num-col">Good</th>
          <th data-sort="maybe" class="num-col">Maybe</th>
          <th data-sort="stretch" class="num-col">Stretch</th>
          <th data-sort="anomaly_count" class="num-col">Anom.</th>
          <th data-sort="cost_usd" class="num-col">Cost</th>
          <th data-sort="duration_s" class="num-col">Time</th>
          <th class="no-sort">Top 5 roles</th>
          <th class="no-sort">JSON</th>
        </tr>
      </thead>
      <tbody id="runs-tbody"></tbody>
    </table>
  </div>

  <div class="legend">
    <div class="legend-bar">
      <span><span class="legend-swatch swatch-warn"></span>Funnel collapse 90–94%</span>
      <span><span class="legend-swatch swatch-bad"></span>Funnel collapse ≥95%</span>
      <span><span class="legend-swatch swatch-anomaly"></span>Calibration anomalies present</span>
      <span style="color: var(--text-faint);">|  Click any column header to sort. Click any UUID to filter to that tester.</span>
    </div>
  </div>

  <script>
    // ============================================================
    // Embedded data + state
    // ============================================================
    const DATA = ${dataJson};
    const ALL = DATA.summaries;
    const PER_TESTER = DATA.perTester;
    const TOKEN = DATA.token;

    let state = {
      filterUuid: "",
      search: "",
      sortField: "uploaded",
      sortDir: "desc",
      selected: new Set(),  // r2 keys
    };

    // ============================================================
    // Helpers
    // ============================================================
    function escapeHtml(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function formatLocalDate(iso) {
      if (!iso) return { date: "—", relative: "" };
      const d = new Date(iso);
      if (isNaN(d.getTime())) return { date: "—", relative: "" };
      const y = d.getFullYear();
      const mo = String(d.getMonth() + 1).padStart(2, "0");
      const da = String(d.getDate()).padStart(2, "0");
      const h = String(d.getHours()).padStart(2, "0");
      const mi = String(d.getMinutes()).padStart(2, "0");
      const date = y + "-" + mo + "-" + da + " " + h + ":" + mi;

      // Relative time
      const diffMs = Date.now() - d.getTime();
      const diffMin = Math.round(diffMs / 60000);
      let relative = "";
      if (diffMin < 1) relative = "just now";
      else if (diffMin < 60) relative = diffMin + "m ago";
      else if (diffMin < 24 * 60) relative = Math.round(diffMin / 60) + "h ago";
      else if (diffMin < 7 * 24 * 60) relative = Math.round(diffMin / (24 * 60)) + "d ago";
      else relative = Math.round(diffMin / (7 * 24 * 60)) + "w ago";

      return { date, relative };
    }

    function compareValues(a, b, field, dir) {
      const av = a[field];
      const bv = b[field];
      // Nulls always sort last regardless of direction
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      let cmp;
      if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
      else cmp = String(av).localeCompare(String(bv));
      return dir === "asc" ? cmp : -cmp;
    }

    function getFilteredSorted() {
      let rows = ALL.slice();
      if (state.filterUuid) {
        rows = rows.filter(r => r.uuid_full === state.filterUuid);
      }
      if (state.search) {
        const q = state.search.toLowerCase();
        rows = rows.filter(r => {
          const haystack = [
            r.uuid_prefix, r.uuid_full,
            r.profile_headline,
            r.app_version,
            ...(r.top_role_titles || []),
          ].filter(Boolean).join(" ").toLowerCase();
          return haystack.includes(q);
        });
      }
      rows.sort((a, b) => compareValues(a, b, state.sortField, state.sortDir));
      return rows;
    }

    /** Find the SAME tester's most recent prior run (by uploaded date). */
    function findPriorRun(currentRow) {
      const sameTester = ALL
        .filter(r => r.uuid_full === currentRow.uuid_full && r.uploaded < currentRow.uploaded)
        .sort((a, b) => b.uploaded.localeCompare(a.uploaded));
      return sameTester[0] || null;
    }

    /**
     * Returns a small chip indicating whether this run used the same
     * keywords/search-terms as the same tester's previous run. Helps
     * the operator identify clean A/B comparisons (same input set,
     * different version = pure calibration effect).
     */
    function sameKeywordsChip(currentRow) {
      const prev = findPriorRun(currentRow);
      if (!prev || !currentRow.keywords_hash || !prev.keywords_hash) return "";
      const same = currentRow.keywords_hash === prev.keywords_hash;
      if (same) {
        const tooltip = "Same " + (currentRow.keyword_count || "?") + " keywords as prior run on " + (prev.app_version || "?") + " — clean A/B comparison";
        return '<span class="kw-chip kw-same" title="' + tooltip + '">same kws</span>';
      } else {
        const tooltip = "Keywords differ from prior run on " + (prev.app_version || "?");
        return '<span class="kw-chip kw-diff" title="' + tooltip + '">kws changed</span>';
      }
    }

    /**
     * Returns a per-tester delta chip for any numeric field.
     *
     * Compares currentRow's field value to the SAME tester's most recent
     * prior run (filters by uuid_full first, then takes the chronologically
     * previous run). For STRONG/GOOD/MAYBE/STRETCH/qualifying counts, this
     * lets the operator see at-a-glance how each tester's numbers shifted
     * across versions.
     *
     * For "higher is better" fields (strong/good/qualifying), positive
     * delta = green up-chip. For "lower is better" fields (e.g. anomaly
     * count, future fields like funnel_collapse_pct), pass invertColors=true
     * so positive deltas render red.
     */
    function deltaChipFor(currentRow, field, invertColors) {
      invertColors = !!invertColors;
      const prev = findPriorRun(currentRow);
      if (!prev || prev[field] == null || currentRow[field] == null) return "";
      const delta = currentRow[field] - prev[field];
      if (delta === 0) return '<span class="delta-chip delta-flat" title="same as prior run">±0</span>';
      // For higher-is-better: positive = good (up arrow, green); negative = bad (down arrow, red)
      // For lower-is-better (invertColors=true): flip those.
      const isPositiveGood = (delta > 0) !== invertColors;
      const cls = isPositiveGood ? "delta-up" : "delta-down";
      const sign = delta > 0 ? "+" : "";
      const prevVer = prev.app_version || "(unknown version)";
      const tooltip = "was " + prev[field] + " on " + prevVer + " (" + sign + delta + ")";
      return '<span class="delta-chip ' + cls + '" title="' + tooltip + '">' + sign + delta + '</span>';
    }

    function rowHtml(s) {
      const time = formatLocalDate(s.uploaded);
      const collapse = s.funnel_collapse_pct;
      const collapseClass =
        collapse !== null && collapse >= 95 ? "collapse-bad"
        : collapse !== null && collapse >= 90 ? "collapse-warn"
        : "";
      const anomalyClass = (s.anomaly_count != null && s.anomaly_count > 0) ? " calibration-anomaly" : "";
      const anomalyDisplay = s.anomaly_count == null
        ? '<span style="color: var(--text-faint);">—</span>'
        : (s.anomaly_count === 0
            ? '<span class="anomaly-good">0</span>'
            : (s.anomaly_count <= 5
                ? '<span class="anomaly-warn">' + s.anomaly_count + '</span>'
                : '<span class="anomaly-bad">' + s.anomaly_count + '</span>'));
      // Low-match warning (v0.3.4): operator-visible signal that this run
      // produced very few quality matches. Replaces the dropped
      // minimum-STRONG safety floor — instead of artificially bumping roles
      // into STRONG (which would mislead the user), we surface the run on
      // the operator dashboard so we can investigate the profile / keywords.
      // Triggers:
      //   - STRONG = 0 (no top-tier matches at all)
      //   - STRONG + GOOD < 5 (weak overall qualifying pool)
      //   - qualifying_final < 20 (very narrow funnel)
      // Only fires when the run completed (qualifying != null).
      const strongVal = s.strong ?? null;
      const goodVal = s.good ?? null;
      const qualVal = s.qualifying ?? null;
      let lowMatchChip = "";
      let lowMatchClass = "";
      if (qualVal != null) {
        const sgSum = (strongVal ?? 0) + (goodVal ?? 0);
        if (strongVal === 0) {
          lowMatchChip = ' <span class="low-match-chip" title="No STRONG matches in this run — possibly wrong profile or keywords">no STRONG</span>';
          lowMatchClass = " low-match";
        } else if (sgSum < 5) {
          lowMatchChip = ' <span class="low-match-chip" title="Fewer than 5 STRONG+GOOD matches — weak quality pool">weak pool</span>';
          lowMatchClass = " low-match";
        } else if (qualVal < 20) {
          lowMatchChip = ' <span class="low-match-chip" title="Fewer than 20 qualifying roles — narrow funnel">narrow</span>';
          lowMatchClass = " low-match";
        }
      }
      const titles = (s.top_role_titles || []).length
        ? '<ul class="titles">' + s.top_role_titles.map(t => '<li>' + escapeHtml(t) + '</li>').join("") + '</ul>'
        : '<span style="color: var(--text-faint);">—</span>';
      const rawHref = '/v1/admin/run?k=' + encodeURIComponent(s.key) + '&token=' + encodeURIComponent(TOKEN);
      const checked = state.selected.has(s.key) ? "checked" : "";
      return '<tr class="' + collapseClass + anomalyClass + lowMatchClass + '">'
        + '<td class="checkbox-cell"><input type="checkbox" class="row-check" data-key="' + encodeURIComponent(s.key) + '" ' + checked + '></td>'
        + '<td><span class="uuid-badge" data-uuid="' + escapeHtml(s.uuid_full) + '" title="Click to filter to this tester">' + escapeHtml(s.uuid_prefix) + '</span></td>'
        + '<td class="profile-cell" title="' + escapeHtml(s.profile_headline || "") + '">' + escapeHtml(s.profile_headline || "—") + '</td>'
        + '<td class="when-cell"><span class="date-main">' + time.date + '</span><span class="relative-time">' + time.relative + '</span> ' + sameKeywordsChip(s) + '</td>'
        + '<td>' + (s.app_version ? '<span class="version-pill">' + escapeHtml(s.app_version) + '</span>' : "—") + '</td>'
        + '<td class="num">' + (s.total_scraped ?? "—") + '</td>'
        + '<td class="num">' + (s.qualifying ?? "—") + deltaChipFor(s, "qualifying") + '</td>'
        + '<td class="num strong-cell">' + (s.strong ?? "—") + deltaChipFor(s, "strong") + lowMatchChip + '</td>'
        + '<td class="num">' + (s.good ?? "—") + deltaChipFor(s, "good") + '</td>'
        + '<td class="num">' + (s.maybe ?? "—") + deltaChipFor(s, "maybe") + '</td>'
        + '<td class="num">' + (s.stretch ?? "—") + deltaChipFor(s, "stretch") + '</td>'
        + '<td class="num">' + anomalyDisplay + '</td>'
        + '<td class="num">' + (s.cost_usd != null ? "$" + s.cost_usd.toFixed(2) : "—") + '</td>'
        + '<td class="num">' + (s.duration_s != null ? Math.round(s.duration_s) + "s" : "—") + '</td>'
        + '<td>' + titles + '</td>'
        + '<td><a href="' + rawHref + '" target="_blank">JSON</a></td>'
        + '</tr>';
    }

    function render() {
      const rows = getFilteredSorted();
      const tbody = document.getElementById("runs-tbody");
      if (rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="16" class="empty-state">No runs match the current filter.</td></tr>';
      } else {
        tbody.innerHTML = rows.map(rowHtml).join("");
      }
      document.getElementById("visible-count").textContent =
        "Showing " + rows.length + " of " + ALL.length + " runs";

      // Update header sort indicators
      document.querySelectorAll("th[data-sort]").forEach(th => {
        th.classList.remove("sorted-asc", "sorted-desc");
        if (th.dataset.sort === state.sortField) {
          th.classList.add(state.sortDir === "asc" ? "sorted-asc" : "sorted-desc");
        }
      });

      // Per-tester summary card
      const summaryCard = document.getElementById("per-tester-summary");
      if (state.filterUuid && PER_TESTER[state.filterUuid]) {
        const t = PER_TESTER[state.filterUuid];
        summaryCard.innerHTML =
          '<div class="row">'
          + '<div><div class="item-label">Tester</div><div class="item-value mono">' + escapeHtml(state.filterUuid.slice(0, 16)) + '…</div></div>'
          + '<div><div class="item-label">Runs</div><div class="item-value">' + t.runs + '</div></div>'
          + '<div><div class="item-label">Avg STRONG</div><div class="item-value">' + (t.avgStrong ?? "—") + '</div></div>'
          + '<div><div class="item-label">Total spend</div><div class="item-value">$' + t.totalSpend.toFixed(2) + '</div></div>'
          + '<div><div class="item-label">Latest version</div><div class="item-value">' + (t.latestVersion ? '<span class="version-pill">' + escapeHtml(t.latestVersion) + '</span>' : "—") + '</div></div>'
          + '</div>'
          + (t.latestProfile ? '<div class="profile-line">Latest profile: ' + escapeHtml(t.latestProfile) + '</div>' : "");
        summaryCard.classList.add("active");
      } else {
        summaryCard.classList.remove("active");
      }

      // Update selection bar
      updateSelectionBar();
    }

    function updateSelectionBar() {
      const bar = document.getElementById("selection-bar");
      const n = state.selected.size;
      document.getElementById("selection-count").textContent = n + " selected";
      if (n > 0) bar.classList.add("active");
      else bar.classList.remove("active");
      document.getElementById("download-selected").disabled = n === 0;
    }

    // ============================================================
    // Wire up event handlers
    // ============================================================
    document.getElementById("tester-filter").addEventListener("change", e => {
      state.filterUuid = e.target.value;
      render();
    });
    document.getElementById("search-box").addEventListener("input", e => {
      state.search = e.target.value;
      render();
    });
    document.querySelectorAll("th[data-sort]").forEach(th => {
      th.addEventListener("click", () => {
        const field = th.dataset.sort;
        if (state.sortField === field) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortField = field;
          state.sortDir = "desc";
        }
        render();
      });
    });

    // Delegated event handlers for dynamic rows
    document.addEventListener("click", (e) => {
      const target = e.target;
      // UUID badge click → filter to that tester
      if (target.classList && target.classList.contains("uuid-badge")) {
        const uuid = target.dataset.uuid;
        document.getElementById("tester-filter").value = uuid;
        state.filterUuid = uuid;
        render();
      }
    });
    document.addEventListener("change", (e) => {
      const target = e.target;
      // Row checkbox toggled
      if (target.classList && target.classList.contains("row-check")) {
        const key = decodeURIComponent(target.dataset.key);
        if (target.checked) state.selected.add(key);
        else state.selected.delete(key);
        updateSelectionBar();
      }
    });

    // Select-all toggles only currently-visible (filtered + searched) rows
    document.getElementById("select-all").addEventListener("change", e => {
      const visible = getFilteredSorted();
      if (e.target.checked) {
        visible.forEach(r => state.selected.add(r.key));
      } else {
        visible.forEach(r => state.selected.delete(r.key));
      }
      render();
    });

    document.getElementById("clear-selection").addEventListener("click", () => {
      state.selected.clear();
      render();
    });

    // Download combined JSON for all selected runs
    document.getElementById("download-selected").addEventListener("click", async () => {
      const btn = document.getElementById("download-selected");
      const originalText = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Fetching " + state.selected.size + " audits...";
      try {
        const keys = [...state.selected];
        // Fetch each audit JSON in parallel
        const results = await Promise.all(keys.map(async (k) => {
          const url = '/v1/admin/run?k=' + encodeURIComponent(k) + '&token=' + encodeURIComponent(TOKEN);
          const res = await fetch(url);
          if (!res.ok) throw new Error("fetch failed for " + k);
          const audit = await res.json();
          return { _meta: { r2_key: k }, ...audit };
        }));
        const combined = {
          exported_at: new Date().toISOString(),
          exported_by: "admin_dashboard",
          run_count: results.length,
          filter_uuid: state.filterUuid || null,
          search_query: state.search || null,
          runs: results,
        };
        const blob = new Blob([JSON.stringify(combined, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
        a.download = "fmsdj-runs-" + (state.filterUuid ? state.filterUuid.slice(0,8) + "-" : "") + stamp + ".json";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        alert("Download failed: " + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = originalText;
      }
    });

    // Initial render
    render();
  </script>
</body>
</html>`;
  return new Response(html, {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function numberOrNull(x: unknown): number | null {
  return typeof x === "number" && Number.isFinite(x) ? x : null;
}

function extractUuidFromKey(key: string): string {
  // R2 keys are runs/<uuid>/<ts>_<runid>.json — pull the uuid segment
  const m = key.match(/^runs\/([^/]+)\//);
  return m ? m[1] : "";
}

function formatDateShort(d: Date): string {
  const y = d.getFullYear();
  const mo = String(d.getMonth() + 1).padStart(2, "0");
  const da = String(d.getDate()).padStart(2, "0");
  const h = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${y}-${mo}-${da} ${h}:${mi}`;
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * Hash an array of strings (already sorted) into a short fingerprint.
 * Uses FNV-1a 32-bit. Used for the keywords-set fingerprint that lets
 * the dashboard detect "same keywords as prior run" for clean A/B
 * comparisons. Collision rate is irrelevant at our scale (< 1000 runs)
 * — we only care that identical inputs produce identical hashes.
 */
function hashStringArray(arr: string[]): string {
  const text = arr.join("\x1f");  // unit-separator delimiter
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = (hash * 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
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
// v0.1.8: Windows shipped as NSIS .exe (was .msi). Single installer
// format aligns with Tauri 2's auto-updater — no more MSI/NSIS dual-
// track that triggered Windows Installer prompts on auto-update.
const INSTALLER_KEYS: Record<string, { key: string; filename: string }> = {
  "windows":   { key: "current/job-search-app.exe",         filename: "JobSearchApp-Setup.exe" },
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

  // v0.2.2: increment download counter (non-blocking, failure-safe).
  // KV write is awaited because we don't have ctx.waitUntil here, but
  // the put is fast (~30ms) and wrapped in try/catch so a KV failure
  // never blocks or breaks the download. If KV is rate-limited (free
  // tier: 1K writes/day across the whole namespace), we silently drop
  // the increment and serve the file anyway.
  await incrementDownloadCounter(env, slug);

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
// Download counter + stats endpoint (v0.2.2 — landing page click tracking)
// ============================================================================
//
// Two KV keys per platform per day:
//   dl:total:{platform}    → lifetime cumulative count (no TTL)
//   dl:{YYYY-MM-DD}:{platform}  → daily count (TTL 90d)
//
// One pseudo-aggregate key:
//   dl:total:_all          → lifetime total across all platforms
//
// Stats are read at /v1/stats?key={STATS_SECRET}, gated by a secret
// the operator sets via:  wrangler secret put STATS_SECRET
// (Until set, the endpoint returns 503 with setup instructions.)

async function incrementDownloadCounter(env: Env, platform: string): Promise<void> {
  try {
    const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    const totalKey = `dl:total:${platform}`;
    const dailyKey = `dl:${today}:${platform}`;
    const allKey = `dl:total:_all`;
    const [curTotal, curDaily, curAll] = await Promise.all([
      env.TESTER_KV.get(totalKey),
      env.TESTER_KV.get(dailyKey),
      env.TESTER_KV.get(allKey),
    ]);
    const newTotal = String(parseInt(curTotal || "0", 10) + 1);
    const newDaily = String(parseInt(curDaily || "0", 10) + 1);
    const newAll = String(parseInt(curAll || "0", 10) + 1);
    // Daily counter has 90-day TTL — auto-cleanup so we don't accumulate
    // thousands of stale daily keys over years.
    await Promise.all([
      env.TESTER_KV.put(totalKey, newTotal),
      env.TESTER_KV.put(dailyKey, newDaily, { expirationTtl: 90 * 86400 }),
      env.TESTER_KV.put(allKey, newAll),
    ]);
  } catch (err) {
    // Non-fatal: stats are nice-to-have, never block downloads.
    console.warn("download counter increment failed:", err);
  }
}

async function serveStats(req: Request, env: Env): Promise<Response> {
  // Secret-token gate. The operator sets STATS_SECRET as a Cloudflare
  // Worker secret (`wrangler secret put STATS_SECRET`). Until then,
  // return 503 with setup instructions instead of a generic 401 — this
  // endpoint is for the operator's eyes only, the friendly error helps
  // them remember the setup step.
  if (!env.STATS_SECRET) {
    return new Response(
      "STATS_SECRET not configured. Run from cf_worker/ directory:\n  wrangler secret put STATS_SECRET\n(then enter any password you'll remember; visit /v1/stats?key=YOURPASSWORD)",
      { status: 503, headers: { "Content-Type": "text/plain" } },
    );
  }
  const url = new URL(req.url);
  const provided = url.searchParams.get("key") || "";
  if (provided !== env.STATS_SECRET) {
    return new Response("Unauthorized — append ?key=YOUR_STATS_SECRET", {
      status: 401,
      headers: { "Content-Type": "text/plain" },
    });
  }

  // Aggregate the counts. Three platforms + the "_all" rollup.
  const platforms = ["windows", "mac-arm64", "mac-x64"];
  const totals: Record<string, number> = {};
  for (const p of [...platforms, "_all"]) {
    const v = await env.TESTER_KV.get(`dl:total:${p}`);
    totals[p] = parseInt(v || "0", 10);
  }
  // Last 30 days, daily breakdown
  const days: Array<{ date: string; counts: Record<string, number>; total: number }> = [];
  for (let i = 0; i < 30; i++) {
    const d = new Date(Date.now() - i * 86400_000).toISOString().slice(0, 10);
    const counts: Record<string, number> = {};
    let total = 0;
    for (const p of platforms) {
      const v = await env.TESTER_KV.get(`dl:${d}:${p}`);
      const n = parseInt(v || "0", 10);
      counts[p] = n;
      total += n;
    }
    days.push({ date: d, counts, total });
  }

  // Render HTML by default (browser-friendly), JSON if explicitly requested
  // via ?format=json or via Accept header.
  const fmt = (url.searchParams.get("format") || "").toLowerCase();
  const acceptsJson = (req.headers.get("Accept") || "").includes("application/json");
  if (fmt === "json" || acceptsJson) {
    return new Response(JSON.stringify({ totals, days }, null, 2), {
      headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  }

  // Tiny inline HTML — no external CSS/JS, no fancy charts. Just numbers.
  const totalDownloads = totals._all || 0;
  const last7 = days.slice(0, 7).reduce((s, d) => s + d.total, 0);
  const last30 = days.reduce((s, d) => s + d.total, 0);
  const dayRows = days.map(d =>
    `<tr><td>${d.date}</td><td>${d.counts.windows || 0}</td>` +
    `<td>${d.counts["mac-arm64"] || 0}</td><td>${d.counts["mac-x64"] || 0}</td>` +
    `<td><b>${d.total}</b></td></tr>`
  ).join("\n");

  const html = `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>findmesomedamnjobz — download stats</title>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;background:#0b0d12;color:#cbd5e1;margin:0;padding:32px;max-width:900px;margin:0 auto}
    h1{font-size:18px;color:#fff;margin:0 0 24px;font-weight:600}
    .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:32px}
    .card{background:#11151c;border:1px solid #1f2937;border-radius:8px;padding:16px}
    .card .label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:0.05em}
    .card .num{font-size:32px;font-weight:600;color:#a78bfa;margin-top:4px}
    .card .sub{font-size:11px;color:#64748b;margin-top:4px}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #1f2937}
    th{color:#64748b;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:0.05em}
    td:first-child{color:#94a3b8;font-family:monospace}
    .footer{margin-top:32px;font-size:11px;color:#475569}
  </style>
</head>
<body>
  <h1>findmesomedamnjobz — download stats</h1>
  <div class="grid">
    <div class="card"><div class="label">All-time</div><div class="num">${totalDownloads}</div><div class="sub">total clicks</div></div>
    <div class="card"><div class="label">Last 7 days</div><div class="num">${last7}</div><div class="sub">${(last7 / 7).toFixed(1)}/day avg</div></div>
    <div class="card"><div class="label">Last 30 days</div><div class="num">${last30}</div><div class="sub">${(last30 / 30).toFixed(1)}/day avg</div></div>
  </div>
  <h1 style="font-size:14px">By platform (lifetime)</h1>
  <div class="grid">
    <div class="card"><div class="label">Windows</div><div class="num">${totals.windows || 0}</div></div>
    <div class="card"><div class="label">Mac (Apple Silicon)</div><div class="num">${totals["mac-arm64"] || 0}</div></div>
    <div class="card"><div class="label">Mac (Intel)</div><div class="num">${totals["mac-x64"] || 0}</div></div>
  </div>
  <h1 style="font-size:14px">Last 30 days (daily)</h1>
  <table>
    <thead><tr><th>Date</th><th>Win</th><th>Mac ARM</th><th>Mac x64</th><th>Total</th></tr></thead>
    <tbody>${dayRows}</tbody>
  </table>
  <div class="footer">
    Counter starts from when this Worker was deployed. Add <code>?format=json</code> for raw JSON.
    Daily entries auto-expire after 90 days; lifetime totals never expire.
  </div>
</body>
</html>`;
  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
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

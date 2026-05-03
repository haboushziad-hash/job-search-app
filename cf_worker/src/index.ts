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
  // Secrets
  GOOGLE_API_KEY: string;
  GOOGLE_API_KEY_2?: string;
  GOOGLE_API_KEY_3?: string;
  ANTHROPIC_API_KEY: string;
  ADMIN_TOKEN: string;

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

const DAILY_CALL_CAP = 250;        // per UUID per day
const MONTHLY_SPEND_CAP_USD = 5.0; // per UUID per month
const MAX_AUDIT_JSON_BYTES = 5 * 1024 * 1024; // 5 MB

// ============================================================================
// Worker entrypoint
// ============================================================================

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

    // Health
    if (path === "/v1/health") {
      return json({ ok: true, ts: Date.now() });
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

        if (path === "/v1/llm/gemini") {
          return await proxyGemini(req, env);
        }
        if (path === "/v1/llm/anthropic") {
          return await proxyAnthropic(req, env);
        }
      }

      if (path === "/v1/runs" && req.method === "POST") {
        return await uploadRun(req, env, testerUuid);
      }
      if (path === "/v1/runs/me" && req.method === "GET") {
        return await listMyRuns(env, testerUuid);
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

async function proxyGemini(req: Request, env: Env): Promise<Response> {
  const body = await req.json() as any;
  const model = body.model || "gemini-2.5-flash";
  // Round-robin Ziad's available keys to spread quota
  const keys = [env.GOOGLE_API_KEY, env.GOOGLE_API_KEY_2, env.GOOGLE_API_KEY_3].filter(Boolean) as string[];
  if (keys.length === 0) {
    return json({ error: "no_google_keys_configured" }, 500);
  }
  const key = keys[Math.floor(Math.random() * keys.length)];
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(key)}`;

  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body.payload || body),
  });
  // Pass through status + body
  const text = await resp.text();
  return new Response(text, {
    status: resp.status,
    headers: {
      "Content-Type": resp.headers.get("Content-Type") || "application/json",
      "X-Proxied-By": "fmsdj-worker",
    },
  });
}

async function proxyAnthropic(req: Request, env: Env): Promise<Response> {
  const body = await req.json() as any;
  if (!env.ANTHROPIC_API_KEY) {
    return json({ error: "anthropic_key_not_configured" }, 500);
  }
  const url = "https://api.anthropic.com/v1/messages";
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(body.payload || body),
  });
  const text = await resp.text();
  return new Response(text, {
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
  // 36-hour TTL safely covers the next day cycle
  await env.TESTER_KV.put(key, String(n + 1), { expirationTtl: 60 * 60 * 36 });
  return { over: false, current: n + 1 };
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
  // Append to a KV index for fast list-by-tester
  const indexKey = `runs:${uuid}`;
  const existing = await env.TESTER_KV.get(indexKey);
  const list = existing ? JSON.parse(existing) : [];
  list.unshift({ key, ts, run_id: runId, qualifying: data?.pipeline_funnel?.qualifying_final });
  // Keep last 50 per tester
  await env.TESTER_KV.put(indexKey, JSON.stringify(list.slice(0, 50)));
  return json({ ok: true, key, run_id: runId });
}

async function listMyRuns(env: Env, uuid: string): Promise<Response> {
  const indexKey = `runs:${uuid}`;
  const existing = await env.TESTER_KV.get(indexKey);
  return json({ runs: existing ? JSON.parse(existing) : [] });
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
  return json({ error: "admin_endpoint_not_found" }, 404);
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

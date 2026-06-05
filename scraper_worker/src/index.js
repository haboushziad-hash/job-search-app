// fmsdj-scraper — dedicated Workday CXS forwarder, separate from the LLM proxy.
// Egress is Cloudflare's network (clean + fast); A/B-proven ~0% errors vs the
// home IP's ~16% HTTP-500s on Workday under load. Transparent passthrough:
// returns Workday's exact status + body so the backend parses it identically
// to a direct response. Guarded: POST only, shared secret, *.myworkdayjobs.com
// (and the wday/cxs path) only — so it can't be abused as an open proxy.
const TARGET_RE = /^https:\/\/[a-z0-9.-]+\.myworkdayjobs\.com\/wday\/cxs\//i;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("POST only", { status: 405 });
    }
    if (request.headers.get("x-scraper-secret") !== env.SCRAPER_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const target = request.headers.get("x-target-url") || "";
    if (!TARGET_RE.test(target)) {
      return new Response("bad target", { status: 400 });
    }
    const body = await request.text();
    try {
      const up = await fetch(target, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        },
        body,
      });
      // Transparent passthrough: backend gets Workday's real status + jobs body.
      // x-scraper-fwd marks this as a genuine Workday response so the backend
      // trusts its status (incl. 400/429) and only falls back to direct on the
      // worker's OWN guard errors (405/403/400 below, which lack this header).
      return new Response(up.body, {
        status: up.status,
        headers: {
          "Content-Type": up.headers.get("content-type") || "application/json",
          "x-scraper-fwd": "1",
        },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e).slice(0, 160) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};

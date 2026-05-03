# Deploying the Findmesomedamnjobz Worker

End-to-end deploy of the Cloudflare Worker that:
- Holds your Gemini + Anthropic API keys server-side
- Auto-collects audit JSONs from every tester's run
- Anonymizes per-tester via UUID (no resumes, no PII centralized)
- ~15 minutes of clicking + a few wrangler commands

After this, every .msi tester install auto-uploads run results to your R2 bucket. Zero tester action.

---

## Prerequisites

1. **Cloudflare account** — sign up free at https://dash.cloudflare.com (no credit card needed for Workers free tier)
2. **Node.js 18+** installed
3. **Wrangler CLI** — install globally:
   ```bash
   npm install -g wrangler
   ```

## Step 1 — Authenticate

```bash
cd "C:/Users/habou/OneDrive/Desktop/Job Search App/cf_worker"
wrangler login
```
Browser opens, click Allow. Returns to terminal with "Successfully logged in."

## Step 2 — Create infrastructure (3 commands)

```bash
# KV namespace for per-tester rate limits + run indexes
wrangler kv namespace create TESTER_KV
```
Output looks like:
```
🌀 Creating namespace with title "fmsdj-worker-TESTER_KV"
✨ Success! Add the following to your wrangler.toml:
[[kv_namespaces]]
binding = "TESTER_KV"
id = "abcd1234efgh5678ijkl9012mnop3456"
```
**Copy the `id` value.** Open `cf_worker/wrangler.toml` and replace `REPLACE_WITH_KV_ID_AFTER_CREATE` with that id.

```bash
# R2 bucket for audit JSON storage
wrangler r2 bucket create fmsdj-audits
```

## Step 3 — Set secrets (4 commands, paste each value when prompted)

```bash
wrangler secret put GOOGLE_API_KEY
# paste your Gemini key, press Enter

wrangler secret put GOOGLE_API_KEY_2
# (optional — paste second Gemini key for quota multiplexing)

wrangler secret put ANTHROPIC_API_KEY
# paste your Claude key

wrangler secret put ADMIN_TOKEN
# paste a random 32-char hex string. Generate one with:
#   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
```

## Step 4 — Deploy

```bash
wrangler deploy
```
Output ends with a URL like:
```
Published fmsdj-worker
  https://fmsdj-worker.YOUR-SUBDOMAIN.workers.dev
```

**Save that URL.** That's your Worker.

## Step 5 — Verify it's alive

```bash
curl https://fmsdj-worker.YOUR-SUBDOMAIN.workers.dev/v1/health
```
Should return `{"ok":true,"ts":...}`.

## Step 6 — Wire desktop app to upload runs to it

Edit the project root `.env`:
```
AUDIT_UPLOAD_URL=https://fmsdj-worker.YOUR-SUBDOMAIN.workers.dev/v1/runs
```

Re-bundle the .msi:
```bash
cd "C:/Users/habou/OneDrive/Desktop/Job Search App"
backend/venv/Scripts/python.exe -m PyInstaller backend.spec
cd desktop_app
npm run tauri build
```

The .msi now auto-uploads every tester's audit JSON to your R2 after each run, keyed by their anonymous UUID.

## Inspecting tester data (Ziad-only)

```bash
ADMIN=$(grep ADMIN_TOKEN .env | cut -d= -f2)
WORKER="https://fmsdj-worker.YOUR-SUBDOMAIN.workers.dev"

# List all uploaded runs across all testers
curl "$WORKER/v1/admin/runs" -H "Authorization: Bearer $ADMIN" | jq

# Fetch a specific run's audit JSON
curl "$WORKER/v1/admin/run?key=runs/UUID/2026-05-03_12-34_xyz.json" \
  -H "Authorization: Bearer $ADMIN" | jq

# List unique tester UUIDs
curl "$WORKER/v1/admin/testers" -H "Authorization: Bearer $ADMIN" | jq
```

## Privacy guarantees

- **Resumes are NEVER uploaded.** Only the audit JSON, which contains profile-derived signals (target_functions, technical_skills) — not raw text.
- **Per-tester UUID isolation**: each tester sees ONLY their own runs. Even with the UUID, the upload endpoint cannot fetch other UUIDs' data.
- **Admin override**: only your `ADMIN_TOKEN` (set above) can list all runs. Don't share it.
- **Free tier capacity**: Workers free tier = 100,000 requests/day. R2 free tier = 10 GB storage + free egress on Cloudflare.

## Scrubbing / Phase D2 (later)

When you want anonymized cross-tester learning data (smarter keyword generation):
1. Build a scrubber Worker route that strips JD bodies + reasoning text from incoming audits
2. Aggregate `(company, title, score, profile_tags)` into a D1 SQLite database
3. Add `/v1/learn/suggest-keywords?tags=...` endpoint that queries the aggregate

Approximate effort: 8-12 hours of focused Worker work. Scaffold not yet built — flagged in the central-server roadmap.

## Cost expectations (your wallet)

| Activity | Free tier covers | Beyond |
|---|---|---|
| Workers requests | 100K/day | $0.30/M req |
| R2 audit storage | 10 GB | $0.015/GB/month |
| R2 egress | Unlimited free on Cloudflare | — |
| Workers KV reads | 100K/day | $0.50/M reads |
| **Realistic month with 10 testers x 5 runs/wk** | **~6,000 requests + 100MB R2** | **$0** |

For tester pilot scale (10-30 testers): Cloudflare costs $0/month. Your only cost is the Gemini + Anthropic API spend, which routes through the Worker using the keys you set above.

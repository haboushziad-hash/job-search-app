# findmesomedamnjobz — Central Worker

Cloudflare Worker that holds Ziad's API keys and proxies all LLM calls
from tester desktop apps. Also stores audit JSONs in R2 for centralized
review.

## Why this exists

Tester apps don't ship with API keys. They send requests to this Worker,
which forwards to Gemini/Anthropic with Ziad's keys + per-tester rate
limits. Audit data uploads here automatically after every search.

## Quick deploy

Prereqs: Cloudflare account (free), `npm install -g wrangler`.

```bash
cd cf_worker
npm install
wrangler login

# Create infrastructure
wrangler kv namespace create TESTER_KV
# → copy the printed id into wrangler.toml's kv_namespaces.id field

wrangler r2 bucket create fmsdj-audits

# Set secrets (will prompt to paste each)
wrangler secret put GOOGLE_API_KEY
wrangler secret put GOOGLE_API_KEY_2     # optional second key
wrangler secret put ANTHROPIC_API_KEY
wrangler secret put ADMIN_TOKEN          # random string e.g. `openssl rand -hex 32`

# Deploy
wrangler deploy
# → outputs: https://fmsdj-worker.<your-subdomain>.workers.dev
```

Total time: ~10-15 minutes for first deploy.

## Endpoints

| Path | Method | Auth | Purpose |
|---|---|---|---|
| `/v1/health` | GET | none | uptime check |
| `/v1/llm/gemini` | POST | X-Tester-UUID | proxied Gemini call |
| `/v1/llm/anthropic` | POST | X-Tester-UUID | proxied Anthropic call |
| `/v1/runs` | POST | X-Tester-UUID | upload audit JSON after a run |
| `/v1/runs/me` | GET | X-Tester-UUID | list calling tester's runs |
| `/v1/admin/runs` | GET | Bearer ADMIN_TOKEN | list ALL testers' runs |
| `/v1/admin/run?key=...` | GET | Bearer ADMIN_TOKEN | fetch a specific audit JSON |
| `/v1/admin/testers` | GET | Bearer ADMIN_TOKEN | list all known tester UUIDs |

## Cost protection

- Per-tester daily call cap: 250 LLM requests / UUID / day (configurable in `index.ts`)
- Hits return HTTP 429 with `cap_hit_reason` — desktop app shows the cost preflight UI
- R2 storage: ~$0.015/GB/month — even 1000 audit JSONs at 500 KB each is $7.5/month

## Testing locally

```bash
wrangler dev   # local server at http://localhost:8787
```

Then point the desktop app's `LLM_PROXY_URL` at `http://localhost:8787`.

## Querying tester data (for Ziad)

```bash
ADMIN=$(grep ADMIN_TOKEN .env | cut -d= -f2)

# List all runs
curl https://fmsdj-worker.WORKERS_SUBDOMAIN.workers.dev/v1/admin/runs \
  -H "Authorization: Bearer $ADMIN"

# Pull a specific run's audit JSON
curl "https://fmsdj-worker.WORKERS_SUBDOMAIN.workers.dev/v1/admin/run?key=runs/UUID/timestamp.json" \
  -H "Authorization: Bearer $ADMIN"
```

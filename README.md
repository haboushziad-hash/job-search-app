# Job Search App

Cross-industry, cross-board job search tool with a polished desktop UI, multi-resume profile generation, smart scoring via Gemini, and a private admin dashboard for the operator.

## Architecture

```
desktop_app  (Tauri + React)
     │
     ▼  HTTPS, license-key auth
proxy_server  (Cloudflare Workers — TypeScript)
     │
     ├─▶ Gemini API  (3-stage cascade)
     ├─▶ R2 storage  (30-day rolling cache)
     └─▶ D1 database (telemetry)
              │
              ▼  daily sync
       Local archive  (this PC, forever)
              │
              ▼
       admin_dashboard  (private monitoring)
```

## Folder layout

| Folder | Purpose |
|---|---|
| `backend/` | Python scoring engine — scraper, filter, Gemini cascade, profile parser |
| `proxy_server/` | Cloudflare Worker — license auth, LLM proxy, telemetry, cost caps |
| `admin_dashboard/` | Private operator UI — runs, costs, alerts, per-tester metrics |
| `desktop_app/` | Tauri shell + React UI — what the testers install and use |
| `landing_page/` | Public website — download links, "how it works", FAQ |
| `archive/` | Local permanent storage — runs, scrapes, scores, raw JDs |
| `shared/` | Cross-package types, constants, prompt templates |
| `docs/` | Internal architecture notes and decisions |

## First-time setup

1. **Get a Google AI Studio API key** at https://aistudio.google.com/app/apikey
2. **Get a Cloudflare account** at https://dash.cloudflare.com (free tier is enough)
3. **Copy `.env.example` → `.env`** and fill in your keys
4. **Install dependencies** (one-time, see `docs/setup.md` once written)
5. **Run the validation test** to confirm Gemini cascade hits cost + quality targets

## Cost target

- Per run: **$0.55-0.75**
- Quality vs current Sonnet+Opus: **~90%**
- Monthly operating: **~$10-25** for 3 testers running monthly

## OneDrive note

This folder lives in OneDrive, but the following subfolders should NOT sync (they're huge and constantly changing):

- `**/node_modules/`
- `**/target/` (Rust build)
- `**/venv/` and `**/.venv/`
- `**/__pycache__/`
- `**/dist/`, `**/build/`, `**/.next/`, `**/.astro/`
- `archive/raw_jds/` (can grow to GBs)

To exclude in OneDrive: right-click each folder → "Free up space" or use Settings → Sync and back up → Manage backup → Choose folders to exclude. The `.gitignore` already handles git-side exclusion.

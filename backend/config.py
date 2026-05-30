"""Centralized configuration for the Job Search App backend.

Loads .env from the project root (or from common alternate locations when
running as a PyInstaller bundle, where __file__ doesn't point at the
original source tree). Provides typed access to every setting.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# Resolve the project root. Behavior differs between source and bundle:
#
#   - Source (python backend/api.py): __file__ = backend/config.py,
#     so parent.parent = repo root.
#   - PyInstaller frozen binary: __file__ resolves to a temp extraction
#     dir (e.g. C:\Users\...\AppData\Local\Temp\_MEIxxxx\backend\config.py).
#     parent.parent = that temp dir, which has no .env. We need to look
#     elsewhere.
#
# In frozen mode we try multiple known-good locations, in order:
#   1. Directory containing the .exe (alongside backend.exe)
#   2. Directory above the .exe (typical install: parent of `binaries/`)
#   3. The user's repo root if running locally during dev (env-resolved)
#   4. Current working directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _find_env_file() -> Optional[Path]:
    candidates: list[Path] = []
    # Source-tree default
    candidates.append(PROJECT_ROOT / ".env")
    if getattr(sys, "frozen", False):
        # Frozen bundle — look near the executable
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / ".env")
        candidates.append(exe_dir.parent / ".env")
        candidates.append(exe_dir.parent.parent / ".env")
    # Current working directory as a last resort
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path
        except OSError:
            continue
    return None


_env_path = _find_env_file()
if _env_path is not None:
    load_dotenv(_env_path, override=True)
# else: rely on inherited environment variables (LLM_PROXY_URL, etc.) —
# the Tauri sidecar passes some of these explicitly via lib.rs.


def _resolve_archive_dir() -> Path:
    """Resolve the archive directory in a way that PERSISTS across app
    restarts in bundled mode.

    v0.3.13 fix: The previous `PROJECT_ROOT / "archive"` path resolves to
    `_MEIPASS/archive/` in PyInstaller bundles. PyInstaller wipes the
    _MEIPASS temp dir on every app exit, which means:
      - cost_log.db is destroyed every time the user closes the app
      - All historical cost data from desktop runs is lost
      - The local "all-time usage" dashboard only shows current-session
        runs because everything else got wiped

    Cost-log forensics on 2026-05-08 confirmed: $11 in CLI's archive vs
    $109 actual Cloud Console spend. The ~$95 gap was bundled-app runs
    that wrote to _MEIPASS then evaporated.

    New behavior:
      - Source/dev mode: ARCHIVE_DIR = PROJECT_ROOT / "archive" (unchanged)
      - Bundled mode (frozen): ARCHIVE_DIR = OS-standard app data dir,
        which persists across app launches and is the conventional place
        for app-state on each platform.

    OS-standard paths used:
      Windows:  %APPDATA%\app.jobsearch.desktop\archive
      macOS:    ~/Library/Application Support/app.jobsearch.desktop/archive
      Linux:    ~/.local/share/app.jobsearch.desktop/archive

    These match where Tauri's log plugin and tester_id.txt already live,
    so users can find all app data in one place.
    """
    if getattr(sys, "frozen", False):
        bundle_id = "app.jobsearch.desktop"
        try:
            if sys.platform == "win32":
                appdata = os.environ.get("APPDATA")
                if appdata:
                    return Path(appdata) / bundle_id / "archive"
                # Fallback if APPDATA is somehow unset
                return Path.home() / "AppData" / "Roaming" / bundle_id / "archive"
            elif sys.platform == "darwin":
                return Path.home() / "Library" / "Application Support" / bundle_id / "archive"
            else:
                # Linux / other Unix
                xdg_data = os.environ.get("XDG_DATA_HOME")
                if xdg_data:
                    return Path(xdg_data) / bundle_id / "archive"
                return Path.home() / ".local" / "share" / bundle_id / "archive"
        except Exception:
            # Fall through to PROJECT_ROOT default if anything goes wrong
            pass
    # Source mode (dev / CLI from-source): preserve old behavior so
    # cost_log.db queries against `archive/cost_log.db` keep working.
    return PROJECT_ROOT / "archive"


ARCHIVE_DIR = _resolve_archive_dir()
REFERENCE_DATA_DIR = ARCHIVE_DIR / "reference_data"

# Emit a one-line breadcrumb so we can verify in production stdout where
# the archive actually landed. v0.3.13 forensics need this.
try:
    print(f"[config] ARCHIVE_DIR={ARCHIVE_DIR} (frozen={getattr(sys, 'frozen', False)})", flush=True)
except Exception:
    pass


class Config:
    """Read-only configuration loaded from .env at import time."""

    # API keys (used in LOCAL mode only — when LLM_PROXY_URL is unset)
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLOUDFLARE_ACCOUNT_ID: str = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    CLOUDFLARE_API_TOKEN: Optional[str] = os.getenv("CLOUDFLARE_API_TOKEN")

    # ============================================================
    # CENTRAL-SERVER MODE (Phase D)
    # ============================================================
    # When LLM_PROXY_URL is set, the backend routes ALL LLM calls through
    # Ziad's Cloudflare Worker (which holds Ziad's API keys server-side).
    # Testers' apps don't need their own API keys.
    #
    # When AUDIT_UPLOAD_URL is set, audit JSONs are uploaded after each run
    # so Ziad sees all tester data without manual sharing.
    #
    # Both default to empty (LOCAL mode) so dev/test still works without
    # the central server. Production .msi builds ship with these set.
    LLM_PROXY_URL: str = os.getenv("LLM_PROXY_URL", "")
    AUDIT_UPLOAD_URL: str = os.getenv("AUDIT_UPLOAD_URL", "")
    TESTER_UUID: str = os.getenv("TESTER_UUID", "")  # generated on first launch by frontend

    # ============================================================
    # JOB BOARD API KEYS — read once at config load (after dotenv).
    # Always read at import time, NEVER directly via os.getenv from scraper
    # modules (those don't reliably trigger dotenv-load).
    # ============================================================
    USAJOBS_API_KEY: str = os.getenv("USAJOBS_API_KEY", "").strip()
    USAJOBS_USER_AGENT: str = os.getenv("USAJOBS_USER_AGENT", "haboushziad@gmail.com").strip()
    ADZUNA_APP_ID: str = os.getenv("ADZUNA_APP_ID", "").strip()
    ADZUNA_APP_KEY: str = os.getenv("ADZUNA_APP_KEY", "").strip()
    ADZUNA_COUNTRY: str = os.getenv("ADZUNA_COUNTRY", "us").strip().lower()
    THEMUSE_API_KEY: str = os.getenv("THEMUSE_API_KEY", "").strip()
    FINDWORK_API_KEY: str = os.getenv("FINDWORK_API_KEY", "").strip()
    # v0.3.5: Serper.dev for Google Jobs aggregation. Free tier 2,500
    # calls/mo; backstops the curated ATS tenant lists with whatever
    # Google has indexed (pharma, biotech, retail, etc.).
    SERPER_API_KEY: str = os.getenv("SERPER_API_KEY", "").strip()
    # v0.3.9: DataForSEO for the ACTUAL Google Jobs endpoint (Serper /jobs
    # was 404, never existed). Pay-as-you-go, $0.0024/query at priority=2.
    # ~$3.23/mo at pilot scale. HTTP Basic Auth, login=email, password=API
    # password (not account password — different field in DataForSEO UI).
    DATAFORSEO_LOGIN: str = os.getenv("DATAFORSEO_LOGIN", "").strip()
    DATAFORSEO_PASSWORD: str = os.getenv("DATAFORSEO_PASSWORD", "").strip()
    # Wave-2B Phase 1 (FIX 18): JSearch via RapidAPI. Aggregates LinkedIn,
    # Indeed, Glassdoor, ZipRecruiter into one HTTP endpoint. Free tier 150
    # calls/mo; paid plans scale linearly. JSEARCH_NUM_PAGES bounds per-query
    # pagination so a single keyword can't blow the monthly budget.
    JSEARCH_RAPIDAPI_KEY: str = os.getenv("JSEARCH_RAPIDAPI_KEY", "").strip()
    # Wave-2B Phase 2 (FIX 3): default raised 3 → 5. Phase 1 introduced the
    # "3" default which silently regressed JSearch raw from ~994 to ~512
    # per run (-49%). Pages 4-5 are free on RapidAPI (the per-call cost is
    # fixed regardless of num_pages), so this is a pure-win revert.
    JSEARCH_NUM_PAGES: int = int(os.getenv("JSEARCH_NUM_PAGES", "5") or 5)

    @classmethod
    def is_central_server_mode(cls) -> bool:
        """True if backend should route LLM calls through Ziad's Worker."""
        return bool(cls.LLM_PROXY_URL)

    @classmethod
    def google_api_keys(cls) -> list[str]:
        """All configured Google API keys, in order. Used by the key rotator
        to multiply daily Pro quota across multiple Cloud projects."""
        keys: list[str] = []
        for env_name in ("GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "GOOGLE_API_KEY_3",
                         "GOOGLE_API_KEY_4", "GOOGLE_API_KEY_5"):
            v = os.getenv(env_name, "").strip()
            if v and not v.startswith("paste-"):
                keys.append(v)
        return keys

    # Spend caps (USD)
    PER_CALL_CAP_USD: float = float(os.getenv("PER_CALL_CAP_USD", "2.00"))
    PER_RUN_CAP_USD: float = float(os.getenv("PER_RUN_CAP_USD", "5.00"))
    PER_LICENSE_MONTHLY_CAP_USD: float = float(
        os.getenv("PER_LICENSE_MONTHLY_CAP_USD", "20.00")
    )
    GLOBAL_MONTHLY_CAP_USD: float = float(os.getenv("GLOBAL_MONTHLY_CAP_USD", "100.00"))

    # Behavior
    DEV_MODE: bool = os.getenv("DEV_MODE", "false").lower() == "true"

    # Models — single source of truth for provider/model selection
    # NOTE: Gemini 3.x preview models are also available — consider upgrading
    # once they go GA. For now, 2.5 stable is the right choice.
    EMBEDDING_MODEL: str = "gemini-embedding-001"         # Gemini, $0.15/MTok (text)
    # v0.3.15: Stage 1+2 stay on Flash for this ship. The Flash-Lite swap
    # (P1.14 + P1.18) is documented as a Phase 1B candidate — 6.25× cheaper
    # output ($0.40/M vs $2.50/M) but model-touching, so it must clear the
    # rescore harness before activating. The PRICING table below already
    # has gemini-2.5-flash-lite entries so cost calc is correct if/when
    # the swap activates. Each swap is a one-line edit.
    STAGE1_MODEL: str = "gemini-2.5-flash"                # Phase 1B candidate: -> "gemini-2.5-flash-lite" (P1.14)
    STAGE2_MODEL: str = "gemini-2.5-flash"                # Phase 1B candidate: -> "gemini-2.5-flash-lite" (P1.18)
    STAGE3_MODEL: str = "gemini-2.5-pro"                  # Deep eval — keep Pro quality

    # Wave-1 (2026-05-11) cost-reduction levers:
    # PAIR_B = drop `concerns` from Stage 3 response schema.
    # 16K-production-baseline retest (round5_research/_pair_b_16k_workarounds_results.json)
    # showed the 3-section prompt strip FAILS at ρ=0.959 (cross-batch noise floor).
    # The original ρ=0.963 was at 8K JD baseline — measurement artifact.
    # Drop_concerns + thinking=768 (W3 workaround) had cleanest STRONG behavior
    # (diff=1, mean delta -1.00). Ships drop_concerns ONLY. Saves ~$0.05/run.
    PAIR_B_ENABLED: bool = True
    # Wave-1 (2026-05-11): 3-section prompt compression disabled by default.
    # Failed 16K-baseline retest at ρ=0.959 (at noise floor, no measurable
    # quality gain over baseline). Kept as code path for future revisit if
    # production data suggests cross-batch noise floor is tighter.
    STAGE3_PROMPT_COMPRESSION_ENABLED: bool = False
    # Wave-1 (2026-05-11): thinking_budget kept at 1024 for production.
    # Round 9 isolation test (n=30 fresh 16K baseline):
    #   - drop_concerns + t=1024: ρ=0.917, mean delta 0.00 (within noise)
    #   - drop_concerns + t=768:  ρ=0.838, mean delta +2.00 (real drift)
    # t=768 introduces a +2.0 systematic upward bias on a 30-fixture sample.
    # Reverted to t=1024 to preserve calibration. Loses ~$1.16/run thinking
    # budget saving but keeps drop_concerns schema saving (~$0.05/run).
    STAGE3_THINKING_BUDGET_PAIR_B: int = 1024
    STAGE3_THINKING_BUDGET_FULL: int = 1024  # fallback if PAIR_B disabled
    # Wave-1 (2026-05-11): cache STAGE3_SYSTEM_PROMPT once per run.
    # Round 9 Test 1: validated 14% per-call cost saving, $0.77/run at
    # 290 Stage 3 calls cold-cache. Infrastructure exists in llm_client.create_cache().
    # Initially DISABLED 2026-05-29 after audit 2026-05-29_02-55_f4ea0252
    # exposed that 121 of 198 Stage 3 attempts (61%) failed with
    # `403 PERMISSION_DENIED CachedContent not found` because the Worker's
    # random key rotation (Math.random across 3 keys) routed ~2/3 of
    # cache reads to a different key than the cache owner.
    # RE-ENABLED 2026-05-29 after fixing the Worker: cf_worker now pins
    # each tester's Gemini calls to a single deterministic key derived
    # from a hash of their UUID. All of a tester's cache reads now hit
    # the same key that created the cache. Wave-2 task #15 will replace
    # this pin-per-tester with full Worker-managed caching for cross-run
    # reuse + N-key generalization.
    PRO_CONTEXT_CACHING_ENABLED: bool = True
    PRO_CONTEXT_CACHE_TTL_SECONDS: int = 3600  # 1 hour Gemini default

    # Wave-1 Section 14.4 (2026-05-11): JD regex compression in Stage 2.
    # Strips EEO/benefits/about-us boilerplate while preserving requirements,
    # qualifications, responsibilities, salary. Safe-fallback: returns original
    # JD if compression would remove >70% of content. Saves ~$0.02-0.07/run
    # on Stage 2 input tokens. Needs pre-ship A/B test on real fixtures.
    STAGE2_JD_COMPRESSION_ENABLED: bool = True

    # Wave-1 Path B (2026-05-11): LightGBM Stage 3 gate.
    # Pre-Stage-3 gate model predicts likely Stage 3 score from Stage 2
    # features. Skips Stage 3 for confidently-LOW predictions and confident-
    # STRONG band (s2 >= 88, redundant with Skip-Above 88 but defensive).
    # Empirical: 8.6% gate fire rate, 0/41 STRONG loss on 18-audit corpus.
    # Cross-archetype: ρ=0.973 on Zach n=78.
    # SHIPS DISABLED for wave-1 launch. Enable per-tester after Phase 1.5
    # cold-cache baseline + 2-tester canary with per-user STRONG-recall ≥95%
    # monitoring. Auto-disable per-user if recall <95% in first 2 runs.
    PATH_B_GATE_ENABLED: bool = False

    # Wave-2B Phase 1 (2026-05-29): Workday upstream facet filters.
    # When True, the Workday scraper translates self._user_filters into
    # `appliedFacets` on the POST body (locations text, Remote-only,
    # postingDateRange). Some Workday tenants reject unknown facet keys
    # with 422 — the scraper retries without facets on first 422.
    # Set to False to disable the feature entirely if a tenant misbehaves
    # and the per-tenant fallback isn't catching it cleanly.
    #
    # v0.3.17 hotfix: DEFAULTED TO FALSE. Audit 2026-05-29_19-49 showed
    # ALL 144 Workday requests returned 400 (not 422) — every tenant
    # rejected our generic facet shape because Workday CXS facets use
    # TENANT-SPECIFIC UUIDs (not plain string keys like "locations"/
    # "Remote"). The 422-retry never fired because the actual response
    # was 400. Disabling facets reverts Workday to post-scrape filtering
    # (preserves the 18K -> 6 qualifying funnel — small but non-zero).
    # Task #28 (Wave-2B.1) tracks the proper fix: probe per-tenant
    # facet UUIDs OR maintain a hand-curated tenant->UUID map for the
    # top ~20 tenants. Re-enable to True after that lands.
    WORKDAY_USE_FACETS: bool = False

    # Profile + keyword generation is THE critical LLM call — every downstream
    # scrape uses these keywords. We use a max-quality pipeline:
    #
    #   Stage 1 (parallel diversity sampling):
    #     - 3x Gemini 2.5 Pro at temp 0.5 with 8K thinking — captures self-consistency
    #     - 1x Claude Opus 4.5 with 16K thinking — cross-model perspective
    #
    #   Stage 2 (synthesis):
    #     - Claude Opus 4.5 with 16K thinking — merges all 4 lists into final
    #
    # Total: ~$0.65/setup, runs ONCE per user. Highest quality possible.
    # If Anthropic key missing: degrades gracefully to 3x Gemini + Gemini merge.
    PROFILE_BUILD_MODEL: str = "gemini-2.5-pro"
    PROFILE_BUILD_THINKING: int = 8192
    PROFILE_BUILD_SAMPLES: int = 3                        # # of Gemini variants
    # v0.3.16-wave2a (2026-05-29): briefly tried 0.5 → 0.2 to reduce
    # cross-run variance, but reverted to 0.5 after recognizing it
    # collapses the 3 samples into near-identical output — wasting the
    # consensus-voting design (3 distinct samples → merge picks best).
    # The variance we wanted to reduce was actually driven by other
    # factors (resume content drift, summary-opening sensitivity) which
    # the new prompt rules now constrain. Keep diversity sampling at
    # 0.5 and let the merge step do its job.
    PROFILE_BUILD_TEMPERATURE: float = 0.5                # Diversity in samples

    # Cross-model — Anthropic Claude (graceful fallback if key missing)
    PROFILE_CONSENSUS_ENABLED: bool = True
    PROFILE_ANTHROPIC_MODEL: str = "claude-opus-4-5"
    PROFILE_ANTHROPIC_THINKING: int = 16384

    # Final merge model (use Claude Opus when available, fallback to Gemini)
    PROFILE_MERGE_USE_CLAUDE: bool = True

    # Legacy single-pass audit (used as fallback only)
    PROFILE_AUDIT_ENABLED: bool = True
    PROFILE_AUDIT_THINKING: int = 8192

    # Pricing (USD per 1M tokens; embedding now per-token, not per-char)
    # Kept here so cost tracker is auditable.
    #
    # v0.3.15 (P1.1, P1.2, P1.3): rate table corrected against
    # https://ai.google.dev/gemini-api/docs/pricing as verified 2026-05-08.
    # Prior values were Gemini 1.5-era prices applied to 2.5 model strings.
    # Specifically:
    #   - Pro output was $5; correct is $10 (≤200K context) / $15 (>200K)
    #   - Flash input was $0.075; correct is $0.30
    #   - Flash output was $0.30; correct is $2.50 (incl. thinking)
    #   - Embedding was charged on chars at $0.0001/1K; correct is per-token at $0.15/M
    #
    # New schema: each entry can have `input_long`, `output_long` for
    # >200K-token tier (Pro only). Cached input rates added explicitly
    # (90% off explicit cache; handled in calculate_cost).
    # Cache STORAGE rates ($/MTok-hour) added for accurate cost tracking
    # of context cache lifetimes (Pro is $4.50/M-hour, Flash $1.00/M-hour).
    PRICING = {
        # Gemini 2.5 family (verified GA list price 2026-05-08)
        "gemini-2.5-pro": {
            "input": 1.25,            # ≤200K context
            "input_long": 2.50,       # >200K context
            "output": 10.00,          # ≤200K context (incl. thinking tokens)
            "output_long": 15.00,     # >200K context
            "cached_input": 0.125,    # 90% off explicit cache
            "cached_input_long": 0.25,
            "cache_storage_per_hour": 4.50,  # $/MTok-hour
            "batch_input": 0.625,     # 50% off via Batch API
            "batch_output": 5.00,
        },
        "gemini-2.5-flash": {
            "input": 0.30,
            "output": 2.50,           # incl. thinking tokens
            "cached_input": 0.03,     # 90% off explicit cache
            "cache_storage_per_hour": 1.00,
            "batch_input": 0.15,
            "batch_output": 1.25,
        },
        "gemini-2.5-flash-lite": {
            "input": 0.10,
            "output": 0.40,           # incl. thinking tokens
            "cached_input": 0.01,     # 90% off explicit cache
            "cache_storage_per_hour": 1.00,
            "batch_input": 0.05,
            "batch_output": 0.20,
        },
        # Embeddings — now per-token, NOT per-char
        "gemini-embedding-001":   {"input": 0.15, "output": 0.0},
        "gemini-embedding-2":     {"input": 0.20, "output": 0.0},
        # Anthropic Claude
        # v0.3.3: corrected Opus 4.5+ pricing. Anthropic dropped the price
        # of Opus 4.5/4.6/4.7 to $5/$25 per MTok (verified against
        # platform.claude.com/docs pricing 2026-05-06). Opus 4.1 and
        # earlier remain at the original $15/$75.
        "claude-opus-4-7":          {"input":  5.00, "output": 25.00},
        "claude-opus-4-6":          {"input":  5.00, "output": 25.00},
        "claude-opus-4-5":          {"input":  5.00, "output": 25.00},
        "claude-opus-4-1":          {"input": 15.00, "output": 75.00},
        "claude-opus-4-0":          {"input": 15.00, "output": 75.00},
        "claude-sonnet-4-6":        {"input": 3.00,  "output": 15.00},
        "claude-sonnet-4-5":        {"input": 3.00,  "output": 15.00},
        "claude-3-5-sonnet-latest": {"input": 3.00,  "output": 15.00},
        "claude-haiku-4-5":         {"input": 1.00,  "output":  5.00},
    }

    # v0.3.15: Pro long-context tier threshold (Google's pricing breakpoint).
    # Calls with input_tokens > this value are billed at the higher rate.
    PRO_LONG_CONTEXT_THRESHOLD: int = 200_000

    # Paths
    PROJECT_ROOT: Path = PROJECT_ROOT
    ARCHIVE_DIR: Path = ARCHIVE_DIR
    REFERENCE_DB: Path = REFERENCE_DATA_DIR / "job_market.db"

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing-config errors. Empty list = healthy."""
        errors: list[str] = []
        if not cls.GOOGLE_API_KEY or cls.GOOGLE_API_KEY.startswith("paste-"):
            errors.append("GOOGLE_API_KEY is not set in .env")
        if not cls.CLOUDFLARE_ACCOUNT_ID or cls.CLOUDFLARE_ACCOUNT_ID.startswith("paste-"):
            errors.append("CLOUDFLARE_ACCOUNT_ID is not set in .env")
        return errors


config = Config()

"""Centralized configuration for the Job Search App backend.

Loads .env from the project root. Provides typed access to every setting.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


# Resolve the project root (one level above backend/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = PROJECT_ROOT / "archive"
REFERENCE_DATA_DIR = ARCHIVE_DIR / "reference_data"

# Load .env from project root. override=True ensures the .env file always
# wins over inherited environment variables — critical when keys get added
# or rotated in .env after the parent shell was launched.
load_dotenv(PROJECT_ROOT / ".env", override=True)


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
    EMBEDDING_MODEL: str = "gemini-embedding-001"         # Gemini, ~$0.0001/1K chars
    STAGE1_MODEL: str = "gemini-2.5-flash"                # Cheap pre-filter
    STAGE2_MODEL: str = "gemini-2.5-flash"                # Triage — Flash for higher daily cap
    STAGE3_MODEL: str = "gemini-2.5-pro"                  # Deep eval — keep Pro quality

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

    # Pricing (USD per 1M tokens for completions; per 1K chars for embeddings)
    # Kept here so cost tracker is auditable.
    PRICING = {
        "gemini-2.5-flash": {"input": 0.075, "output": 0.30, "batch_input": 0.0375, "batch_output": 0.15},
        "gemini-2.5-pro":   {"input": 1.25,  "output": 5.00, "batch_input": 0.625,  "batch_output": 2.50},
        "gemini-embedding-001": {"input": 0.0001, "output": 0.0},   # per 1K chars
        "gemini-embedding-2":   {"input": 0.0001, "output": 0.0},
        # Anthropic Claude
        "claude-opus-4-5":          {"input": 15.00, "output": 75.00},
        "claude-opus-4-1":          {"input": 15.00, "output": 75.00},
        "claude-sonnet-4-5":        {"input": 3.00,  "output": 15.00},
        "claude-3-5-sonnet-latest": {"input": 3.00,  "output": 15.00},
    }

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

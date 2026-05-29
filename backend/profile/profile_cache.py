"""Profile-build cache (v0.3.15, P1.12).

Profile build = 3 Gemini Pro samples (thinking_budget=8192) + 1 Claude Opus
4.5 consensus + synthesis. Total cost: ~$0.50-0.92 per build. Today there
is NO cache — every build runs the full pipeline even if the inputs are
identical to a prior build.

User behavior in audit logs: profile rebuilds happen frequently during
testing (resume tweaks, preference changes, version migrations). Many
rebuilds are TRIGGERED with no actual input change.

This module provides a SQLite-backed cache keyed on:
  hash(resume_text + preferences + prompt_version + model_versions)

Cache hits short-circuit the entire build pipeline and return the prior
synthesized profile. Cache invalidates automatically on any prompt change
(prompt_version is part of the key) or model upgrade.

Storage: %APPDATA%/app.jobsearch.desktop/archive/profile_build_cache.db
(or PROJECT_ROOT/archive/profile_build_cache.db in source mode — same
ARCHIVE_DIR pattern as jd_score_cache after the v0.3.13 _MEIPASS fix).

TTL: 30 days. Long enough that minor prompt edits don't bust everyone's
cache mid-version, short enough that month-old prompt rot does eventually
re-bake.

Schema:
  profile_build_cache (
    cache_key TEXT PRIMARY KEY,        -- hash of inputs+versions
    profile_json TEXT NOT NULL,         -- serialized CandidateProfile
    prompt_version TEXT,                -- for diagnostic / cache busting
    model_versions TEXT,                -- diagnostic
    cached_at INTEGER,                  -- unix timestamp
    last_hit_at INTEGER,                -- when last reused
    hit_count INTEGER DEFAULT 0
  )
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any, Optional

from backend.config import config


CACHE_DB_PATH = config.ARCHIVE_DIR / "profile_build_cache.db"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# Bump this whenever the profile-build prompt text changes meaningfully.
# Keeps stale outputs out of cache after prompt edits even within the 30d TTL.
# v0.3.16-wave2a: rewrote INPUT TYPES section — resume is now identity base
# (full read, all sections contribute), freeform is effort-proportional
# directional refinement (weighted by [Word count: N] header), summary
# opening is no longer the dominant signal. Also changed user-message
# construction to label freeform separately from hard-requirement preferences.
PROMPT_VERSION = "v0.3.16-wave2a-r8"


def _ensure_db() -> None:
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_build_cache (
                cache_key TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                prompt_version TEXT,
                model_versions TEXT,
                cached_at INTEGER,
                last_hit_at INTEGER,
                hit_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    finally:
        conn.close()


def compute_cache_key(
    *,
    parsed_resumes: list[tuple[str, str]],
    user_preferences: dict,
    prompt_version: str = PROMPT_VERSION,
    model_versions: Optional[dict[str, str]] = None,
) -> str:
    """Hash the full input set for cache lookup/store.

    Inputs:
      - parsed_resumes: list of (filename, text) tuples (same shape used
        by build_profile_from_resumes)
      - user_preferences: dict of prefs (salary, locations, etc.)
      - prompt_version: bumped manually when prompt text changes
      - model_versions: dict like {"gemini": "gemini-2.5-pro",
        "anthropic": "claude-opus-4-5"} so a model swap busts the cache

    Hashing:
      - Resume text is hashed in (filename, content) tuples in canonical order
      - User preferences are JSON-canonicalized (sorted keys)
      - All four inputs concatenated, then SHA-256, first 32 hex chars
    """
    if model_versions is None:
        model_versions = {
            "gemini": config.PROFILE_BUILD_MODEL,
            "anthropic": config.PROFILE_ANTHROPIC_MODEL,
        }

    # Canonical resume bytes — sort by filename for stability
    resumes_sorted = sorted(parsed_resumes, key=lambda x: x[0])
    resume_blob = "\n".join(
        f"{name}\x00{text}" for name, text in resumes_sorted
    )
    prefs_blob = json.dumps(user_preferences, sort_keys=True, separators=(",", ":"))
    models_blob = json.dumps(model_versions, sort_keys=True, separators=(",", ":"))

    blob = "\n".join([resume_blob, prefs_blob, prompt_version, models_blob])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def lookup(cache_key: str) -> Optional[dict]:
    """Return cached profile dict on hit, None on miss or stale entry."""
    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        row = conn.execute(
            "SELECT profile_json, cached_at FROM profile_build_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None

        profile_json, cached_at = row
        if cached_at and (time.time() - cached_at) > CACHE_TTL_SECONDS:
            return None  # stale

        # Update hit stats (best-effort; ignore failures)
        try:
            conn.execute(
                "UPDATE profile_build_cache "
                "SET last_hit_at = ?, hit_count = hit_count + 1 "
                "WHERE cache_key = ?",
                (int(time.time()), cache_key),
            )
            conn.commit()
        except Exception:
            pass

        try:
            return json.loads(profile_json)
        except Exception:
            return None
    finally:
        conn.close()


def store(
    cache_key: str,
    profile_dict: dict,
    *,
    prompt_version: str = PROMPT_VERSION,
    model_versions: Optional[dict[str, str]] = None,
) -> None:
    """Persist a built profile under the given key. Idempotent on
    duplicate key (replaces with new content)."""
    if not profile_dict:
        return

    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO profile_build_cache "
            "(cache_key, profile_json, prompt_version, model_versions, "
            " cached_at, last_hit_at, hit_count) "
            "VALUES (?, ?, ?, ?, ?, NULL, 0)",
            (
                cache_key,
                json.dumps(profile_dict, default=str),
                prompt_version,
                json.dumps(model_versions or {}, sort_keys=True),
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def stats() -> dict[str, Any]:
    """Cache stats for diagnostics. Returns counts, hit rate, etc."""
    if not CACHE_DB_PATH.exists():
        return {"total": 0, "fresh": 0, "stale": 0, "total_hits": 0}
    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM profile_build_cache"
        ).fetchone()[0]
        cutoff = time.time() - CACHE_TTL_SECONDS
        stale = conn.execute(
            "SELECT COUNT(*) FROM profile_build_cache WHERE cached_at < ?",
            (cutoff,),
        ).fetchone()[0]
        total_hits = conn.execute(
            "SELECT COALESCE(SUM(hit_count), 0) FROM profile_build_cache"
        ).fetchone()[0]
        return {
            "total": total,
            "fresh": total - stale,
            "stale": stale,
            "total_hits": int(total_hits),
        }
    finally:
        conn.close()


def prune_stale() -> int:
    """Delete cache entries older than CACHE_TTL_SECONDS. Returns count
    deleted. Call on app startup or daily."""
    if not CACHE_DB_PATH.exists():
        return 0
    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        cutoff = time.time() - CACHE_TTL_SECONDS
        cur = conn.execute(
            "DELETE FROM profile_build_cache WHERE cached_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()

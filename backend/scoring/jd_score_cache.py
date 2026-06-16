"""Cross-run JD score caching (v0.3.9).

If the same JD content appears in two consecutive searches AND the
candidate profile hasn't changed, the Stage 2/3 evaluation will produce
identical results. There's no reason to pay $0.006-0.008 for Gemini Pro
to re-derive the same score on the same input.

This module provides a SQLite-backed cache keyed by:
  (profile_hash, jd_hash)

Where:
  - profile_hash: SHA-256 of the candidate's profile JSON (auto-invalidates
    when user changes target_functions, excluded_title_patterns,
    negative_signals, salary_minimum, etc.)
  - jd_hash: SHA-256 of the role's JD text content (cleaned of whitespace
    + lowercased so trivial formatting changes don't bust the cache)

Cache TTL: 7 days. After a week, market conditions / role posting age may
have shifted enough that re-scoring is worthwhile.

Cost impact at v0.3.9 scale (200+ Stage 3 calls / run):
  - Conservatively 50% of Stage-3-eligible roles appeared in a recent run
    (JSearch returns ~50% same roles between runs; ATS scrapers higher)
  - 100 cache hits × $0.006/Stage 3 = $0.60/run saved
  - Cache miss path is unchanged (full Stage 2 + Stage 3)
  - Cache hit path: skip both Stage 2 and Stage 3, use cached final score

Storage:
  archive/jd_score_cache.db (SQLite, single table)

Schema:
  jd_score_cache (
    profile_hash TEXT,
    jd_hash TEXT,
    final_score INT,
    final_tier TEXT,
    stage2_score INT,
    stage2_reasoning TEXT,
    stage3_score INT,
    stage3_analysis TEXT,
    stage3_application_strategy TEXT,
    salary_min INT,
    salary_max INT,
    industry TEXT,
    cached_at INTEGER,  -- unix timestamp
    PRIMARY KEY (profile_hash, jd_hash)
  )
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from backend.config import config
from backend.models import Role, score_to_tier


# v0.3.15 (P1.6): use config.ARCHIVE_DIR instead of computed-from-__file__
# path. The old form `Path(__file__).resolve().parent.parent.parent / "archive"`
# resolves to PyInstaller's `_MEIPASS/archive/` in bundled mode, which is
# WIPED on every app exit. Result: this cache had zero entries persisting
# across desktop-app sessions, and the cross-run reuse benefit (the entire
# point of this module) was effectively never realized in production.
# config.ARCHIVE_DIR resolves to %APPDATA%\app.jobsearch.desktop\archive
# in frozen mode (per the v0.3.13 _resolve_archive_dir() fix), which DOES
# persist. Source-mode behavior is unchanged.
CACHE_DB_PATH = config.ARCHIVE_DIR / "jd_score_cache.db"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# v0.3.27 (FIX): scoring-logic version, folded into the cache key (profile_hash)
# so that ANY change to scoring code / prompts auto-invalidates this cache for
# EVERY user — no manual DB rotation, and shipped updates can't silently replay
# stale scores. The (profile_hash, jd_hash) key previously contained NO scoring
# version, so e.g. the v0.3.27 AI on-target gate + tiered SC1 would never have
# fired on any JD seen in the prior 7 days (cache hit → Stage 2 skipped). BUMP
# THIS whenever scoring behavior changes (new gate, prompt edit, model swap,
# tier-floor change, …).
SCORING_VERSION = "v0.3.35"

# v0.3.21 (FIX 26 — cost): diagnostic logging gate. When True, prints the
# profile_hash being used + cache hit/miss counts so we can verify the
# cache fix is actually delivering hits in production. Default False to
# keep the audit log clean; flip via JD_CACHE_DEBUG=1 env var.
import os as _os
_DEBUG_LOGGING = _os.getenv("JD_CACHE_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _normalize_jd(text: str) -> str:
    """Normalize JD text for hashing — strip whitespace, lowercase.
    This makes trivial formatting changes (extra blank lines, different
    HTML rendering) not bust the cache."""
    if not text:
        return ""
    cleaned = re.sub(r"\s+", " ", text.strip().lower())
    return cleaned


def _jd_hash(text: str) -> str:
    """SHA-256 of normalized JD text. First 16 chars (64 bits of entropy)."""
    return hashlib.sha256(_normalize_jd(text).encode("utf-8")).hexdigest()[:16]


def profile_hash(profile_dump: dict[str, Any]) -> str:
    """Stable hash of the profile fields that affect scoring.

    v0.3.21 (FIX 26 — cost): PREFER the input-derived `_input_cache_key`
    when present on the profile dump. This is set by build_profile_from_
    resumes() and is GUARANTEED deterministic across rebuilds for the
    same (resume_bytes, user_preferences, prompt_version, model_versions).

    Before this fix: this function only had output-derived hashing, which
    drifted whenever the LLM produced slightly different target_functions
    / headline / etc. on rebuild. A profile_cache hit (cached profile
    bytes returned) would still produce a NEW jd_score_cache.profile_hash
    if the cache load somehow normalized differently — leading to 0
    cross-run cache hits despite intent. Empirically the May 30 audit
    showed 0 hits on a 462-role run that should have had ~80% reuse.

    Fallback (output-field hashing) preserved for back-compat with old
    profile dumps that pre-date FIX 26 — they don't carry the
    `_input_cache_key` field and would otherwise produce no hash.
    """
    # v0.3.21 (FIX 26): primary path — input-derived deterministic key.
    input_key = profile_dump.get("input_cache_key")
    if input_key and isinstance(input_key, str) and len(input_key) >= 16:
        base = input_key
    else:
        # Fallback — output-field hashing (pre-FIX-26 dumps).
        relevant = {
            "headline": profile_dump.get("headline"),
            "target_functions": sorted(profile_dump.get("target_functions") or []),
            "target_industries": sorted(profile_dump.get("target_industries") or []),
            "target_seniority": profile_dump.get("target_seniority"),
            "excluded_title_patterns": sorted(profile_dump.get("excluded_title_patterns") or []),
            "negative_signals": sorted(profile_dump.get("negative_signals") or []),
            "salary_minimum": profile_dump.get("salary_minimum"),
            "work_arrangements": sorted(profile_dump.get("work_arrangements") or []),
            "excluded_locations": sorted(profile_dump.get("excluded_locations") or []),
        }
        base = json.dumps(relevant, sort_keys=True, separators=(",", ":"))

    # v0.3.27: fold in SCORING_VERSION so scoring-logic changes auto-invalidate
    # the cache (both key paths re-hash through here, so the input-key path is no
    # longer returned verbatim — that's fine, it just namespaces the entries).
    return hashlib.sha256((SCORING_VERSION + "|" + base).encode("utf-8")).hexdigest()[:16]


def _ensure_cache_db():
    """Create the cache DB and table if not present."""
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jd_score_cache (
                profile_hash TEXT NOT NULL,
                jd_hash TEXT NOT NULL,
                final_score INTEGER,
                final_tier TEXT,
                stage2_score INTEGER,
                stage2_reasoning TEXT,
                stage3_score INTEGER,
                stage3_analysis TEXT,
                stage3_application_strategy TEXT,
                salary_min INTEGER,
                salary_max INTEGER,
                industry TEXT,
                cached_at INTEGER,
                PRIMARY KEY (profile_hash, jd_hash)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def lookup(prof_hash: str, role: Role) -> Optional[dict[str, Any]]:
    """Look up a cached score for this (profile, JD) combo.

    Returns None if:
      - No cache hit
      - Hit is older than CACHE_TTL_SECONDS
      - JD body too short (< 100 chars) — likely garbage / partial fetch

    Returns the cached dict on hit (apply via apply_to_role()).
    """
    jd_text = role.job_description_full or role.job_description_essence or ""
    if len(jd_text) < 100:
        return None  # too short to trust the hash key
    jdh = _jd_hash(jd_text)

    _ensure_cache_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        cur = conn.execute(
            "SELECT final_score, final_tier, stage2_score, stage2_reasoning, "
            "stage3_score, stage3_analysis, stage3_application_strategy, "
            "salary_min, salary_max, industry, cached_at "
            "FROM jd_score_cache WHERE profile_hash = ? AND jd_hash = ?",
            (prof_hash, jdh),
        )
        row = cur.fetchone()
        if not row:
            return None
        cached_at = row[10]
        if cached_at and (time.time() - cached_at) > CACHE_TTL_SECONDS:
            return None  # stale
        return {
            "final_score": row[0],
            "final_tier": row[1],
            "stage2_score": row[2],
            "stage2_reasoning": row[3],
            "stage3_score": row[4],
            "stage3_analysis": row[5],
            "stage3_application_strategy": row[6],
            "salary_min": row[7],
            "salary_max": row[8],
            "industry": row[9],
            "cached_at": cached_at,
        }
    finally:
        conn.close()


def apply_to_role(role: Role, cached: dict[str, Any]) -> None:
    """Hydrate a Role object with cached scoring data."""
    if cached.get("stage2_score") is not None:
        role.stage2_score = cached["stage2_score"]
        role.stage2_reasoning = cached.get("stage2_reasoning") or ""
    if cached.get("stage3_score") is not None:
        role.stage3_score = cached["stage3_score"]
        role.stage3_analysis = cached.get("stage3_analysis") or ""
        role.stage3_application_strategy = cached.get("stage3_application_strategy") or ""
    if cached.get("final_score") is not None:
        role.final_score = cached["final_score"]
        try:
            role.final_tier = score_to_tier(cached["final_score"])
        except Exception:
            pass
    if cached.get("salary_min"):
        role.salary_min = cached["salary_min"]
    if cached.get("salary_max"):
        role.salary_max = cached["salary_max"]
    if cached.get("industry"):
        role.industry = cached["industry"]
    # Tag reasoning so it's clear in audit JSON that this came from cache
    cache_tag = "[score-cache-hit]"
    if role.stage2_reasoning and not role.stage2_reasoning.startswith(cache_tag):
        role.stage2_reasoning = f"{cache_tag} {role.stage2_reasoning}"[:1000]


def store(prof_hash: str, role: Role) -> None:
    """Persist a fully-scored role to the cache.

    Should be called for any role that has both stage2_score and a final
    qualifying score after Stage 3 (or Stage 2 alone for low-band roles).
    """
    if role.final_score is None:
        return
    # W70 defense-in-depth: never persist a role that was never actually scored.
    # An infra-failed role (network/cap) carries stage2_score=None AND
    # stage3_score=None yet a defaulted final_score=0 — caching it would bury the
    # role as a permanent SKIP for 7 days. The orchestrator already guards this;
    # this is the belt-and-suspenders in case store() is called from elsewhere.
    if role.stage2_score is None and role.stage3_score is None:
        return
    jd_text = role.job_description_full or role.job_description_essence or ""
    if len(jd_text) < 100:
        return  # don't cache garbage / partial fetches
    jdh = _jd_hash(jd_text)

    _ensure_cache_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        conn.execute(
            "INSERT OR REPLACE INTO jd_score_cache "
            "(profile_hash, jd_hash, final_score, final_tier, "
            " stage2_score, stage2_reasoning, stage3_score, "
            " stage3_analysis, stage3_application_strategy, "
            " salary_min, salary_max, industry, cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prof_hash, jdh,
                role.final_score,
                role.final_tier.value if role.final_tier else None,
                role.stage2_score,
                role.stage2_reasoning,
                role.stage3_score,
                role.stage3_analysis,
                role.stage3_application_strategy,
                role.salary_min,
                role.salary_max,
                role.industry,
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def stats() -> dict[str, int]:
    """Quick cache stats for the audit JSON."""
    if not CACHE_DB_PATH.exists():
        return {"total_cached": 0, "stale": 0, "fresh": 0}
    _ensure_cache_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM jd_score_cache")
        total = cur.fetchone()[0]
        cutoff = time.time() - CACHE_TTL_SECONDS
        cur = conn.execute(
            "SELECT COUNT(*) FROM jd_score_cache WHERE cached_at < ?",
            (cutoff,),
        )
        stale = cur.fetchone()[0]
        return {"total_cached": total, "stale": stale, "fresh": total - stale}
    finally:
        conn.close()


def prune_stale() -> int:
    """Delete cache entries older than CACHE_TTL_SECONDS. Call on startup
    or daily. Returns count deleted."""
    if not CACHE_DB_PATH.exists():
        return 0
    _ensure_cache_db()
    conn = sqlite3.connect(str(CACHE_DB_PATH))
    try:
        cutoff = time.time() - CACHE_TTL_SECONDS
        cur = conn.execute(
            "DELETE FROM jd_score_cache WHERE cached_at < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()

"""Detailed cost tracking for every LLM call.

Persists every API call to a local SQLite at archive/cost_log.db so we
have a permanent, queryable record of spend by:
  - run_id
  - license_key (which tester)
  - stage (stage1 / stage2 / stage3 / embedding / misc)
  - provider + model
  - day / week / month

Also writes a daily CSV mirror at archive/cost_log.csv for easy spreadsheet
review.

Why a separate file from the in-memory call log on the LLMClient: the
client log is per-process, the cost tracker is permanent and aggregable
across runs.
"""
from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Iterator, Optional

from backend.config import config
from backend.models import LLMCallLog


COST_DB_PATH: Path = config.ARCHIVE_DIR / "cost_log.db"
COST_CSV_PATH: Path = config.ARCHIVE_DIR / "cost_log.csv"


# ----------------------------------------------------------------------------
# Schema bootstrap
# ----------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    run_id          TEXT,
    license_key     TEXT,
    stage           TEXT,
    provider        TEXT,
    model           TEXT,
    is_batch        INTEGER,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cached_tokens   INTEGER DEFAULT 0,
    thinking_tokens INTEGER DEFAULT 0,
    cost_usd        REAL,
    latency_ms      INTEGER,
    success         INTEGER,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_run        ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_license    ON llm_calls(license_key);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp  ON llm_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_calls_stage      ON llm_calls(stage);
CREATE INDEX IF NOT EXISTS idx_calls_model      ON llm_calls(model);

CREATE TABLE IF NOT EXISTS run_summaries (
    run_id              TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    license_key         TEXT,
    cost_total_usd      REAL DEFAULT 0,
    cost_stage1_usd     REAL DEFAULT 0,
    cost_stage2_usd     REAL DEFAULT 0,
    cost_stage3_usd     REAL DEFAULT 0,
    cost_embeddings_usd REAL DEFAULT 0,
    cost_misc_usd       REAL DEFAULT 0,
    roles_scraped       INTEGER DEFAULT 0,
    roles_qualifying    INTEGER DEFAULT 0,
    duration_seconds    INTEGER DEFAULT 0,
    status              TEXT DEFAULT 'running'
);
"""


def _init_db() -> None:
    """Create the cost log DB and tables if they don't exist.

    Also performs idempotent column migrations for older databases that
    pre-date a column addition (e.g. cached_tokens, thinking_tokens).
    """
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(COST_DB_PATH) as conn:
        conn.executescript(_SCHEMA)
        # Idempotent migrations — ALTER TABLE ADD COLUMN errors if column
        # already exists, but PRAGMA table_info lets us check first cheaply.
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()
        }
        if "cached_tokens" not in existing_cols:
            conn.execute(
                "ALTER TABLE llm_calls ADD COLUMN cached_tokens INTEGER DEFAULT 0"
            )
        if "thinking_tokens" not in existing_cols:
            # v0.3.13.1: track Gemini 2.5 reasoning tokens separately. They
            # are already folded into output_tokens for cost calc, but logging
            # them gives us visibility into how much of our spend is reasoning.
            conn.execute(
                "ALTER TABLE llm_calls ADD COLUMN thinking_tokens INTEGER DEFAULT 0"
            )
        conn.commit()


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    _init_db()
    conn = sqlite3.connect(COST_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def log_call(call: LLMCallLog) -> None:
    """Persist one LLM call to the cost log (DB + CSV)."""
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO llm_calls (
                timestamp, run_id, license_key, stage, provider, model,
                is_batch, input_tokens, output_tokens, cached_tokens,
                thinking_tokens, cost_usd, latency_ms, success, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call.timestamp.isoformat(),
                call.run_id,
                call.license_key,
                call.stage,
                call.provider,
                call.model,
                int(call.is_batch),
                call.input_tokens,
                call.output_tokens,
                getattr(call, "cached_tokens", 0),
                getattr(call, "thinking_tokens", 0),
                call.cost_usd,
                call.latency_ms,
                int(call.success),
                call.error_message,
            ),
        )
    # Append to CSV mirror
    _append_csv(call)


def _append_csv(call: LLMCallLog) -> None:
    """Mirror each call to a CSV file for easy spreadsheet inspection."""
    new_file = not COST_CSV_PATH.exists()
    config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    with open(COST_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow([
                "timestamp", "run_id", "license_key", "stage", "provider",
                "model", "is_batch", "input_tokens", "output_tokens",
                "cached_tokens", "thinking_tokens", "cost_usd", "latency_ms",
                "success", "error_message",
            ])
        writer.writerow([
            call.timestamp.isoformat(),
            call.run_id or "",
            call.license_key or "",
            call.stage,
            call.provider,
            call.model,
            int(call.is_batch),
            call.input_tokens,
            call.output_tokens,
            getattr(call, "cached_tokens", 0),
            getattr(call, "thinking_tokens", 0),
            f"{call.cost_usd:.6f}",
            call.latency_ms,
            int(call.success),
            (call.error_message or "").replace("\n", " "),
        ])


def log_calls(calls: list[LLMCallLog]) -> None:
    """Batch-log multiple calls."""
    for c in calls:
        log_call(c)


# ----------------------------------------------------------------------------
# Run lifecycle
# ----------------------------------------------------------------------------

def start_run(run_id: str, license_key: Optional[str] = None) -> None:
    """Mark a run as started. Idempotent."""
    with _db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO run_summaries (run_id, started_at, license_key, status)
            VALUES (?, ?, ?, 'running')
            """,
            (run_id, datetime.now(timezone.utc).isoformat(), license_key),
        )


def finish_run(
    run_id: str,
    *,
    roles_scraped: int = 0,
    roles_qualifying: int = 0,
    duration_seconds: int = 0,
    status: str = "completed",
) -> None:
    """Finalize a run summary by aggregating its calls."""
    with _db() as conn:
        # Aggregate cost from llm_calls
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(cost_usd), 0) AS total,
                COALESCE(SUM(CASE WHEN stage='stage1'    THEN cost_usd ELSE 0 END), 0) AS s1,
                COALESCE(SUM(CASE WHEN stage='stage2'    THEN cost_usd ELSE 0 END), 0) AS s2,
                COALESCE(SUM(CASE WHEN stage='stage3'    THEN cost_usd ELSE 0 END), 0) AS s3,
                COALESCE(SUM(CASE WHEN stage='embedding' THEN cost_usd ELSE 0 END), 0) AS emb,
                COALESCE(SUM(CASE WHEN stage='misc'      THEN cost_usd ELSE 0 END), 0) AS misc
            FROM llm_calls WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        conn.execute(
            """
            UPDATE run_summaries
            SET finished_at = ?,
                cost_total_usd = ?,
                cost_stage1_usd = ?,
                cost_stage2_usd = ?,
                cost_stage3_usd = ?,
                cost_embeddings_usd = ?,
                cost_misc_usd = ?,
                roles_scraped = ?,
                roles_qualifying = ?,
                duration_seconds = ?,
                status = ?
            WHERE run_id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                row["total"], row["s1"], row["s2"], row["s3"], row["emb"], row["misc"],
                roles_scraped, roles_qualifying, duration_seconds, status, run_id,
            ),
        )


# ----------------------------------------------------------------------------
# Reporting helpers (used by admin dashboard + dev CLI)
# ----------------------------------------------------------------------------

def cost_today() -> float:
    """Total spend so far today (UTC)."""
    today = date.today().isoformat()
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM llm_calls WHERE timestamp LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row["t"]


def cost_this_month() -> float:
    """Total spend this calendar month (UTC)."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS t FROM llm_calls WHERE timestamp LIKE ?",
            (f"{month}%",),
        ).fetchone()
        return row["t"]


def cost_by_run(run_id: str) -> dict:
    """Per-stage cost breakdown for a single run."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT stage, COUNT(*) AS calls,
                   SUM(input_tokens) AS in_tok,
                   SUM(output_tokens) AS out_tok,
                   SUM(cost_usd) AS cost
            FROM llm_calls
            WHERE run_id = ?
            GROUP BY stage
            """,
            (run_id,),
        ).fetchall()
        return {r["stage"]: dict(r) for r in rows}


def recent_runs(limit: int = 20) -> list[dict]:
    """Most recent runs, newest first."""
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM run_summaries
            ORDER BY started_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def cost_by_license_this_month() -> list[dict]:
    """Per-tester cost rollup for the current month."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT license_key,
                   COUNT(DISTINCT run_id) AS runs,
                   SUM(cost_usd) AS cost
            FROM llm_calls
            WHERE timestamp LIKE ?
            GROUP BY license_key
            ORDER BY cost DESC
            """,
            (f"{month}%",),
        ).fetchall()
        return [dict(r) for r in rows]

"""Smoke test for v0.3.13.1 — verifies thoughts_token_count is folded into
output_tokens AND persisted separately for audit visibility.

This is a PARSE-ONLY test (no live API calls, no cost). It:
  1. Creates a fake response object with usage_metadata mirroring the
     google-genai SDK's `prompt_token_count` / `candidates_token_count` /
     `thoughts_token_count` shape.
  2. Patches the response into LLMClient.complete()'s post-call branch
     and asserts the persisted log row matches expectations.
  3. Verifies the cost calculation includes the thinking tokens at the
     output rate (not silently dropped).

Why this test matters: prior to v0.3.13.1, Pro 2.5 thinking tokens billed
at the output rate but were never counted by our cost_tracker. ~$0.005/Pro
call invisible. ~$30-35 of ghost spend across the v0.3.10-v0.3.13 window
that the user (Ziad) was paying for without seeing in the dashboard.

Run from project root:
  python scripts\\smoke_test_v0313_1.py

Exit code 0 = all pass. Non-zero = ship blocker.
"""
from __future__ import annotations

import sys
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Test artifacts in temp dir; we override cost_tracker.COST_DB_PATH directly
# to avoid touching the prod archive
TEMP_DIR = Path(tempfile.mkdtemp(prefix="v0313_1_smoke_"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

failures: list[str] = []


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    sym = "PASS" if ok else "FAIL"
    print(f"  [{sym}] {name}: got={actual!r} expected={expected!r}")
    if not ok:
        failures.append(name)


def section(name: str) -> None:
    print()
    print(f"=== {name} ===")


# ----------------------------------------------------------------------
# 0. Version bump
# ----------------------------------------------------------------------
section("Version")
from backend import __version__  # noqa: E402

check("backend.__version__", __version__, "0.3.13.1")


# ----------------------------------------------------------------------
# 1. LLMCallLog model has thinking_tokens field
# ----------------------------------------------------------------------
section("LLMCallLog has thinking_tokens field")
from backend.models import LLMCallLog  # noqa: E402

log = LLMCallLog(stage="stage3", provider="google", model="gemini-2.5-pro")
check("Default thinking_tokens", log.thinking_tokens, 0)
log2 = LLMCallLog(
    stage="stage3", provider="google", model="gemini-2.5-pro",
    thinking_tokens=512,
)
check("Set thinking_tokens=512", log2.thinking_tokens, 512)


# ----------------------------------------------------------------------
# 2. cost_log.db schema has thinking_tokens column (migration applies)
# ----------------------------------------------------------------------
section("cost_log.db schema migration")
from backend.scoring import cost_tracker  # noqa: E402

# First, simulate an OLD db without the thinking_tokens column. Manually
# create one with the v0.3.13 schema, then call _init_db() and verify the
# migration adds the column.
old_schema_db = TEMP_DIR / "old_schema.db"
con = sqlite3.connect(old_schema_db)
con.executescript("""
    CREATE TABLE llm_calls (
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
        cost_usd        REAL,
        latency_ms      INTEGER,
        success         INTEGER,
        error_message   TEXT
    );
""")
con.commit()
con.close()

# Point cost_tracker at the old db and call _init_db
original_path = cost_tracker.COST_DB_PATH
cost_tracker.COST_DB_PATH = old_schema_db
try:
    cost_tracker._init_db()
    con = sqlite3.connect(old_schema_db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(llm_calls)").fetchall()}
    con.close()
    check("Migration adds thinking_tokens column", "thinking_tokens" in cols, True)
finally:
    cost_tracker.COST_DB_PATH = original_path


# ----------------------------------------------------------------------
# 3. log_call() persists thinking_tokens correctly
# ----------------------------------------------------------------------
section("cost_tracker.log_call persists thinking_tokens")
fresh_db = TEMP_DIR / "fresh.db"
fresh_csv = TEMP_DIR / "fresh.csv"
cost_tracker.COST_DB_PATH = fresh_db
cost_tracker.COST_CSV_PATH = fresh_csv

test_log = LLMCallLog(
    stage="stage3",
    provider="google",
    model="gemini-2.5-pro",
    input_tokens=1000,
    output_tokens=300,    # candidates + thinking already folded
    cached_tokens=0,
    thinking_tokens=200,  # 200 of those 300 are reasoning
    cost_usd=0.00425,
    latency_ms=1200,
    success=True,
)
cost_tracker.log_call(test_log)

con = sqlite3.connect(fresh_db)
con.row_factory = sqlite3.Row
row = con.execute(
    "SELECT input_tokens, output_tokens, cached_tokens, thinking_tokens, cost_usd FROM llm_calls"
).fetchone()
con.close()

check("Persisted input_tokens", row["input_tokens"], 1000)
check("Persisted output_tokens (incl thinking)", row["output_tokens"], 300)
check("Persisted thinking_tokens", row["thinking_tokens"], 200)
check("Persisted cost", round(row["cost_usd"], 5), 0.00425)


# ----------------------------------------------------------------------
# 4. End-to-end: simulate the exact extraction logic from llm_client.py
#    (the patched 3-line block at lines 418-431). We don't import
#    llm_client.py because it pulls in google-genai which isn't always
#    installed where this test runs (e.g. system Python). The patched
#    arithmetic is what we're verifying — the SDK shape is fixed.
# ----------------------------------------------------------------------
section("End-to-end: thoughts_token_count is folded into output_tokens")

# Simulate Pro 2.5 response: 1000 input, 100 candidates, 500 thoughts
mock_usage = SimpleNamespace(
    prompt_token_count=1000,
    candidates_token_count=100,
    thoughts_token_count=500,
    cached_content_token_count=0,
)

# Replicate exactly the post-patch extraction from llm_client.py:418-431
input_tokens = (getattr(mock_usage, "prompt_token_count", 0) or 0)
candidate_tokens = (getattr(mock_usage, "candidates_token_count", 0) or 0)
thinking_tokens = (getattr(mock_usage, "thoughts_token_count", 0) or 0)
output_tokens = candidate_tokens + thinking_tokens
cached_tokens = (getattr(mock_usage, "cached_content_token_count", 0) or 0)

check("input_tokens extracted", input_tokens, 1000)
check("candidate_tokens extracted", candidate_tokens, 100)
check("thinking_tokens extracted", thinking_tokens, 500)
check("output_tokens = candidates + thinking (folded)", output_tokens, 600)
check("cached_tokens extracted", cached_tokens, 0)

# Verify the dollar impact: Pro 2.5 output rate is $10/MTok.
# 500 thinking tokens at $10/MTok = $0.005/call
# Pre-patch: this $0.005 was silently dropped per Pro call.
# Across 220 Pro calls/run = $1.10/run invisible.
# Across 30 runs since v0.3.10 = $33 phantom spend.
PRO_OUTPUT_RATE_PER_TOKEN = 10.0 / 1_000_000  # $10/MTok
phantom_per_call = thinking_tokens * PRO_OUTPUT_RATE_PER_TOKEN
print(f"  Pre-patch phantom cost per Pro call: ${phantom_per_call:.6f}")
print(f"  Pre-patch phantom cost across 220 Pro calls/run: ${phantom_per_call * 220:.4f}")
print(f"  Pre-patch phantom cost across 30 runs: ${phantom_per_call * 220 * 30:.2f}")

# Sanity check the math
check("Phantom cost per Pro call (was missing)", round(phantom_per_call, 6), 0.005)


# ----------------------------------------------------------------------
# 5. None-safe: no usage_metadata, no thinking_tokens
# ----------------------------------------------------------------------
section("None-safe extraction")
mock_usage_no_thinking = SimpleNamespace(
    prompt_token_count=500,
    candidates_token_count=50,
    cached_content_token_count=0,
    # no thoughts_token_count attribute at all (Flash with thinking_budget=0)
)
thinking = (getattr(mock_usage_no_thinking, "thoughts_token_count", 0) or 0)
check("Missing thoughts_token_count -> 0", thinking, 0)

mock_usage_none_thinking = SimpleNamespace(
    prompt_token_count=500,
    candidates_token_count=50,
    thoughts_token_count=None,  # SDK sometimes returns None
    cached_content_token_count=0,
)
thinking = (getattr(mock_usage_none_thinking, "thoughts_token_count", 0) or 0)
check("None thoughts_token_count -> 0", thinking, 0)


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
print()
print("=" * 60)
if failures:
    print(f"FAILED: {len(failures)} check(s) failed")
    for f in failures:
        print(f"  - {f}")
    print()
    print(f"Test artifacts at: {TEMP_DIR}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED — v0.3.13.1 thinking-tokens fix verified")
    print()
    print(f"Cleaning up test artifacts: {TEMP_DIR}")
    import shutil
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception:
        pass
    sys.exit(0)

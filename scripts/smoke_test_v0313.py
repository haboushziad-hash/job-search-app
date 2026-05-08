"""Smoke test for v0.3.13 fixes.

Verifies all six fixes load correctly and have the expected behavior:
  1. GoogleJobs X-Tester-UUID header
  2. Refined silent-zero alert
  3. Retry-aware logging in llm_client
  4. Persistent cost_log.db path
  5. Balance reconciliation in audit
  6. Stage 3 concurrency 6 -> 3 (env-var configurable)

Run from project root:
  backend\venv\Scripts\python.exe scripts\smoke_test_v0313.py
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

# Ensure we import from the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set proxy mode env vars so we test the proxy code paths
os.environ["LLM_PROXY_URL"] = "https://api.findmesomedamnjobz.com"
os.environ["TESTER_UUID"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
os.environ["STAGE3_CONCURRENCY"] = "3"


results: list[tuple[str, str, object, object]] = []


def check(label: str, value: object, expected: object) -> None:
    status = "PASS" if value == expected else "FAIL"
    results.append((status, label, value, expected))
    print(f"  [{status}] {label}: got={value!r}")


def section(name: str) -> None:
    print()
    print(f"=== {name} ===")


# ----------------------------------------------------------------------
# Fix 0: Version bump
# ----------------------------------------------------------------------
section("Version")
from backend import __version__
check("backend.__version__", __version__, "0.3.13.1")


# ----------------------------------------------------------------------
# Fix 1: GoogleJobs X-Tester-UUID header
# ----------------------------------------------------------------------
section("Fix 1 — GoogleJobs X-Tester-UUID header")
from backend.scraper.google_jobs import _proxy_headers, _dfseo_endpoints

headers = _proxy_headers()
check("Proxy headers contain X-Tester-UUID", "X-Tester-UUID" in headers, True)
check("UUID matches env value",
      headers.get("X-Tester-UUID"),
      "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
check("Content-Type set", headers.get("Content-Type"), "application/json")

# Force config to think we're in proxy mode
from backend import config as cfg
cfg.config.LLM_PROXY_URL = "https://api.findmesomedamnjobz.com"
post_url, get_base_url, auth = _dfseo_endpoints()
check("Proxy mode resolves to Worker URL",
      "/v1/scraper/dataforseo/jobs/task_post" in post_url, True)
check("Proxy mode auth is None (Worker injects)", auth is None, True)


# ----------------------------------------------------------------------
# Fix 2: Refined silent-zero alert
# ----------------------------------------------------------------------
section("Fix 2 — Silent-zero alert refinement")
from backend.scraper import orchestrator as orch
src_orch = inspect.getsource(orch.scrape_all)
check("Has ran_but_zero_outcome branch", "ran_but_zero_outcome" in src_orch, True)
check("Checks scraper cost_estimate", "scraper_cost == 0" in src_orch, True)


# ----------------------------------------------------------------------
# Fix 3: Retry-aware logging
# ----------------------------------------------------------------------
section("Fix 3 — Retry-aware logging in llm_client.py")
from backend.scoring import llm_client as llmc
src_complete = inspect.getsource(llmc.GeminiClient.complete)
check("@retry decorator removed",
      not src_complete.lstrip().startswith("@retry"), True)
check("Manual retry loop present", "for attempt_idx in range" in src_complete, True)
check("Logs failed attempts", "failed_log" in src_complete, True)
check("Has _classify_error helper", "_classify_error" in src_complete, True)
check("Has _estimate_failed_cost helper", "_estimate_failed_cost" in src_complete, True)
check("Conservative 429 estimate $0.0005",
      "0.0005 if is_pro else 0.0001" in src_complete, True)
check("5xx estimate $0.005 for Pro",
      "0.005 if is_pro else 0.0008" in src_complete, True)


# ----------------------------------------------------------------------
# Fix 4: Persistent cost_log.db path
# ----------------------------------------------------------------------
section("Fix 4 — Persistent cost_log.db path resolver")
from backend.config import _resolve_archive_dir, ARCHIVE_DIR
src_resolve = inspect.getsource(_resolve_archive_dir)
check("Resolver checks sys.frozen", "frozen" in src_resolve, True)
check("Windows path uses APPDATA", "APPDATA" in src_resolve, True)
check("macOS path uses Library/Application Support",
      "Library" in src_resolve and "Application Support" in src_resolve, True)
check("Linux path uses XDG_DATA_HOME or ~/.local/share",
      "XDG_DATA_HOME" in src_resolve and ".local" in src_resolve, True)
check("Source-mode unchanged (PROJECT_ROOT/archive)",
      ARCHIVE_DIR.name == "archive", True)


# ----------------------------------------------------------------------
# Fix 5: Balance reconciliation in audit
# ----------------------------------------------------------------------
section("Fix 5 — Balance reconciliation in audit JSON")
from backend.storage import audit as audit_mod
src_audit = inspect.getsource(audit_mod)
check("audit reads BALANCE_GEMINI_PRE", "BALANCE_GEMINI_PRE" in src_audit, True)
check("audit reads BALANCE_GEMINI_POST", "BALANCE_GEMINI_POST" in src_audit, True)
check("audit reads BALANCE_CLAUDE_PRE", "BALANCE_CLAUDE_PRE" in src_audit, True)
check("audit reads BALANCE_DATAFORSEO_PRE",
      "BALANCE_DATAFORSEO_PRE" in src_audit, True)
check("audit emits balance_reconciliation block",
      "balance_reconciliation" in src_audit, True)
check("audit computes discrepancy_pct", "discrepancy_pct" in src_audit, True)
check("audit rolls up failed_attempt_cost_usd",
      "failed_attempt_cost_usd" in src_audit, True)


# ----------------------------------------------------------------------
# Fix 6: Stage 3 concurrency env-var override
# ----------------------------------------------------------------------
section("Fix 6 — Stage 3 concurrency 6 -> 3 (env-var configurable)")
from backend.scoring import stage3_deep_eval as s3
sig = inspect.signature(s3.stage3_deep_eval)
default_conc = sig.parameters["concurrency"].default
check("Stage 3 default concurrency = 3 (with STAGE3_CONCURRENCY=3 env)",
      default_conc, 3)


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
print()
fails = [r for r in results if r[0] == "FAIL"]
print(f"Total: {len(results)} checks, {len(results) - len(fails)} passed, {len(fails)} failed")
if fails:
    print()
    print("FAILED CHECKS:")
    for status, label, value, expected in fails:
        print(f"  - {label}")
        print(f"      got:      {value!r}")
        print(f"      expected: {expected!r}")
    sys.exit(1)
else:
    print()
    print("ALL CHECKS PASSED ✓ — v0.3.13 fixes verified")
    sys.exit(0)

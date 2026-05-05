"""Cross-tester telemetry — anonymous run/scraper/bug data shared via cloud-synced folder.

v0.2.1 introduces three new sidecar files alongside the existing
`market_contributions.jsonl`:

  * `run_telemetry.jsonl`   — one entry per completed run; metadata only
                              (app_version, OS, duration, scraped/qualifying
                              counts, tier breakdown, keyword count). No
                              role-level data.
  * `scraper_health.jsonl`  — one entry per scraper per run; raw_scraped,
                              qualifying, elapsed, errored, error_type,
                              quota_exhausted. Powers cohort-wide scraper
                              degradation detection in v0.3+.
  * `bug_reports.jsonl`     — sanitized error patterns. error_class +
                              module + count + first_seen. NO stack
                              traces, NO file paths beyond module name,
                              NO role data.

Privacy posture:
  - Files live ONLY in the user's chosen audit folder, which they sync
    via Dropbox/Drive/OneDrive (or don't sync at all).
  - No central server. No user identifiers. No PII.
  - Each file has a header comment that the user can read to understand
    what's collected.
  - All three files are write-only from this module's perspective in
    v0.2.1 — no reader code yet. v0.3 will add consumers (cohort
    dashboards, automated tenant-disable, scoring calibration loops).

The data flowing into these files starts the moment v0.2.1 ships, so
by the time consumers exist, we have weeks of cohort data ready.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Header comments emitted at the top of each new file the first time
# we write to it. Plain text, ignored by JSONL readers (line starts
# with `#` which won't parse as JSON, so we skip these in the reader
# logic in v0.3).
_HEADER_RUN_TELEMETRY = (
    "# run_telemetry.jsonl — one entry per completed run\n"
    "# Anonymous run-level metadata: app_version, OS, duration, scraped/qualifying counts.\n"
    "# No role data, no user identifiers. Safe to share.\n"
)
_HEADER_SCRAPER_HEALTH = (
    "# scraper_health.jsonl — one entry per scraper per run\n"
    "# Operational data: raw_scraped, qualifying, elapsed, errored, quota_exhausted.\n"
    "# Powers cohort-wide scraper degradation detection. No role data.\n"
)
_HEADER_BUG_REPORTS = (
    "# bug_reports.jsonl — sanitized error patterns\n"
    "# error_class + module + count + first_seen. No stack traces, no file paths\n"
    "# beyond module name, no role data.\n"
)


def _append_jsonl(path: Path, entry: dict, header: str) -> None:
    """Append one JSON line to `path`, writing the header comment if
    the file doesn't exist yet. Atomic-ish via append-only writes (each
    line is independently parseable; partial writes truncate to the
    last complete line on next read)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8") as f:
        if write_header:
            f.write(header)
        f.write(json.dumps(entry, default=str) + "\n")


# ----------------------------------------------------------------------------
# Run telemetry
# ----------------------------------------------------------------------------

def write_run_telemetry(
    audit_folder: Path,
    *,
    run_id: str,
    app_version: str,
    duration_seconds: float,
    total_scraped: int,
    after_hard_filters: int,
    qualifying_final: int,
    tier_breakdown: dict[str, int],
    keyword_count: int,
    target_title_count: int = 0,
    cache_hit: bool = False,
) -> None:
    """Append one anonymous run-summary entry. Caller is `runner.py` at
    end of search."""
    import platform
    entry = {
        "run_id": run_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "app_version": app_version,
        "os": f"{platform.system()} {platform.release()}",
        "duration_seconds": int(duration_seconds),
        "total_scraped": int(total_scraped),
        "after_hard_filters": int(after_hard_filters),
        "qualifying_final": int(qualifying_final),
        "tier_breakdown": dict(tier_breakdown or {}),
        "keyword_count": int(keyword_count),
        "target_title_count": int(target_title_count),
        "cache_hit": bool(cache_hit),
    }
    _append_jsonl(Path(audit_folder) / "run_telemetry.jsonl", entry, _HEADER_RUN_TELEMETRY)


# ----------------------------------------------------------------------------
# Per-scraper health
# ----------------------------------------------------------------------------

def write_scraper_health(
    audit_folder: Path,
    *,
    run_id: str,
    app_version: str,
    per_source: dict[str, dict[str, Any]],
) -> None:
    """Append one entry per scraper for this run. `per_source` is the
    dict assembled by the scraper orchestrator: keys are scraper names,
    values are dicts with raw_scraped/qualifying_final/elapsed_s/errored/
    error/quota_exhausted/quota_exhausted_reason."""
    folder = Path(audit_folder)
    out_path = folder / "scraper_health.jsonl"
    folder.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists()
    ts = datetime.now(timezone.utc).isoformat()
    with out_path.open("a", encoding="utf-8") as f:
        if write_header:
            f.write(_HEADER_SCRAPER_HEALTH)
        for source, data in (per_source or {}).items():
            entry = {
                "run_id": run_id,
                "ts": ts,
                "app_version": app_version,
                "scraper": source,
                "raw_scraped": int(data.get("raw_scraped") or data.get("roles") or 0),
                "after_dedup": data.get("after_dedup"),
                "after_hard_filters": data.get("after_hard_filters"),
                "qualifying_final": data.get("qualifying_final"),
                "elapsed_s": data.get("elapsed_s"),
                "errored": bool(data.get("errored", False)),
                "error_type": _sanitize_error(data.get("error")),
                "quota_exhausted": bool(data.get("quota_exhausted", False)),
                "quota_exhausted_reason": data.get("quota_exhausted_reason") or None,
            }
            # Drop None values to keep file compact + forward-compatible
            entry = {k: v for k, v in entry.items() if v is not None}
            f.write(json.dumps(entry, default=str) + "\n")


# ----------------------------------------------------------------------------
# Bug reports (sanitized)
# ----------------------------------------------------------------------------

# Allowlist of error class names we'll surface in bug_reports.jsonl.
# Any class not in this set is reported as "OtherException" with no
# detail. This avoids leaking module names from third-party exceptions
# we haven't reviewed.
_KNOWN_ERROR_CLASSES = {
    "ConnectionError", "TimeoutError", "asyncio.TimeoutError",
    "HTTPError", "ConnectError", "ReadTimeout", "ConnectTimeout",
    "JSONDecodeError", "ValidationError", "KeyError", "ValueError",
    "TypeError", "AttributeError", "IndexError", "ImportError",
    "UnicodeDecodeError", "UnicodeEncodeError", "PermissionError",
    "FileNotFoundError", "OSError",
}

# Allowlist of module prefixes we'll surface as the `module` field.
# Internal-only — third-party module paths are collapsed to "external".
_INTERNAL_MODULE_PREFIXES = ("backend.",)


def _sanitize_error(raw_err: Any) -> str | None:
    """Return a short error-class label for `raw_err`, or None.
    We never include the message body — that can contain URLs with
    query params, role titles, or other accidental PII."""
    if raw_err is None:
        return None
    s = str(raw_err)
    # Just take the leading class name if it looks like "ClassName: ..."
    head = s.split(":", 1)[0].strip()
    if head in _KNOWN_ERROR_CLASSES:
        return head
    return "OtherException"


def _sanitize_module(raw_module: str | None) -> str:
    if not raw_module:
        return "unknown"
    if any(raw_module.startswith(p) for p in _INTERNAL_MODULE_PREFIXES):
        return raw_module
    return "external"


def write_bug_reports(
    audit_folder: Path,
    *,
    run_id: str,
    app_version: str,
    bugs: Iterable[dict],
) -> int:
    """Append one entry per unique (error_class, module) pair seen
    during the run. `bugs` is an iterable of dicts with at least
    `error_class`, `module`, `count`. Returns number of entries written."""
    folder = Path(audit_folder)
    out_path = folder / "bug_reports.jsonl"
    folder.mkdir(parents=True, exist_ok=True)
    write_header = not out_path.exists()
    ts = datetime.now(timezone.utc).isoformat()
    n = 0
    with out_path.open("a", encoding="utf-8") as f:
        if write_header:
            f.write(_HEADER_BUG_REPORTS)
        for bug in bugs or []:
            cls = _sanitize_error(bug.get("error_class") or bug.get("exception"))
            if cls is None:
                continue
            entry = {
                "run_id": run_id,
                "ts": ts,
                "app_version": app_version,
                "error_class": cls,
                "module": _sanitize_module(bug.get("module")),
                "count": int(bug.get("count", 1)),
            }
            f.write(json.dumps(entry, default=str) + "\n")
            n += 1
    return n

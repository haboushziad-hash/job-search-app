"""Test runner — drives the /profile/build + /search/run API directly.

Bypasses the GUI so you can fire searches for multiple personas without
clicking through the desktop app. Reuses the running tauri dev backend
on localhost:8765.

Usage:
    python scripts/test_run_search.py --resume "C:/path/to/Ziad_Resume.pdf" --label ziad --salary 130000
    python scripts/test_run_search.py --resume "C:/path/to/Zach_Resume.pdf" --label zach --salary 110000
    python scripts/test_run_search.py --resume "C:/path/to/Ryan_Resume.pdf" --label ryan --salary 100000

Outputs a labeled summary to stdout + saves a copy of the latest audit
JSON to ./scripts/test_runs/<label>_<timestamp>.json for easy comparison.

Prerequisites:
  - Backend.exe must be running (`npm run tauri dev` is sufficient — it
    spawns the backend on localhost:8765)
  - Resumes must be at the paths you pass

Notes:
  - The tester UUID is shared with the desktop app, so audit uploads to
    R2 still work and these test runs will appear in your central archive.
  - Default salary $130K, defaults remote+hybrid+on-site arrangements,
    defaults Richmond VA + Washington DC as acceptable locations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx


BACKEND_BASE = "http://localhost:8765"
POLL_INTERVAL_SECONDS = 3.0
PROFILE_BUILD_TIMEOUT_SECONDS = 600   # 10 min
SEARCH_RUN_TIMEOUT_SECONDS = 1500    # 25 min


async def build_profile(
    client: httpx.AsyncClient,
    resume_path: Path,
    *,
    salary_minimum: int,
    extra_context: str,
    locations: list[str],
    radii: list[int],
    work_arrangements: list[str],
) -> dict:
    """POST resume + preferences to /profile/build, poll until done, return profile."""
    print(f"[{resume_path.stem}] Uploading resume + building profile...")
    with resume_path.open("rb") as fh:
        files = {"files": (resume_path.name, fh.read(), "application/pdf")}
    data = {
        "extra_context": extra_context,
        "salary_minimum": str(salary_minimum),
        "work_arrangements": json.dumps(work_arrangements),
        "acceptable_locations": json.dumps(locations),
        "acceptable_location_radii": json.dumps(radii),
        "excluded_locations": "[]",
    }
    r = await client.post(f"{BACKEND_BASE}/profile/build", files=files, data=data, timeout=60.0)
    r.raise_for_status()
    build_id = r.json()["build_id"]
    print(f"[{resume_path.stem}] build_id={build_id}, polling...")

    deadline = time.time() + PROFILE_BUILD_TIMEOUT_SECONDS
    while time.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        sr = await client.get(f"{BACKEND_BASE}/profile/status/{build_id}", timeout=10.0)
        sr.raise_for_status()
        st = sr.json()
        status = st.get("status")
        step = st.get("current_step", "")
        print(f"[{resume_path.stem}] profile build: {status} | {step}")
        if status == "completed":
            res = await client.get(f"{BACKEND_BASE}/profile/result/{build_id}", timeout=30.0)
            res.raise_for_status()
            return res.json()["profile"]
        if status == "failed":
            raise RuntimeError(f"Profile build failed: {st.get('error') or 'unknown'}")
    raise TimeoutError(f"Profile build for {resume_path.stem} timed out after {PROFILE_BUILD_TIMEOUT_SECONDS}s")


async def run_search(client: httpx.AsyncClient, profile: dict, *, label: str) -> dict:
    """POST profile to /search/run, poll until done, return final results dict."""
    print(f"[{label}] Starting search...")
    body = {"profile": profile}
    r = await client.post(f"{BACKEND_BASE}/search/run", json=body, timeout=30.0)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"[{label}] run_id={run_id}, polling every {POLL_INTERVAL_SECONDS}s...")

    deadline = time.time() + SEARCH_RUN_TIMEOUT_SECONDS
    last_step = ""
    while time.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            sr = await client.get(f"{BACKEND_BASE}/search/status/{run_id}", timeout=15.0)
            sr.raise_for_status()
            st = sr.json()
        except Exception as e:
            print(f"[{label}] poll error (will retry): {type(e).__name__}: {e}")
            continue
        status = st.get("status")
        progress = st.get("progress", 0)
        step = st.get("current_step", "")
        detail = st.get("current_step_detail", "")
        # Only print when something changed
        marker = f"{status}|{step}|{detail}"
        if marker != last_step:
            print(f"[{label}] {progress:>3}% | {step}: {detail or '(no detail)'}")
            last_step = marker
        if status == "completed":
            res = await client.get(f"{BACKEND_BASE}/search/results/{run_id}", timeout=30.0)
            res.raise_for_status()
            return {"run_id": run_id, "results": res.json(), "status": st}
        if status == "failed":
            raise RuntimeError(f"Search failed: {st.get('error') or 'unknown'}")
        if status == "cancelled":
            raise RuntimeError("Search was cancelled")
    raise TimeoutError(f"Search for {label} timed out after {SEARCH_RUN_TIMEOUT_SECONDS}s")


def summarize(label: str, results: dict, status: dict) -> str:
    """Pretty-print the results for quick eyeballing."""
    summary = results.get("summary") or {}
    roles = results.get("roles") or []
    by_tier: dict[str, int] = {}
    for r in roles:
        t = r.get("final_tier", "UNKNOWN")
        by_tier[t] = by_tier.get(t, 0) + 1
    cost = summary.get("cost_total_usd") or 0
    duration = summary.get("duration_seconds") or 0

    top = sorted(roles, key=lambda r: -(r.get("final_score") or 0))[:5]

    lines = [
        "",
        f"================ {label.upper()} RESULTS ================",
        f"Duration:  {duration}s ({duration/60:.1f} min)",
        f"Cost:      ${cost:.4f}",
        f"Roles:     {len(roles)} qualifying",
        f"Tier mix:  STRONG={by_tier.get('STRONG',0)}, GOOD={by_tier.get('GOOD',0)}, "
        f"MAYBE={by_tier.get('MAYBE',0)}, STRETCH={by_tier.get('STRETCH',0)}",
        "Top 5:",
    ]
    for r in top:
        score = r.get("final_score")
        lines.append(
            f"  [{score}] {r.get('company','?')} - {(r.get('job_title') or '')[:55]}"
        )
    lines.append("=" * (len(label) + 30))
    return "\n".join(lines)


async def run_one(args, client) -> str:
    profile = await build_profile(
        client, args.resume,
        salary_minimum=args.salary,
        extra_context=args.context,
        locations=args.locations,
        radii=args.radii,
        work_arrangements=args.arrangements,
    )
    print(f"[{args.label}] Profile built: {len((profile.get('keywords') or []))} keywords, "
          f"salary_minimum=${profile.get('salary_minimum')}")
    out = await run_search(client, profile, label=args.label)
    summary_text = summarize(args.label, out["results"], out["status"])
    print(summary_text)

    # Save labeled audit copy
    out_dir = Path(__file__).resolve().parent / "test_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{args.label}_{ts}.json"
    out_path.write_text(json.dumps(out["results"], indent=2, default=str), encoding="utf-8")
    print(f"[{args.label}] Saved results to {out_path}")
    return summary_text


async def main():
    p = argparse.ArgumentParser(description="Drive a search run for a single persona")
    p.add_argument("--resume", required=True, type=Path, help="Path to resume PDF/DOCX/TXT")
    p.add_argument("--label", required=True, help="Short label for output filename (e.g. ziad/zach/ryan)")
    p.add_argument("--salary", type=int, default=130000, help="Salary minimum (default 130000)")
    p.add_argument("--context", default="", help="Free-form extra context for profile builder")
    p.add_argument("--locations", nargs="+", default=["Richmond VA", "Washington DC"],
                   help="Acceptable locations (space-separated, default Richmond+DC)")
    p.add_argument("--radii", nargs="+", type=int, default=[40, 25],
                   help="Radii in miles for each location (default 40 25)")
    p.add_argument("--arrangements", nargs="+", default=["remote", "hybrid", "on-site"],
                   help="Work arrangements (default all three)")
    args = p.parse_args()

    if not args.resume.exists():
        print(f"ERROR: resume not found at {args.resume}", file=sys.stderr)
        sys.exit(1)

    async with httpx.AsyncClient() as client:
        # Quick health check first
        try:
            r = await client.get(f"{BACKEND_BASE}/health", timeout=5.0)
            r.raise_for_status()
        except Exception as e:
            print(f"ERROR: backend not reachable at {BACKEND_BASE}.", file=sys.stderr)
            print(f"       Make sure 'npm run tauri dev' is running. ({type(e).__name__})", file=sys.stderr)
            sys.exit(2)

        await run_one(args, client)


if __name__ == "__main__":
    asyncio.run(main())

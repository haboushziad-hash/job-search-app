"""FastAPI server that bridges the React desktop app to the Python backend.

Runs on localhost:8765. The Tauri app spawns this as a subprocess on launch
and tears it down on quit. CORS is open so the React app on localhost:5173
(dev) and the Tauri webview (production) can both call it.

Endpoints:
  POST /profile/build         build profile + keywords from uploaded resumes
  POST /search/run            kick off a full search; returns run_id
  GET  /search/status/{id}    poll progress + roles found so far
  GET  /search/results/{id}   final scored roles
  GET  /admin/cost            spend so far today / this month (operator-only)

Run locally:
  backend\\venv\\Scripts\\python.exe -m backend.api
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.config import config
from backend.models import CandidateProfile, Role, RunSummary
from backend.profile.builder import build_profile_from_resumes
from backend.runner import run_search, _maybe_archive, _safe_json_loads
from backend.scoring import cost_tracker


# ----------------------------------------------------------------------------
# v0.3.34: logging + stdout hardening — runs at IMPORT time (uvicorn loads
# "backend.api:app", so this always executes for the shipped backend).
# ----------------------------------------------------------------------------
# The shipped backend enters via THIS module, NOT backend_main.py — so
# backend_main's file-logging + stdout reconfigure never ran for testers. Two
# consequences this fixes:
#   1. The search-failure handler already logs a full traceback, but the root
#      logger had no file handler in this entry path → failures like the
#      Windows "[Errno 22] Invalid argument" left NO stack anywhere. Now they
#      land in %APPDATA%/JobSearchApp/backend.log.
#   2. The raw piped stdout (Tauri sidecar) can throw OSError under a log flood
#      at the 560-tenant scale (hundreds of Workday 429 lines) and crash the
#      whole run. We wrap stdout/stderr so a write can never do that.
import logging as _logging
import os as _os
import sys as _sys
from pathlib import Path as _Path


class _SafeStream:
    """stdout/stderr wrapper: a broken/overwhelmed pipe can never crash us."""

    def __init__(self, stream: Any) -> None:
        self._s = stream

    def write(self, data: Any) -> int:
        try:
            if self._s is not None:
                return self._s.write(data)
        except (OSError, ValueError):
            pass
        return len(data) if isinstance(data, str) else 0

    def flush(self) -> None:
        try:
            if self._s is not None:
                self._s.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._s) and self._s.isatty()
        except Exception:
            return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._s, name)


def _init_logging_and_stdout() -> None:
    try:
        _sys.stdout = _SafeStream(_sys.stdout)  # type: ignore[assignment]
        _sys.stderr = _SafeStream(_sys.stderr)  # type: ignore[assignment]
    except Exception:
        pass
    try:
        if _sys.platform == "win32":
            base = _Path(_os.environ.get("APPDATA", _Path.home()))
        elif _sys.platform == "darwin":
            base = _Path.home() / "Library" / "Application Support"
        else:
            base = _Path.home() / ".config"
        folder = base / "JobSearchApp"
        folder.mkdir(parents=True, exist_ok=True)
        root = _logging.getLogger()
        if not any(getattr(h, "_jsa_file", False) for h in root.handlers):
            fh = _logging.FileHandler(str(folder / "backend.log"), encoding="utf-8")
            fh.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            fh._jsa_file = True  # type: ignore[attr-defined]
            root.addHandler(fh)
            root.setLevel(_logging.INFO)
        # Quiet the per-request httpx/genai spam so the log stays diagnosable.
        for _noisy in ("httpx", "httpcore", "google_genai", "google.genai"):
            _logging.getLogger(_noisy).setLevel(_logging.WARNING)
    except Exception:
        pass


_init_logging_and_stdout()


# ----------------------------------------------------------------------------
# In-memory registries
# ----------------------------------------------------------------------------
# Production version stores in SQLite; this is fine for v0.

_RUNS: dict[str, "RunState"] = {}
# Track the asyncio.Task for each in-progress run so we can cancel cleanly.
# Key = run_id. Removed when the run completes / fails / is cancelled.
_RUN_TASKS: dict[str, asyncio.Task[None]] = {}
_BUILDS: dict[str, "BuildState"] = {}


class BuildState(BaseModel):
    """Tracks an in-progress profile build."""
    build_id: str
    started_at: datetime
    status: str = "pending"      # pending / running / completed / failed
    progress: float = 0.0         # 0-100
    current_step: str = ""
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True


class RunState(BaseModel):
    run_id: str
    started_at: datetime
    status: str = "pending"           # pending / scraping / scoring / completed / failed
    progress: float = 0.0              # 0-100
    current_step: str = ""             # high-level step label, matches UI STEPS array
    current_step_index: int = 0        # 1..6 in the UI
    current_step_detail: str = ""      # granular live text shown under the active step
                                        # ("Scoring 47 of 120 with Flash...")
    total_steps: int = 6
    roles_scraped: int = 0
    roles_qualifying: int = 0
    tier_strong: int = 0
    tier_good: int = 0
    tier_maybe: int = 0
    tier_stretch: int = 0
    error: Optional[str] = None
    final_roles: list[dict[str, Any]] = Field(default_factory=list)
    summary: Optional[dict[str, Any]] = None

    class Config:
        arbitrary_types_allowed = True


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------

from backend import __version__ as _APP_VERSION

app = FastAPI(
    title="Job Search API",
    description="Local bridge between the React desktop app and the Python search backend.",
    # v0.2.3: was hardcoded "0.2.0" — read from package metadata so every
    # release auto-reports correctly without a version-bump landmine.
    version=_APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # safe — server only listens on 127.0.0.1
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------------------------------------------------------
# Archive — initialized at startup with default audit folder. The frontend
# can later call /settings/audit-folder to point it elsewhere (e.g. a synced
# OneDrive/Google Drive folder), which closes and re-opens the connection.
# ----------------------------------------------------------------------------

from backend.storage import set_audit_folder

DEFAULT_AUDIT_FOLDER = Path.home() / "Documents" / "JobSearchApp" / "audits"

@app.on_event("startup")
async def _init_archive() -> None:
    try:
        set_audit_folder(DEFAULT_AUDIT_FOLDER)
        print(f"[archive] initialized at {DEFAULT_AUDIT_FOLDER}")
    except Exception as e:
        print(f"[archive] startup init failed: {e}")

    # ----------------------------------------------------------------
    # Diagnostic: print which mode the backend is starting in. Helps us
    # verify Tauri is actually injecting LLM_PROXY_URL when it should.
    # Tester builds: should print "[mode] LLM proxy mode" with the URL.
    # Local dev (no proxy): should print "[mode] direct keys" with count.
    # ----------------------------------------------------------------
    proxy_url = (config.LLM_PROXY_URL or "").strip()
    audit_url = (config.AUDIT_UPLOAD_URL or "").strip()
    tester_uuid = (config.TESTER_UUID or "").strip()
    if proxy_url:
        print(f"[mode] LLM proxy mode -> {proxy_url}")
    else:
        n_keys = len(config.google_api_keys())
        print(f"[mode] direct keys -> {n_keys} Google key(s) loaded from .env")
    if audit_url:
        print(f"[mode] audit upload -> {audit_url}")
    if tester_uuid:
        # Only show first 8 chars — enough for diagnostics, doesn't leak full UUID
        print(f"[mode] tester uuid -> {tester_uuid[:8]}...")


# ============================================================================
# Health
# ============================================================================

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        # v0.2.3: was hardcoded "0.2.0" — see _APP_VERSION import above.
        "version": _APP_VERSION,
        "env": {
            "google_keys_configured": len(config.google_api_keys()),
            "dev_mode": config.DEV_MODE,
        },
    }


# ============================================================================
# Identity
# ============================================================================

@app.get("/identity")
async def identity() -> dict[str, Any]:
    """Return the anonymous tester UUID for this install.

    The Rust shim generates a UUID v4 on first launch and persists it to
    `%APPDATA%\\com.findmesomedamnjobz.app\\tester_id.txt` (Windows). It
    passes that UUID to the backend as the TESTER_UUID env var on every
    sidecar spawn, where config.TESTER_UUID picks it up.

    The Settings page reads this so users can copy their tester ID and
    share it with the operator (Ziad) for support / triage. Without this
    endpoint, the UUID was hidden in a file folder users wouldn't think
    to dig into.
    """
    tester_uuid = (config.TESTER_UUID or "").strip()
    return {"tester_uuid": tester_uuid}


# ============================================================================
# Profile build
# ============================================================================

class BuildProfileRequest(BaseModel):
    """Pydantic equivalent for non-multipart calls (rarely used)."""
    extra_context: Optional[str] = None
    salary_minimum: Optional[int] = None
    work_arrangements: list[str] = Field(default_factory=list)
    acceptable_locations: list[str] = Field(default_factory=list)
    excluded_locations: list[str] = Field(default_factory=list)


@app.post("/profile/build")
async def profile_build(
    files: list[UploadFile] = File(...),
    extra_context: str = Form(""),
    salary_minimum: int = Form(0),
    work_arrangements: str = Form("[]"),
    acceptable_locations: str = Form("[]"),
    acceptable_location_radii: str = Form("[]"),
    excluded_locations: str = Form("[]"),
) -> dict[str, Any]:
    """Accept resume file uploads + freeform extra context + preferences.

    Returns a fully-built CandidateProfile with auto-generated keywords.
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one resume is required.")

    # Save uploads to temp files so the parser can read by path
    saved_paths: list[Path] = []
    tmpdir = Path(tempfile.mkdtemp(prefix="jobsearch_"))
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix or ".bin"
        path = tmpdir / f.filename
        content = await f.read()
        path.write_bytes(content)
        saved_paths.append(path)

    if not saved_paths:
        raise HTTPException(status_code=400, detail="No valid resume files received.")

    # Parse JSON-encoded list fields
    try:
        arr_arrangements = json.loads(work_arrangements) if work_arrangements else []
        arr_locations = json.loads(acceptable_locations) if acceptable_locations else []
        arr_radii = json.loads(acceptable_location_radii) if acceptable_location_radii else []
        arr_excluded = json.loads(excluded_locations) if excluded_locations else []
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in form field: {e}")

    user_preferences: dict[str, Any] = {}
    if salary_minimum:
        user_preferences["salary_minimum"] = salary_minimum
    if arr_arrangements:
        user_preferences["work_arrangements"] = arr_arrangements
    if arr_locations:
        user_preferences["acceptable_locations"] = arr_locations
    if arr_radii:
        user_preferences["acceptable_location_radii"] = arr_radii
    if arr_excluded:
        user_preferences["excluded_locations"] = arr_excluded
    if extra_context.strip():
        # v0.3.16-wave2a: cap freeform at 5000 chars (~750 words) so a user
        # pasting 10 long JDs can't blow the prompt budget or distort the
        # word-count tier in the system prompt. 5K leaves room for several
        # pasted JDs plus targeting commentary; anything beyond that is
        # truncated with an explicit marker so the LLM knows it's clipped.
        _ff = extra_context.strip()
        if len(_ff) > 5000:
            _ff = _ff[:5000] + "\n[... freeform truncated at 5000 chars ...]"
        user_preferences["freeform_context"] = _ff

    # Kick off the build asynchronously and return a build_id immediately.
    # The React frontend polls /profile/status/{id} for progress.
    build_id = str(uuid.uuid4())
    state = BuildState(
        build_id=build_id,
        started_at=datetime.now(timezone.utc),
        status="pending",
        current_step="Queued",
    )
    _BUILDS[build_id] = state

    asyncio.create_task(
        _execute_profile_build(state, saved_paths, tmpdir, user_preferences)
    )

    return {"build_id": build_id, "status": "pending"}


async def _execute_profile_build(
    state: BuildState,
    saved_paths: list[Path],
    tmpdir: Path,
    user_preferences: dict[str, Any],
) -> None:
    """Run the profile build, updating state.progress/current_step as it goes."""
    def _on_progress(pct: int, stage: str) -> None:
        state.progress = float(pct)
        state.current_step = stage

    try:
        state.status = "running"
        state.current_step = "Starting"

        profile = await build_profile_from_resumes(
            resume_paths=saved_paths,
            user_preferences=user_preferences,
            progress=_on_progress,
        )

        state.result = {
            "profile": profile.model_dump(mode="json"),
            "keyword_count": len(profile.keywords),
            "tier_breakdown": {
                "tier_1": sum(1 for k in profile.keywords if k.tier == 1),
                "tier_2": sum(1 for k in profile.keywords if k.tier == 2),
                "tier_3": sum(1 for k in profile.keywords if k.tier == 3),
            },
        }
        state.status = "completed"
        state.progress = 100.0
        state.current_step = "Done"
    except Exception as e:
        state.status = "failed"
        state.error = f"{type(e).__name__}: {str(e)[:500]}"
    finally:
        # Cleanup temp files regardless of outcome
        for p in saved_paths:
            try: p.unlink()
            except Exception: pass
        try: tmpdir.rmdir()
        except Exception: pass


@app.get("/profile/status/{build_id}")
async def profile_status(build_id: str) -> dict[str, Any]:
    state = _BUILDS.get(build_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown build_id")
    return state.model_dump(mode="json", exclude={"result"})


@app.get("/profile/result/{build_id}")
async def profile_result(build_id: str) -> dict[str, Any]:
    state = _BUILDS.get(build_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown build_id")
    if state.status != "completed":
        raise HTTPException(status_code=409, detail=f"Build not complete (status={state.status})")
    return state.result or {}


@app.get("/profile/last-built")
async def profile_last_built() -> dict[str, Any]:
    """Return the most-recently-built profile, for frontend auto-heal.

    v0.3.23 (FIX 28): The desktop UI keeps the active profile in
    localStorage (zustand persist). A rehydration edge-case or an
    origin-partition mismatch (dev server vs installed build) can leave
    that copy null even though the user has built profiles before — which
    hides the re-run option behind the "fresh search" gate.

    This endpoint reads profile_build_cache.db (which persists EVERY
    profile the user has built, in %APPDATA%) and returns the most recent
    one. The frontend calls it on launch when its own profile is null and
    re-hydrates from the result. Returns {"profile": null} when the cache
    is genuinely empty (a true first-time user) so the setup flow runs
    normally.
    """
    try:
        from backend.profile import profile_cache
        prof = profile_cache.get_most_recent_profile()
        return {"profile": prof}
    except Exception as e:
        # Never fail the launch path — fall back to null (setup flow).
        return {"profile": None, "error": str(e)[:200]}


# ============================================================================
# Search run
# ============================================================================

class RunSearchRequest(BaseModel):
    profile: dict[str, Any]
    keywords: Optional[list[str]] = None       # if omitted, derived from profile
    sources: Optional[list[str]] = None        # default: all active scrapers
    posted_within_days: int = 30
    # List of (company, title) pairs the user has already applied to.
    # The frontend persists these in localStorage and passes them along
    # so we don't resurface roles already in their funnel.
    applied_roles: Optional[list[dict[str, str]]] = None
    # Cache controls (Phase 1 #12). If a fresh-enough run exists for the same
    # profile, results are replayed in seconds instead of re-running the full
    # 12-25 min pipeline. Force refresh bypasses the cache.
    cache_max_age_days: int = 7
    force_refresh: bool = False


@app.post("/search/run")
async def search_run(req: RunSearchRequest) -> dict[str, Any]:
    """Kick off a full search asynchronously. Returns run_id for polling."""
    profile = CandidateProfile(**req.profile)

    if req.keywords:
        # Caller explicitly overrode (rare — used by debug tooling)
        keywords = req.keywords
    elif profile.search_terms:
        # v0.1.4: prefer search_terms (broad 2-3 word phrases) over keywords
        # (specific titles). Search terms produce wider upstream API matches.
        keywords = list(profile.search_terms)
    else:
        # Fallback: Tier 1 + Tier 2 keywords from profile (back-compat for
        # profiles built before the search-terms split).
        keywords = [k.text for k in profile.keywords if k.tier <= 2]

    if not keywords:
        raise HTTPException(status_code=400, detail="No keywords available; rebuild profile.")

    # Convert applied_roles list of dicts into the (company_lower, title_lower)
    # tuple-set format expected by hard_filters.passes_not_applied.
    applied_keys: set[tuple[str, str]] = set()
    for entry in req.applied_roles or []:
        company = (entry.get("company") or "").strip().lower()
        title = (entry.get("title") or "").strip().lower()
        if company and title:
            applied_keys.add((company, title))

    run_id = str(uuid.uuid4())
    state = RunState(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        status="pending",
        current_step="Initializing",
    )
    _RUNS[run_id] = state

    # Merge in any applications the user has marked in their local archive
    # (cross-session persistence — keeps already-applied tracking consistent
    # across reinstalls and across multiple devices syncing the same folder).
    try:
        from backend.storage import get_archive
        archive_applied = get_archive().applied_role_keys()
        applied_keys |= archive_applied
    except Exception:
        pass

    # Fire and forget; updates state in the background. Track the task
    # so we can cancel it via /search/cancel/{run_id}.
    task = asyncio.create_task(_execute_search(
        state, profile, keywords, req.sources, req.posted_within_days,
        applied_keys, run_id, req.cache_max_age_days, req.force_refresh,
    ))
    _RUN_TASKS[run_id] = task
    task.add_done_callback(lambda _t: _RUN_TASKS.pop(run_id, None))

    return {"run_id": run_id, "status": "pending"}


async def _execute_search(
    state: RunState,
    profile: CandidateProfile,
    keywords: list[str],
    sources: Optional[list[str]],
    posted_within_days: int,
    applied_keys: set[tuple[str, str]],
    run_id: str,
    cache_max_age_days: int,
    force_refresh: bool,
) -> None:
    """Run the search end-to-end, mutating the RunState as it progresses."""
    try:
        state.status = "running"
        state.current_step = "Scraping job boards"
        state.current_step_index = 2
        state.progress = 5.0

        # Wire the progress callback so the polling frontend (Running.tsx)
        # actually sees stage transitions instead of being frozen on step 1
        # for the full 15-25 min run. The runner emits at each major stage
        # boundary; orchestrator emits inside the scoring cascade. The
        # `detail` string is the granular live text shown under the active
        # step (e.g. "Scoring 47 of 120 with Flash" — replaces the static
        # describeProgress() fallback).
        def _on_progress(pct: int, step_label: str, step_index: int, detail: str = "") -> None:
            state.progress = float(pct)
            state.current_step = step_label
            state.current_step_index = step_index
            if detail:
                state.current_step_detail = detail

        scored, summary = await run_search(
            profile=profile,
            keywords=keywords,
            sources=sources,
            posted_within_days=posted_within_days,
            applied_keys=applied_keys,
            run_id=run_id,
            cache_max_age_days=cache_max_age_days,
            force_refresh=force_refresh,
            log=True,
            progress=_on_progress,
        )

        state.roles_scraped = summary.roles_scraped
        state.roles_qualifying = summary.roles_qualifying
        state.tier_strong = summary.tier_strong
        state.tier_good = summary.tier_good
        state.tier_maybe = summary.tier_maybe
        state.tier_stretch = summary.tier_stretch
        state.final_roles = [
            r.model_dump(mode="json", exclude={"embedding"})
            for r in scored
            if (r.final_score or 0) >= 40
        ]
        state.summary = summary.model_dump(mode="json")
        state.status = "completed"
        state.current_step = "Done"
        state.current_step_index = 6
        state.progress = 100.0
    except asyncio.CancelledError:
        # User cancelled mid-run. Mark in-memory state as cancelled and
        # propagate so any awaiters resolve cleanly.
        #
        # There are TWO mid-run database writes that need cleanup:
        #
        # 1) audits/runs.db — archive.begin_run() inserted a row with
        #    status="running". Without cleanup, the dashboard's run-history
        #    view would show an orphan "running" entry, and a buggy cache
        #    lookup could conceivably match against it. Delete the row
        #    outright (cancel_run uses DELETE, not UPDATE).
        #
        # 2) archive/cost.db (run_summaries table) — cost_tracker.start_run()
        #    inserted a row with status="running" at the top of scoring.
        #    Mark it status="cancelled" via finish_run(); the per-call
        #    cost rows in llm_calls stay because those represent real money
        #    already spent — we want spend-cap accounting honest.
        #
        # Roles, scores, market contributions, audit JSONs, and the audit
        # upload to the central Worker are all written together in
        # _archive_run() AFTER the cascade finishes, so they're already
        # correctly absent on a mid-run cancel — no cleanup needed for those.
        state.status = "cancelled"
        state.current_step = "Cancelled"
        state.error = None
        try:
            archive = _maybe_archive()
            if archive is not None:
                archive.cancel_run(run_id=run_id)
        except Exception as e:
            print(f"[cancel] archive cleanup failed (non-fatal): {e}")
        try:
            cost_tracker.finish_run(run_id, status="cancelled")
        except Exception as e:
            print(f"[cancel] cost_tracker cleanup failed (non-fatal): {e}")
        # Re-raise so asyncio knows the task was cancelled (lets the
        # cancel() caller's await complete cleanly).
        raise
    except Exception as e:
        state.status = "failed"
        state.error = f"{type(e).__name__}: {str(e)[:500]}"
        # v0.3.26: log the FULL traceback to backend.log. Previously only the
        # one-line message was kept in state.error (shown to the user as
        # "Something went wrong"), with NO stack anywhere — which made run
        # failures like the Windows-asyncio "[Errno 22] Invalid argument"
        # impossible to diagnose. Now every run failure leaves a stack trace.
        import logging as _logging, traceback as _tb
        _logging.getLogger("backend.search").error(
            "search run %s FAILED — %s\n%s", run_id, state.error, _tb.format_exc())


@app.post("/search/cancel/{run_id}")
async def search_cancel(run_id: str) -> dict[str, Any]:
    """Cancel an in-progress search. The asyncio task is cancelled, which
    raises CancelledError inside run_search() at the next await — abandoning
    any partial work. No archive entry, no audit JSON, no run history is
    written for cancelled runs.

    Returns 404 if the run_id is unknown, 409 if the run already completed
    (can't cancel what already finished).
    """
    state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    if state.status in ("completed", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Run already {state.status}; nothing to cancel",
        )
    task = _RUN_TASKS.get(run_id)
    if task is None or task.done():
        # State says running but task is gone — race. Mark cancelled so the
        # frontend can move on, even though there's nothing to actually cancel.
        state.status = "cancelled"
        state.current_step = "Cancelled"
        return {"run_id": run_id, "status": "cancelled"}
    task.cancel()
    # Optimistic state update so the polling frontend sees it immediately.
    # The task's CancelledError handler will set the same state.
    state.status = "cancelled"
    state.current_step = "Cancelled"
    return {"run_id": run_id, "status": "cancelled"}


@app.get("/search/status/{run_id}")
async def search_status(run_id: str) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    return state.model_dump(mode="json", exclude={"final_roles"})


@app.get("/search/results/{run_id}")
async def search_results(run_id: str) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    if state.status != "completed":
        raise HTTPException(status_code=409, detail=f"Run not complete (status={state.status})")
    return {
        "run_id": run_id,
        "summary": state.summary,
        "roles": state.final_roles,
    }


# ============================================================================
# Freshness re-check (v0.3.26) — re-validate an existing run's links live,
# WITHOUT re-running the whole search. Cheap (HEAD + a GET, ~$0 — no scraping,
# no LLM) and fast (~30-60s for a few hundred URLs). Uses the SAME liveness
# engine a full search uses. Returns URLs we're CONFIDENT are dead (404/410 /
# expired-redirect / closure-banner) so the dashboard can drop them, plus the
# UNCERTAIN ones (403/timeout/5xx) which we keep + flag — never silently drop.
# ============================================================================

class RecheckRequest(BaseModel):
    urls: list[str]


@app.post("/roles/recheck")
async def roles_recheck(req: RecheckRequest) -> dict[str, Any]:
    from backend.scraper.liveness import verify_liveness
    urls = [u for u in (req.urls or []) if isinstance(u, str) and u.strip()][:600]
    if not urls:
        return {"checked": 0, "dead": [], "uncertain": []}
    # Minimal shims — liveness only reads job_url. fetch_body_check=True also
    # GETs each page and scans for closure banners; drop_dead=False returns all
    # roles with their verification result populated so we classify here.
    shims = [Role(job_title="recheck", company="recheck", job_url=u) for u in urls]
    verified = await verify_liveness(shims, fetch_body_check=True, drop_dead=False, log=False)
    CONFIRMED_DEAD = {"dead", "expired_redirect", "marked_filled"}
    dead: list[str] = []
    uncertain: list[str] = []
    for r in verified:
        res = r.last_verified_result or ""
        if res in CONFIRMED_DEAD:
            dead.append(r.job_url)
        elif res in ("blocked", "server_error") or res.startswith("error") or res.startswith("http_"):
            uncertain.append(r.job_url)
    return {"checked": len(verified), "dead": dead, "uncertain": uncertain}


# ============================================================================
# Run history — list previous completed runs + load any one of them
# ============================================================================

@app.get("/runs")
async def list_runs() -> dict[str, Any]:
    """List all completed runs in runs.db, newest first.

    Returns minimal summary stats per run so the History page can render
    rows quickly. Full results are fetched on-demand via /runs/{run_id}.
    """
    archive = _maybe_archive()
    if archive is None:
        return {"runs": []}
    try:
        with archive._cursor() as cur:
            cur.execute("""
                SELECT run_id, started_at, completed_at, status, qualifying_count,
                       summary_json, profile_snapshot_json, profile_tags_json
                FROM runs
                WHERE status = 'completed'
                ORDER BY started_at DESC
                LIMIT 100
            """)
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    out = []
    for r in rows:
        summary = _safe_json_loads(r.get("summary_json")) or {}
        profile = _safe_json_loads(r.get("profile_snapshot_json")) or {}
        out.append({
            "run_id": r["run_id"],
            "started_at": r.get("started_at"),
            "completed_at": r.get("completed_at"),
            "qualifying_count": r.get("qualifying_count"),
            "scraped": summary.get("roles_scraped"),
            "tier_strong": summary.get("tier_strong"),
            "tier_good": summary.get("tier_good"),
            "tier_maybe": summary.get("tier_maybe"),
            "tier_stretch": summary.get("tier_stretch"),
            "duration_seconds": summary.get("duration_seconds"),
            "profile_headline": profile.get("headline"),
        })
    return {"runs": out}


@app.get("/profiles/recent")
async def list_recent_profiles() -> dict[str, Any]:
    """Return up to N most-recent UNIQUE profiles, ordered by latest run.

    Used by the StartSearch choice screen so a returning user can pick
    which past profile to re-run.

    Dedup key: (resume filenames, salary_minimum). We deliberately do
    NOT use the stored `profile_hash` here because that hash is keyed
    on AI-extracted fields (headline, target_functions, technical
    skills) which vary slightly across runs of the same resume due to
    LLM non-determinism — e.g. "AI Strategy and Enablement Consultant"
    vs "AI Strategy & Enablement Consultant" produce different hashes
    even though it's the same person + same resume + same intent.
    `profile_hash` is the right key for cache invalidation (any change
    = miss) but the wrong key for "is this the same user." For
    user-facing dedup, the resume filename(s) plus salary preference
    capture identity correctly: same resume + same explicit salary
    floor = same profile in the user's mental model.

    Cap at 3 unique profiles — that's the cognitive limit on the
    choice screen and matches what users actually maintain in practice
    (typically 1, occasionally 2 for "AI track" vs "non-AI track").
    """
    archive = _maybe_archive()
    if archive is None:
        return {"profiles": []}
    try:
        with archive._cursor() as cur:
            cur.execute("""
                SELECT run_id, started_at, profile_hash,
                       profile_snapshot_json, qualifying_count, summary_json
                FROM runs
                WHERE status = 'completed'
                ORDER BY started_at DESC
                LIMIT 50
            """)
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    def _dedup_key(snap: dict) -> tuple:
        """Identity key insensitive to LLM-driven phrasing drift."""
        resumes = snap.get("resumes") or []
        filenames = tuple(sorted(
            (r.get("filename", "") if isinstance(r, dict) else str(r))
            for r in resumes
        ))
        salary = snap.get("salary_minimum") or 0
        return (filenames, salary)

    seen_keys: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        snapshot = _safe_json_loads(r.get("profile_snapshot_json"))
        if not snapshot:
            continue
        key = _dedup_key(snapshot)
        # Skip rows whose dedup key is empty (no resume metadata stored).
        if not key[0]:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        summary = _safe_json_loads(r.get("summary_json")) or {}
        out.append({
            "run_id": r["run_id"],
            "last_used_at": r.get("started_at"),
            "profile_hash": r.get("profile_hash") or "",
            "profile": snapshot,
            "qualifying_count": r.get("qualifying_count"),
            "tier_strong": summary.get("tier_strong"),
            "tier_good": summary.get("tier_good"),
        })
        if len(out) >= 3:
            break
    return {"profiles": out}


@app.get("/runs/{run_id}/audit")
async def get_run_audit(run_id: str) -> dict[str, Any]:
    """Return the full audit JSON for a completed run.

    Used by the Stats page (v0.2.0) to surface aggregate statistics like
    salary distribution, work-arrangement breakdown, top companies, top
    keywords, etc. The audit JSON is the richest data source for a run
    — it contains profile snapshot, pipeline funnel, per-source health,
    all qualifying roles with full metadata, near-miss roles, keyword
    effectiveness, salary coverage, and coverage gap analysis.

    The runs.db tracks the audit JSON file path. This endpoint reads it
    off disk and returns the contents directly. For runs older than the
    audit format we currently support, fields may be missing — the
    Stats page handles those gracefully on the frontend.
    """
    archive = _maybe_archive()
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not configured")
    try:
        with archive._cursor() as cur:
            cur.execute(
                "SELECT audit_file_path FROM runs WHERE run_id = ?",
                (run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
            audit_path_str = row["audit_file_path"]
        if not audit_path_str:
            raise HTTPException(status_code=404, detail=f"Run {run_id} has no audit file")
        audit_path = Path(audit_path_str)
        if not audit_path.exists():
            raise HTTPException(status_code=404, detail=f"Audit file not found at {audit_path}")
        with audit_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/runs/{run_id}")
async def delete_run(run_id: str) -> dict[str, Any]:
    """Delete a completed run by id, including its audit JSON on disk.

    Used by the History page's per-row delete button. Idempotent — if
    the run is already gone, returns ok with deleted=False so the
    frontend can refresh its list without an error toast.
    """
    archive = _maybe_archive()
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not configured")
    try:
        deleted = archive.delete_completed_run(run_id=run_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"run_id": run_id, "deleted": deleted}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """Return full role list + summary for a completed run from runs.db."""
    archive = _maybe_archive()
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not configured")
    try:
        with archive._cursor() as cur:
            cur.execute("""
                SELECT run_id, summary_json, profile_snapshot_json
                FROM runs
                WHERE run_id = ? AND status = 'completed'
            """, (run_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
            summary = _safe_json_loads(row["summary_json"]) or {}
            profile = _safe_json_loads(row["profile_snapshot_json"]) or {}
        # replay_cached_run joins roles + role_scores into a single dict per role
        roles = archive.replay_cached_run(run_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Convert SQL row dicts to Role-shaped dicts the frontend expects.
    # role_scores joined columns: final_score, final_tier, stage2_*, stage3_*,
    # plus matched_keyword (the "via X" tag) and stage3_summary (the AI-C
    # dashboard preview, aliased from rs.summary in replay_cached_run to
    # avoid collision with the runs.summary_json column).
    out_roles = []
    for r in roles:
        out_roles.append({
            "job_id": r.get("id"),
            "job_title": r.get("job_title"),
            "company": r.get("company"),
            "job_url": r.get("job_url"),
            "location": r.get("location"),
            "location_type": r.get("location_type"),
            "salary_min": r.get("salary_min"),
            "salary_max": r.get("salary_max"),
            "salary_text": r.get("salary_text"),
            "industry": r.get("industry"),
            "posted_date": r.get("posted_date"),
            "primary_source": r.get("source"),
            "job_description_full": r.get("job_description_full"),
            "final_score": r.get("final_score"),
            "final_tier": r.get("final_tier"),
            "stage2_score": r.get("stage2_score"),
            "stage2_reasoning": r.get("stage2_reasoning"),
            "stage3_score": r.get("stage3_score"),
            "stage3_analysis": r.get("stage3_analysis"),
            "stage3_application_strategy": r.get("stage3_application_strategy"),
            "embedding_similarity": r.get("embedding_similarity"),
            # Restored on /runs/{id} so the dashboard's "via X" tag and
            # 2-sentence preview text survive a startup-sync from runs.db
            # (previously silently stripped).
            "matched_keyword": r.get("matched_keyword"),
            "summary": r.get("stage3_summary"),
        })
    return {
        "run_id": run_id,
        "summary": summary,
        "profile": profile,
        "roles": out_roles,
    }


# ============================================================================
# Applications — saved/applied/hidden status persisted in runs.db
# ============================================================================

class ApplicationStatusRequest(BaseModel):
    company: str
    job_title: str
    job_url: Optional[str] = None
    status: str   # 'saved' | 'applied' | 'hidden'
    application_stage: Optional[str] = None
    notes: Optional[str] = None


@app.post("/applications/status")
async def set_application_status(req: ApplicationStatusRequest) -> dict[str, Any]:
    """Mark a role as saved/applied/hidden. Persists in runs.db so the status
    survives reinstalls and is available for future search runs to suppress
    already-applied roles."""
    if req.status not in ("saved", "applied", "hidden"):
        raise HTTPException(status_code=400, detail=f"Invalid status: {req.status}")
    try:
        from backend.storage import get_archive
        archive = get_archive()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Archive unavailable: {e}")

    # Upsert role first to get role_id
    role_id = archive.upsert_role(
        company=req.company.strip(),
        job_title=req.job_title.strip(),
        job_url=req.job_url,
    )
    archive.set_application_status(
        role_id=role_id,
        status=req.status,
        application_stage=req.application_stage,
        notes=req.notes,
    )
    return {"ok": True, "role_id": role_id}


# ============================================================================
# v0.3.15 (P1.11) — Role event logging (behavioral signals)
# ============================================================================
# Append-only timeline of user interactions with role cards. Distinct from
# /applications/status, which is the persistent "user's current relationship"
# flag (saved/applied/hidden) that the app reads back later. This endpoint
# captures the EVENT — the moment the click happened — even when status
# toggles back to a prior state.
#
# Why both? The status change tells you the current relationship. The event
# log tells you the SEQUENCE: did the user click visit_link before mark_applied?
# Did they save first, then apply? Did they hide and then unhide? That sequence
# is what enables the 70-application ground-truth validation referenced in
# CUSTOM_MODEL_EXPLORATION.md.
#
# Event types fall into THREE categories. Only the first two are usable
# signals for fit-quality analysis. Toggle-off events ("un-X") look like
# negatives but conflate distinct scenarios — log them for sequence
# integrity, do NOT treat them as analytical signals.
#
# === USABLE POSITIVE SIGNALS (for fit-quality validation) ===
#   mark_applied      — STRONGEST    — user went through the apply flow AND came
#                                      back to mark it. Multi-step commitment;
#                                      highest action cost. Closest thing to
#                                      ground-truth "good fit" the system has.
#   visit_link        — MEDIUM-STRONG — user opened the posting. Genuine
#                                      interest OR confirmation-of-disinterest
#                                      OR curiosity. Lower commitment cost than
#                                      mark_applied — visiting and then NOT
#                                      applying is a common rejection pattern.
#   save              — MEDIUM       — bookmarked for later. Explicit but
#                                      uncommitted; many saves never convert.
#
# === USABLE OUTCOME SIGNALS (for application-pipeline tracking, NOT fit) ===
#   update_app_stage  — implies prior mark_applied. Tells you what HAPPENED
#                       after applying (interview / offer / rejected /
#                       ghosted / withdrew). Useful for response-rate and
#                       outcome analysis, but the stage is downstream of
#                       fit — a STRONG-fit role can still get rejected, and
#                       a STRETCH-fit role can still convert.
#
# === NOISE / DO NOT USE FOR ANALYSIS ===
# These look like negatives but they conflate multiple scenarios:
#   unmark_applied    — Could be: (a) genuine retraction of a misclick,
#                       (b) post-rejection cleanup ("got denied, removing
#                       from tracker"), (c) post-withdrawal cleanup, or
#                       (d) post-acceptance cleanup. The dominant case is
#                       probably (b)–(d), which say NOTHING about whether
#                       the role was a good fit when it appeared.
#   unsave            — Mind change after save. Could be lost interest OR
#                       already-applied-cleanup OR dashboard hygiene.
#   unhide            — Mind change after hide. Same conflation.
#   hide              — AMBIGUOUS in the other direction: bad fit OR
#                       already-rejected OR already-applied-elsewhere OR
#                       seen-on-dashboard fatigue.
#
# These four event types ARE logged for sequence completeness — if the
# downstream code wants to reconstruct "what state is this role in right
# now?" it needs the full event timeline. But analytical/training pipelines
# should EXCLUDE them when computing fit-quality signals.
#
# Stacking patterns (for 70-application validation and similar analyses):
# Group events by role_url. Use only the USABLE signals above. Read the
# sequence as a confidence ladder:
#   saved only                          → weakest positive ("might apply later")
#   visited only                        → ambiguous ("looked at it")
#   saved + visited                     → stronger ("seriously considered")
#   saved + visited + mark_applied      → STRONGEST positive (full funnel)
#   mark_applied without prior visit    → applied via referral / external link
#   visited + (no apply) + extended gap → ambiguous (could be lost interest
#                                         OR genuine reject — can't tell)
# Note: do NOT add unmark_applied / unsave to these patterns. They're noise.
#
# Persists to archive/role_events.jsonl (append-only). Each event is one
# JSON line so the file is portable, greppable, and join-friendly.

VALID_EVENT_TYPES = {
    "visit_link",
    "mark_applied",
    "unmark_applied",
    "save",
    "unsave",
    "hide",
    "unhide",
    "update_app_stage",
}


class RoleEventRequest(BaseModel):
    event_type: str
    role_url: str
    role_id: Optional[int] = None
    run_id: Optional[str] = None
    profile_hash: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    final_score: Optional[int] = None
    final_tier: Optional[str] = None
    stage2_score: Optional[int] = None
    stage3_score: Optional[int] = None
    application_stage: Optional[str] = None  # only used for update_app_stage
    source: Optional[str] = None  # 'role_card' / 'detail_view' / 'list' / etc.


@app.post("/applications/event")
async def log_role_event(req: RoleEventRequest) -> dict[str, Any]:
    """Log a role-interaction event to archive/role_events.jsonl.

    Fire-and-forget by design: failures MUST NOT block the user's flow.
    Returns ok=True even on write failure (with a flag the frontend can
    optionally surface as a soft warning).
    """
    import json as _json
    import time as _time

    if req.event_type not in VALID_EVENT_TYPES:
        # Don't reject — log it as 'unknown' with the value preserved so we
        # can audit what the frontend tried to send.
        event_type = f"unknown:{req.event_type[:64]}"
    else:
        event_type = req.event_type

    event = {
        "event_type": event_type,
        "timestamp": _time.time(),
        "iso_time": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "role_url": req.role_url,
        "role_id": req.role_id,
        "run_id": req.run_id,
        "profile_hash": req.profile_hash,
        "company": req.company,
        "job_title": req.job_title,
        "final_score": req.final_score,
        "final_tier": req.final_tier,
        "stage2_score": req.stage2_score,
        "stage3_score": req.stage3_score,
        "application_stage": req.application_stage,
        "source": req.source,
    }

    try:
        from backend.config import config as _cfg
        events_path = _cfg.ARCHIVE_DIR / "role_events.jsonl"
        events_path.parent.mkdir(parents=True, exist_ok=True)
        with events_path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(event, default=str))
            f.write("\n")
        return {"ok": True}
    except Exception as e:
        # Don't fail the click — just note the write failure
        return {"ok": True, "logged": False, "error": str(e)[:200]}


# ============================================================================
# Settings — audit folder location, cache duration are stored here
# ============================================================================

class AuditFolderRequest(BaseModel):
    path: str


# ============================================================================
# v0.1.4 Phase 15b — Pre-flight budget check
# ============================================================================
# Hits the Worker's /v1/llm/budget endpoint (which the app cannot reach
# directly from the browser-equivalent context due to X-Tester-UUID
# requirements + cross-origin concerns). Returns the daily-cap state so
# the frontend can warn the user pre-flight when budget can't cover a run.

@app.get("/llm/budget")
async def llm_budget() -> dict[str, Any]:
    """Pre-flight check: how many LLM calls are left in today's daily cap?

    Returns:
        {used, cap, remaining, typical_run_calls, can_run, sampling_note}

    can_run is False when remaining < typical_run_calls (~1000) — the
    frontend should show a modal warning the user that today's quota is
    too low for a full search.

    When LLM_PROXY_URL is unset (local dev mode), returns can_run=True
    with a synthetic response since the app uses local API keys directly
    and the Worker cap doesn't apply.
    """
    proxy = (config.LLM_PROXY_URL or "").rstrip("/")
    if not proxy:
        # Dev mode: no Worker cap to check, the app uses local keys.
        return {
            "used": 0,
            "cap": 0,
            "remaining": -1,  # sentinel: dev-mode, unlimited
            "typical_run_calls": 1000,
            "can_run": True,
            "sampling_note": "Dev mode — Worker cap not applicable (local keys).",
        }
    uuid = (config.TESTER_UUID or "").strip()
    if not uuid:
        # Configured for proxy but no UUID — can't query the per-tester counter
        return {
            "used": 0,
            "cap": 0,
            "remaining": -1,
            "typical_run_calls": 1000,
            "can_run": True,
            "sampling_note": "No tester UUID configured — budget check skipped.",
        }
    import httpx
    url = f"{proxy}/v1/llm/budget"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={"X-Tester-UUID": uuid})
            if r.status_code == 200:
                return r.json()
            return {
                "used": 0,
                "cap": 0,
                "remaining": -1,
                "typical_run_calls": 1000,
                "can_run": True,
                "sampling_note": f"Budget endpoint returned {r.status_code} — defaulting to allow.",
            }
    except Exception as e:
        return {
            "used": 0,
            "cap": 0,
            "remaining": -1,
            "typical_run_calls": 1000,
            "can_run": True,
            "sampling_note": f"Budget check failed ({type(e).__name__}) — defaulting to allow.",
        }


@app.get("/settings/audit-folder")
async def get_audit_folder() -> dict[str, Any]:
    """Return the currently-configured audit folder path."""
    try:
        from backend.storage import get_archive
        return {"path": str(get_archive().folder)}
    except Exception:
        return {"path": str(DEFAULT_AUDIT_FOLDER), "uninitialized": True}


@app.post("/settings/audit-folder")
async def set_audit_folder_endpoint(req: AuditFolderRequest) -> dict[str, Any]:
    """Change the audit folder. Closes current archive connection and opens
    a new one at the requested path. Used when the user wants to point the
    app at a synced cloud-drive folder (OneDrive / Google Drive)."""
    from backend.storage import set_audit_folder as _set
    try:
        archive = _set(req.path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not open folder: {e}")
    return {"ok": True, "path": str(archive.folder)}


@app.post("/reset")
async def reset_all_data() -> dict[str, Any]:
    """Wipe all local user data and return to a fresh-install state.

    Backs the Settings → Reset all data button. Replaces the manual
    `rmdir /s /q` dance testers had to run during v0.1.5 testing.

    Steps, in order:
      1. Cancel any in-progress search task (mirrors /search/cancel logic
         per run, but applied to every active task at once).
      2. Drop the in-memory _RUNS and _BUILDS registries so stale state
         from this session can't leak into the next.
      3. archive.reset() — wipes runs.db + audit JSONs + diffs and
         reopens on a fresh empty schema.

    What's intentionally NOT wiped:
      - The audit folder path itself (so the user's chosen folder
        location persists — they don't have to re-pick it).
      - tester_id.txt — lives in the OS app-data dir managed by the
        Tauri shell; preserving it keeps Worker budget continuity.
      - cost_log.db — operational spend tracking, not user content.

    The frontend is responsible for the Tauri-side cleanup after this
    returns: clearing localStorage (Zustand persist) and calling
    relaunch(). The two halves are split so each side controls its own
    state — the backend doesn't reach into the webview, the frontend
    doesn't reach into runs.db.
    """
    # 1. Cancel any in-progress search tasks. Snapshot the task list first
    # because cancellation triggers done callbacks that mutate _RUN_TASKS.
    cancelled_runs: list[str] = []
    for run_id, task in list(_RUN_TASKS.items()):
        if not task.done():
            task.cancel()
            cancelled_runs.append(run_id)
        # Mirror what /search/cancel does to in-memory state, so any
        # frontend still polling sees the cancelled status before the
        # reset relaunches.
        st = _RUNS.get(run_id)
        if st and st.status not in ("completed", "failed", "cancelled"):
            st.status = "cancelled"
            st.current_step = "Cancelled"

    # 2. Drop in-memory run / build state. After reset, no run should be
    # discoverable via /search/status or /profile/status — they're gone.
    _RUNS.clear()
    _BUILDS.clear()

    # 3. Wipe the on-disk archive (runs.db + audits + diffs). If the
    # archive isn't initialized for some reason (rare — would only happen
    # if startup init failed), there's nothing to wipe and we still
    # report success since the goal state ("clean slate") is achieved.
    removed = {"db_files": 0, "audit_files": 0, "diff_files": 0, "other": 0}
    try:
        from backend.storage import get_archive
        archive = get_archive()
        removed = archive.reset()
    except RuntimeError:
        pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Archive reset failed: {e}")

    return {
        "ok": True,
        "cancelled_runs": cancelled_runs,
        "removed": removed,
    }


# ============================================================================
# Tester feedback — captured per-run, written into runs.db + audit JSON
# ============================================================================

class FeedbackRequest(BaseModel):
    run_id: str
    feedback: str
    bad_roles: Optional[list[dict[str, str]]] = None  # [{company, title, why}]


@app.post("/feedback")
async def submit_feedback(req: FeedbackRequest) -> dict[str, Any]:
    """Save tester feedback against a run. Updates runs.db and (when present)
    the audit JSON file alongside it. Used by the dashboard's post-run prompt."""
    try:
        from backend.storage import get_archive
        archive = get_archive()
        with archive._cursor() as cur:
            cur.execute(
                "UPDATE runs SET tester_feedback = ? WHERE run_id = ?",
                (req.feedback, req.run_id),
            )
            cur.execute(
                "SELECT audit_file_path FROM runs WHERE run_id = ?", (req.run_id,)
            )
            row = cur.fetchone()
        # Also patch the audit JSON file if present
        if row and row["audit_file_path"]:
            from pathlib import Path
            apath = Path(row["audit_file_path"])
            if apath.exists():
                try:
                    data = json.loads(apath.read_text(encoding="utf-8"))
                    data["tester_feedback"] = {
                        "free_text": req.feedback,
                        "bad_roles": req.bad_roles or [],
                        "submitted_at": datetime.now(timezone.utc).isoformat(),
                    }
                    apath.write_text(
                        json.dumps(data, indent=2, default=str), encoding="utf-8"
                    )
                except Exception:
                    pass
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Failed to save feedback: {e}")


@app.get("/applications")
async def list_applications() -> dict[str, Any]:
    """Return all saved/applied/hidden applications. Used by the frontend
    on first load to hydrate the UI from the archive (instead of relying
    only on Zustand's localStorage cache)."""
    try:
        from backend.storage import get_archive
        archive = get_archive()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Archive unavailable: {e}")
    rows = archive.get_all_applications()
    return {
        "applications": [
            {
                "role_id": r["role_id"],
                "company": r["company"],
                "job_title": r["job_title"],
                "job_url": r["job_url"],
                "status": r["status"],
                "application_stage": r["application_stage"],
                "notes": r["notes"],
                "status_date": r["status_date"],
            }
            for r in rows
        ]
    }


# ============================================================================
# Admin / cost
# ============================================================================

@app.get("/admin/cost")
async def admin_cost() -> dict[str, Any]:
    return {
        "today_usd": cost_tracker.cost_today(),
        "month_usd": cost_tracker.cost_this_month(),
        "recent_runs": cost_tracker.recent_runs(10),
    }


# ============================================================================
# Entrypoint
# ============================================================================

def main() -> None:
    """Run the API server. Listen only on 127.0.0.1 for safety."""
    import uvicorn
    uvicorn.run(
        "backend.api:app",
        host="127.0.0.1",
        port=8765,
        reload=config.DEV_MODE,
        log_level="info" if config.DEV_MODE else "warning",
    )


if __name__ == "__main__":
    main()

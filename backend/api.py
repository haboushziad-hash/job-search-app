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
from datetime import datetime
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
    current_step: str = ""
    current_step_index: int = 0        # 1..6 in the UI
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

app = FastAPI(
    title="Job Search API",
    description="Local bridge between the React desktop app and the Python search backend.",
    version="0.1.0",
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
        "version": "0.1.0",
        "env": {
            "google_keys_configured": len(config.google_api_keys()),
            "dev_mode": config.DEV_MODE,
        },
    }


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
        user_preferences["freeform_context"] = extra_context.strip()

    # Kick off the build asynchronously and return a build_id immediately.
    # The React frontend polls /profile/status/{id} for progress.
    build_id = str(uuid.uuid4())
    state = BuildState(
        build_id=build_id,
        started_at=datetime.utcnow(),
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
        keywords = req.keywords
    else:
        # Default: Tier 1 + Tier 2 keywords from profile
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
        started_at=datetime.utcnow(),
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
        state.current_step_index = 1
        state.progress = 5.0

        # Patch the runner's progress reporting via a wrapper
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
    # role_scores joined columns: final_score, final_tier, stage2_*, stage3_*
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
# Settings — audit folder location, cache duration are stored here
# ============================================================================

class AuditFolderRequest(BaseModel):
    path: str


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
                        "submitted_at": datetime.utcnow().isoformat(),
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

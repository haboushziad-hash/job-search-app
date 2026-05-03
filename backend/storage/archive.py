"""SQLite archive — single source of truth for everything the desktop app
remembers. Lives at <audit_folder>/runs.db. Schema is forward-compatible:
new columns are added with `ALTER TABLE` on init if they don't exist yet,
so a tester upgrading the app doesn't lose their archive.

What's stored:
  - roles:                  every role ever scraped (deduped by company+title+url)
  - liveness_checks:        per-role liveness verification history
  - runs:                   per-search summary + cost + audit-file pointer
  - role_scores:            per-(run, role) scoring decisions w/ Stage 2/3 reasoning
  - applications:           saved/applied/hidden status (migrated from Zustand)
  - market_contributions:   (company, title, score, profile_tags) tuples we
                            export for cross-tester closed-loop learning
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Module-level singleton — one Archive per process. Set via set_audit_folder()
# at app startup or when the user changes the audit folder in Settings.
# ---------------------------------------------------------------------------

_archive_singleton: Optional["Archive"] = None
_singleton_lock = threading.Lock()


def set_audit_folder(folder: Path | str) -> "Archive":
    """Set or reset the audit folder. Closes any prior connection and
    opens runs.db at the new location. Call at app startup."""
    global _archive_singleton
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs").mkdir(exist_ok=True)
    (folder / "diffs").mkdir(exist_ok=True)
    with _singleton_lock:
        if _archive_singleton is not None:
            _archive_singleton.close()
        _archive_singleton = Archive(folder)
    return _archive_singleton


def get_archive() -> "Archive":
    """Return the current archive. Caller must have called set_audit_folder
    first; otherwise raises RuntimeError."""
    if _archive_singleton is None:
        raise RuntimeError(
            "Archive not initialized. Call set_audit_folder(path) first "
            "(usually during app startup)."
        )
    return _archive_singleton


# ---------------------------------------------------------------------------
# Archive — owns the SQLite connection + provides typed query/insert helpers
# ---------------------------------------------------------------------------


class Archive:
    """Connection wrapper + schema management."""

    SCHEMA_VERSION = 1

    def __init__(self, folder: Path):
        self.folder = folder
        self.db_path = folder / "runs.db"
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL = better concurrency for cloud-synced files
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @contextmanager
    def _cursor(self):
        """Cursor with automatic commit on success, rollback on exception."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    # ----------------------------------------------------------------------
    # Schema
    # ----------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._cursor() as cur:
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS roles (
                    id INTEGER PRIMARY KEY,
                    company TEXT NOT NULL,
                    job_title TEXT NOT NULL,
                    job_url TEXT,
                    location TEXT,
                    location_type TEXT,
                    salary_min INTEGER,
                    salary_max INTEGER,
                    salary_text TEXT,
                    salary_source TEXT,                  -- structured / jd_extracted / reasoning_extracted
                    job_description_full TEXT,
                    industry TEXT,
                    posted_date TEXT,                    -- ISO8601
                    source TEXT,                         -- Greenhouse / Workday / Lever / Ashby
                    first_seen_date TEXT NOT NULL,
                    last_seen_date TEXT NOT NULL,
                    last_alive_date TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(company, job_title, job_url)
                );

                CREATE INDEX IF NOT EXISTS idx_roles_company       ON roles(company);
                CREATE INDEX IF NOT EXISTS idx_roles_company_title ON roles(company, job_title);
                CREATE INDEX IF NOT EXISTS idx_roles_last_seen     ON roles(last_seen_date);
                CREATE INDEX IF NOT EXISTS idx_roles_last_alive    ON roles(last_alive_date);

                CREATE TABLE IF NOT EXISTS liveness_checks (
                    role_id INTEGER NOT NULL,
                    check_date TEXT NOT NULL,
                    result TEXT NOT NULL,                -- alive / dead / blocked / no_url
                    PRIMARY KEY (role_id, check_date),
                    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,                -- running / completed / failed
                    profile_hash TEXT NOT NULL,
                    profile_tags_json TEXT,
                    keywords_json TEXT,
                    profile_snapshot_json TEXT,
                    summary_json TEXT,
                    cost_total_usd REAL,
                    qualifying_count INTEGER,
                    audit_file_path TEXT,
                    tester_feedback TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_completed_at ON runs(completed_at);
                CREATE INDEX IF NOT EXISTS idx_runs_profile_hash ON runs(profile_hash);

                CREATE TABLE IF NOT EXISTS role_scores (
                    run_id TEXT NOT NULL,
                    role_id INTEGER NOT NULL,
                    final_score REAL,
                    final_tier TEXT,
                    stage1_kept INTEGER,
                    stage2_score INTEGER,
                    stage2_tier TEXT,
                    stage2_reasoning TEXT,
                    stage3_score INTEGER,
                    stage3_tier TEXT,
                    stage3_analysis TEXT,
                    stage3_application_strategy TEXT,
                    embedding_similarity REAL,
                    keyword_matches_json TEXT,
                    summary TEXT,
                    PRIMARY KEY (run_id, role_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_role_scores_run ON role_scores(run_id);
                CREATE INDEX IF NOT EXISTS idx_role_scores_score ON role_scores(final_score);

                CREATE TABLE IF NOT EXISTS applications (
                    role_id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,                -- saved / applied / hidden
                    application_stage TEXT,              -- applied / phone_screen / interview / offer / rejected / ghosted / withdrew
                    notes TEXT,
                    status_date TEXT NOT NULL,
                    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);

                -- Cross-tester closed-loop learning: every qualifying role from
                -- every tester's run gets a row here. The keyword generator
                -- queries this when planning a new search and filters by
                -- profile_tag overlap to inject relevant titles.
                CREATE TABLE IF NOT EXISTS market_contributions (
                    company TEXT NOT NULL,
                    job_title TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    profile_tags_json TEXT NOT NULL,    -- JSON list of contributing user's tags
                    source_user TEXT NOT NULL,          -- 'self' for own runs; <hash> for ingested
                    contributed_date TEXT NOT NULL,
                    PRIMARY KEY (company, job_title, source_user)
                );

                CREATE INDEX IF NOT EXISTS idx_market_company_title ON market_contributions(company, job_title);
            """)
            # ---- Forward-compatible column migrations ----
            # Add columns to existing tables when schema_meta version is older
            # than the current code. CREATE TABLE IF NOT EXISTS is no-op on
            # existing tables, so new columns require ALTER.
            self._migrate_columns(cur)

            cur.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(self.SCHEMA_VERSION),)
            )

    def _migrate_columns(self, cur) -> None:
        """Apply ALTER TABLE migrations for new columns. Idempotent."""
        # role_scores.summary — added when AI-C dashboard summary was wired in
        cur.execute("PRAGMA table_info(role_scores)")
        rs_cols = {row["name"] for row in cur.fetchall()}
        if "summary" not in rs_cols:
            cur.execute("ALTER TABLE role_scores ADD COLUMN summary TEXT")
        if "keyword_matches_json" not in rs_cols:
            cur.execute("ALTER TABLE role_scores ADD COLUMN keyword_matches_json TEXT")
        # roles.industry — added with AI-A industry detection
        cur.execute("PRAGMA table_info(roles)")
        r_cols = {row["name"] for row in cur.fetchall()}
        if "industry" not in r_cols:
            cur.execute("ALTER TABLE roles ADD COLUMN industry TEXT")
        if "salary_source" not in r_cols:
            cur.execute("ALTER TABLE roles ADD COLUMN salary_source TEXT")

    # ----------------------------------------------------------------------
    # Roles — upsert + freshness queries
    # ----------------------------------------------------------------------

    def upsert_role(
        self,
        *,
        company: str,
        job_title: str,
        job_url: Optional[str],
        location: Optional[str] = None,
        location_type: Optional[str] = None,
        salary_min: Optional[int] = None,
        salary_max: Optional[int] = None,
        salary_text: Optional[str] = None,
        salary_source: Optional[str] = None,
        job_description_full: Optional[str] = None,
        industry: Optional[str] = None,
        posted_date: Optional[str] = None,
        source: Optional[str] = None,
        scrape_date: Optional[str] = None,
    ) -> int:
        """Insert-or-update a role; return its id."""
        if not scrape_date:
            scrape_date = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            # Try to find by (company, job_title, job_url)
            cur.execute(
                "SELECT id FROM roles WHERE company=? AND job_title=? AND IFNULL(job_url,'')=IFNULL(?,'')",
                (company, job_title, job_url),
            )
            row = cur.fetchone()
            if row:
                role_id = row["id"]
                cur.execute("""
                    UPDATE roles SET
                        location = COALESCE(?, location),
                        location_type = COALESCE(?, location_type),
                        salary_min = COALESCE(?, salary_min),
                        salary_max = COALESCE(?, salary_max),
                        salary_text = COALESCE(?, salary_text),
                        salary_source = COALESCE(?, salary_source),
                        job_description_full = COALESCE(?, job_description_full),
                        industry = COALESCE(?, industry),
                        posted_date = COALESCE(?, posted_date),
                        source = COALESCE(?, source),
                        last_seen_date = ?
                    WHERE id = ?
                """, (
                    location, location_type, salary_min, salary_max, salary_text,
                    salary_source, job_description_full, industry, posted_date,
                    source, scrape_date, role_id,
                ))
                return role_id
            cur.execute("""
                INSERT INTO roles (
                    company, job_title, job_url, location, location_type,
                    salary_min, salary_max, salary_text, salary_source,
                    job_description_full, industry, posted_date, source,
                    first_seen_date, last_seen_date
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                company, job_title, job_url, location, location_type,
                salary_min, salary_max, salary_text, salary_source,
                job_description_full, industry, posted_date, source,
                scrape_date, scrape_date,
            ))
            return cur.lastrowid

    def find_fresh_roles(
        self, *, max_age_days: int = 7
    ) -> list[sqlite3.Row]:
        """Return all roles whose last_seen_date is within max_age_days.
        Used by the cache-hit logic in run_search."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM roles WHERE last_seen_date >= ? AND is_active = 1",
                (cutoff,),
            )
            return cur.fetchall()

    def find_company_freshness(self, max_age_days: int = 7) -> dict[str, str]:
        """Return {company.lower(): most_recent_last_seen_iso} for roles within
        max_age_days. Used to skip rescraping companies we already have fresh
        data for."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._cursor() as cur:
            cur.execute("""
                SELECT LOWER(company) AS k, MAX(last_seen_date) AS last_seen
                FROM roles
                WHERE last_seen_date >= ? AND is_active = 1
                GROUP BY LOWER(company)
            """, (cutoff,))
            return {row["k"]: row["last_seen"] for row in cur.fetchall()}

    def record_liveness(self, *, role_id: int, result: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO liveness_checks(role_id, check_date, result) VALUES(?,?,?)",
                (role_id, now, result),
            )
            if result == "alive":
                cur.execute(
                    "UPDATE roles SET last_alive_date = ?, is_active = 1 WHERE id = ?",
                    (now, role_id),
                )
            elif result in ("dead", "expired"):
                cur.execute("UPDATE roles SET is_active = 0 WHERE id = ?", (role_id,))

    def role_alive_recently(self, role_id: int, max_age_hours: int = 24) -> bool:
        """Has this role been confirmed alive within the last N hours?
        If yes, the cache layer can skip re-running liveness check."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "SELECT 1 FROM roles WHERE id = ? AND last_alive_date >= ?",
                (role_id, cutoff),
            )
            return cur.fetchone() is not None

    # ----------------------------------------------------------------------
    # Runs — write summaries
    # ----------------------------------------------------------------------

    def begin_run(
        self,
        *,
        run_id: str,
        profile_snapshot: dict,
        keywords: list[dict],
    ) -> None:
        profile_tags = profile_snapshot.get("profile_tags") or []
        with self._cursor() as cur:
            cur.execute("""
                INSERT OR REPLACE INTO runs (
                    run_id, started_at, status, profile_hash,
                    profile_tags_json, keywords_json, profile_snapshot_json
                ) VALUES (?,?,?,?,?,?,?)
            """, (
                run_id,
                datetime.now(timezone.utc).isoformat(),
                "running",
                hash_profile(profile_snapshot),
                json.dumps(profile_tags),
                json.dumps(keywords),
                json.dumps(profile_snapshot, default=str),
            ))

    def complete_run(
        self,
        *,
        run_id: str,
        status: str,
        summary: dict,
        audit_file_path: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                UPDATE runs SET
                    completed_at = ?,
                    status = ?,
                    summary_json = ?,
                    cost_total_usd = ?,
                    qualifying_count = ?,
                    audit_file_path = ?
                WHERE run_id = ?
            """, (
                datetime.now(timezone.utc).isoformat(),
                status,
                json.dumps(summary, default=str),
                summary.get("cost_total_usd"),
                summary.get("roles_qualifying"),
                audit_file_path,
                run_id,
            ))

    def find_recent_run_for_profile(
        self, *, profile_snapshot: dict, max_age_days: int = 7
    ) -> Optional[sqlite3.Row]:
        """Return the most recent COMPLETED run for the same profile signature,
        if within max_age_days. None otherwise.

        Profile signature = hash(headline + target_functions + technical_skills
        + keywords + locations + salary_minimum). Any change to those fields
        invalidates the cache for that profile."""
        ph = hash_profile(profile_snapshot)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with self._cursor() as cur:
            cur.execute("""
                SELECT * FROM runs
                WHERE profile_hash = ? AND status = 'completed' AND completed_at >= ?
                ORDER BY completed_at DESC
                LIMIT 1
            """, (ph, cutoff))
            return cur.fetchone()

    def replay_cached_run(self, run_id: str) -> list[dict]:
        """Reconstruct the scored Role list from a prior run, joining roles
        + role_scores. Returns plain dicts (caller converts to Role objects).

        Used by the cache-hit path: when a fresh run exists for the same
        profile_hash, we replay its scored roles instead of re-scraping +
        re-scoring. Saves 12-25 minutes per repeat search.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT
                    r.*,
                    rs.final_score, rs.final_tier,
                    rs.stage2_score, rs.stage2_tier, rs.stage2_reasoning,
                    rs.stage3_score, rs.stage3_tier, rs.stage3_analysis,
                    rs.stage3_application_strategy,
                    rs.embedding_similarity
                FROM role_scores rs
                JOIN roles r ON r.id = rs.role_id
                WHERE rs.run_id = ?
            """, (run_id,))
            return [dict(row) for row in cur.fetchall()]

    def store_role_score(
        self, *, run_id: str, role_id: int, role_obj: Any
    ) -> None:
        """Persist a Role object's scoring decisions for this run.
        role_obj is the Role pydantic model with stage_*_score etc."""
        with self._cursor() as cur:
            cur.execute("""
                INSERT OR REPLACE INTO role_scores (
                    run_id, role_id,
                    final_score, final_tier,
                    stage1_kept, stage2_score, stage2_tier, stage2_reasoning,
                    stage3_score, stage3_tier, stage3_analysis, stage3_application_strategy,
                    embedding_similarity, keyword_matches_json, summary
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                run_id, role_id,
                getattr(role_obj, "final_score", None),
                _enum_value(getattr(role_obj, "final_tier", None)),
                int(getattr(role_obj, "stage1_anti_pattern", False) is False),
                getattr(role_obj, "stage2_score", None),
                _enum_value(getattr(role_obj, "stage2_tier", None)),
                getattr(role_obj, "stage2_reasoning", None),
                getattr(role_obj, "stage3_score", None),
                _enum_value(getattr(role_obj, "stage3_tier", None)),
                getattr(role_obj, "stage3_analysis", None),
                getattr(role_obj, "stage3_application_strategy", None),
                getattr(role_obj, "embedding_similarity", None),
                json.dumps(getattr(role_obj, "keyword_matches", []) or []),
                getattr(role_obj, "summary", None),
            ))

    # ----------------------------------------------------------------------
    # Applications — saved/applied/hidden
    # ----------------------------------------------------------------------

    def set_application_status(
        self,
        *,
        role_id: int,
        status: str,
        application_stage: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT OR REPLACE INTO applications
                    (role_id, status, application_stage, notes, status_date)
                VALUES (?,?,?,?,?)
            """, (
                role_id, status, application_stage, notes,
                datetime.now(timezone.utc).isoformat(),
            ))

    def get_all_applications(self) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            cur.execute("""
                SELECT a.*, r.company, r.job_title, r.job_url
                FROM applications a JOIN roles r ON r.id = a.role_id
            """)
            return cur.fetchall()

    def applied_role_keys(self) -> set[tuple[str, str]]:
        """Set of (company.lower(), title.lower()) pairs the user has marked
        applied. Used by the search runner to suppress already-applied roles."""
        with self._cursor() as cur:
            cur.execute("""
                SELECT LOWER(r.company) AS c, LOWER(r.job_title) AS t
                FROM applications a JOIN roles r ON r.id = a.role_id
                WHERE a.status = 'applied'
            """)
            return {(row["c"], row["t"]) for row in cur.fetchall()}

    # ----------------------------------------------------------------------
    # Market contributions — closed-loop learning
    # ----------------------------------------------------------------------

    def add_market_contribution(
        self,
        *,
        company: str,
        job_title: str,
        score: int,
        profile_tags: list[str],
        source_user: str = "self",
    ) -> None:
        with self._cursor() as cur:
            cur.execute("""
                INSERT OR REPLACE INTO market_contributions
                    (company, job_title, score, profile_tags_json, source_user, contributed_date)
                VALUES (?,?,?,?,?,?)
            """, (
                company, job_title, score, json.dumps(profile_tags),
                source_user, datetime.now(timezone.utc).isoformat(),
            ))

    def get_self_contributions(self) -> list[sqlite3.Row]:
        """Returns just *this* user's contributions, for export to the
        sibling-readable jsonl file."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM market_contributions WHERE source_user = 'self'"
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_profile(profile_snapshot: dict) -> str:
    """Stable hash over the user-changeable fields. Cache invalidates if any
    of these change (resume re-uploaded, keywords edited, locations changed)."""
    canonical_keys = (
        "headline", "target_functions", "target_industries", "target_seniority",
        "technical_skills", "domain_expertise", "salary_minimum",
        "work_arrangements", "acceptable_locations", "acceptable_location_radii",
        "excluded_locations", "negative_signals", "excluded_title_patterns",
    )
    canonical = {k: profile_snapshot.get(k) for k in canonical_keys}
    # Include keyword text list (tier doesn't matter for cache hit)
    keywords = profile_snapshot.get("keywords") or []
    canonical["_keywords"] = sorted([
        (kw.get("text") if isinstance(kw, dict) else getattr(kw, "text", str(kw)))
        for kw in keywords
    ])
    blob = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _enum_value(v) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "value"):
        return v.value
    return str(v)

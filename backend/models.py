"""Shared data models for the Job Search App backend.

Every component (scraper, filter, scorer, orchestrator) reads/writes
these structures. Pydantic gives us free validation + serialization.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ----------------------------------------------------------------------------
# Tier enum — used by Stage 2 / Stage 3 to bucket roles for the dashboard
# ----------------------------------------------------------------------------

class Tier(str, Enum):
    STRONG = "STRONG"     # 85+
    GOOD = "GOOD"         # 70-84
    MAYBE = "MAYBE"       # 55-69
    STRETCH = "STRETCH"   # 40-54
    SKIP = "SKIP"         # < 40 (filtered out, not shown)


def score_to_tier(score: int | float) -> Tier:
    """Convert numeric score to tier label."""
    s = float(score)
    if s >= 85: return Tier.STRONG
    if s >= 70: return Tier.GOOD
    if s >= 55: return Tier.MAYBE
    if s >= 40: return Tier.STRETCH
    return Tier.SKIP


# ----------------------------------------------------------------------------
# Role — the central data structure passed between every stage
# ----------------------------------------------------------------------------

class Role(BaseModel):
    """A scraped role, optionally enriched with scoring data.

    Mirrors the schema of the reference job_market.db roles table so we can
    seamlessly load historical data for validation testing.
    """
    # Identity
    job_id: Optional[int] = None
    job_title: str
    company: str
    job_url: Optional[str] = None

    # Salary
    salary_text: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_type: Optional[str] = None
    salary_currency: str = "USD"

    # Location
    location: Optional[str] = None
    location_type: Optional[str] = None        # "Remote" | "Hybrid" | "On-site"
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    remote_flexibility: Optional[str] = None

    # JD content
    job_description_full: Optional[str] = None
    job_description_essence: Optional[str] = None
    jd_completeness: Optional[str] = None       # "Full" | "Partial" | "Missing"

    # Extracted attributes (filled by scraper or LLM)
    years_required: Optional[int] = None
    degree_required: Optional[str] = None
    management_required: Optional[bool] = None
    travel_percent: Optional[str] = None
    clearance_required: Optional[str] = None
    seniority_level: Optional[str] = None
    technical_vs_strategic: Optional[str] = None
    role_category: Optional[str] = None
    consulting_experience_req: Optional[bool] = None
    company_size: Optional[str] = None
    industry: Optional[str] = None
    # AI-C: 2-sentence dashboard summary written by Stage 3.
    # First sentence describes what the role IS, second describes the
    # match signal or top concern. Skimmable preview text.
    summary: Optional[str] = None

    # The single most-relevant keyword from the candidate's profile that
    # matched this role. Computed after hard filters with this priority:
    #   1. Tier-1 keyword whose phrase appears in the job title (strongest signal)
    #   2. Tier-2 keyword whose phrase appears in the job title
    #   3. Tier-1 keyword whose phrase appears in the JD body
    #   4. Tier-2 keyword whose phrase appears in the JD body
    # Within the same tier+match-type, longer keywords win (more specific
    # = more meaningful). Surfaced on the dashboard card to show the user
    # WHY this role was scored.
    matched_keyword: Optional[str] = None

    # Provenance
    primary_source: Optional[str] = None
    date_first_seen: Optional[str] = None
    date_last_seen_in_scrape: Optional[str] = None
    posted_date: Optional[str] = None

    # Liveness check
    last_verified_attempt: Optional[str] = None
    last_verified_result: Optional[str] = None
    verification_confidence: Optional[str] = None
    freshness_score: Optional[int] = None       # 0-100

    # Misc
    is_contract: bool = False

    # Scoring (filled in by Stage 1/2/3)
    stage1_keep: Optional[bool] = None
    stage1_reason: Optional[str] = None
    embedding: Optional[list[float]] = None     # not persisted; runtime only
    embedding_similarity: Optional[float] = None

    stage2_score: Optional[int] = None
    stage2_tier: Optional[Tier] = None
    stage2_reasoning: Optional[str] = None
    stage2_confidence: Optional[float] = None

    stage3_score: Optional[int] = None
    stage3_tier: Optional[Tier] = None
    stage3_analysis: Optional[str] = None
    stage3_application_strategy: Optional[str] = None
    stage3_best_resume_match: Optional[str] = None  # filename of best-matching resume

    # Final score after cascade — used by dashboard
    final_score: Optional[int] = None
    final_tier: Optional[Tier] = None

    class Config:
        extra = "allow"  # tolerate fields we haven't modeled yet


# ----------------------------------------------------------------------------
# CandidateProfile — produced by ingesting one or more resumes
# ----------------------------------------------------------------------------

class CandidateProfile(BaseModel):
    """Unified profile derived from one or more uploaded resumes.

    A single profile drives the entire run. If multiple resumes are uploaded,
    the LLM merges them into one profile with weighted target areas.
    """
    # Identity
    name: Optional[str] = None
    headline: Optional[str] = None              # one-line professional summary

    # Targeting
    target_functions: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    target_seniority: Optional[str] = None      # e.g. "Senior IC" or "Mid-level Manager"
    years_experience: Optional[int] = None

    # Skills
    technical_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    domain_expertise: list[str] = Field(default_factory=list)

    # Geography + comp preferences (set in setup wizard)
    salary_minimum: Optional[int] = None        # soft floor
    work_arrangements: list[str] = Field(default_factory=list)  # ["remote", "hybrid", ...]
    acceptable_locations: list[str] = Field(default_factory=list)
    # Parallel list — same length as acceptable_locations, one radius per entry.
    # If shorter, missing entries default to 50 miles.
    acceptable_location_radii: list[int] = Field(default_factory=list)
    excluded_locations: list[str] = Field(default_factory=list)

    # Things the candidate explicitly wants to AVOID. Populated by the
    # profile builder from freeform context like "avoid hands-on engineering"
    # or "moving out of federal." Read by Stage 2 triage (-15 to -25 penalty)
    # and by hard_filters for cheap title-pattern exclusions.
    negative_signals: list[str] = Field(default_factory=list)

    # LLM-derived list of concrete title-keyword patterns to exclude at the
    # hard-filter stage. Generated by the profile builder based on the
    # candidate's actual resume + targets. Examples:
    #   - For a non-engineer: ["architect", "engineer", "developer", "data scientist"]
    #   - For an engineer with no sales bg: ["account executive", "sdr", "sales manager"]
    #   - For a marketing person: ["engineer", "data scientist", "actuary"]
    # Whole-word matched (case-insensitive) against role.job_title only.
    excluded_title_patterns: list[str] = Field(default_factory=list)

    # 5-10 short tags (industry / function / skill / domain) generated by the
    # profile builder. Used for cross-tester closed-loop learning: when a new
    # search runs, we filter the shared market_contributions to entries from
    # users whose profile_tags overlap >= 20% with this candidate's. The
    # matching titles get injected into the keyword-gen prompt.
    # Examples: ["consulting","federal","AI","enablement","governance",
    # "change_management"] / ["CPG","retail","sales","operations",
    # "territory_management","account_management"]
    profile_tags: list[str] = Field(default_factory=list)

    # Auto-generated keywords for scraping (specific job titles, e.g.
    # "AI Enablement Lead", "Senior Tax Accountant"). Used as fallback when
    # search_terms is empty (back-compat for profiles built before the
    # search-terms split). Also drives the "via X" matched_keyword UI tag.
    keywords: list["Keyword"] = Field(default_factory=list)

    # v0.1.4: Broad search phrases (8-12 entries, 2-3 words each) used as
    # upstream API queries by scrapers. Examples: "AI strategy", "tax
    # compliance", "sales operations". Designed to widen the recall of
    # upstream APIs (Workday/Adzuna/JSearch/etc.) which match by literal
    # phrase. Specific titles like "AI Enablement Lead" miss "Lead, AI
    # Enablement" at the upstream API level even when our token-overlap
    # matcher would have caught it — broader phrases solve that.
    #
    # Scraper logic: prefer search_terms when present; fall back to keywords
    # for old profiles. Token-overlap matcher rules already handle
    # 1-2 token phrases correctly (requires ALL tokens) so broad terms like
    # "AI strategy" don't false-match "AI Engineer".
    search_terms: list[str] = Field(default_factory=list)

    # Resume metadata — when multiple are uploaded
    resumes: list["ResumeMetadata"] = Field(default_factory=list)

    # v0.3.21 (FIX 26 — cost): input-derived cache key for the jd_score_cache.
    # Computed once when the profile is built (from resume_bytes +
    # user_preferences + prompt_version + model_versions) and stored here
    # so downstream stages can key the cross-run score cache on a
    # GUARANTEED-deterministic value rather than re-hashing LLM-generated
    # output fields (target_functions, headline, etc.) which can drift
    # slightly across rebuilds.
    #
    # Before this fix: the jd_score_cache.profile_hash() function
    # recomputed a hash from the profile dump's OUTPUT fields. Tiny LLM
    # drift between rebuilds produced different hashes → cache hits
    # silently went to 0 (audit showed 0 hits / 462 misses on a profile
    # that should have ~80% hit rate). Switching to this input-based
    # key restores the intended 50-89% hit rate per the
    # cost_optimization_research/02_jd_cache_simulation.py prototype
    # ($0.42-0.60/run saved on rerun days).
    input_cache_key: Optional[str] = None


class Keyword(BaseModel):
    """A search keyword with tier and quality flags."""
    text: str
    tier: int = 1                               # 1 = always run, 2 = if quota allows, 3 = optional
    flags: list[str] = Field(default_factory=list)  # ["too_broad", "too_narrow", "redundant"]
    estimated_match_count: Optional[int] = None


class ResumeMetadata(BaseModel):
    """One uploaded resume with its derived emphasis."""
    filename: str
    emphasis: Optional[str] = None              # e.g. "AI strategy" vs "consulting"
    headline: Optional[str] = None


# ----------------------------------------------------------------------------
# Run summary — what the dashboard shows after completion
# ----------------------------------------------------------------------------

class RunSummary(BaseModel):
    run_id: str
    # Use timezone-aware UTC so the ISO string serializes with `+00:00`.
    # `datetime.utcnow()` returns a NAIVE datetime that pydantic emits
    # without a tz suffix — and JS `new Date(isoString)` interprets a
    # tz-less string as LOCAL time, so the dashboard would display UTC
    # values as if they were local (off by your local UTC offset).
    run_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    license_key: Optional[str] = None

    # Volume
    keywords_used: int = 0
    boards_searched: list[str] = Field(default_factory=list)
    roles_scraped: int = 0
    roles_after_filter: int = 0
    roles_after_stage1: int = 0
    roles_after_stage2: int = 0
    roles_qualifying: int = 0   # final >= 40

    # Tier breakdown
    tier_strong: int = 0
    tier_good: int = 0
    tier_maybe: int = 0
    tier_stretch: int = 0

    # Cost (operator-only — never shown to end user)
    cost_stage1_usd: float = 0.0
    cost_stage2_usd: float = 0.0
    cost_stage3_usd: float = 0.0
    cost_embeddings_usd: float = 0.0
    cost_misc_usd: float = 0.0
    cost_total_usd: float = 0.0

    # Performance
    duration_seconds: int = 0
    cap_hits: list[str] = Field(default_factory=list)  # which caps tripped, if any

    # Data quality
    jd_coverage_pct: float = 0.0
    salary_coverage_pct: float = 0.0          # % of all scraped roles
    salary_coverage_qualifying_pct: float = 0.0  # % of qualifying roles (≥40 final score)
    location_coverage_pct: float = 0.0
    self_heal_triggered: bool = False

    # Coverage-gap signal — set when target_industries are under-represented
    # in the scraper pool. Surfaced as a banner on the dashboard so testers
    # see "your sector has limited coverage" instead of mysteriously weak results.
    coverage_gap_severity: Optional[str] = None  # HIGH/MEDIUM/LOW/UNKNOWN/NO_DATA
    coverage_gap_message: Optional[str] = None
    coverage_gap_target_match_pct: Optional[float] = None

    # Per-source health snapshot — populated automatically by the orchestrator
    # at scrape time. Lets us detect at audit-time when a scraper is producing
    # zero or anomalously few results (likely broken/rate-limited).
    # Format: {"Greenhouse": {"roles": 1047, "elapsed_s": 109.1, "errored": false}, ...}
    per_source_counts: dict = Field(default_factory=dict)

    # Per-source funnel — populated by runner.py after scoring completes.
    # Reveals where each scraper's roles get dropped: scrape → dedup →
    # hard_filters → liveness → final qualifying. Critical for diagnosing
    # cases like Workday 8147 raw → 0 qualifying.
    # Format: {"Workday": {"raw_scraped": 8147, "after_dedup": 8001,
    #          "after_hard_filters": 12, "after_liveness_and_deadlist": 12,
    #          "qualifying_final": 0}, ...}
    per_source_funnel: dict = Field(default_factory=dict)

    # Status
    status: str = "running"                     # running / completed / failed / cancelled


# ----------------------------------------------------------------------------
# LLM call log — every API call gets one of these (telemetry)
# ----------------------------------------------------------------------------

class LLMCallLog(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    run_id: Optional[str] = None
    license_key: Optional[str] = None
    stage: str                                  # "stage1" | "stage2" | "stage3" | "embedding" | "misc"
    provider: str                               # "google" | "cloudflare"
    model: str
    is_batch: bool = False
    input_tokens: int = 0
    output_tokens: int = 0                      # NOTE (v0.3.13.1): includes thinking_tokens
    cached_tokens: int = 0
    thinking_tokens: int = 0                    # Gemini 2.5 reasoning tokens (already folded into output_tokens for cost)
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = True
    error_message: Optional[str] = None


# Pydantic forward-ref resolution
CandidateProfile.model_rebuild()

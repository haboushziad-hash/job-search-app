// Mirrors the Pydantic models in backend/models.py
// Kept thin — only fields the UI actually reads.

export type Tier = 'STRONG' | 'GOOD' | 'MAYBE' | 'STRETCH' | 'SKIP'

export interface Role {
  job_id?: number
  job_title: string
  company: string
  job_url?: string
  salary_text?: string | null
  salary_min?: number | null
  salary_max?: number | null
  location?: string | null
  location_type?: 'Remote' | 'Hybrid' | 'On-site' | string | null
  job_description_full?: string | null
  posted_date?: string | null
  primary_source?: string | null
  freshness_score?: number | null

  // Scoring fields
  final_score?: number | null
  final_tier?: Tier | null
  stage2_score?: number | null
  stage2_reasoning?: string | null
  stage3_score?: number | null
  stage3_analysis?: string | null
  stage3_application_strategy?: string | null
  stage3_best_resume_match?: string | null
  // AI-C: 2-sentence dashboard preview written by Stage 3
  summary?: string | null
  industry?: string | null
  // The single most-relevant keyword that surfaced this role (Tier-1
  // title-match preferred). Shown as the "via {keyword}" hint on the card.
  matched_keyword?: string | null
}

export interface ResumeMetadata {
  filename: string
  emphasis?: string | null
  headline?: string | null
}

export interface Keyword {
  text: string
  tier: number
  flags?: string[]
}

export interface CandidateProfile {
  name?: string | null
  headline?: string | null
  target_functions: string[]
  target_industries: string[]
  target_seniority?: string | null
  years_experience?: number | null
  technical_skills: string[]
  domain_expertise: string[]
  soft_skills: string[]
  salary_minimum?: number | null
  work_arrangements: string[]
  acceptable_locations: string[]
  acceptable_location_radii?: number[]
  excluded_locations: string[]
  // Specific job titles ("AI Enablement Lead", "Senior Tax Accountant")
  // Used by the scoring rubric and "via X" tag matcher.
  keywords: Keyword[]
  // v0.1.4: broad 2-3 word phrases ("AI strategy", "tax compliance") used as
  // upstream API queries by scrapers. Wider net than specific titles.
  // Fallback to keywords for old profiles without search_terms.
  search_terms?: string[]
  resumes: ResumeMetadata[]
}

export interface RunSummary {
  run_id: string
  run_date: string
  roles_scraped: number
  roles_after_filter: number
  roles_qualifying: number
  tier_strong: number
  tier_good: number
  tier_maybe: number
  tier_stretch: number
  duration_seconds: number
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  jd_coverage_pct: number
  salary_coverage_pct: number
  location_coverage_pct: number
  // Coverage gap signal — set by the audit writer when a tester's target
  // industries are under-represented in the scraper's employer pool.
  // Surfaces a transparent "this version doesn't cover your sector well"
  // warning instead of silently showing weak adjacent-industry results.
  coverage_gap_severity?: 'HIGH' | 'MEDIUM' | 'LOW' | 'UNKNOWN' | 'NO_DATA'
  coverage_gap_message?: string
  coverage_gap_target_match_pct?: number
}

export interface ApplicationRecord {
  application_id: string
  role_id: number
  company: string
  title: string
  url: string
  score: number
  tier: Tier
  applied_date: string
  status:
    | 'applied' | 'phone_screen' | 'interview' | 'offer'
    | 'rejected' | 'ghosted' | 'withdrew'
  resume_used?: string
  notes?: string
  next_action?: string
  next_action_date?: string
}

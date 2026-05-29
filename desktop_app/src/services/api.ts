/**
 * Typed API client for the local Python FastAPI backend.
 *
 * All requests go to localhost:8765 (the FastAPI server spawned alongside
 * the Tauri app). In dev, both run side-by-side on the host machine.
 */

import type { CandidateProfile, Role, RunSummary } from '@/types'

const API_BASE = 'http://127.0.0.1:8765'

export interface BackendError {
  detail: string
}

class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function handle<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = (await response.json()) as BackendError
      detail = body.detail || detail
    } catch {}
    throw new ApiError(response.status, detail)
  }
  return response.json() as Promise<T>
}

// ----------------------------------------------------------------------------
// Health check
// ----------------------------------------------------------------------------

export async function healthCheck(): Promise<{ status: string; version: string; env: { google_keys_configured: number; dev_mode: boolean } }> {
  const res = await fetch(`${API_BASE}/health`)
  return handle(res)
}

// ----------------------------------------------------------------------------
// Identity — anonymous tester UUID (generated on first launch, persisted in
// app data, sent to the backend as TESTER_UUID env var on every spawn).
// Surfaced in Settings so users can copy/share their ID for support.
// ----------------------------------------------------------------------------

export async function getIdentity(): Promise<{ tester_uuid: string }> {
  const res = await fetch(`${API_BASE}/identity`)
  return handle(res)
}

// ----------------------------------------------------------------------------
// Profile build (multipart upload)
// ----------------------------------------------------------------------------

export interface BuildProfileInput {
  files: File[]
  extraContext?: string
  salaryMinimum?: number
  workArrangements?: string[]
  acceptableLocations?: string[]
  acceptableLocationRadii?: number[]
  excludedLocations?: string[]
}

export interface BuildProfileResult {
  profile: CandidateProfile
  keyword_count: number
  tier_breakdown: { tier_1: number; tier_2: number; tier_3: number }
}

export interface BuildProfileStartResult {
  build_id: string
  status: string
}

/**
 * Kick off a profile build. Returns immediately with a build_id.
 * Poll /profile/status/{id} for progress, then /profile/result/{id} on done.
 */
export async function startProfileBuild(input: BuildProfileInput): Promise<BuildProfileStartResult> {
  const fd = new FormData()
  for (const f of input.files) fd.append('files', f)
  fd.append('extra_context', input.extraContext || '')
  fd.append('salary_minimum', String(input.salaryMinimum || 0))
  fd.append('work_arrangements', JSON.stringify(input.workArrangements || []))
  fd.append('acceptable_locations', JSON.stringify(input.acceptableLocations || []))
  fd.append('acceptable_location_radii', JSON.stringify(input.acceptableLocationRadii || []))
  fd.append('excluded_locations', JSON.stringify(input.excludedLocations || []))

  const res = await fetch(`${API_BASE}/profile/build`, {
    method: 'POST',
    body: fd,
  })
  return handle(res)
}

export interface BuildProgress {
  build_id: string
  started_at: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number       // 0-100
  current_step: string
  error?: string | null
}

export async function getBuildStatus(buildId: string): Promise<BuildProgress> {
  const res = await fetch(`${API_BASE}/profile/status/${buildId}`)
  return handle(res)
}

export async function getBuildResult(buildId: string): Promise<BuildProfileResult> {
  const res = await fetch(`${API_BASE}/profile/result/${buildId}`)
  return handle(res)
}

/**
 * Convenience helper — kick off a build and poll until complete.
 * Calls onProgress() each time the percentage advances.
 */
export async function buildProfile(
  input: BuildProfileInput,
  onProgress?: (pct: number, step: string) => void,
): Promise<BuildProfileResult> {
  const start = await startProfileBuild(input)
  const buildId = start.build_id

  let lastPct = -1
  while (true) {
    const status = await getBuildStatus(buildId)
    if (status.progress !== lastPct || status.current_step) {
      lastPct = status.progress
      onProgress?.(status.progress, status.current_step)
    }
    if (status.status === 'completed') {
      return getBuildResult(buildId)
    }
    if (status.status === 'failed') {
      throw new ApiError(500, status.error || 'Profile build failed')
    }
    await new Promise((r) => setTimeout(r, 1500))
  }
}

// ----------------------------------------------------------------------------
// Search run
// ----------------------------------------------------------------------------

export interface AppliedRoleRef {
  company: string
  title: string
}

export interface RunSearchInput {
  profile: CandidateProfile
  keywords?: string[]
  sources?: string[]
  postedWithinDays?: number
  appliedRoles?: AppliedRoleRef[]   // suppressed from results
  cacheMaxAgeDays?: number          // default 7 — cache window
  forceRefresh?: boolean            // bypass cache entirely
}

export interface RunSearchResult {
  run_id: string
  status: string
}

// ----------------------------------------------------------------------------
// v0.1.4 — Pre-flight budget check
// ----------------------------------------------------------------------------

export interface BudgetStatus {
  used: number
  cap: number
  remaining: number
  typical_run_calls: number
  can_run: boolean
  sampling_note: string
}

/** Pre-flight: check whether today's Worker cap has enough headroom for a
 *  full search. In dev mode, returns can_run=true with sentinel values. */
export async function getBudget(): Promise<BudgetStatus> {
  const res = await fetch(`${API_BASE}/llm/budget`)
  return handle(res)
}

export async function runSearch(input: RunSearchInput): Promise<RunSearchResult> {
  const res = await fetch(`${API_BASE}/search/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      profile: input.profile,
      keywords: input.keywords,
      sources: input.sources,
      posted_within_days: input.postedWithinDays ?? 30,
      applied_roles: input.appliedRoles,
      cache_max_age_days: input.cacheMaxAgeDays ?? 7,
      force_refresh: input.forceRefresh ?? false,
    }),
  })
  return handle(res)
}

// ----------------------------------------------------------------------------
// Run history — list past completed runs + load any one of them
// ----------------------------------------------------------------------------

export interface RunHistoryItem {
  run_id: string
  started_at?: string | null
  completed_at?: string | null
  qualifying_count?: number | null
  scraped?: number | null
  tier_strong?: number | null
  tier_good?: number | null
  tier_maybe?: number | null
  tier_stretch?: number | null
  duration_seconds?: number | null
  profile_headline?: string | null
}

export async function listRuns(): Promise<{ runs: RunHistoryItem[] }> {
  const res = await fetch(`${API_BASE}/runs`)
  return handle(res)
}

export async function getRun(runId: string): Promise<{
  run_id: string
  summary: Record<string, unknown>
  profile: Record<string, unknown>
  roles: Array<Record<string, unknown>>
}> {
  const res = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}`)
  return handle(res)
}

// v0.2.0: Stats page reads the full audit JSON for a run. Returns the
// raw audit dict — the Stats page computes salary buckets, top
// companies, etc. client-side. Type kept loose because audit fields
// vary by version; the Stats page handles missing fields gracefully.
export async function getRunAudit(runId: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/audit`)
  return handle(res)
}

export async function deleteRun(runId: string): Promise<{ run_id: string; deleted: boolean }> {
  const res = await fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}`, {
    method: 'DELETE',
  })
  return handle(res)
}

// ----------------------------------------------------------------------------
// Recent unique profiles — for the StartSearch choice screen so a returning
// user can pick which past profile to re-run. Backend dedupes by profile_hash
// and caps at 3 unique profiles ordered by most recent run.
// ----------------------------------------------------------------------------

export interface RecentProfile {
  run_id: string
  last_used_at: string
  profile_hash: string
  profile: CandidateProfile
  qualifying_count?: number | null
  tier_strong?: number | null
  tier_good?: number | null
}

export async function getRecentProfiles(): Promise<{ profiles: RecentProfile[] }> {
  const res = await fetch(`${API_BASE}/profiles/recent`)
  return handle(res)
}

// ----------------------------------------------------------------------------
// Search status (polled while running)
// ----------------------------------------------------------------------------

export interface SearchStatus {
  run_id: string
  started_at: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  current_step: string
  current_step_index: number
  // Granular live text shown under the active step
  // (e.g. "Stage 3 deep evaluation on 51 qualifying roles (Pro, ~5-10s each)...")
  current_step_detail?: string
  total_steps: number
  roles_scraped: number
  roles_qualifying: number
  tier_strong: number
  tier_good: number
  tier_maybe: number
  tier_stretch: number
  error?: string | null
}

export async function getSearchStatus(runId: string): Promise<SearchStatus> {
  const res = await fetch(`${API_BASE}/search/status/${runId}`)
  return handle(res)
}

// Cancel an in-progress search. Backend abandons partial work — no archive
// entry, no audit JSON, no run-history record is written for cancelled runs.
export async function cancelSearch(runId: string): Promise<{ run_id: string; status: string }> {
  const res = await fetch(`${API_BASE}/search/cancel/${runId}`, { method: 'POST' })
  return handle(res)
}

// ----------------------------------------------------------------------------
// Search results (after completion)
// ----------------------------------------------------------------------------

export interface SearchResults {
  run_id: string
  summary: RunSummary
  roles: Role[]
}

export async function getSearchResults(runId: string): Promise<SearchResults> {
  const res = await fetch(`${API_BASE}/search/results/${runId}`)
  return handle(res)
}

// ----------------------------------------------------------------------------
// Admin / cost (operator only)
// ----------------------------------------------------------------------------

export async function getCost(): Promise<{
  today_usd: number
  month_usd: number
  recent_runs: Array<{ run_id: string; status: string; cost_total_usd: number }>
}> {
  const res = await fetch(`${API_BASE}/admin/cost`)
  return handle(res)
}

// ----------------------------------------------------------------------------
// Application status — persists saved/applied/hidden into runs.db so it
// survives reinstalls and is available to future search runs
// ----------------------------------------------------------------------------

export type ApplicationStatusKind = 'saved' | 'applied' | 'hidden'

export interface ApplicationStatusInput {
  company: string
  jobTitle: string
  jobUrl?: string
  status: ApplicationStatusKind
  applicationStage?: string
  notes?: string
}

export async function setApplicationStatus(input: ApplicationStatusInput): Promise<void> {
  const res = await fetch(`${API_BASE}/applications/status`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      company: input.company,
      job_title: input.jobTitle,
      job_url: input.jobUrl,
      status: input.status,
      application_stage: input.applicationStage,
      notes: input.notes,
    }),
  })
  await handle(res)
}

// v0.3.15 (P1.11): Log a role-interaction event. Append-only timeline of
// user behavior. Distinct from setApplicationStatus, which writes the
// PERSISTENT user-relationship flag (saved/applied/hidden) to runs.db.
// This event log is the SEQUENCE — what the user did and when — which is
// what enables the 70-application ground-truth validation.
//
// Event types fall into three categories. Only the first two are usable
// for fit-quality analysis. See backend/api.py docstring for the full
// rationale; short version below.
//
// USABLE POSITIVE SIGNALS:
//   mark_applied  — STRONGEST    — multi-step commitment; closest to ground-truth "good fit"
//   visit_link    — MEDIUM-STRONG — opened the posting; ambiguous (interest vs disinterest-confirm)
//   save          — MEDIUM       — bookmarked but uncommitted
// USABLE OUTCOME SIGNAL (post-application, not fit):
//   update_app_stage — interview/offer/rejected/etc; downstream of fit
// NOISE — log for sequence completeness, DO NOT use for analysis:
//   unmark_applied   — conflates retraction with post-rejection cleanup
//   unsave           — conflates mind-change with cleanup
//   unhide           — conflates mind-change with cleanup
//   hide             — conflates bad-fit with already-rejected/applied/fatigue
//
// Stacking patterns for analysis (group by roleUrl, USABLE signals only):
//   saved + visited + mark_applied   → STRONGEST positive (full funnel)
//   mark_applied without prior visit → applied via referral / external
//   visited only, no apply           → ambiguous (interest vs reject)
//
// Fire-and-forget. Failures must NOT block the user's flow.
export type RoleEventType =
  | 'visit_link'
  | 'mark_applied'
  | 'unmark_applied'
  | 'save'
  | 'unsave'
  | 'hide'
  | 'unhide'
  | 'update_app_stage'

export interface RoleEventInput {
  eventType: RoleEventType
  roleUrl: string
  company?: string
  jobTitle?: string
  finalScore?: number | null
  finalTier?: string | null
  stage2Score?: number | null
  stage3Score?: number | null
  source?: string  // 'role_card' | 'detail_view' | 'list' | 'tracker' | etc.
  runId?: string
  profileHash?: string
  applicationStage?: string  // only used for update_app_stage
}

export async function logRoleEvent(input: RoleEventInput): Promise<void> {
  try {
    await fetch(`${API_BASE}/applications/event`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        event_type: input.eventType,
        role_url: input.roleUrl,
        company: input.company,
        job_title: input.jobTitle,
        final_score: input.finalScore,
        final_tier: input.finalTier,
        stage2_score: input.stage2Score,
        stage3_score: input.stage3Score,
        source: input.source,
        run_id: input.runId,
        profile_hash: input.profileHash,
        application_stage: input.applicationStage,
      }),
    })
  } catch {
    // Never block the user flow on logging failures
  }
}

// ----------------------------------------------------------------------------
// Settings — audit folder + cache duration
// ----------------------------------------------------------------------------

export interface FeedbackInput {
  runId: string
  feedback: string
  badRoles?: Array<{ company: string; title: string; why: string }>
}

export async function submitFeedback(input: FeedbackInput): Promise<void> {
  const res = await fetch(`${API_BASE}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      run_id: input.runId,
      feedback: input.feedback,
      bad_roles: input.badRoles,
    }),
  })
  await handle(res)
}

// ----------------------------------------------------------------------------
// User feedback — general open-ended feedback from any tester. Posts to the
// central Worker (api.findmesomedamnjobz.com) which stores submissions in R2
// under feedback/YYYY-MM-DD/. Distinct from submitFeedback() above which is
// per-run scoring-quality feedback that lives in the local backend.
// ----------------------------------------------------------------------------

const FEEDBACK_WORKER_URL = 'https://api.findmesomedamnjobz.com/v1/feedback'

export type FeedbackCategory =
  | 'bug'
  | 'false-positive'   // role surfaced that shouldn't have
  | 'false-negative'   // role missed that should have surfaced
  | 'ui'               // confusing copy / layout / interaction
  | 'performance'      // slow / hangs / crashes
  | 'setup'            // install / first-run friction
  | 'suggestion'       // feature request / idea
  | 'other'

export interface SendFeedbackInput {
  message: string
  category: FeedbackCategory
  email?: string
  appVersion?: string
}

export async function sendFeedback(input: SendFeedbackInput): Promise<{ ok: boolean; id: string }> {
  // Pull tester UUID from the local backend health check so the Worker can
  // tag this submission to the same identity as the tester's audit uploads.
  // If the call fails (offline, backend down), we still send the feedback
  // anonymously rather than blocking the user.
  let testerUuid = ''
  try {
    // Backend exposes the UUID indirectly via the env var it loaded — we
    // don't have a dedicated endpoint, so we just send the request without
    // and let the Worker treat it as anonymous (still rate-limited by IP).
    // (If we add a /health/uuid endpoint later, plumb it here.)
  } catch {}

  const res = await fetch(FEEDBACK_WORKER_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(testerUuid ? { 'X-Tester-UUID': testerUuid } : {}),
    },
    body: JSON.stringify({
      source: 'app',
      category: input.category,
      message: input.message,
      email: input.email || undefined,
      app_version: input.appVersion || undefined,
    }),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json() as { error?: string; message?: string }
      detail = body.message || body.error || detail
    } catch {}
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<{ ok: boolean; id: string }>
}

export async function getAuditFolder(): Promise<{ path: string; uninitialized?: boolean }> {
  const res = await fetch(`${API_BASE}/settings/audit-folder`)
  return handle(res)
}

export async function setAuditFolder(path: string): Promise<{ ok: boolean; path: string }> {
  const res = await fetch(`${API_BASE}/settings/audit-folder`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
  return handle(res)
}

export interface ApplicationRecord {
  role_id: number
  company: string
  job_title: string
  job_url: string | null
  status: ApplicationStatusKind
  application_stage: string | null
  notes: string | null
  status_date: string
}

export async function listApplications(): Promise<ApplicationRecord[]> {
  const res = await fetch(`${API_BASE}/applications`)
  const data = await handle<{ applications: ApplicationRecord[] }>(res)
  return data.applications
}

// ----------------------------------------------------------------------------
// Reset all data — Settings → Danger Zone
// ----------------------------------------------------------------------------
// Wipes runs.db, audit JSONs, diffs, and in-memory run/build state on the
// backend. The frontend half (clearing Zustand's localStorage and relaunching
// the app) is the caller's responsibility — see Settings.tsx for the wiring.
//
// Only the backend cleanup happens here. Splitting it this way keeps the
// API client a thin transport layer and lets the page own UX choices like
// when to relaunch and what to show during the wipe.

export interface ResetResult {
  ok: boolean
  cancelled_runs: string[]
  removed: {
    db_files: number
    audit_files: number
    diff_files: number
    other: number
  }
}

export async function resetAllData(): Promise<ResetResult> {
  const res = await fetch(`${API_BASE}/reset`, { method: 'POST' })
  return handle(res)
}

export { ApiError }

/**
 * Choice page that returning users hit between /how-it-works and the
 * actual search.
 *
 * Behavior:
 *   - Fetches up to 3 unique past profiles from `runs.db` (deduped on
 *     the backend by (resume filenames, salary_minimum) — NOT by the
 *     stored profile_hash, since that hash is keyed on AI-extracted
 *     fields that vary slightly across runs of the same resume).
 *     Each card shows enough context (filename, search terms count,
 *     keywords count, last used) for the user to pick.
 *   - User must explicitly click a card to select it. Re-Run button is
 *     disabled until selection — eliminates accidental mis-clicks.
 *   - On Re-Run: hydrate the selected profile into localStorage (so
 *     downstream pages like Dashboard / Run see it) and fire `runSearch`
 *     directly. Skip the /run review screen for this entry path — the
 *     review already happened here on the choice screen.
 *   - "Start fresh with new resume" → /setup, always available.
 *
 * Edge cases:
 *   - No backend connectivity / new install: fall back to whatever's in
 *     localStorage (the saved profile from a build that hasn't run yet).
 *     If even that's empty, redirect to /setup.
 */
import { useEffect, useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, ArrowLeft, Upload, Loader2, Check, FileText, Sparkles, Clock, ChevronDown, X, MapPin, DollarSign } from 'lucide-react'
import { toast } from 'sonner'
import { ZMark } from '@/components/ZMark'
import { SparkleBurst } from '@/components/SparkleBurst'
import { LocationAutocomplete } from '@/components/LocationAutocomplete'
import { useAppStore } from '@/stores/appStore'
import { runSearch, getRecentProfiles, ApiError } from '@/services/api'
import type { RecentProfile } from '@/services/api'
import type { CandidateProfile } from '@/types'
import { cn } from '@/lib/utils'


export default function StartSearch() {
  const navigate = useNavigate()
  const localProfile = useAppStore((s) => s.profile)
  const lastSummary = useAppStore((s) => s.lastSummary)
  const setProfile = useAppStore((s) => s.setProfile)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)

  // Server-side profile list (deduped by profile_hash, max 3). Fetched on
  // mount; we fall back to a single-card view of localProfile if the
  // backend is empty (first run after build, no completed search yet).
  const [profiles, setProfiles] = useState<RecentProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  // Increment to trigger the SparkleBurst animation on the Re-Run button.
  const [burstTrigger, setBurstTrigger] = useState(0)

  // Inline "Adjust salary or locations" expansion. Auto-populates from
  // the selected profile so the user can tweak before re-running
  // without rebuilding the whole profile from scratch.
  const [showAdjust, setShowAdjust] = useState(false)
  const [adjustedSalary, setAdjustedSalary] = useState<number | null>(null)
  const [adjustedLocations, setAdjustedLocations] = useState<string[]>([])
  const [newLocationDraft, setNewLocationDraft] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await getRecentProfiles()
        if (cancelled) return
        setProfiles(res.profiles || [])
      } catch (e) {
        if (cancelled) return
        // Backend hiccup — show local-only fallback instead of an error
        // page. The user will still be able to re-run if a localProfile
        // exists.
        console.warn('[start-search] failed to load recent profiles:', e)
        setProfiles([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  // Edge case: no profiles AND no localStorage profile → bounce to /setup.
  useEffect(() => {
    if (loading) return
    const hasLocal = !!localProfile && localProfile.keywords.length > 0
    if (profiles.length === 0 && !hasLocal) {
      navigate('/setup', { replace: true })
    }
  }, [loading, profiles.length, localProfile, navigate])

  // Cards to render: prefer backend list when present. Fall back to a
  // synthetic single card built from localProfile + lastSummary when
  // backend has no completed runs yet.
  const cards: RecentProfile[] = (() => {
    if (profiles.length > 0) return profiles
    if (localProfile && localProfile.keywords.length > 0) {
      return [{
        run_id: 'local',
        last_used_at: lastSummary?.run_date || new Date().toISOString(),
        profile_hash: 'local',
        profile: localProfile,
      }]
    }
    return []
  })()

  // The currently-selected profile, derived once for downstream use
  // (handleReRun and the adjust panel reset effect).
  const selectedCard = useMemo(
    () => cards.find((c) => c.run_id === selectedRunId) || null,
    [cards, selectedRunId]
  )

  // When the user picks a (different) profile, reset the adjust-panel
  // values to that profile's current settings. Keeps the panel in sync
  // with whichever card is selected so the user always sees the values
  // attached to that profile, not stale values from a previous click.
  useEffect(() => {
    if (!selectedCard) {
      setAdjustedSalary(null)
      setAdjustedLocations([])
      return
    }
    const p = selectedCard.profile
    setAdjustedSalary(p.salary_minimum ?? 100_000)
    setAdjustedLocations([...(p.acceptable_locations || [])])
  }, [selectedCard])

  // Orphaned handleAddLocation removed in v0.2.0 — replaced by inline
  // onAdd in <LocationAutocomplete>. The shared component handles the
  // dedup + draft reset internally.

  const handleRemoveLocation = (loc: string) => {
    setAdjustedLocations((prev) => prev.filter((l) => l !== loc))
  }

  // Build the applied-roles list — same logic as Run.tsx.
  const appliedRoles = Object.values(roleStatuses)
    .filter((entry) => entry.status === 'applied')
    .map((entry) => ({
      company: entry.roleSnapshot?.company || '',
      title: entry.roleSnapshot?.job_title || '',
    }))
    .filter((r) => r.company && r.title)

  const handleReRun = async () => {
    if (submitting || !selectedRunId || !selectedCard) return

    // Fire the celebratory sparkle burst on the button. Increment the
    // trigger so a fresh AnimatePresence cycle plays even on rapid
    // re-clicks (though the button is also disabled while submitting).
    setBurstTrigger((t) => t + 1)
    setSubmitting(true)
    try {
      // Build the profile we'll search with: start from the selected
      // saved snapshot, then overlay any inline adjustments the user
      // made (salary minimum, acceptable locations). If the user didn't
      // expand the adjust panel, the values are identical to the
      // saved profile and this is a no-op.
      const profileToRun = {
        ...(selectedCard.profile as CandidateProfile),
        salary_minimum: adjustedSalary ?? selectedCard.profile.salary_minimum ?? null,
        acceptable_locations: adjustedLocations,
      }

      // Hydrate localStorage with the (possibly adjusted) profile so
      // downstream pages see the updated settings as the "active"
      // profile. The adjustments stick across re-runs until the user
      // changes them again or rebuilds from a new resume.
      setProfile(profileToRun)

      const res = await runSearch({
        profile: profileToRun,
        keywords: (profileToRun.keywords || [])
          .filter((k) => (k.tier ?? 3) <= 2)
          .map((k) => k.text),
        postedWithinDays: 30,
        appliedRoles,
        cacheMaxAgeDays,
        // Force a real fresh search every time the user clicks Re-Run.
        // Without this the backend hits the result cache (default 7-day
        // window) and replays the same scored roles from a previous run
        // — so a user who re-runs an hour later sees the same job
        // listings instead of the new ones posted since. Cache is a dev
        // optimization; for end-users, "Re-Run" must mean fresh data.
        forceRefresh: true,
      })
      setActiveRunId(res.run_id)
      const skipMsg = appliedRoles.length > 0
        ? ` · skipping ${appliedRoles.length} already-applied role${appliedRoles.length === 1 ? '' : 's'}`
        : ''
      toast.success(`Search started · this will take 10–20 minutes${skipMsg}`)
      navigate('/running')
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'Unknown error')
      toast.error(`Failed to start search: ${msg}`)
      setSubmitting(false)
    }
  }

  const selectedProfile = cards.find((c) => c.run_id === selectedRunId)?.profile
  const selectedResumeName = selectedProfile?.resumes?.[0]?.filename

  return (
    <div className="min-h-screen w-full flex items-start justify-center px-6 py-7">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="glass-strong w-full max-w-2xl rounded-2xl p-8"
      >
        <div className="flex items-center gap-3 mb-6">
          <ZMark size={36} className="shadow-lg shadow-accent-900/40" />
          <h1 className="text-xl font-semibold tracking-tight">How would you like to search?</h1>
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-12 text-base-400 text-sm">
            <Loader2 size={16} className="animate-spin mr-2" />
            Loading your saved profiles…
          </div>
        ) : (
          <>
            <p className="text-[12px] text-base-400 mb-3 uppercase tracking-wider font-medium">
              {cards.length > 1
                ? 'Pick a saved profile to re-run'
                : 'Your saved profile'}
            </p>

            <div className="space-y-2 mb-3">
              {cards.map((card) => (
                <ProfileCard
                  key={card.run_id}
                  profile={card}
                  selected={selectedRunId === card.run_id}
                  onSelect={() => setSelectedRunId(card.run_id)}
                  disabled={submitting}
                />
              ))}
            </div>

            {/* Inline "Adjust before running" panel — only when a card
                is selected, since it auto-populates from that card. */}
            {selectedRunId && (
              <div className="mb-5">
                <button
                  type="button"
                  onClick={() => setShowAdjust((v) => !v)}
                  disabled={submitting}
                  className="w-full flex items-center justify-between
                             px-4 py-2.5 rounded-lg
                             bg-white/[0.03] hover:bg-white/[0.06]
                             border border-white/[0.06]
                             text-[12px] text-base-300 transition-colors"
                >
                  <span className="flex items-center gap-2">
                    <Sparkles size={12} className="text-accent-400" />
                    Adjust salary or locations before running
                  </span>
                  <ChevronDown
                    size={14}
                    className={cn(
                      'text-base-400 transition-transform',
                      showAdjust && 'rotate-180'
                    )}
                  />
                </button>

                <AnimatePresence initial={false}>
                  {showAdjust && (
                    <motion.div
                      initial={{ opacity: 0, height: 0, marginTop: 0 }}
                      animate={{ opacity: 1, height: 'auto', marginTop: 8 }}
                      exit={{ opacity: 0, height: 0, marginTop: 0 }}
                      transition={{ duration: 0.2 }}
                      className="overflow-hidden"
                    >
                      <div className="rounded-lg bg-white/[0.02] border border-white/[0.06] p-4 space-y-4">
                        {/* Salary */}
                        <div>
                          <label className="flex items-center gap-1.5 text-[11px] uppercase
                                            tracking-wider text-base-400 font-medium mb-2">
                            <DollarSign size={11} /> Salary minimum
                          </label>
                          <div className="flex items-center gap-2">
                            <span className="text-base-400 text-sm">$</span>
                            <input
                              type="number"
                              value={adjustedSalary ?? ''}
                              min={0}
                              max={500_000}
                              step={5_000}
                              onChange={(e) => {
                                const v = e.target.value
                                setAdjustedSalary(v === '' ? null : parseInt(v, 10))
                              }}
                              disabled={submitting}
                              className="flex-1 bg-white/[0.03] border border-white/[0.10]
                                         rounded px-3 py-1.5 text-sm text-base-100
                                         focus:outline-none focus:border-accent-500/50
                                         tabular-nums"
                            />
                            <span className="text-[11px] text-base-500">
                              {adjustedSalary != null
                                ? `(${adjustedSalary.toLocaleString()})`
                                : ''}
                            </span>
                          </div>
                        </div>

                        {/* Locations */}
                        <div>
                          <label className="flex items-center gap-1.5 text-[11px] uppercase
                                            tracking-wider text-base-400 font-medium mb-2">
                            <MapPin size={11} /> Acceptable locations
                          </label>
                          <div className="flex flex-wrap gap-1.5 mb-2">
                            {adjustedLocations.length === 0 && (
                              <span className="text-[11px] text-base-500 italic">
                                None — add at least one or roles won't pass the location filter
                              </span>
                            )}
                            {adjustedLocations.map((loc) => (
                              <span
                                key={loc}
                                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md
                                           bg-accent-500/10 border border-accent-500/30
                                           text-[11px] text-accent-100"
                              >
                                {loc}
                                <button
                                  type="button"
                                  onClick={() => handleRemoveLocation(loc)}
                                  disabled={submitting}
                                  className="hover:text-red-300 transition-colors"
                                  aria-label={`Remove ${loc}`}
                                >
                                  <X size={10} />
                                </button>
                              </span>
                            ))}
                          </div>
                          <LocationAutocomplete
                            value={newLocationDraft}
                            onChange={setNewLocationDraft}
                            onAdd={(v) => {
                              const trimmed = v.trim()
                              if (!trimmed) return
                              if (adjustedLocations.some((l) => l.toLowerCase() === trimmed.toLowerCase())) {
                                setNewLocationDraft('')
                                return
                              }
                              setAdjustedLocations((prev) => [...prev, trimmed])
                              setNewLocationDraft('')
                            }}
                            existingLocations={adjustedLocations.map((l) => l.toLowerCase())}
                            placeholder="e.g. Richmond VA, Remote, Hampton VA"
                            disabled={submitting}
                          />
                          <p className="text-[10px] text-base-500 mt-1.5 leading-snug">
                            Type a city + state (or "Remote") and press Enter or Add. New cities
                            use a 50mi default radius.
                            <br />
                            Changes save when you click Re-Run.
                          </p>
                        </div>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            )}

            {/* Two action buttons. Primary disabled until a card is selected. */}
            <div className="flex flex-col sm:flex-row gap-3">
              <div className="flex-1">
                <button
                  onClick={handleReRun}
                  disabled={!selectedRunId || submitting}
                  className={cn(
                    'relative overflow-visible w-full flex items-center justify-center gap-2',
                    'px-5 py-3 rounded-lg',
                    'bg-white text-base-950 font-medium text-sm',
                    'transition-colors shadow-lg shadow-black/30',
                    'enabled:hover:bg-base-200',
                    'disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none'
                  )}
                >
                  {submitting ? (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      Starting search…
                    </>
                  ) : (
                    <>
                      Re-Run Search <ArrowRight size={14} />
                    </>
                  )}
                  <SparkleBurst trigger={burstTrigger} />
                </button>
                {/* Sub-text under the primary button. Confirms which
                    profile will run; nudges the user to select if not. */}
                <p className={cn(
                  'text-[10px] mt-1.5 text-center leading-snug',
                  selectedRunId ? 'text-base-400' : 'text-base-500'
                )}>
                  {selectedRunId
                    ? `With ${selectedResumeName || 'selected profile'}`
                    : 'Select a saved profile to continue'}
                </p>
              </div>
              <button
                onClick={() => navigate('/setup')}
                disabled={submitting}
                className="flex-1 self-start flex items-center justify-center gap-2
                           px-5 py-3 rounded-lg
                           bg-white/[0.06] border border-white/[0.10] text-base-100 text-sm
                           hover:bg-white/[0.10] transition-colors
                           disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Upload size={14} className="text-accent-300" />
                Start fresh with new resume
              </button>
            </div>
          </>
        )}

        <div className="flex items-center justify-start mt-6 pt-4 border-t border-white/[0.06]">
          <button
            onClick={() => navigate('/how-it-works')}
            className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
          >
            <ArrowLeft size={14} /> Back
          </button>
        </div>
      </motion.div>
    </div>
  )
}


/** Single selectable profile card. Radio-style — clicking activates it. */
function ProfileCard({
  profile: card,
  selected,
  onSelect,
  disabled,
}: {
  profile: RecentProfile
  selected: boolean
  onSelect: () => void
  disabled: boolean
}) {
  const p = card.profile
  const resumeName = p.resumes?.[0]?.filename || 'your resume'
  const totalKw = (p.keywords || []).length
  const searchTermCount = (p.search_terms || []).length

  // Format the last-used date defensively against tz-naive ISO strings.
  const lastUsedLabel = card.last_used_at
    ? new Date(
        /[+-]\d\d:?\d\d$|Z$/.test(card.last_used_at)
          ? card.last_used_at
          : card.last_used_at + 'Z'
      ).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : null

  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      className={cn(
        'w-full text-left rounded-xl px-5 py-4 transition-all',
        'border flex items-start gap-3',
        selected
          ? 'border-accent-500/50 bg-accent-500/[0.10] shadow-sm shadow-accent-500/20'
          : 'border-white/[0.08] bg-white/[0.02] hover:bg-white/[0.05] hover:border-white/[0.14]',
        disabled && 'opacity-50 cursor-not-allowed'
      )}
    >
      {/* Selection indicator — empty circle when not selected, filled with
          a check when selected. Radio-button affordance. */}
      <div className={cn(
        'flex-shrink-0 mt-0.5 w-5 h-5 rounded-full border flex items-center justify-center',
        selected
          ? 'bg-accent-500 border-accent-400 text-base-950'
          : 'border-white/20 bg-white/[0.02]'
      )}>
        {selected && <Check size={12} strokeWidth={3} />}
      </div>

      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-base-50 truncate">
          {resumeName}
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1
                        text-[11px] text-base-400">
          {searchTermCount > 0 && (
            <span className="flex items-center gap-1">
              <Sparkles size={10} /> {searchTermCount} search terms
            </span>
          )}
          <span className="flex items-center gap-1">
            <FileText size={10} /> {totalKw} keywords
          </span>
          {lastUsedLabel && (
            <span className="flex items-center gap-1">
              <Clock size={10} /> last used {lastUsedLabel}
            </span>
          )}
        </div>
      </div>
    </button>
  )
}

import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Play, Globe, FileText, Clock, Sparkles, Loader2, AlertCircle, Edit3, Home, Upload, ChevronDown, X, MapPin, DollarSign } from 'lucide-react'
import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { useAppStore } from '@/stores/appStore'
import { runSearch, ApiError } from '@/services/api'
import { SparkleBurst } from '@/components/SparkleBurst'
import { LocationAutocomplete } from '@/components/LocationAutocomplete'
import { cn } from '@/lib/utils'

export default function Run() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const setProfile = useAppStore((s) => s.setProfile)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)
  const [submitting, setSubmitting] = useState(false)
  const [burstTrigger, setBurstTrigger] = useState(0)

  // Inline "Adjust salary or locations" expansion. Mirror of the panel
  // on /start-search so the user can tweak from either entry point.
  // Auto-populates from the active profile and writes back on launch.
  const [showAdjust, setShowAdjust] = useState(false)
  const [adjustedSalary, setAdjustedSalary] = useState<number | null>(null)
  const [adjustedLocations, setAdjustedLocations] = useState<string[]>([])
  const [newLocationDraft, setNewLocationDraft] = useState('')

  // Sync the adjust panel to the active profile whenever it changes
  // (e.g. user came back from /setup with a fresh build, or hydrated
  // from /start-search). Keeps the panel honest about what's loaded.
  useEffect(() => {
    if (!profile) return
    setAdjustedSalary(profile.salary_minimum ?? 100_000)
    setAdjustedLocations([...(profile.acceptable_locations || [])])
  }, [profile])

  // Orphaned handleAddLocation removed in v0.2.0 — replaced by inline
  // onAdd in <LocationAutocomplete>. The shared component handles the
  // dedup + draft reset internally.

  const handleRemoveLocation = (loc: string) => {
    setAdjustedLocations((prev) => prev.filter((l) => l !== loc))
  }

  // No profile yet — guide user back to setup
  if (!profile) {
    return (
      <div className="px-10 py-8 max-w-3xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">New Search</h1>
        <div className="glass mt-8 rounded-xl p-8 text-center">
          <AlertCircle size={28} className="text-base-400 mx-auto mb-3" />
          <h2 className="text-base font-medium mb-2">No profile yet</h2>
          <p className="text-sm text-base-400 mb-5">
            Upload your resume first so we know what to search for.
          </p>
          <button
            onClick={() => navigate('/welcome')}
            className="px-5 py-2 rounded-lg bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors"
          >
            Set up your profile
          </button>
        </div>
      </div>
    )
  }

  const totalKw = profile.keywords.length
  // v0.2.0: simplified to just the total. Tier breakdown
  // (T1/T2/T3 split) was internal scoring detail not meaningful for
  // testers — they just want to know how many keywords/phrases the
  // search will use.
  const keywordsLabel = `${totalKw} ${totalKw === 1 ? 'keyword' : 'keywords'}`
  // For the "saved profile" indicator. We don't store a build timestamp
  // on the profile itself, but the most-recent run_date is a close proxy
  // for "when this profile was last used." Falls back to "saved" with no
  // date if the user hasn't run a search yet with this profile.
  // Subscribe via useAppStore so this updates if the run completes while
  // we're on this page; .getState() would give a one-shot snapshot only.
  const lastSummary = useAppStore((s) => s.lastSummary)
  const lastUsedIso = lastSummary?.run_date
  const lastUsedLabel = lastUsedIso
    ? new Date(
        /[+-]\d\d:?\d\d$|Z$/.test(lastUsedIso) ? lastUsedIso : lastUsedIso + 'Z'
      ).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : null
  const resumeName = profile.resumes[0]?.filename || 'your resume'

  // Build the applied-roles list from local-state — anything the user has
  // marked applied (or in a downstream stage like phone_screen, interview,
  // offer, rejected) is sent so the backend won't resurface it.
  const appliedRoles = Object.values(roleStatuses)
    .filter((entry) => entry.status === 'applied')
    .map((entry) => ({
      company: entry.roleSnapshot?.company || '',
      title: entry.roleSnapshot?.job_title || '',
    }))
    .filter((r) => r.company && r.title)

  const startSearch = async () => {
    // Fire the sparkle burst before the async work so the animation
    // overlaps with the network round-trip (feels snappier).
    setBurstTrigger((t) => t + 1)
    setSubmitting(true)
    try {
      // Overlay any inline adjustments onto the active profile. If the
      // user didn't expand the adjust panel, these values match the
      // saved profile and this is a no-op. If they edited, the new
      // values flow into the run AND get persisted back to localStorage
      // so they stick across future sessions.
      const profileToRun = {
        ...profile,
        salary_minimum: adjustedSalary ?? profile.salary_minimum ?? null,
        acceptable_locations: adjustedLocations,
      }
      setProfile(profileToRun)

      const res = await runSearch({
        profile: profileToRun,
        // Default: tier 1+2 keywords
        keywords: profileToRun.keywords.filter((k) => k.tier <= 2).map((k) => k.text),
        // Omit `sources` so backend uses ALL active scrapers (16 total
        // as of v0.1.4 — JSearch was added on top of the prior 15).
        // See Keywords.tsx for full reasoning.
        postedWithinDays: 30,
        appliedRoles,
        cacheMaxAgeDays,
        // Force fresh — every user-initiated search from this page
        // should hit live job boards, not return cached results from
        // a recent run with the same profile_hash. See StartSearch.tsx
        // for the same reasoning.
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

  // True when the user has at least one prior completed run on record;
  // we frame this page as "Re-Run" rather than "New Search" in that case
  // so it's obvious clicking the launch button reuses the saved profile.
  const isReRun = !!lastSummary
  const pageTitle = isReRun ? 'Re-Run Search' : 'New Search'
  const pageSubtitle = isReRun
    ? 'Re-running with your saved profile — same resume, same keywords. Use "Use new resume" to rebuild instead.'
    : 'Review your configuration and launch when ready.'

  return (
    <div className="px-10 py-8 max-w-3xl mx-auto">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{pageTitle}</h1>
          <p className="text-sm text-base-400 mt-1">{pageSubtitle}</p>
        </div>
        <button
          onClick={() => navigate('/welcome')}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs
                     bg-white/[0.04] hover:bg-white/[0.08] text-base-300
                     border border-white/[0.06]"
        >
          <Home size={12} /> Home
        </button>
      </div>

      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="glass mt-8 rounded-xl p-7"
      >
        {/* Header row — title on left, action buttons on right.
            "Reusing saved profile" sub-line makes the silent reuse
            explicit + offers a one-click rebuild path via "Use new
            resume". The Stats below give the full breakdown. */}
        <div className="flex items-start justify-between gap-3 mb-5">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-medium text-base-200">
              <Sparkles size={14} className="text-accent-400" /> Search summary
            </h2>
            <p className="text-[11px] text-base-400 mt-1 truncate">
              Re-running with your saved profile from {resumeName}
              {lastUsedLabel ? ` · last used ${lastUsedLabel}` : ''}
            </p>
          </div>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <button
              onClick={() => navigate('/setup')}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px]
                         bg-white/[0.04] hover:bg-white/[0.08] text-base-300
                         border border-white/[0.06] transition-colors"
              title="Re-upload your resume to rebuild profile + regenerate keywords"
            >
              <Upload size={11} /> Use new resume
            </button>
            <button
              onClick={() => navigate('/keywords')}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px]
                         bg-white/[0.04] hover:bg-white/[0.08] text-base-300
                         border border-white/[0.06] transition-colors"
              title="Review or edit generated keywords"
            >
              <Edit3 size={11} /> Edit keywords
            </button>
          </div>
        </div>

        <div className="space-y-4">
          <Stat
            icon={FileText}
            label="Resumes uploaded"
            value={`${profile.resumes.length} file${profile.resumes.length === 1 ? '' : 's'}`}
          />
          <Stat
            icon={Sparkles}
            label="Keywords & Phrases Searched"
            value={keywordsLabel}
          />
          <Stat
            icon={Globe}
            label="Job boards"
            value="16 sources searched in parallel"
          />
          <Stat
            icon={Clock}
            label="Estimated runtime"
            value="10–20 minutes"
          />
        </div>

        {/* Inline "Adjust salary or locations" panel — same UI as the
            choice screen on /start-search so the user has a single
            mental model regardless of which entry point they used. */}
        <div className="mt-5">
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

        <div className="mt-5 p-4 rounded-lg bg-tier-strong/10 border border-tier-strong/20">
          <div className="flex items-center gap-2 text-tier-strong text-sm">
            <Sparkles size={14} />
            <span className="font-medium">Configuration looks healthy</span>
          </div>
          <p className="text-xs text-base-300 mt-1">
            We'll scrape ~1,500 roles, hard-filter for fit, then score the survivors with AI.
            You'll see live progress as the search runs.
          </p>
        </div>

        <div className="flex justify-end mt-7 gap-3">
          <button
            onClick={() => navigate('/dashboard')}
            disabled={submitting}
            className="px-4 py-2 text-sm text-base-400 hover:text-base-200 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={startSearch}
            disabled={submitting}
            className="relative overflow-visible flex items-center gap-2 px-5 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       disabled:opacity-60 disabled:cursor-not-allowed
                       shadow-lg shadow-black/30"
          >
            {submitting ? (
              <>
                <Loader2 size={14} className="animate-spin" /> Starting...
              </>
            ) : (
              <>
                <Play size={14} />
                {isReRun ? 'Re-Run Search' : 'Start Search'}
              </>
            )}
            <SparkleBurst trigger={burstTrigger} />
          </button>
        </div>
      </motion.div>
    </div>
  )
}

function Stat({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ElementType
  label: string
  value: string
}) {
  return (
    <div className="flex items-center gap-3">
      <div className="w-8 h-8 rounded-lg bg-white/[0.04] border border-white/[0.06]
                      flex items-center justify-center text-base-400">
        <Icon size={14} />
      </div>
      <div className="flex-1">
        <div className="text-xs text-base-400">{label}</div>
        <div className="text-sm text-base-100">{value}</div>
      </div>
    </div>
  )
}

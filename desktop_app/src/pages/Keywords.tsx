import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ArrowRight, ArrowLeft, X, Plus, AlertCircle,
  Star, Layers, Compass, Loader2, Play,
} from 'lucide-react'
import { toast } from 'sonner'
import { ZMark } from '@/components/ZMark'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/stores/appStore'
import { runSearch, ApiError } from '@/services/api'
import type { Keyword } from '@/types'

export default function Keywords() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const setProfile = useAppStore((s) => s.setProfile)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)

  // Local working copy of keywords (so cancellation doesn't mutate store)
  const [keywords, setKeywords] = useState<Keyword[]>(profile?.keywords || [])
  const [newKeyword, setNewKeyword] = useState('')
  const [newKeywordTier, setNewKeywordTier] = useState(1)
  const [submitting, setSubmitting] = useState(false)

  if (!profile) {
    return (
      <div className="min-h-screen w-full flex items-center justify-center px-6">
        <div className="glass-strong rounded-2xl p-10 max-w-md text-center">
          <AlertCircle size={28} className="mx-auto text-base-400 mb-3" />
          <h1 className="text-lg font-medium mb-1.5">No profile yet</h1>
          <p className="text-sm text-base-400 mb-5">
            Upload your resume first so we can generate keywords.
          </p>
          <button
            onClick={() => navigate('/setup')}
            className="px-5 py-2 rounded-lg bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors"
          >
            Go to setup
          </button>
        </div>
      </div>
    )
  }

  const removeKeyword = (text: string) => {
    setKeywords(keywords.filter((k) => k.text !== text))
  }

  const addKeyword = () => {
    const text = newKeyword.trim()
    if (!text) return
    if (keywords.some((k) => k.text.toLowerCase() === text.toLowerCase())) return
    setKeywords([...keywords, { text, tier: newKeywordTier }])
    setNewKeyword('')
  }

  const moveKeywordTier = (text: string, newTier: number) => {
    setKeywords(keywords.map((k) => (k.text === text ? { ...k, tier: newTier } : k)))
  }

  // Kicks off the actual search directly from this page. Skips the /run
  // summary screen — the user already reviewed/edited their keywords here,
  // so the extra click on /run was redundant. Mirrors the same body that
  // /run sends to /search/run (keywords tier 1+2, default sources, applied-role
  // skip list). The /run page still exists as an entry point for "+ New
  // Search" from the dashboard, where the user lands without having just
  // edited keywords and a recap is more useful.
  const startSearch = async () => {
    if (!profile) return

    // Persist edited keywords to the store BEFORE firing the search so
    // the run uses what the user sees on screen, not the stale profile.
    const updatedProfile = { ...profile, keywords }
    setProfile(updatedProfile)

    // Build the applied-roles skip list from local state — anything the
    // user marked applied (or further along) is sent so the backend
    // doesn't resurface it on this run.
    const appliedRoles = Object.values(roleStatuses)
      .filter((entry) => entry.status === 'applied')
      .map((entry) => ({
        company: entry.roleSnapshot?.company || '',
        title: entry.roleSnapshot?.job_title || '',
      }))
      .filter((r) => r.company && r.title)

    setSubmitting(true)
    try {
      const res = await runSearch({
        profile: updatedProfile,
        keywords: keywords.filter((k) => k.tier <= 2).map((k) => k.text),
        // Omit `sources` so the backend uses ALL active scrapers in the
        // registry (Greenhouse, Lever, Ashby, Workday, iCIMS, BuiltIn,
        // TheMuse, Remotive, USAJOBS, Adzuna, Climatebase, SmartRecruiters,
        // Arbeitnow, HN-WhoIsHiring). Hardcoding 4 here meant testers got
        // a Greenhouse-heavy view with all the other coverage missing.
        // Sources requiring API keys (USAJOBS, Adzuna, Findwork) gracefully
        // no-op when the key isn't set, so this is safe even for testers
        // who haven't configured their own keys.
        postedWithinDays: 30,
        appliedRoles,
        cacheMaxAgeDays,
      })
      setActiveRunId(res.run_id)
      const skipMsg = appliedRoles.length > 0
        ? ` · skipping ${appliedRoles.length} already-applied role${appliedRoles.length === 1 ? '' : 's'}`
        : ''
      toast.success(`Search started · this will take 5–15 minutes${skipMsg}`)
      navigate('/running')
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'Unknown error')
      toast.error(`Failed to start search: ${msg}`)
      setSubmitting(false)
    }
  }

  const tier1 = keywords.filter((k) => k.tier === 1)
  const tier2 = keywords.filter((k) => k.tier === 2)
  const tier3 = keywords.filter((k) => k.tier === 3)

  return (
    <div className="min-h-screen w-full flex items-start justify-center px-6 py-7">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="glass-strong w-full max-w-4xl rounded-2xl p-8"
      >
        {/* Header */}
        <div className="flex items-center gap-3 mb-1">
          <ZMark size={40} className="shadow-lg shadow-accent-900/40" />
          <h1 className="text-2xl font-semibold tracking-tight">Review your keywords</h1>
        </div>
        <p className="text-sm text-base-400 mb-2">
          We generated <span className="text-base-100 font-medium">{keywords.length}</span> personalized keywords from your resume + extra context.
        </p>
        <p className="text-xs text-base-500 mb-6 leading-relaxed">
          Remove any that don't fit, add ones we missed, or change a keyword's tier. Tier 1 always runs;
          Tier 2 runs if quota allows; Tier 3 is broader exploration.
        </p>

        {/* Add new keyword */}
        <div className="mb-6 p-4 rounded-xl bg-white/[0.03] border border-white/[0.06]">
          <h3 className="text-[10px] uppercase tracking-wider text-base-400 font-medium mb-2.5">
            Add a keyword
          </h3>
          <div className="flex gap-2">
            <input
              type="text"
              value={newKeyword}
              onChange={(e) => setNewKeyword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  addKeyword()
                }
              }}
              placeholder="e.g. AI program manager, Responsible AI lead..."
              className="flex-1 px-3 py-2 rounded-lg
                         bg-white/[0.04] border border-white/[0.08]
                         text-sm placeholder:text-base-500
                         focus:outline-none focus:border-accent-500/50"
            />
            <select
              value={newKeywordTier}
              onChange={(e) => setNewKeywordTier(Number(e.target.value))}
              className="px-3 py-2 rounded-lg bg-white/[0.04] border border-white/[0.08]
                         text-sm focus:outline-none focus:border-accent-500/50"
            >
              <option value={1}>Tier 1</option>
              <option value={2}>Tier 2</option>
              <option value={3}>Tier 3</option>
            </select>
            <button
              onClick={addKeyword}
              disabled={!newKeyword.trim()}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg
                         bg-white/[0.06] hover:bg-white/[0.10]
                         text-sm text-base-200
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Plus size={14} /> Add
            </button>
          </div>
        </div>

        {/* Tier sections */}
        <div className="space-y-5">
          <TierSection
            tier={1}
            icon={Star}
            label="Tier 1 — Always run"
            description="Exact-fit roles you're targeting. These run on every search."
            keywords={tier1}
            color="text-tier-strong"
            onRemove={removeKeyword}
            onMove={moveKeywordTier}
          />
          <TierSection
            tier={2}
            icon={Layers}
            label="Tier 2 — Run if quota allows"
            description="Adjacent roles worth exploring."
            keywords={tier2}
            color="text-tier-good"
            onRemove={removeKeyword}
            onMove={moveKeywordTier}
          />
          <TierSection
            tier={3}
            icon={Compass}
            label="Tier 3 — Broader exploration"
            description="Optional. Tangentially related roles."
            keywords={tier3}
            color="text-tier-maybe"
            onRemove={removeKeyword}
            onMove={moveKeywordTier}
          />
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between mt-7 pt-5 border-t border-white/[0.06]">
          <button
            onClick={() => navigate('/setup')}
            className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
          >
            <ArrowLeft size={14} /> Back to setup
          </button>
          <div className="text-xs text-base-500">
            {keywords.length} keyword{keywords.length === 1 ? '' : 's'} ready
          </div>
          <button
            onClick={startSearch}
            disabled={submitting || keywords.length === 0}
            className="flex items-center gap-2 px-6 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       disabled:opacity-60 disabled:cursor-not-allowed
                       shadow-lg shadow-black/30"
          >
            {submitting ? (
              <>
                <Loader2 size={14} className="animate-spin" /> Starting search...
              </>
            ) : (
              <>
                <Play size={13} /> Start search <ArrowRight size={14} />
              </>
            )}
          </button>
        </div>
      </motion.div>
    </div>
  )
}

function TierSection({
  tier,
  icon: Icon,
  label,
  description,
  keywords,
  color,
  onRemove,
  onMove,
}: {
  tier: number
  icon: React.ElementType
  label: string
  description: string
  keywords: Keyword[]
  color: string
  onRemove: (text: string) => void
  onMove: (text: string, newTier: number) => void
}) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Icon size={13} className={color} />
        <h3 className="text-xs uppercase tracking-wider text-base-200 font-medium">{label}</h3>
        <span className="text-xs text-base-500">({keywords.length})</span>
      </div>
      <p className="text-xs text-base-500 mb-3">{description}</p>

      {keywords.length === 0 ? (
        <div className="px-4 py-3 rounded-lg border border-dashed border-white/[0.06] text-xs text-base-500">
          No keywords in this tier.
        </div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          <AnimatePresence>
            {keywords.map((kw) => (
              <motion.div
                key={kw.text}
                layout
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.9 }}
                transition={{ duration: 0.15 }}
                className="group flex items-center gap-1.5 pl-3 pr-1.5 py-1.5
                           rounded-full glass-subtle hover:bg-white/[0.06]
                           transition-colors"
              >
                <span className="text-xs text-base-100">{kw.text}</span>

                {/* Move-tier dropdown — visible on hover */}
                <div className="relative opacity-0 group-hover:opacity-100 transition-opacity">
                  <select
                    value={tier}
                    onChange={(e) => onMove(kw.text, Number(e.target.value))}
                    className="text-[10px] bg-transparent border-0 cursor-pointer
                               text-base-400 hover:text-base-100 outline-none"
                    title="Move to tier"
                  >
                    <option value={1}>T1</option>
                    <option value={2}>T2</option>
                    <option value={3}>T3</option>
                  </select>
                </div>

                <button
                  onClick={() => onRemove(kw.text)}
                  className={cn(
                    'p-0.5 rounded-full text-base-500 hover:text-base-100 hover:bg-white/[0.10]'
                  )}
                  title="Remove"
                >
                  <X size={11} />
                </button>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}
    </div>
  )
}

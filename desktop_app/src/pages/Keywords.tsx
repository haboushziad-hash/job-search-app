import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ArrowRight, ArrowLeft, X, Plus, AlertCircle,
  Star, Layers, Compass, Loader2, Play, Search, Target,
} from 'lucide-react'
import { toast } from 'sonner'
import { ZMark } from '@/components/ZMark'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/stores/appStore'
import { runSearch, getBudget, ApiError } from '@/services/api'
import type { Keyword } from '@/types'

export default function Keywords() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const setProfile = useAppStore((s) => s.setProfile)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)

  // Local working copies (cancel doesn't mutate the store)
  const [keywords, setKeywords] = useState<Keyword[]>(profile?.keywords || [])
  const [searchTerms, setSearchTerms] = useState<string[]>(
    profile?.search_terms || []
  )
  const [newKeyword, setNewKeyword] = useState('')
  const [newKeywordTier, setNewKeywordTier] = useState(1)
  const [newSearchTerm, setNewSearchTerm] = useState('')
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

  // Search-term mutators (broad upstream-API queries; v0.1.4 split)
  const addSearchTerm = () => {
    const text = newSearchTerm.trim()
    if (!text) return
    if (searchTerms.some((s) => s.toLowerCase() === text.toLowerCase())) return
    setSearchTerms([...searchTerms, text])
    setNewSearchTerm('')
  }
  const removeSearchTerm = (text: string) => {
    setSearchTerms(searchTerms.filter((s) => s !== text))
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

    // Persist edited keywords + search_terms to the store BEFORE firing
    // the search so the run uses what the user sees on screen.
    const updatedProfile = { ...profile, keywords, search_terms: searchTerms }
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
    // v0.1.4 Phase 15b: pre-flight budget check. Warn the user if today's
    // Worker cap doesn't have enough headroom for a full search (~1000
    // LLM calls). In dev mode the budget endpoint returns can_run=true
    // with sentinel values so this is a no-op.
    try {
      const budget = await getBudget()
      if (!budget.can_run && budget.remaining >= 0) {
        const proceed = confirm(
          `Today's API budget is low.\n\n` +
          `Used: ${budget.used} / ${budget.cap} calls\n` +
          `Remaining: ${budget.remaining} (a full search needs ~${budget.typical_run_calls})\n\n` +
          `The search may complete partially or fail mid-run if the cap is hit.\n` +
          `Try again tomorrow when the daily counter resets, or proceed anyway?`
        )
        if (!proceed) {
          setSubmitting(false)
          return
        }
      }
    } catch (e) {
      // Budget check failed — don't block the search. Soft-fail.
      console.warn('[budget] pre-flight check failed:', e)
    }

    try {
      const res = await runSearch({
        profile: updatedProfile,
        // Prefer search_terms (broad phrases) over keywords (specific titles).
        // Backend will fall back to keywords if search_terms is empty.
        keywords: searchTerms.length > 0
          ? searchTerms
          : keywords.filter((k) => k.tier <= 2).map((k) => k.text),
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
        // v0.2.0: every user-initiated search forces fresh — no cached
        // results from a peer's recent run with the same profile_hash.
        // Mirrors the policy in Run.tsx and StartSearch.tsx (Re-Run paths)
        // so testers have one consistent mental model: every search you
        // start hits live job boards. Cache infrastructure is preserved
        // for v0.3 smart-cache (e.g. retain scraped roles when only
        // post-scrape filters like location/radius/salary change), but
        // for v0.2.0 ship, simplicity > peer-inheritance optimization.
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
          <h1 className="text-2xl font-semibold tracking-tight">Review your search</h1>
        </div>
        <p className="text-sm text-base-400 mb-2">
          We generated <span className="text-base-100 font-medium">{searchTerms.length}</span> search
          queries and <span className="text-base-100 font-medium">{keywords.length}</span> target
          titles from your resume + extra context.
        </p>
        <p className="text-xs text-base-500 mb-6 leading-relaxed">
          <span className="text-base-300 font-medium">Search queries</span> are the broad phrases we
          send to job boards to find roles.{' '}
          <span className="text-base-300 font-medium">Target titles</span> are specific role names
          we score each result against. Edit either list if something looks off — the next search
          uses what you see here.
        </p>

        {/* SEARCH QUERIES — broad upstream-API phrases (v0.1.4) */}
        <div className="mb-6 p-4 rounded-xl bg-accent-500/[0.04] border border-accent-500/20">
          <div className="flex items-center gap-2 mb-1">
            <Search size={13} className="text-accent-300" />
            <h3 className="text-xs uppercase tracking-wider text-accent-200 font-medium">
              Search queries — what we search for
            </h3>
            <span className="text-xs text-base-500">({searchTerms.length})</span>
          </div>
          <p className="text-xs text-base-500 mb-3 leading-relaxed">
            Broad 2-3 word phrases sent to each job board. These cast the widest net.
            Examples: "AI strategy", "tax compliance", "leadership development program".
          </p>

          {/* Add new search term */}
          <div className="flex gap-2 mb-3">
            <input
              type="text"
              value={newSearchTerm}
              onChange={(e) => setNewSearchTerm(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  addSearchTerm()
                }
              }}
              placeholder="e.g. AI strategy, sales operations, tax compliance"
              className="flex-1 px-3 py-2 rounded-lg bg-white/[0.04] border border-white/[0.08]
                         text-sm placeholder:text-base-500 focus:outline-none focus:border-accent-500/50"
            />
            <button
              onClick={addSearchTerm}
              disabled={!newSearchTerm.trim()}
              className="flex items-center gap-1.5 px-4 py-2 rounded-lg
                         bg-white/[0.06] hover:bg-white/[0.10] text-sm text-base-200
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <Plus size={14} /> Add
            </button>
          </div>

          {/* Existing search terms */}
          {searchTerms.length === 0 ? (
            <div className="px-4 py-3 rounded-lg border border-dashed border-white/[0.06] text-xs text-base-500">
              No search queries yet — add 4-12 broad phrases above, or rebuild your profile.
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              <AnimatePresence>
                {searchTerms.map((st) => (
                  <motion.div
                    key={st}
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.9 }}
                    className="group flex items-center gap-1.5 px-3 py-1.5 rounded-full
                               bg-accent-500/[0.10] border border-accent-500/30
                               text-xs text-accent-100"
                  >
                    <span>{st}</span>
                    <button
                      onClick={() => removeSearchTerm(st)}
                      className="opacity-50 hover:opacity-100 transition-opacity"
                      aria-label={`Remove ${st}`}
                    >
                      <X size={12} />
                    </button>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>

        {/* TARGET TITLES — specific job titles for scoring rubric */}
        <div className="mb-3 flex items-center gap-2">
          <Target size={14} className="text-base-300" />
          <h3 className="text-xs uppercase tracking-wider text-base-200 font-medium">
            Target titles — what we score against
          </h3>
        </div>
        <p className="text-xs text-base-500 mb-4 leading-relaxed">
          Specific role titles. The AI scoring cascade compares each scraped role against
          these to assign STRONG / GOOD / MAYBE / STRETCH. Tier 1 always runs;
          Tier 2 if quota allows; Tier 3 broader.
        </p>

        {/* Add new keyword */}
        <div className="mb-6 p-4 rounded-xl bg-white/[0.03] border border-white/[0.06]">
          <h3 className="text-[10px] uppercase tracking-wider text-base-400 font-medium mb-2.5">
            Add a target title
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
              placeholder="e.g. Senior AI Strategist, AI Program Manager..."
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
            {searchTerms.length} {searchTerms.length === 1 ? 'query' : 'queries'} ·{' '}
            {keywords.length} target {keywords.length === 1 ? 'title' : 'titles'}
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

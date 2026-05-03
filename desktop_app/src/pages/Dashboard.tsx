import { useEffect, useState, useRef, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Filter, Play, Sparkles, Search, Download, FileSpreadsheet, FileText, FileType, ChevronDown, AlertTriangle, Home } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { TierCard } from '@/components/TierCard'
import { RoleCard } from '@/components/RoleCard'
import { Confetti } from '@/components/Confetti'
import { useAppStore } from '@/stores/appStore'
import { exportToExcel, exportToCSV, exportToMarkdown } from '@/services/export'
import { submitFeedback } from '@/services/api'
import type { Tier, Role } from '@/types'

export default function Dashboard() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const lastRoles = useAppStore((s) => s.lastRoles)
  const lastSummary = useAppStore((s) => s.lastSummary)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const activeRunId = useAppStore((s) => s.activeRunId)
  const [filterTier, setFilterTier] = useState<Tier | null>(null)
  const [showConfetti, setShowConfetti] = useState(false)
  const [exportMenuOpen, setExportMenuOpen] = useState(false)
  // Feedback prompt — appears once after a fresh run, dismissed via button
  const [feedbackOpen, setFeedbackOpen] = useState<boolean>(false)
  const [feedbackText, setFeedbackText] = useState<string>('')
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false)
  // Companies the user has explicitly clicked "show all" on. Lowercased keys.
  const [expandedCompanies, setExpandedCompanies] = useState<Set<string>>(new Set())
  const exportMenuRef = useRef<HTMLDivElement>(null)

  // Per-company display cap — historically capped at 3-per-company with
  // "Show N more" expanders to prevent monopolization. With the broader
  // scraper pool now active (15 sources, 426+ employers), monopolization
  // is no longer a real concern, AND hiding STRONG/GOOD roles behind
  // expanders made the tier counts confusing (header said "9" but +11
  // hidden under expanders meant 20 total). Set to a very large number
  // so all roles render.
  const COMPANY_CAP = 9999

  // Close export menu on outside click
  useEffect(() => {
    if (!exportMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target as Node)) {
        setExportMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [exportMenuOpen])

  const handleExport = (format: 'excel' | 'csv' | 'markdown') => {
    const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 16)
    const ctx = { roles: lastRoles, summary: lastSummary, statuses: roleStatuses }
    try {
      if (format === 'excel') {
        exportToExcel(ctx, `job-search-${ts}.xlsx`)
        toast.success('Exported to Excel')
      } else if (format === 'csv') {
        exportToCSV(ctx, `job-search-${ts}.csv`)
        toast.success('Exported to CSV')
      } else {
        exportToMarkdown(ctx, `job-search-${ts}.md`)
        toast.success('Exported to Markdown', {
          description: 'Drop the .md file into Claude.ai for analysis',
        })
      }
    } catch (e) {
      toast.error(`Export failed: ${e instanceof Error ? e.message : 'unknown'}`)
    }
    setExportMenuOpen(false)
  }

  // Fire confetti once when this Dashboard mount has STRONG-tier hits and
  // we just finished a fresh run (lastSummary populated this session).
  useEffect(() => {
    const hasStrong = lastRoles.some((r) => r.final_tier === 'STRONG')
    const justFinished =
      lastSummary?.run_date &&
      Date.now() - new Date(lastSummary.run_date).getTime() < 60_000
    if (hasStrong && justFinished) {
      setShowConfetti(true)
      const t = setTimeout(() => setShowConfetti(false), 4000)
      return () => clearTimeout(t)
    }
  }, [lastRoles, lastSummary])

  // Show feedback prompt once after a fresh run completes (within 5 minutes
  // of the run finishing). Persists "asked" state in localStorage so we
  // don't re-ask after page refresh.
  useEffect(() => {
    if (!activeRunId || !lastSummary?.run_date) return
    const justFinished = Date.now() - new Date(lastSummary.run_date).getTime() < 5 * 60_000
    if (!justFinished) return
    const askedKey = `feedback-asked-${activeRunId}`
    if (localStorage.getItem(askedKey)) return
    const t = setTimeout(() => setFeedbackOpen(true), 30_000) // ask 30s after landing on dashboard
    return () => clearTimeout(t)
  }, [activeRunId, lastSummary])

  const submitFeedbackForRun = async () => {
    if (!activeRunId || !feedbackText.trim()) {
      setFeedbackOpen(false)
      return
    }
    setFeedbackSubmitting(true)
    try {
      await submitFeedback({ runId: activeRunId, feedback: feedbackText.trim() })
      localStorage.setItem(`feedback-asked-${activeRunId}`, '1')
      toast.success('Thanks — feedback saved with this run')
      setFeedbackOpen(false)
      setFeedbackText('')
    } catch (e) {
      toast.error(`Failed to save: ${e instanceof Error ? e.message : 'unknown'}`)
    } finally {
      setFeedbackSubmitting(false)
    }
  }

  const dismissFeedback = () => {
    if (activeRunId) {
      localStorage.setItem(`feedback-asked-${activeRunId}`, '1')
    }
    setFeedbackOpen(false)
  }

  // No results yet — show empty state
  if (lastRoles.length === 0) {
    return (
      <div className="px-10 py-8 max-w-3xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-base-400 mt-1">Your job search results will appear here.</p>

        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="glass mt-10 rounded-xl p-12 text-center"
        >
          <div className="w-12 h-12 mx-auto mb-4 rounded-xl bg-white/[0.04] border border-white/[0.06]
                          flex items-center justify-center">
            <Search size={20} className="text-base-400" />
          </div>
          <h2 className="text-lg font-medium">
            {profile ? 'Ready for your first search' : 'No profile yet'}
          </h2>
          <p className="text-sm text-base-400 mt-1.5 max-w-sm mx-auto">
            {profile
              ? 'Click below to scrape thousands of roles and score them against your profile.'
              : 'Upload your resume to get personalized job matches.'}
          </p>
          <button
            onClick={() => navigate(profile ? '/run' : '/welcome')}
            className="mt-6 inline-flex items-center gap-2 px-5 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       shadow-lg shadow-black/30"
          >
            {profile ? (
              <>
                <Play size={14} /> Run new search
              </>
            ) : (
              <>
                <Sparkles size={14} /> Set up your profile
              </>
            )}
          </button>
        </motion.div>
      </div>
    )
  }

  const counts = {
    STRONG: lastRoles.filter((r) => r.final_tier === 'STRONG').length,
    GOOD: lastRoles.filter((r) => r.final_tier === 'GOOD').length,
    MAYBE: lastRoles.filter((r) => r.final_tier === 'MAYBE').length,
    STRETCH: lastRoles.filter((r) => r.final_tier === 'STRETCH').length,
  }

  const hideSalarylessRoles = useAppStore((s) => s.hideSalarylessRoles)

  const tierFiltered = filterTier
    ? lastRoles.filter((r) => r.final_tier === filterTier)
    : lastRoles
  const visibleRoles = hideSalarylessRoles
    ? tierFiltered.filter((r) => r.salary_min != null || r.salary_max != null || !!r.salary_text)
    : tierFiltered

  // Sort visible roles by score descending. Tiebreaker: when scores are
  // within 3 points, prefer the role with salary disclosed — equivalent
  // matches are more actionable when comp is known. Score difference > 3
  // means the higher-scored role is genuinely a better fit, regardless of
  // salary disclosure.
  const sortedRoles = [...visibleRoles].sort((a, b) => {
    const scoreDiff = (b.final_score ?? 0) - (a.final_score ?? 0)
    if (Math.abs(scoreDiff) >= 3) return scoreDiff
    const aHasSalary = a.salary_min != null || a.salary_max != null || !!a.salary_text
    const bHasSalary = b.salary_min != null || b.salary_max != null || !!b.salary_text
    if (aHasSalary !== bHasSalary) return aHasSalary ? -1 : 1
    return scoreDiff
  })

  // Apply per-company display cap: only top COMPANY_CAP roles per company are
  // shown by default. The rest collapse behind a "see N more from {company}"
  // expander. Hidden roles re-appear when the user clicks the expander.
  type RenderEntry =
    | { kind: 'role'; role: typeof sortedRoles[number]; companyRank: number }
    | { kind: 'expander'; company: string; hiddenCount: number }
  const renderEntries: RenderEntry[] = useMemo(() => {
    const seenPerCompany = new Map<string, number>()
    const totalPerCompany = new Map<string, number>()
    sortedRoles.forEach((r) => {
      const k = (r.company || '').toLowerCase()
      totalPerCompany.set(k, (totalPerCompany.get(k) || 0) + 1)
    })
    const out: RenderEntry[] = []
    const expanderInsertedFor = new Set<string>()
    for (const role of sortedRoles) {
      const k = (role.company || '').toLowerCase()
      const rank = (seenPerCompany.get(k) || 0) + 1
      seenPerCompany.set(k, rank)
      const expanded = expandedCompanies.has(k)
      if (rank <= COMPANY_CAP || expanded) {
        out.push({ kind: 'role', role, companyRank: rank })
        // After the COMPANY_CAP-th role of a company we have more of (and
        // user hasn't expanded), insert the "see N more" button.
        const total = totalPerCompany.get(k) || 0
        if (
          rank === COMPANY_CAP &&
          total > COMPANY_CAP &&
          !expanded &&
          !expanderInsertedFor.has(k)
        ) {
          out.push({
            kind: 'expander',
            company: role.company || '',
            hiddenCount: total - COMPANY_CAP,
          })
          expanderInsertedFor.add(k)
        }
      }
    }
    return out
  }, [sortedRoles, expandedCompanies])

  const toggleExpand = (company: string) => {
    const k = company.toLowerCase()
    setExpandedCompanies((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }

  const runDate = lastSummary?.run_date
    ? new Date(lastSummary.run_date).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })
    : '—'

  return (
    <div className="px-10 py-8 max-w-6xl mx-auto">
      <Confetti trigger={showConfetti} />

      {/* Feedback prompt — appears once after a fresh run */}
      <AnimatePresence>
        {feedbackOpen && (
          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 24 }}
            className="fixed bottom-6 right-6 w-[380px] z-30
                       glass-strong rounded-xl border border-accent-500/30 p-5 shadow-2xl"
          >
            <div className="flex items-start justify-between mb-2">
              <div>
                <h3 className="text-sm font-semibold text-base-50">How did this run go?</h3>
                <p className="text-[11px] text-base-400 mt-0.5">
                  Optional — anything that surprised you, scored wrong, or felt off
                </p>
              </div>
              <button
                onClick={dismissFeedback}
                className="text-base-500 hover:text-base-300 text-xs"
                aria-label="Dismiss"
              >
                ✕
              </button>
            </div>
            <textarea
              value={feedbackText}
              onChange={(e) => setFeedbackText(e.target.value)}
              placeholder="Any roles that scored too high or too low? Companies you wish were here? Anything weird?"
              rows={4}
              className="w-full mt-2 px-3 py-2 rounded-md bg-white/[0.04] border border-white/[0.08]
                         text-xs text-base-100 placeholder:text-base-600 leading-relaxed
                         focus:outline-none focus:border-accent-500/50"
            />
            <div className="flex justify-end gap-2 mt-3">
              <button
                onClick={dismissFeedback}
                className="text-xs text-base-400 hover:text-base-200 px-3 py-1.5"
              >
                Skip
              </button>
              <button
                onClick={submitFeedbackForRun}
                disabled={feedbackSubmitting || !feedbackText.trim()}
                className="text-xs px-3 py-1.5 rounded-md
                           bg-accent-500/20 text-accent-200 border border-accent-500/40
                           hover:bg-accent-500/30 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {feedbackSubmitting ? 'Saving…' : 'Send feedback'}
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Header */}
      <div className="flex items-center justify-between mb-1">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
          <p className="text-sm text-base-400 mt-1">
            Last run · {runDate} · {lastRoles.length} qualifying roles
            {lastSummary && (
              <>
                {' '}from {lastSummary.roles_scraped.toLocaleString()} scraped
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* Home — return to Welcome page */}
          <button
            onClick={() => navigate('/welcome')}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg
                       glass-subtle hover:bg-white/[0.08]
                       text-sm text-base-300 transition-colors"
            title="Back to home"
          >
            <Home size={14} />
            Home
          </button>

          {/* Export dropdown */}
          <div ref={exportMenuRef} className="relative">
            <button
              onClick={() => setExportMenuOpen(!exportMenuOpen)}
              className="flex items-center gap-2 px-3.5 py-2 rounded-lg
                         glass-subtle hover:bg-white/[0.08]
                         text-sm text-base-200 transition-colors"
            >
              <Download size={14} />
              Export
            </button>
            <AnimatePresence>
              {exportMenuOpen && (
                <motion.div
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: 0.15 }}
                  className="absolute right-0 top-full mt-1.5 z-20
                             glass-strong rounded-lg border border-white/[0.10]
                             min-w-[220px] overflow-hidden py-1"
                >
                  <ExportMenuItem
                    icon={FileSpreadsheet}
                    label="Excel (.xlsx)"
                    sublabel="Multi-sheet with tier tabs"
                    onClick={() => handleExport('excel')}
                  />
                  <ExportMenuItem
                    icon={FileType}
                    label="CSV (.csv)"
                    sublabel="Plain spreadsheet, single sheet"
                    onClick={() => handleExport('csv')}
                  />
                  <ExportMenuItem
                    icon={FileText}
                    label="Markdown (.md)"
                    sublabel="Drop into Claude.ai for analysis"
                    onClick={() => handleExport('markdown')}
                  />
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          <button
            onClick={() => navigate('/run')}
            className="flex items-center gap-2 px-4 py-2 rounded-lg
                       bg-white text-base-950 text-sm font-medium
                       hover:bg-base-200 transition-colors
                       shadow-lg shadow-black/30"
          >
            <Play size={14} />
            New Search
          </button>
        </div>
      </div>

      {/* Tier KPIs */}
      <motion.div
        initial="hidden"
        animate="visible"
        variants={{ visible: { transition: { staggerChildren: 0.05 } } }}
        className="grid grid-cols-4 gap-3 mt-8"
      >
        {(['STRONG', 'GOOD', 'MAYBE', 'STRETCH'] as const).map((tier) => (
          <motion.div
            key={tier}
            variants={{
              hidden: { opacity: 0, y: 12 },
              visible: { opacity: 1, y: 0, transition: { duration: 0.3 } },
            }}
          >
            <TierCard
              tier={tier}
              count={counts[tier]}
              scoreRange={
                tier === 'STRONG' ? '85–100'
                : tier === 'GOOD' ? '70–84'
                : tier === 'MAYBE' ? '55–69'
                : '40–54'
              }
              description={
                tier === 'STRONG' ? 'Apply this week'
                : tier === 'GOOD' ? 'Apply within 2 weeks'
                : tier === 'MAYBE' ? 'Worth reading'
                : 'Long shot'
              }
              onClick={() => setFilterTier(filterTier === tier ? null : tier)}
              selected={filterTier === tier}
            />
          </motion.div>
        ))}
      </motion.div>

      {/* Filter chip */}
      {filterTier && (
        <div className="mt-6 flex items-center gap-2">
          <Filter size={13} className="text-base-400" />
          <span className="text-xs text-base-400">Filtering:</span>
          <button
            onClick={() => setFilterTier(null)}
            className="px-2.5 py-1 rounded-full text-xs bg-white/[0.06]
                       border border-white/[0.08] hover:bg-white/[0.10]"
          >
            {filterTier} · clear ✕
          </button>
        </div>
      )}

      {/* Coverage gap banner — surfaces when target_industries are
          under-represented in the scraper pool. Honest warning instead
          of silently weak results. */}
      {lastSummary?.coverage_gap_severity === 'HIGH' &&
       lastSummary.coverage_gap_message && (
        <motion.div
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          className="mt-6 rounded-xl border border-amber-500/30 bg-amber-500/[0.06]
                     px-4 py-3 flex items-start gap-3"
        >
          <AlertTriangle size={16} className="text-amber-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <div className="text-xs font-semibold text-amber-200 mb-1">
              Limited coverage in your target sectors
            </div>
            <p className="text-[11px] text-base-300 leading-relaxed">
              {lastSummary.coverage_gap_message}
            </p>
          </div>
        </motion.div>
      )}

      {/* Roles list — tier-grouped when no filter applied, flat when filtered.
          STRONG/GOOD/MAYBE get full cards with accent borders; STRETCH gets
          condensed single-row cards in a separate section so primary matches
          earn the visual weight. */}
      {filterTier ? (
        <div className="mt-8 space-y-2.5">
          <div className="flex items-center gap-2 mb-3">
            <Sparkles size={14} className="text-accent-400" />
            <h2 className="text-sm font-medium text-base-200">
              {filterTier}-tier roles
            </h2>
            <span className="text-xs text-base-500">({sortedRoles.length})</span>
          </div>
          {renderEntries.map((entry, idx) => {
            if (entry.kind === 'role') {
              return (
                <motion.div
                  key={`${entry.role.company}-${entry.role.job_title}-${idx}`}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3, delay: Math.min(idx * 0.02, 0.6) }}
                >
                  <RoleCard
                    role={entry.role}
                    condensed={entry.role.final_tier === 'STRETCH'}
                  />
                </motion.div>
              )
            }
            return (
              <button
                key={`expander-${entry.company}-${idx}`}
                onClick={() => toggleExpand(entry.company)}
                className="w-full flex items-center justify-center gap-2 py-2.5
                           rounded-lg text-xs text-base-400 hover:text-base-200
                           bg-white/[0.02] hover:bg-white/[0.05]
                           border border-dashed border-white/[0.08]
                           transition-colors"
              >
                <ChevronDown size={13} />
                Show {entry.hiddenCount} more from {entry.company}
              </button>
            )
          })}
        </div>
      ) : (
        <TierGroupedList
          renderEntries={renderEntries}
          toggleExpand={toggleExpand}
        />
      )}
    </div>
  )
}

/** Renders roles grouped by tier with section headers. STRONG/GOOD/MAYBE get
 *  full RoleCard treatment; STRETCH renders condensed BUT each card is
 *  click-to-expand into the full layout (with Visit/Save/Applied/Hide actions
 *  matching higher tiers). Click again to re-collapse. */
function TierGroupedList({
  renderEntries,
  toggleExpand,
}: {
  renderEntries: Array<
    | { kind: 'role'; role: Role; companyRank: number }
    | { kind: 'expander'; company: string; hiddenCount: number }
  >
  toggleExpand: (company: string) => void
}) {
  const [expandedStretch, setExpandedStretch] = useState<Set<string>>(new Set())
  const stretchKey = (r: Role) =>
    `${(r.company || '').toLowerCase()}::${(r.job_title || '').toLowerCase()}`
  const toggleStretch = (r: Role) => {
    const k = stretchKey(r)
    setExpandedStretch((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }
  const tierOrder: Tier[] = ['STRONG', 'GOOD', 'MAYBE', 'STRETCH']
  const tierMeta: Record<Tier, { label: string; sub: string; condensed: boolean }> = {
    STRONG:  { label: 'Strong matches',  sub: 'apply this week',     condensed: false },
    GOOD:    { label: 'Good matches',    sub: 'apply within 2 weeks',condensed: false },
    MAYBE:   { label: 'Maybe',           sub: 'worth reading',       condensed: false },
    STRETCH: { label: 'Stretch',         sub: 'long shots — review if interested', condensed: true },
    SKIP:    { label: 'Skip',            sub: '',                    condensed: true },
  }

  // Bucket render entries by tier (carrying expanders with their owning role)
  const buckets: Record<Tier, typeof renderEntries> = {
    STRONG: [], GOOD: [], MAYBE: [], STRETCH: [], SKIP: [],
  }
  let lastTier: Tier | null = null
  for (const e of renderEntries) {
    if (e.kind === 'role') {
      const t = (e.role.final_tier || 'STRETCH') as Tier
      buckets[t].push(e)
      lastTier = t
    } else if (lastTier) {
      // expander follows its owning role's tier bucket
      buckets[lastTier].push(e)
    }
  }

  return (
    <div className="mt-8 space-y-8">
      {tierOrder.map((tier) => {
        const entries = buckets[tier]
        const roleCount = entries.filter((e) => e.kind === 'role').length
        if (roleCount === 0) return null
        const meta = tierMeta[tier]
        return (
          <section key={tier}>
            <div className="flex items-baseline gap-2 mb-3">
              <h2 className="text-sm font-semibold text-base-100">{meta.label}</h2>
              <span className="text-xs text-base-500">
                ({roleCount}) · {meta.sub}
              </span>
            </div>
            <div className={meta.condensed ? 'space-y-1.5' : 'space-y-2.5'}>
              {entries.map((entry, idx) => {
                if (entry.kind === 'role') {
                  // STRETCH defaults to condensed but expands on click into
                  // the full-card layout (matches Strong/Good/Maybe sections).
                  // Click again to collapse.
                  const isStretchExpanded =
                    meta.condensed && expandedStretch.has(stretchKey(entry.role))
                  const renderCondensed = meta.condensed && !isStretchExpanded
                  return (
                    <motion.div
                      key={`${entry.role.company}-${entry.role.job_title}-${idx}`}
                      initial={{ opacity: 0, y: 8 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: 0.3, delay: Math.min(idx * 0.02, 0.4) }}
                    >
                      <RoleCard
                        role={entry.role}
                        condensed={renderCondensed}
                        onClick={meta.condensed ? () => toggleStretch(entry.role) : undefined}
                      />
                    </motion.div>
                  )
                }
                return (
                  <button
                    key={`expander-${entry.company}-${idx}`}
                    onClick={() => toggleExpand(entry.company)}
                    className="w-full flex items-center justify-center gap-2 py-2
                               rounded-lg text-[11px] text-base-400 hover:text-base-200
                               bg-white/[0.02] hover:bg-white/[0.05]
                               border border-dashed border-white/[0.08]
                               transition-colors"
                  >
                    <ChevronDown size={12} />
                    Show {entry.hiddenCount} more from {entry.company}
                  </button>
                )
              })}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function ExportMenuItem({
  icon: Icon,
  label,
  sublabel,
  onClick,
}: {
  icon: React.ElementType
  label: string
  sublabel: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-center gap-3 px-3.5 py-2.5
                 text-left hover:bg-white/[0.06] transition-colors"
    >
      <Icon size={16} className="text-accent-400 flex-shrink-0" />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-base-100">{label}</div>
        <div className="text-[11px] text-base-400">{sublabel}</div>
      </div>
    </button>
  )
}

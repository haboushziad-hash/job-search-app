import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Calendar, Loader2, AlertCircle, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { listRuns, getRun, deleteRun, ApiError, type RunHistoryItem } from '@/services/api'
import { useAppStore } from '@/stores/appStore'
import type { Role, RunSummary } from '@/types'
import { cn } from '@/lib/utils'

export default function History() {
  const navigate = useNavigate()
  const setLastResults = useAppStore((s) => s.setLastResults)
  const lastSummary = useAppStore((s) => s.lastSummary)
  const setLastResultsRaw = useAppStore.setState
  const [runs, setRuns] = useState<RunHistoryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [openingId, setOpeningId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listRuns()
      .then(({ runs }) => {
        if (cancelled) return
        setRuns(runs)
        setLoading(false)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load run history')
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Per-row delete handler. Confirms before firing — the audit JSON is
  // also deleted from disk, and there's no undo on the server side. If
  // the deleted run was the one currently displayed on the dashboard,
  // we also clear lastResults in the store so the dashboard doesn't
  // keep rendering stale data tied to a now-missing run_id.
  const handleDelete = async (runId: string, runDateLabel: string) => {
    if (deletingId) return
    const confirmed = confirm(
      `Delete this run from ${runDateLabel}?\n\n` +
      `Removes the run row, scoring data, and audit JSON file from your machine. ` +
      `This cannot be undone.`
    )
    if (!confirmed) return

    setDeletingId(runId)
    try {
      await deleteRun(runId)
      // Refresh the list from the source of truth.
      const { runs: fresh } = await listRuns()
      setRuns(fresh)
      // If we just deleted the run currently shown on Dashboard, blank
      // out lastResults so the user doesn't see ghost-row data tied to
      // a run that no longer exists. They'll see the empty dashboard
      // until they pick another run from history or run a new search.
      if (lastSummary?.run_id === runId) {
        setLastResultsRaw({ lastRoles: [], lastSummary: null })
      }
      toast.success('Run deleted')
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'unknown')
      toast.error(`Failed to delete run: ${msg}`)
    } finally {
      setDeletingId(null)
    }
  }

  const openRun = async (runId: string) => {
    setOpeningId(runId)
    try {
      const data = await getRun(runId)
      // Roles from /runs/{id} are flat dicts shaped like Role — cast.
      const roles = data.roles as unknown as Role[]
      const summary = data.summary as unknown as RunSummary
      setLastResults(roles, summary)
      toast.success('Loaded historical run', {
        description: `${roles.length} qualifying roles · click "Dashboard" to view`,
      })
      navigate('/dashboard')
    } catch (e) {
      const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'unknown')
      toast.error(`Failed to load run: ${msg}`)
    } finally {
      setOpeningId(null)
    }
  }

  if (loading) {
    return (
      <div className="px-10 py-8 max-w-5xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Run History</h1>
        <p className="text-sm text-base-400 mt-1">Loading your past searches…</p>
        <div className="mt-12 flex items-center justify-center text-base-400">
          <Loader2 size={20} className="animate-spin" />
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="px-10 py-8 max-w-5xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Run History</h1>
        <div className="glass mt-8 rounded-xl p-8 text-center">
          <AlertCircle size={28} className="text-base-400 mx-auto mb-3" />
          <h2 className="text-base font-medium mb-2">Couldn't load run history</h2>
          <p className="text-sm text-base-400">{error}</p>
        </div>
      </div>
    )
  }

  if (runs.length === 0) {
    return (
      <div className="px-10 py-8 max-w-5xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Run History</h1>
        <p className="text-sm text-base-400 mt-1">No saved runs yet — your search results will appear here.</p>
        <div className="glass mt-12 rounded-xl p-12 text-center">
          <Calendar size={28} className="text-base-400 mx-auto mb-3" />
          <h2 className="text-base font-medium">No runs yet</h2>
          <p className="text-sm text-base-400 mt-1.5 max-w-sm mx-auto">
            Click "New Search" to run your first search. Past runs will accumulate here.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="px-10 py-8 max-w-5xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight">Run History</h1>
      <p className="text-sm text-base-400 mt-1">
        Every search you've run, with full results saved. Click any run to load its results.
      </p>

      <div className="mt-8 space-y-2.5">
        {runs.map((run, idx) => {
          const dateLabel = formatRunDate(run.started_at)
          const isOpening = openingId === run.run_id
          const isDeleting = deletingId === run.run_id
          // Disable both actions on this row while ANY row is opening/
          // deleting to prevent racing requests.
          const rowBusy = !!openingId || !!deletingId
          return (
            // Wrap the row in a relative div so we can absolute-position
            // the delete button on the right edge and keep it visible on
            // hover only — clean default look, action available on intent.
            <motion.div
              key={run.run_id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: idx * 0.04 }}
              className="group relative"
            >
              <button
                onClick={() => openRun(run.run_id)}
                disabled={rowBusy}
                className="w-full text-left glass-subtle hover:glass rounded-xl p-5
                           cursor-pointer transition-colors disabled:opacity-50
                           disabled:cursor-wait"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-4">
                    <div className="w-10 h-10 rounded-lg bg-white/[0.04] border border-white/[0.06]
                                    flex items-center justify-center">
                      {isOpening ? (
                        <Loader2 size={16} className="text-base-400 animate-spin" />
                      ) : (
                        <Calendar size={16} className="text-base-400" />
                      )}
                    </div>
                    <div>
                      <div className="text-sm font-medium">{dateLabel}</div>
                      <div className="text-xs text-base-500">
                        {run.profile_headline ? truncate(run.profile_headline, 60) : 'profile snapshot'}
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-7 text-xs pr-10">
                    <Stat label="Scraped" value={run.scraped?.toLocaleString() ?? '—'} />
                    <Stat label="Qualifying" value={run.qualifying_count ?? '—'} />
                    <Stat label="Strong" value={run.tier_strong ?? 0} accent={(run.tier_strong ?? 0) > 0} />
                    <Stat label="Good" value={run.tier_good ?? 0} accent={(run.tier_good ?? 0) > 0} />
                  </div>
                </div>
              </button>
              {/* Delete button — visible on hover (or always while
                  THIS row is mid-delete so the spinner stays visible).
                  Stops propagation so the row's openRun handler doesn't
                  fire on click. */}
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  handleDelete(run.run_id, dateLabel)
                }}
                disabled={rowBusy}
                title="Delete this run from history"
                aria-label="Delete run"
                className={cn(
                  'absolute top-1/2 -translate-y-1/2 right-4 p-2 rounded-md',
                  'text-base-400 hover:text-red-300 hover:bg-red-500/10',
                  'border border-transparent hover:border-red-500/20',
                  'transition-all',
                  // Default hidden, revealed on row hover. Force-visible
                  // while this specific row is mid-delete.
                  isDeleting
                    ? 'opacity-100'
                    : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
                  rowBusy && 'opacity-50 cursor-not-allowed'
                )}
              >
                {isDeleting ? (
                  <Loader2 size={14} className="animate-spin" />
                ) : (
                  <Trash2 size={14} />
                )}
              </button>
            </motion.div>
          )
        })}
      </div>
    </div>
  )
}

function formatRunDate(iso?: string | null): string {
  if (!iso) return '?'
  // Treat tz-less ISO strings as UTC. The runs.db started_at column
  // already stores tz-aware ISO via archive.py, but defending against
  // any legacy or upstream code that might emit naive timestamps so the
  // History page never silently shows the wrong time-of-day.
  const safe = /[+-]\d\d:?\d\d$|Z$/.test(iso) ? iso : iso + 'Z'
  const d = new Date(safe)
  if (isNaN(d.getTime())) return '?'
  const now = new Date()
  const days = Math.floor((now.getTime() - d.getTime()) / 86_400_000)
  const dateStr = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  const timeStr = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  let rel = ''
  if (days === 0) rel = 'Today'
  else if (days === 1) rel = 'Yesterday'
  else if (days < 7) rel = `${days} days ago`
  else if (days < 30) rel = `${Math.floor(days / 7)} week${Math.floor(days / 7) === 1 ? '' : 's'} ago`
  else rel = `${Math.floor(days / 30)} mo ago`
  return `${dateStr} · ${timeStr} (${rel})`
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s
}

function Stat({ label, value, accent }: { label: string; value: string | number; accent?: boolean }) {
  return (
    <div className="text-right">
      <div className="text-[10px] tracking-wider text-base-500 uppercase">{label}</div>
      <div className={accent ? 'text-tier-strong text-sm font-medium tabular-nums' : 'text-base-200 text-sm tabular-nums'}>
        {value}
      </div>
    </div>
  )
}

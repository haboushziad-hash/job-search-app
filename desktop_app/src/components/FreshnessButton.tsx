import { useState } from 'react'
import { createPortal } from 'react-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { RefreshCw, X, Loader2, Undo2, ShieldCheck } from 'lucide-react'
import { toast } from 'sonner'
import { useAppStore } from '@/stores/appStore'
import { recheckFreshness } from '@/services/api'
import type { Role } from '@/types'

interface FreshnessButtonProps {
  // The full qualifying set to re-validate (we check all, not just the filtered view).
  roles: Role[]
  // Run date (ISO) — used to nudge "run a fresh search" when the run is old.
  runDate?: string | null
}

function relativeTime(ts: number): string {
  const s = Math.floor((Date.now() - ts) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function FreshnessButton({ roles, runDate }: FreshnessButtonProps) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const applyFreshness = useAppStore((s) => s.applyFreshness)
  const clearFreshness = useAppStore((s) => s.clearFreshness)
  const checkedAt = useAppStore((s) => s.freshnessCheckedAt)
  const staleCount = useAppStore((s) => s.staleUrls.length)

  const urls = Array.from(
    new Set(roles.map((r) => r.job_url).filter((u): u is string => !!u)),
  )

  // Nudge a fresh search when the underlying run is more than ~3 days old —
  // re-check only re-validates THESE jobs, it can't surface new postings.
  const runAgeDays = runDate ? (Date.now() - new Date(runDate).getTime()) / 86400000 : 0
  const stale = runAgeDays >= 3

  const run = async () => {
    setBusy(true)
    try {
      const { dead, uncertain } = await recheckFreshness(urls)
      applyFreshness(dead)
      setOpen(false)
      if (dead.length === 0) {
        toast.success('Freshness re-checked', {
          description: `All ${urls.length} links still look active${uncertain.length ? ` · ${uncertain.length} couldn't be confirmed (kept)` : ''}.`,
        })
      } else {
        toast.success(`Removed ${dead.length} expired ${dead.length === 1 ? 'job' : 'jobs'}`, {
          description: `${uncertain.length} couldn't be confirmed and were kept. Undo anytime.`,
        })
      }
    } catch (e) {
      toast.error('Freshness re-check failed', { description: String(e).slice(0, 140) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <button
          onClick={() => setOpen(true)}
          className="flex items-center gap-1.5 whitespace-nowrap rounded-lg border border-base-600/40 bg-base-800/40 px-3 py-1.5 text-xs font-medium text-base-200 transition-colors hover:bg-base-700/50"
          title="Re-check that these jobs are still live — cheaper and faster than a new search"
        >
          <RefreshCw size={13} /> Re-Check Freshness
        </button>
        {checkedAt != null && (
          <span className="flex items-center gap-1 text-[11px] text-base-400">
            checked {relativeTime(checkedAt)}
            {staleCount > 0 && (
              <button
                onClick={() => { clearFreshness(); toast('Restored removed jobs') }}
                className="ml-1 flex items-center gap-0.5 text-base-300 underline-offset-2 hover:underline"
              >
                <Undo2 size={11} /> undo
              </button>
            )}
          </span>
        )}
      </div>

      {createPortal(
        <AnimatePresence>
          {open && (
            <motion.div
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
              className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-4"
              onClick={() => !busy && setOpen(false)}
            >
              <motion.div
                initial={{ scale: 0.96, y: 8 }} animate={{ scale: 1, y: 0 }} exit={{ scale: 0.96, y: 8 }}
                onClick={(e) => e.stopPropagation()}
                className="glass relative w-full max-w-md rounded-2xl border border-base-600/40 p-6"
              >
                <button
                  onClick={() => !busy && setOpen(false)}
                  className="absolute right-4 top-4 text-base-400 hover:text-base-200"
                ><X size={18} /></button>

                <div className="mb-3 flex items-center gap-2">
                  <ShieldCheck size={18} className="text-tier-good" />
                  <h2 className="text-lg font-semibold text-base-100">Re-Check Freshness</h2>
                </div>

                <p className="text-sm text-base-300">
                  We'll re-visit all <span className="font-semibold text-base-100">{urls.length}</span> job
                  links from this search and <span className="font-semibold text-base-100">remove only the ones
                  we're confident are gone</span> — a dead link (404/expired) or a "no longer accepting
                  applications" notice on the page. Jobs we can't confirm (a slow site or sign-in wall) are
                  <span className="font-semibold text-base-100"> kept and left untouched</span>.
                </p>

                <div className="mt-3 rounded-lg border border-base-600/40 bg-base-800/40 p-3 text-xs text-base-400">
                  <p className="mb-1 font-medium text-base-300">What this does and doesn't do</p>
                  <ul className="list-disc space-y-0.5 pl-4">
                    <li>Re-checks these jobs only — it <em>can't</em> find new postings.</li>
                    <li>It isn't foolproof: boards that hide closures behind a login or JavaScript can slip through.</li>
                    <li>Cheap &amp; fast (no re-scraping, no AI) — takes ~30–60s.</li>
                  </ul>
                </div>

                {stale && (
                  <p className="mt-3 rounded-lg border border-tier-maybe/30 bg-tier-maybe/10 p-2.5 text-xs text-tier-maybe">
                    This search is {Math.round(runAgeDays)} days old — for the most current results, run a fresh search instead.
                  </p>
                )}

                <div className="mt-5 flex justify-end gap-2">
                  <button
                    onClick={() => setOpen(false)} disabled={busy}
                    className="rounded-lg px-4 py-2 text-sm text-base-300 hover:bg-base-700/40 disabled:opacity-50"
                  >Cancel</button>
                  <button
                    onClick={run} disabled={busy || urls.length === 0}
                    className="flex items-center gap-2 rounded-lg bg-tier-good/90 px-4 py-2 text-sm font-medium text-black transition-colors hover:bg-tier-good disabled:opacity-50"
                  >
                    {busy ? <><Loader2 size={14} className="animate-spin" /> Checking…</> : <>Re-check {urls.length} jobs</>}
                  </button>
                </div>
              </motion.div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  )
}

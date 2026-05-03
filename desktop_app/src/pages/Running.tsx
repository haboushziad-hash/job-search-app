import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Check, Loader2, Sparkles, AlertCircle } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/stores/appStore'
import { getSearchStatus, getSearchResults, ApiError } from '@/services/api'
import type { SearchStatus } from '@/services/api'

const STEPS = [
  { id: 1, label: 'Profile + keywords ready' },
  { id: 2, label: 'Scraping job boards' },
  { id: 3, label: 'Filtering + verifying liveness' },
  { id: 4, label: 'Embedding pre-filter' },
  { id: 5, label: 'Scoring with AI cascade' },
  { id: 6, label: 'Building dashboard' },
]

// Build a plain-language description of what's happening RIGHT NOW based on
// which step is active and the live counts the backend sends. Replaces the
// generic "current_step" string with something that tells the user the tool
// is actually working — "1,847 roles scraped from 3 of 6 sources" beats
// "scraping job boards" any day.
function describeProgress(stepIdx: number, status: SearchStatus | null): string | null {
  if (!status) return null
  const scraped = status.roles_scraped || 0
  const qualifying = status.roles_qualifying || 0
  switch (stepIdx) {
    case 0:  // Profile + keywords
      return null
    case 1:  // Scraping
      return scraped > 0 ? `${scraped.toLocaleString()} roles found so far` : 'Querying career pages…'
    case 2:  // Filtering + liveness
      return scraped > 0 ? `Filtering ${scraped.toLocaleString()} roles by your preferences` : null
    case 3:  // Embedding
      return 'Comparing each role against your profile semantically'
    case 4:  // Scoring cascade
      if (qualifying > 0) return `${qualifying} qualifying so far · evaluating remainder`
      return 'Reading job descriptions in detail'
    case 5:  // Building dashboard
      return 'Assembling your scored results'
    default:
      return status.current_step || null
  }
}

export default function Running() {
  const navigate = useNavigate()
  const runId = useAppStore((s) => s.activeRunId)
  const setLastResults = useAppStore((s) => s.setLastResults)
  const [status, setStatus] = useState<SearchStatus | null>(null)
  const [error, setError] = useState<string | null>(null)

  // No active run — bounce back to Run setup
  useEffect(() => {
    if (!runId) {
      navigate('/run')
    }
  }, [runId, navigate])

  // Poll status every 2 seconds
  useEffect(() => {
    if (!runId) return
    let cancelled = false

    const tick = async () => {
      if (cancelled) return
      try {
        const s = await getSearchStatus(runId)
        if (cancelled) return
        setStatus(s)

        if (s.status === 'completed') {
          // Pull final results
          const results = await getSearchResults(runId)
          setLastResults(results.roles, results.summary)
          toast.success(
            `Search complete · ${results.roles.length} qualifying roles found`
          )
          navigate('/dashboard')
          return
        }

        if (s.status === 'failed') {
          setError(s.error || 'Run failed')
          return
        }

        // Keep polling
        setTimeout(tick, 2000)
      } catch (e) {
        if (cancelled) return
        const msg = e instanceof ApiError ? e.detail : 'Connection error'
        setError(msg)
      }
    }

    tick()
    return () => { cancelled = true }
  }, [runId, navigate, setLastResults])

  // Map server progress → UI step index
  const activeStep = status ? Math.max(0, status.current_step_index - 1) : 0
  const progress = status?.progress ?? 0

  return (
    <div className="h-screen w-screen flex items-center justify-center px-8 overflow-hidden">
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        className="glass-strong w-full max-w-xl rounded-2xl p-10"
      >
        <div className="flex items-center gap-3 mb-2">
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-accent-400 to-accent-700
                          flex items-center justify-center">
            <Sparkles size={16} className="text-white" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight">
            {error ? 'Search failed' : 'Search in progress'}
          </h1>
        </div>

        {error ? (
          <>
            <div className="flex items-start gap-3 p-4 mt-4 rounded-lg bg-red-500/10 border border-red-500/30">
              <AlertCircle size={16} className="text-red-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm text-base-100 font-medium">Something went wrong</p>
                <p className="text-xs text-base-400 mt-1 leading-relaxed">{error}</p>
              </div>
            </div>
            <button
              onClick={() => navigate('/run')}
              className="mt-6 w-full px-4 py-2.5 rounded-lg
                         bg-white text-base-950 font-medium text-sm
                         hover:bg-base-200 transition-colors"
            >
              Back to setup
            </button>
          </>
        ) : (
          <>
            <p className="text-sm text-base-400 mb-7">
              {progress < 100
                ? "This will take 5–15 minutes. You can leave this screen open."
                : 'Done! Loading your results...'}
            </p>

            {/* Progress bar — uses Framer's animate (not style) so transitions actually tween */}
            <div className="relative h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
              <motion.div
                className="absolute inset-y-0 left-0 bg-gradient-to-r from-accent-500 to-accent-400 rounded-full"
                animate={{ width: `${Math.max(2, progress)}%` }}
                transition={{ duration: 0.8, ease: 'easeOut' }}
              />
            </div>
            <div className="flex justify-between mt-2 text-xs text-base-500">
              <span>{Math.round(progress)}%</span>
              {status && status.roles_scraped > 0 && (
                <span>{status.roles_scraped.toLocaleString()} roles scraped</span>
              )}
            </div>

            {/* Steps */}
            <div className="mt-8 space-y-2.5">
              {STEPS.map((step, idx) => {
                const isComplete = idx < activeStep
                const isActive = idx === activeStep && progress < 100
                const liveDetail = isActive ? describeProgress(idx, status) : null
                return (
                  <motion.div
                    key={step.id}
                    initial={{ opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: idx * 0.05 }}
                    className={cn(
                      'flex flex-col gap-1 px-3 py-2 rounded-lg',
                      isActive && 'bg-accent-500/[0.08]'
                    )}
                  >
                    <div className="flex items-center gap-3">
                      <div className={cn(
                        'w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0',
                        isComplete && 'bg-tier-strong/20 text-tier-strong',
                        isActive && 'bg-accent-500/20 text-accent-300',
                        !isComplete && !isActive && 'bg-white/[0.04] text-base-500'
                      )}>
                        <AnimatePresence mode="wait">
                          {isComplete && (
                            <motion.span key="check" initial={{ scale: 0 }} animate={{ scale: 1 }}>
                              <Check size={11} />
                            </motion.span>
                          )}
                          {isActive && (
                            <motion.span
                              key="loading"
                              initial={{ rotate: 0 }}
                              animate={{ rotate: 360 }}
                              transition={{ repeat: Infinity, duration: 1.2, ease: 'linear' }}
                            >
                              <Loader2 size={11} />
                            </motion.span>
                          )}
                        </AnimatePresence>
                      </div>
                      <span className={cn(
                        'text-sm flex-1',
                        isActive ? 'text-base-50' : isComplete ? 'text-base-300' : 'text-base-500'
                      )}>
                        {step.label}
                      </span>
                    </div>
                    {/* Live "what's actually happening" line — only on the active stage */}
                    <AnimatePresence>
                      {isActive && liveDetail && (
                        <motion.span
                          key="detail"
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: 'auto' }}
                          exit={{ opacity: 0, height: 0 }}
                          className="text-[11px] text-accent-200/80 pl-8 leading-snug"
                        >
                          {liveDetail}
                        </motion.span>
                      )}
                    </AnimatePresence>
                  </motion.div>
                )
              })}
            </div>

            {/* Live tier counts (only show once Stage 2 starts producing scores) */}
            {status && (status.tier_strong + status.tier_good + status.tier_maybe + status.tier_stretch > 0) && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className="glass-subtle rounded-xl p-4 mt-6"
              >
                <div className="text-[10px] uppercase tracking-wider text-base-400 font-medium mb-2">
                  Found so far
                </div>
                <div className="grid grid-cols-4 gap-3">
                  <Stat label="STRONG" value={status.tier_strong} color="tier-strong" />
                  <Stat label="GOOD" value={status.tier_good} color="tier-good" />
                  <Stat label="MAYBE" value={status.tier_maybe} color="tier-maybe" />
                  <Stat label="STRETCH" value={status.tier_stretch} color="base-400" />
                </div>
              </motion.div>
            )}

            <button
              onClick={() => navigate('/dashboard')}
              className="mt-7 w-full px-4 py-2 rounded-lg text-sm text-base-400 hover:text-base-200
                         border border-white/[0.06] hover:border-white/[0.10] transition-colors"
            >
              Continue in background
            </button>
          </>
        )}
      </motion.div>
    </div>
  )
}

function Stat({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="text-center">
      <div className={cn('text-xl font-semibold tabular-nums', `text-${color}`)}>{value}</div>
      <div className="text-[9px] tracking-wider text-base-500 mt-0.5">{label}</div>
    </div>
  )
}

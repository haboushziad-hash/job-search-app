import { useEffect, useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import { Sparkles, Loader2, AlertCircle, Check } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { useAppStore } from '@/stores/appStore'
import { getBuildStatus, getBuildResult, ApiError } from '@/services/api'

interface LocationState {
  buildId: string
}

// Collapsed stages — each maps to a contiguous progress range. The
// "Running 4 AI models" stage shows a live X-of-4 count derived from
// progress so users see incremental completion without burst-jumps.
const STAGES = [
  { startAt: 0,  endAt: 11,  label: 'Reading your resume(s)' },
  { startAt: 11, endAt: 71,  label: 'Running 4 AI models in parallel', dynamic: 'ai_generations' },
  { startAt: 71, endAt: 94,  label: 'Synthesizing the best result' },
  { startAt: 94, endAt: 100, label: 'Finalizing your keyword list' },
]

// How many AI generations have completed at this progress level. The
// 4 backend events fire at progress=26.5, 41, 55.5, 70 (approx evenly
// spaced between the 12% start and 72% finish).
function aiGenerationsComplete(progress: number): number {
  if (progress >= 70) return 4
  if (progress >= 55) return 3
  if (progress >= 40) return 2
  if (progress >= 25) return 1
  return 0
}

export default function Building() {
  const navigate = useNavigate()
  const location = useLocation()
  const setProfile = useAppStore((s) => s.setProfile)

  // React Router puts navigate({ state: ... }) into location.state.
  // Fall back to window.history.state for hard-refresh edge cases.
  const buildId =
    (location.state as LocationState | null)?.buildId ??
    (typeof window !== 'undefined'
      ? (window.history.state as { usr?: LocationState } | null)?.usr?.buildId
      : null) ??
    null

  const [progress, setProgress] = useState(0)
  const [currentStep, setCurrentStep] = useState('Initializing...')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!buildId) {
      navigate('/setup')
      return
    }

    let cancelled = false

    const tick = async () => {
      if (cancelled) return
      try {
        const status = await getBuildStatus(buildId)
        if (cancelled) return

        // Smoothly approach the target — don't snap backward
        setProgress((prev) => Math.max(prev, status.progress))
        if (status.current_step) setCurrentStep(status.current_step)

        if (status.status === 'completed') {
          const result = await getBuildResult(buildId)
          setProfile(result.profile)
          toast.success(
            `Profile built · ${result.keyword_count} keywords (${result.tier_breakdown.tier_1} T1 · ${result.tier_breakdown.tier_2} T2 · ${result.tier_breakdown.tier_3} T3)`
          )
          // Brief moment to let the bar finish to 100%
          setTimeout(() => navigate('/keywords'), 600)
          return
        }

        if (status.status === 'failed') {
          setError(status.error || 'Profile build failed')
          return
        }

        setTimeout(tick, 1500)
      } catch (e) {
        if (cancelled) return
        const msg = e instanceof ApiError ? e.detail : 'Connection error'
        setError(msg)
      }
    }

    tick()
    return () => {
      cancelled = true
    }
  }, [buildId, navigate, setProfile])

  // Active stage = the one whose progress range currently contains us.
  // Stages before that are complete; stages after are pending.
  const activeStageIdx = (() => {
    for (let i = 0; i < STAGES.length; i++) {
      if (progress < STAGES[i].endAt) return i
    }
    return STAGES.length - 1
  })()

  return (
    <div className="min-h-screen w-full flex items-center justify-center px-6 py-7">
      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="glass-strong w-full max-w-xl rounded-2xl p-9"
      >
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-accent-400 to-accent-700
                          flex items-center justify-center shadow-lg shadow-accent-900/40">
            <Sparkles size={17} className="text-white" />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {error ? 'Build failed' : 'Building your profile'}
          </h1>
        </div>

        {error ? (
          <>
            <div className="flex items-start gap-3 p-4 mt-4 rounded-lg
                            bg-red-500/10 border border-red-500/30">
              <AlertCircle size={16} className="text-red-400 flex-shrink-0 mt-0.5" />
              <div>
                <p className="text-sm text-base-100 font-medium">Something went wrong</p>
                <p className="text-xs text-base-400 mt-1 leading-relaxed">{error}</p>
              </div>
            </div>
            <button
              onClick={() => navigate('/setup')}
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
              Multiple AI models are reading your resume + extra context, generating
              keyword candidates, and synthesizing the best result. This usually takes
              60–90 seconds.
            </p>

            {/* Progress bar */}
            <div className="relative h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
              <motion.div
                className="absolute inset-y-0 left-0 bg-gradient-to-r from-accent-500 to-accent-400 rounded-full"
                animate={{ width: `${Math.max(2, progress)}%` }}
                transition={{ duration: 0.6, ease: 'easeOut' }}
              />
            </div>
            <div className="mt-2 flex items-center justify-between text-xs text-base-500">
              <span>{Math.round(progress)}%</span>
              <span className="truncate ml-3 max-w-[60%] text-right">
                {currentStep}
              </span>
            </div>

            {/* Stage list */}
            <div className="mt-8 space-y-2">
              {STAGES.map((stage, idx) => {
                const isComplete = idx < activeStageIdx
                const isActive = idx === activeStageIdx && progress < 100
                // Live detail for stages that need it
                let liveDetail: string | null = null
                if (isActive && stage.dynamic === 'ai_generations') {
                  const done = aiGenerationsComplete(progress)
                  liveDetail = `${done} of 4 done`
                }
                return (
                  <motion.div
                    key={stage.label}
                    initial={{ opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: idx * 0.04 }}
                    className={cn(
                      'flex items-center gap-3 px-3 py-1.5 rounded-lg',
                      isActive && 'bg-accent-500/[0.08]'
                    )}
                  >
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
                        {isActive && !isComplete && (
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
                      {stage.label}
                    </span>
                    {liveDetail && (
                      <span className="text-[11px] text-accent-200/80 tabular-nums">
                        {liveDetail}
                      </span>
                    )}
                  </motion.div>
                )
              })}
            </div>
          </>
        )}
      </motion.div>
    </div>
  )
}

import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Play, Globe, FileText, Clock, Sparkles, Loader2, AlertCircle, Edit3, Home, Upload } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { useAppStore } from '@/stores/appStore'
import { runSearch, ApiError } from '@/services/api'

export default function Run() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)
  const [submitting, setSubmitting] = useState(false)

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

  const tier1 = profile.keywords.filter((k) => k.tier === 1).length
  const tier2 = profile.keywords.filter((k) => k.tier === 2).length

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
    setSubmitting(true)
    try {
      const res = await runSearch({
        profile,
        // Default: tier 1+2 keywords
        keywords: profile.keywords.filter((k) => k.tier <= 2).map((k) => k.text),
        // Omit `sources` so backend uses ALL active scrapers (14 total).
        // See Keywords.tsx for full reasoning.
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

  return (
    <div className="px-10 py-8 max-w-3xl mx-auto">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">New Search</h1>
          <p className="text-sm text-base-400 mt-1">
            Review your configuration and launch when ready.
          </p>
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
        <div className="flex items-center justify-between mb-5">
          <h2 className="flex items-center gap-2 text-sm font-medium text-base-200">
            <Sparkles size={14} className="text-accent-400" /> Search summary
          </h2>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => navigate('/setup')}
              className="flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px]
                         bg-white/[0.04] hover:bg-white/[0.08] text-base-300
                         border border-white/[0.06] transition-colors"
              title="Edit profile / preferences / re-upload resume"
            >
              <Upload size={11} /> Edit profile
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
            label="Keywords"
            value={`${tier1 + tier2} (${tier1} Tier 1 · ${tier2} Tier 2)`}
          />
          <Stat
            icon={Globe}
            label="Job boards"
            value="Greenhouse · Lever · Ashby · Workday · iCIMS · 5 broad aggregators"
          />
          <Stat
            icon={Clock}
            label="Estimated runtime"
            value="5–15 minutes"
          />
        </div>

        <div className="mt-7 p-4 rounded-lg bg-tier-strong/10 border border-tier-strong/20">
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
            className="flex items-center gap-2 px-5 py-2.5 rounded-lg
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
                Start search
              </>
            )}
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

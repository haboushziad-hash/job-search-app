import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import {
  Briefcase, Phone, Users, Award, X, Ghost, ArrowUpRight,
  ChevronDown, Trash2,
} from 'lucide-react'
import { toast } from 'sonner'
import { cn, formatRelativeDate } from '@/lib/utils'
import { useAppStore, type ApplicationStage } from '@/stores/appStore'
import type { Role } from '@/types'

/** Application progression order — drives the column layout. */
const STAGE_ORDER: ApplicationStage[] = [
  'applied', 'phone_screen', 'interview', 'offer', 'rejected', 'ghosted', 'withdrew',
]

const STAGE_META: Record<ApplicationStage, { label: string; icon: React.ElementType; color: string }> = {
  applied:      { label: 'Applied',       icon: Briefcase, color: 'text-base-200' },
  phone_screen: { label: 'Phone screen',  icon: Phone,     color: 'text-tier-maybe' },
  interview:    { label: 'Interview',     icon: Users,     color: 'text-tier-good' },
  offer:        { label: 'Offer',         icon: Award,     color: 'text-tier-strong' },
  rejected:     { label: 'Rejected',      icon: X,         color: 'text-tier-stretch' },
  ghosted:      { label: 'Ghosted',       icon: Ghost,     color: 'text-base-500' },
  withdrew:     { label: 'Withdrew',      icon: Trash2,    color: 'text-base-500' },
}


export default function Tracker() {
  const navigate = useNavigate()
  const roleStatuses = useAppStore((s) => s.roleStatuses)
  const updateApplicationStage = useAppStore((s) => s.updateApplicationStage)
  const moveAppliedToSaved = useAppStore((s) => s.moveAppliedToSaved)

  // Bucket applied roles by stage
  const buckets = useMemo(() => {
    const out: Record<ApplicationStage, Array<{ role: Role; appliedAt: string }>> = {
      applied: [], phone_screen: [], interview: [], offer: [],
      rejected: [], ghosted: [], withdrew: [],
    }
    for (const e of Object.values(roleStatuses)) {
      if (e.status !== 'applied' || !e.roleSnapshot) continue
      const stage = (e.applicationStage || 'applied') as ApplicationStage
      out[stage].push({ role: e.roleSnapshot, appliedAt: e.date })
    }
    // Sort each bucket newest-first
    for (const s of STAGE_ORDER) {
      out[s].sort((a, b) => (a.appliedAt < b.appliedAt ? 1 : -1))
    }
    return out
  }, [roleStatuses])

  const totalApplied = STAGE_ORDER.reduce((acc, s) => acc + buckets[s].length, 0)

  if (totalApplied === 0) {
    return (
      <div className="px-10 py-8 max-w-6xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Applications</h1>
        <p className="text-sm text-base-400 mt-1">Track every role you've applied to.</p>
        <div className="glass mt-10 rounded-xl p-12 text-center">
          <div className="w-12 h-12 mx-auto mb-4 rounded-xl bg-white/[0.04] border border-white/[0.06]
                          flex items-center justify-center">
            <Briefcase size={20} className="text-base-400" />
          </div>
          <h2 className="text-lg font-medium">No applications yet</h2>
          <p className="text-sm text-base-400 mt-1.5 max-w-sm mx-auto">
            Click "Apply" on any role from the dashboard, then move it through stages here as you hear back.
          </p>
          <button
            onClick={() => navigate('/dashboard')}
            className="mt-6 inline-flex items-center gap-2 px-5 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       shadow-lg shadow-black/30"
          >
            <ArrowUpRight size={14} /> Browse roles
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="px-10 py-8 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-1">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Applications</h1>
          <p className="text-sm text-base-400 mt-1">
            {totalApplied} application{totalApplied === 1 ? '' : 's'} ·
            move each one through stages as you hear back
          </p>
        </div>
      </div>

      {/* Stage summary chips */}
      <div className="flex flex-wrap gap-2 mt-6">
        {STAGE_ORDER.map((s) => {
          const meta = STAGE_META[s]
          const Icon = meta.icon
          const count = buckets[s].length
          if (count === 0) return null
          return (
            <div
              key={s}
              className="flex items-center gap-2 px-3 py-1.5 rounded-full
                         bg-white/[0.04] border border-white/[0.08] text-xs"
            >
              <Icon size={12} className={meta.color} />
              <span className="text-base-300">{meta.label}</span>
              <span className="text-base-100 font-medium">{count}</span>
            </div>
          )
        })}
      </div>

      {/* Stage columns */}
      <div className="mt-8 space-y-8">
        {STAGE_ORDER.map((s) => {
          const items = buckets[s]
          if (items.length === 0) return null
          const meta = STAGE_META[s]
          const Icon = meta.icon
          return (
            <section key={s}>
              <div className="flex items-center gap-2 mb-3">
                <Icon size={14} className={meta.color} />
                <h2 className="text-sm font-semibold">{meta.label}</h2>
                <span className="text-xs text-base-500">({items.length})</span>
              </div>
              <div className="space-y-2">
                {items.map((entry, idx) => (
                  <ApplicationCard
                    key={`${entry.role.company}-${entry.role.job_title}-${idx}`}
                    role={entry.role}
                    stage={s}
                    appliedAt={entry.appliedAt}
                    onMoveStage={(newStage) => {
                      updateApplicationStage(entry.role, newStage)
                      toast.success(`Moved to "${STAGE_META[newStage].label}"`, {
                        description: entry.role.job_title,
                      })
                    }}
                    onUnapply={() => {
                      moveAppliedToSaved(entry.role)
                      toast(`Moved back to saved`, { description: entry.role.job_title })
                    }}
                  />
                ))}
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}


function ApplicationCard({
  role, stage, appliedAt, onMoveStage, onUnapply,
}: {
  role: Role
  stage: ApplicationStage
  appliedAt: string
  onMoveStage: (s: ApplicationStage) => void
  onUnapply: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const meta = STAGE_META[stage]

  const visit = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!role.job_url) return
    import('@tauri-apps/plugin-shell')
      .then(({ open }) => open(role.job_url!))
      .catch(() => window.open(role.job_url!, '_blank', 'noopener,noreferrer'))
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="glass-subtle hover:glass rounded-lg px-4 py-3 transition-colors"
    >
      <div className="flex items-center gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-base-100 truncate">{role.job_title}</span>
            <span className={cn('text-[10px] uppercase tracking-wider', meta.color)}>
              {role.company}
            </span>
          </div>
          <div className="flex items-center gap-3 text-[11px] text-base-500 mt-1">
            {role.location_type && <span>{role.location_type}</span>}
            <span>Applied {formatRelativeDate(appliedAt)}</span>
            {role.final_tier && <span className="text-tier-strong">{role.final_tier}</span>}
          </div>
        </div>

        <button
          onClick={visit}
          disabled={!role.job_url}
          className="px-3 py-1.5 rounded text-[11px] font-medium
                     bg-white/[0.04] hover:bg-white/[0.08] text-base-300 disabled:opacity-40
                     border border-white/[0.06]"
        >
          Visit
        </button>

        <div className="relative">
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="px-3 py-1.5 rounded text-[11px] font-medium
                       bg-white/[0.04] hover:bg-white/[0.08] text-base-200
                       border border-white/[0.06] flex items-center gap-1"
          >
            Move to <ChevronDown size={11} />
          </button>
          {menuOpen && (
            <>
              {/* Click-away */}
              <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
              <div className="absolute right-0 top-full mt-1 z-20
                              glass-strong rounded-lg border border-white/[0.10]
                              min-w-[180px] overflow-hidden py-1">
                {STAGE_ORDER.map((s) => {
                  const m = STAGE_META[s]
                  const SIcon = m.icon
                  if (s === stage) return null
                  return (
                    <button
                      key={s}
                      onClick={() => {
                        setMenuOpen(false)
                        onMoveStage(s)
                      }}
                      className="w-full flex items-center gap-2 px-3 py-2
                                 text-left text-xs hover:bg-white/[0.06] transition-colors"
                    >
                      <SIcon size={12} className={m.color} />
                      <span className="text-base-100">{m.label}</span>
                    </button>
                  )
                })}
                <div className="border-t border-white/[0.06] mt-1 pt-1">
                  <button
                    onClick={() => {
                      setMenuOpen(false)
                      onUnapply()
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2
                               text-left text-xs hover:bg-white/[0.06] transition-colors text-base-400"
                  >
                    <Trash2 size={12} />
                    <span>Move back to saved</span>
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </motion.div>
  )
}

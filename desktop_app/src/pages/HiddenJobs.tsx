/**
 * Hidden Jobs page — a dedicated screen for reviewing and managing
 * roles the user clicked "Hide" on. Lives at /hidden.
 *
 * Why a separate page (not just an inline toggle on Dashboard):
 *   When a user unhides a role inline on the Dashboard, the role
 *   re-appears at its original sorted position — usually buried among
 *   30+ other STRONGs. The user has no idea WHICH one they just
 *   unhid. On a dedicated page, the unhidden role drops OUT of the
 *   list and the user stays on the page with their context intact;
 *   they then navigate to the Dashboard to see the role re-listed.
 *
 * Each card uses the standard RoleCard, which already adapts its
 * "Hide" button to "Unhide" when the role is currently hidden — so
 * clicking that button removes the status and the card disappears
 * from this list on next render.
 */
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { EyeOff, Search } from 'lucide-react'
import { RoleCard } from '@/components/RoleCard'
import { useAppStore } from '@/stores/appStore'
import type { Role } from '@/types'

export default function HiddenJobs() {
  const navigate = useNavigate()
  const roleStatuses = useAppStore((s) => s.roleStatuses)

  // Build the hidden-roles list from the status map. We use the stored
  // `roleSnapshot` so the card renders even if the role isn't in the
  // current `lastRoles` (e.g., user hid it during a prior run, then
  // re-ran and the role didn't surface again — the snapshot still has
  // the data).
  const hiddenRoles = Object.values(roleStatuses)
    .filter((e) => e.status === 'hidden' && e.roleSnapshot)
    .map((e) => ({ role: e.roleSnapshot as Role, hiddenAt: e.date }))
    .sort((a, b) => (a.hiddenAt < b.hiddenAt ? 1 : -1))

  if (hiddenRoles.length === 0) {
    return (
      <div className="px-10 py-8 max-w-3xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Hidden Roles</h1>
        <p className="text-sm text-base-400 mt-1">
          Roles you've hidden from the dashboard show up here so you can
          review them later or change your mind.
        </p>
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="glass mt-10 rounded-xl p-12 text-center"
        >
          <div className="w-12 h-12 mx-auto mb-4 rounded-xl bg-white/[0.04] border border-white/[0.06]
                          flex items-center justify-center">
            <EyeOff size={20} className="text-base-400" />
          </div>
          <h2 className="text-lg font-medium">No hidden roles</h2>
          <p className="text-sm text-base-400 mt-1.5 max-w-sm mx-auto">
            Click the eye icon on any role in the dashboard to hide it. Hidden roles
            stay accessible here without cluttering your main results view.
          </p>
          <button
            onClick={() => navigate('/dashboard')}
            className="mt-6 inline-flex items-center gap-2 px-5 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       shadow-lg shadow-black/30"
          >
            <Search size={14} /> Browse roles
          </button>
        </motion.div>
      </div>
    )
  }

  return (
    <div className="px-10 py-8 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-1">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Hidden Roles</h1>
          <p className="text-sm text-base-400 mt-1">
            {hiddenRoles.length} role{hiddenRoles.length === 1 ? '' : 's'} hidden ·
            click "Unhide" on any card to restore it to the dashboard
          </p>
        </div>
      </div>

      <div className="mt-8 space-y-2.5">
        {hiddenRoles.map((entry, idx) => (
          <motion.div
            key={`${entry.role.company}-${entry.role.job_title}-${idx}`}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: Math.min(idx * 0.02, 0.5) }}
          >
            <RoleCard role={entry.role} />
          </motion.div>
        ))}
      </div>
    </div>
  )
}

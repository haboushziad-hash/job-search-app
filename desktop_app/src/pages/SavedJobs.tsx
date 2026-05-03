import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Bookmark, Search } from 'lucide-react'
import { RoleCard } from '@/components/RoleCard'
import { useAppStore } from '@/stores/appStore'
import type { Role } from '@/types'

/** Saved Jobs page — shows roles the user has bookmarked via the save button.
 *  Sorted by save date (newest first). Empty state with CTA to dashboard. */
export default function SavedJobs() {
  const navigate = useNavigate()
  const roleStatuses = useAppStore((s) => s.roleStatuses)

  const savedRoles = useMemo<Array<{ role: Role; savedAt: string }>>(() => {
    return Object.values(roleStatuses)
      .filter((e) => e.status === 'saved' && e.roleSnapshot)
      .map((e) => ({
        role: e.roleSnapshot as Role,
        savedAt: e.date,
      }))
      .sort((a, b) => (a.savedAt < b.savedAt ? 1 : -1))
  }, [roleStatuses])

  if (savedRoles.length === 0) {
    return (
      <div className="px-10 py-8 max-w-3xl mx-auto">
        <h1 className="text-2xl font-semibold tracking-tight">Saved Jobs</h1>
        <p className="text-sm text-base-400 mt-1">
          Bookmark roles from the dashboard to find them here later.
        </p>
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="glass mt-10 rounded-xl p-12 text-center"
        >
          <div className="w-12 h-12 mx-auto mb-4 rounded-xl bg-white/[0.04] border border-white/[0.06]
                          flex items-center justify-center">
            <Bookmark size={20} className="text-base-400" />
          </div>
          <h2 className="text-lg font-medium">No saved jobs yet</h2>
          <p className="text-sm text-base-400 mt-1.5 max-w-sm mx-auto">
            Click the bookmark icon on any role in the dashboard to save it for later review.
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
          <h1 className="text-2xl font-semibold tracking-tight">Saved Jobs</h1>
          <p className="text-sm text-base-400 mt-1">
            {savedRoles.length} role{savedRoles.length === 1 ? '' : 's'} bookmarked · sorted by save date
          </p>
        </div>
      </div>

      <div className="mt-8 space-y-2.5">
        {savedRoles.map((entry, idx) => (
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

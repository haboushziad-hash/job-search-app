import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import {
  Compass, PlayCircle, Briefcase,
  History, BookmarkCheck, EyeOff, Settings, MessageCircle, Loader2,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { motion } from 'framer-motion'
import { ZMark } from './ZMark'
import { useAppVersion } from '@/lib/updater'
import { useAppStore } from '@/stores/appStore'
import { useActiveSearchStatus } from '@/hooks/useActiveSearchPoll'

interface NavItem {
  to: string
  label: string
  icon: React.ElementType
}

const NAV: NavItem[] = [
  { to: '/dashboard',  label: 'Dashboard',       icon: Compass },
  { to: '/run',        label: 'New Search',      icon: PlayCircle },
  { to: '/saved-jobs', label: 'Saved Jobs',      icon: BookmarkCheck },
  { to: '/tracker',    label: 'Applications',    icon: Briefcase },
  { to: '/history',    label: 'Run History',     icon: History },
  { to: '/hidden',     label: 'Hidden Roles',    icon: EyeOff },
  { to: '/feedback',   label: 'Send Feedback',   icon: MessageCircle },
]

export function Sidebar() {
  const location = useLocation()
  const navigate = useNavigate()
  const version = useAppVersion()
  const activeRunId = useAppStore((s) => s.activeRunId)
  const status = useActiveSearchStatus()
  const showRunningIndicator = !!activeRunId && status?.status === 'running'

  return (
    <aside className="glass-strong w-60 h-full flex flex-col border-r border-white/[0.06] relative z-10">
      {/* Brand — Z mark + product name. The mark matches the app icon
          shipped in src-tauri/icons (Tauri reads those for taskbar/dock). */}
      <div className="px-5 pt-5 pb-5">
        <div className="flex items-center gap-2.5">
          <ZMark size={28} />
          <span className="font-semibold text-sm tracking-tight">findmesomedamnjobz</span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2.5 space-y-0.5">
        {NAV.map((item) => {
          const Icon = item.icon
          // Exact-match active state so prefixes don't bleed (e.g. /run
          // shouldn't highlight when the user is on /running). Pathname
          // must equal the route exactly (or be the route + a trailing
          // slash).
          const active = (
            location.pathname === item.to ||
            location.pathname === item.to + "/"
          )
          return (
            <NavLink
              key={item.to}
              to={item.to}
              className={cn(
                'group flex items-center gap-3 px-3 py-2 rounded-lg text-sm relative',
                'transition-colors',
                active
                  ? 'text-base-50'
                  : 'text-base-300 hover:text-base-100 hover:bg-white/[0.03]'
              )}
            >
              {active && (
                <motion.div
                  layoutId="active-nav"
                  className="absolute inset-0 rounded-lg bg-white/[0.06] border border-white/[0.08]"
                  transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                />
              )}
              <Icon size={16} className="relative z-10" />
              <span className="relative z-10">{item.label}</span>
            </NavLink>
          )
        })}
      </nav>

      {/* Active search indicator — shows whenever a search is running.
          Click to navigate back to the Running page. Lets the user
          "Continue in background" without losing track of their search. */}
      {showRunningIndicator && (
        <button
          onClick={() => navigate('/running')}
          className="mx-2.5 mb-2 px-3 py-2.5 rounded-lg flex items-center gap-2.5
                     bg-accent-500/[0.08] border border-accent-500/30
                     hover:bg-accent-500/[0.14] hover:border-accent-500/50
                     transition-colors text-left group"
          title="Click to view running search"
        >
          <motion.div
            animate={{ rotate: 360 }}
            transition={{ repeat: Infinity, duration: 1.4, ease: 'linear' }}
            className="flex-shrink-0"
          >
            <Loader2 size={14} className="text-accent-300" />
          </motion.div>
          <div className="flex-1 min-w-0">
            <div className="text-[11px] font-medium text-accent-200 truncate">
              Search running
            </div>
            <div className="text-[10px] text-accent-300/70 truncate">
              {Math.round(status?.progress ?? 0)}% &middot; click to view
            </div>
          </div>
        </button>
      )}

      {/* Settings (bottom) */}
      <div className="px-2.5 pb-4 pt-2 border-t border-white/[0.04]">
        <NavLink
          to="/settings"
          className={({ isActive }) =>
            cn(
              'flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors',
              isActive
                ? 'text-base-50 bg-white/[0.06]'
                : 'text-base-400 hover:text-base-200 hover:bg-white/[0.03]'
            )
          }
        >
          <Settings size={16} />
          <span>Settings</span>
        </NavLink>

        <div className="px-3 pt-4 text-[10px] text-base-500 leading-tight">
          <div>findmesomedamnjobz v{version}</div>
          <div className="text-base-600">
            Built by{' '}
            <span className="text-base-400 font-medium">Ziad Haboush</span>
          </div>
        </div>
      </div>
    </aside>
  )
}

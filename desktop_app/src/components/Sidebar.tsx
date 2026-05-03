import { NavLink, useLocation } from 'react-router-dom'
import {
  Sparkles, Compass, PlayCircle, Briefcase,
  History, Bookmark, BookmarkCheck, Settings,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { motion } from 'framer-motion'

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
  { to: '/saved',      label: 'Saved Searches',  icon: Bookmark },
]

export function Sidebar() {
  const location = useLocation()

  return (
    <aside className="glass-strong w-60 h-full flex flex-col border-r border-white/[0.06] relative z-10">
      {/* Brand */}
      <div className="px-5 pt-5 pb-5">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-accent-400 to-accent-700
                          flex items-center justify-center shadow-lg shadow-accent-900/40">
            <Sparkles size={15} className="text-white" />
          </div>
          <span className="font-semibold text-sm tracking-tight">Job Search</span>
        </div>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-2.5 space-y-0.5">
        {NAV.map((item) => {
          const Icon = item.icon
          // Exact-match active state so /saved doesn't activate when the
          // user is on /saved-jobs. Pathname must equal the route exactly
          // (or be the route + a trailing slash).
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
          <div>Job Search v0.1.0</div>
          <div className="text-base-600">
            Built by{' '}
            <span className="text-base-400 font-medium">Ziad Haboush</span>
          </div>
        </div>
      </div>
    </aside>
  )
}

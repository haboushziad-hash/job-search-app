import { Routes, Route, Navigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'

import { Sidebar } from '@/components/Sidebar'
import { useAppStore } from '@/stores/appStore'

import Welcome from '@/pages/Welcome'
import HowItWorks from '@/pages/HowItWorks'
import Setup from '@/pages/Setup'
import Building from '@/pages/Building'
import Keywords from '@/pages/Keywords'
import Dashboard from '@/pages/Dashboard'
import Run from '@/pages/Run'
import Running from '@/pages/Running'
import Tracker from '@/pages/Tracker'
import History from '@/pages/History'
import SavedSearches from '@/pages/SavedSearches'
import SavedJobs from '@/pages/SavedJobs'
import Settings from '@/pages/Settings'
import Feedback from '@/pages/Feedback'

export default function App() {
  return (
    <Routes>
      {/* Root redirect: returning users with prior runs land on the
          dashboard; first-timers see the Welcome onboarding flow. */}
      <Route path="/" element={<RootRedirect />} />
      {/* Standalone screens — no sidebar */}
      <Route path="/welcome" element={<Welcome />} />
      <Route path="/how-it-works" element={<HowItWorks />} />
      <Route path="/setup" element={<Setup />} />
      <Route path="/building" element={<Building />} />
      <Route path="/keywords" element={<Keywords />} />
      <Route path="/running" element={<Running />} />

      {/* In-app screens — with sidebar */}
      <Route path="*" element={<AppShell />} />
    </Routes>
  )
}

/** Decides where "/" should land:
 *   - User has a saved profile + at least one run → /dashboard (their results)
 *   - User has a profile but no run yet            → /run (start their first search)
 *   - Brand new user                               → /welcome (resume upload flow)
 */
function RootRedirect() {
  // Always land on the Welcome (home) page on app launch. The Welcome page
  // itself handles the "you've run before" case by showing both
  // "Run a new search" AND "View previous results" CTAs side-by-side.
  // Previously we'd auto-bounce to /dashboard which felt jarring — users
  // landed in raw results instead of the brand surface.
  return <Navigate to="/welcome" replace />
}

function AppShell() {
  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar />

      <main className="flex-1 overflow-y-auto relative">
        <AnimatePresence mode="wait">
          <Routes>
            <Route path="/dashboard" element={<AnimatedPage><Dashboard /></AnimatedPage>} />
            <Route path="/run" element={<AnimatedPage><Run /></AnimatedPage>} />
            <Route path="/tracker" element={<AnimatedPage><Tracker /></AnimatedPage>} />
            <Route path="/history" element={<AnimatedPage><History /></AnimatedPage>} />
            <Route path="/saved-jobs" element={<AnimatedPage><SavedJobs /></AnimatedPage>} />
            <Route path="/saved" element={<AnimatedPage><SavedSearches /></AnimatedPage>} />
            <Route path="/settings" element={<AnimatedPage><Settings /></AnimatedPage>} />
            <Route path="/feedback" element={<AnimatedPage><Feedback /></AnimatedPage>} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </AnimatePresence>
      </main>
    </div>
  )
}

function AnimatedPage({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -8 }}
      transition={{ duration: 0.18, ease: 'easeOut' }}
      className="h-full"
    >
      {children}
    </motion.div>
  )
}

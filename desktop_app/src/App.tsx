import { useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'

import { Sidebar } from '@/components/Sidebar'
import { checkForUpdates } from '@/lib/updater'
import { useActiveSearchPoll } from '@/hooks/useActiveSearchPoll'
import { listRuns, getRun } from '@/services/api'
import { useAppStore } from '@/stores/appStore'
import type { Role, RunSummary } from '@/types'

import Welcome from '@/pages/Welcome'
import HowItWorks from '@/pages/HowItWorks'
import StartSearch from '@/pages/StartSearch'
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
import HiddenJobs from '@/pages/HiddenJobs'
import Settings from '@/pages/Settings'
import Feedback from '@/pages/Feedback'

export default function App() {
  // Check for updates once shortly after the app mounts. The 3s delay
  // gives the UI a moment to settle and the backend a moment to spin up
  // before we hit the network for the manifest. Errors are swallowed
  // inside checkForUpdates so a flaky connection won't break startup.
  useEffect(() => {
    const t = setTimeout(() => {
      checkForUpdates()
    }, 3000)
    return () => clearTimeout(t)
  }, [])

  // Force-maximize on every app mount. The `maximized: true` setting in
  // tauri.conf.json should already handle this on cold launches, but in
  // practice it's inconsistent across some restart paths (e.g. Cargo
  // recompile in dev mode, OS window-state retention) — the user can
  // open the app and find it back at its 1400x900 default. Calling
  // `maximize()` explicitly here is idempotent and guarantees a
  // consistent maximized launch every time. Wrapped in try/catch so
  // we don't break running in a browser dev preview where Tauri's
  // window API isn't loaded.
  useEffect(() => {
    ;(async () => {
      try {
        const { getCurrentWindow } = await import('@tauri-apps/api/window')
        await getCurrentWindow().maximize()
      } catch {
        // Not in Tauri (browser preview) or window plugin unavailable —
        // safely skip.
      }
    })()
  }, [])

  // Global active-search poll — runs regardless of route so "Continue in
  // background" actually works. Fires the success toast + updates results
  // when the search completes, even if the user has navigated away from
  // /running.
  useActiveSearchPoll()

  // Session-startup hidden-roles cleanup: drop any "hidden" status
  // older than 4 weeks. Hidden roles are otherwise sticky forever —
  // weekly searches accumulate stale hides that clutter the Hidden
  // Roles page and stay suppressed from results long after they're
  // relevant. 4 weeks is the same window as our default
  // `posted_within_days=30` filter, so a role we hide today stops
  // being a candidate on the user's next-month searches anyway.
  //
  // Implementation: inspect roleStatuses, expire any 'hidden' entries
  // older than 28 days by calling unsaveRole (which removes the
  // status entirely; the role re-appears in normal results if it's
  // still scraped).
  useEffect(() => {
    const FOUR_WEEKS_MS = 28 * 24 * 60 * 60 * 1000
    const now = Date.now()
    const statuses = useAppStore.getState().roleStatuses
    const unsaveRole = useAppStore.getState().unsaveRole
    let expired = 0
    for (const entry of Object.values(statuses)) {
      if (entry.status !== 'hidden') continue
      const ts = Date.parse(entry.date)
      if (Number.isNaN(ts)) continue
      if (now - ts > FOUR_WEEKS_MS && entry.roleSnapshot) {
        unsaveRole(entry.roleSnapshot)
        expired += 1
      }
    }
    if (expired > 0) {
      console.info(`[hidden-cleanup] expired ${expired} hidden role(s) older than 4 weeks`)
    }
  }, [])

  // Session-startup sync: on each app launch (this effect fires once per
  // mounted App), reconcile the persisted `lastResults` in localStorage
  // with the actual most-recent completed run in runs.db. This stops the
  // dashboard from showing a stale historical run that the user clicked
  // into mid-session and never explicitly reset.
  //
  // Behavior:
  //   - If runs.db has no completed runs → no-op (first install / dev wipe).
  //   - If the most-recent run_id matches what's already in localStorage
  //     → no-op (we're already showing it).
  //   - Otherwise → fetch that run's roles + summary and seed lastResults
  //     so the Dashboard renders the latest data on first navigation.
  //
  // Manual switching via History.tsx still works: that calls setLastResults
  // explicitly during the session, and this sync only runs ONCE on app
  // launch — so a user can click into a past run and stay there for the
  // rest of the session without this hook overriding them.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { runs } = await listRuns()
        if (cancelled || !runs || runs.length === 0) return
        const mostRecent = runs[0]
        const currentRunId = useAppStore.getState().lastSummary?.run_id
        if (mostRecent.run_id === currentRunId) return
        const data = await getRun(mostRecent.run_id)
        if (cancelled) return
        const roles = data.roles as unknown as Role[]
        const summary = data.summary as unknown as RunSummary
        useAppStore.getState().setLastResults(roles, summary)
      } catch (e) {
        // Silent fail — backend may be slow to start, or first launch may
        // not have an archive yet. Keep whatever's in localStorage.
        console.warn('[startup-sync] failed to sync to most recent run:', e)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <Routes>
      {/* Root redirect: returning users with prior runs land on the
          dashboard; first-timers see the Welcome onboarding flow. */}
      <Route path="/" element={<RootRedirect />} />
      {/* Standalone screens — no sidebar */}
      <Route path="/welcome" element={<Welcome />} />
      <Route path="/how-it-works" element={<HowItWorks />} />
      <Route path="/start-search" element={<StartSearch />} />
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
            <Route path="/hidden" element={<AnimatedPage><HiddenJobs /></AnimatedPage>} />
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

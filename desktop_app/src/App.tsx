import { useEffect, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertCircle, Loader2, Copy, Check } from 'lucide-react'
import { toast } from 'sonner'

import { Sidebar } from '@/components/Sidebar'
import { ZMark } from '@/components/ZMark'
import { checkForUpdates } from '@/lib/updater'
import { useActiveSearchPoll } from '@/hooks/useActiveSearchPoll'
import { listRuns, getRun, healthCheck, getLastBuiltProfile } from '@/services/api'
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
import SavedJobs from '@/pages/SavedJobs'
import HiddenJobs from '@/pages/HiddenJobs'
import Stats from '@/pages/Stats'
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

  // v0.3.23 (FIX 28 — profile auto-heal): on launch, if the persisted
  // profile is null but the backend has previously built profiles, recover
  // the most recent one. This self-heals the origin-partition / zustand
  // rehydration edge case where the UI loses its `profile` field (which
  // hides the re-run option behind the "fresh search" gate) even though
  // the user has a profile + run history. The backend's
  // profile_build_cache.db persists every profile ever built in %APPDATA%,
  // independent of the WebView localStorage origin, so it's the reliable
  // recovery source. No-op for genuine first-time users (cache empty →
  // profile stays null → normal setup flow). Only heals when the in-memory
  // profile is STILL null at resolve time (guards against racing a profile
  // the user just built this session).
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        if (useAppStore.getState().profile) return // already have one
        const { profile } = await getLastBuiltProfile()
        if (cancelled || !profile) return
        // Only heal a non-trivial profile (has keywords) and only if the
        // store is still empty — don't clobber a fresh build mid-session.
        const kwCount = (profile.keywords as unknown[] | undefined)?.length ?? 0
        if (kwCount > 0 && !useAppStore.getState().profile) {
          useAppStore.getState().setProfile(profile)
          console.info('[profile-heal] recovered profile from backend cache (localStorage was null)')
        }
      } catch (e) {
        // Backend slow/unavailable or cache empty — keep null, setup flow runs.
        console.warn('[profile-heal] could not recover profile:', e)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <BackendGate>
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
    </BackendGate>
  )
}

/**
 * Backend availability gate.
 *
 * Polls the local FastAPI sidecar's /health endpoint on app startup. If
 * the backend is reachable within ~30s, renders children (normal app).
 * If not, renders a fatal error screen with diagnostic info instead of
 * letting users hit "Load failed" toasts at every interaction.
 *
 * Why we need this: the Tauri shell plugin spawns the Python sidecar
 * out-of-process. On Mac, hardened-runtime + library validation issues
 * can cause the sidecar to crash on launch (PyInstaller's dlopen of
 * extracted .dylibs in /var/folders/.../_MEIxxxxx fails despite
 * disable-library-validation entitlement). Without this gate, the user
 * navigates around the app for a while and only discovers the backend
 * is dead when they hit Setup → Build Profile and see "Load failed".
 *
 * With this gate, the failure surfaces on first launch with the log
 * path users can grab and send to us.
 */
function BackendGate({ children }: { children: React.ReactNode }) {
  // 'checking' = polling, 'up' = render app, 'down' = render error
  const [status, setStatus] = useState<'checking' | 'up' | 'down'>('checking')
  const [lastError, setLastError] = useState<string>('')
  const [retryCount, setRetryCount] = useState(0)
  const [logCopied, setLogCopied] = useState(false)

  /** Read the log file via Tauri's fs plugin, then copy contents to clipboard.
   *  Saves the tester from having to find the log file manually — they hit
   *  the button, paste into a chat with us, and we have everything we need. */
  async function copyLogToClipboard() {
    try {
      const { appLogDir } = await import('@tauri-apps/api/path')
      const { readTextFile } = await import('@tauri-apps/plugin-fs')

      const logDir = await appLogDir()
      // Tauri's TargetKind::LogDir with file_name=None defaults to the
      // productName from tauri.conf.json. The .log extension is added
      // automatically. So our file is `<logDir>/findmesomedamnjobz.log`.
      const logPath = `${logDir}/findmesomedamnjobz.log`
      const contents = await readTextFile(logPath)

      // Try modern clipboard API first; fall back to legacy textarea trick
      // for older WebKit if needed (rare but worth covering since this
      // path matters most on Mac WebKit).
      try {
        await navigator.clipboard.writeText(contents)
      } catch {
        const ta = document.createElement('textarea')
        ta.value = contents
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        document.body.removeChild(ta)
      }

      setLogCopied(true)
      toast.success(`Copied log file (${(contents.length / 1024).toFixed(1)}KB) to clipboard`)
      // Reset the button state after 3s so the user can copy again if they
      // accidentally lose the clipboard contents.
      setTimeout(() => setLogCopied(false), 3000)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      toast.error(`Could not read log file: ${msg}`)
    }
  }

  useEffect(() => {
    let cancelled = false
    let attempts = 0
    // Up to 60 attempts × 500ms = 30s wait. PyInstaller cold-starts can
    // take 5-15s on Mac (binary unpack + Python interpreter init), so
    // we give it a generous window before declaring the backend dead.
    const MAX_ATTEMPTS = 60
    const POLL_MS = 500

    const tick = async () => {
      if (cancelled) return
      attempts += 1
      try {
        await healthCheck()
        if (!cancelled) setStatus('up')
        return
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        if (!cancelled) setLastError(msg)
        if (attempts >= MAX_ATTEMPTS) {
          if (!cancelled) setStatus('down')
          return
        }
        setTimeout(tick, POLL_MS)
      }
    }
    tick()
    return () => { cancelled = true }
  }, [retryCount])

  if (status === 'up') return <>{children}</>

  if (status === 'checking') {
    return (
      <div className="fixed inset-0 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4 text-center">
          <ZMark size={64} />
          <Loader2 size={20} className="text-base-400 animate-spin" />
          <p className="text-sm text-base-400">Starting backend…</p>
        </div>
      </div>
    )
  }

  // status === 'down' — show diagnostic error screen
  return (
    <div className="fixed inset-0 flex items-center justify-center p-6">
      <div className="glass-strong w-full max-w-xl rounded-2xl p-9">
        <div className="flex items-center gap-3 mb-4">
          <ZMark size={40} />
          <h1 className="text-2xl font-semibold tracking-tight">Backend not responding</h1>
        </div>
        <div className="flex items-start gap-3 p-4 rounded-lg bg-red-500/10 border border-red-500/30 mb-4">
          <AlertCircle size={16} className="text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-base-100 font-medium">
              The local search backend didn't start within 30 seconds.
            </p>
            <p className="text-xs text-base-400 mt-1 leading-relaxed font-mono break-all">
              {lastError || 'Unknown error'}
            </p>
          </div>
        </div>

        <div className="text-sm text-base-300 space-y-3 leading-relaxed">
          <p className="font-medium text-base-100">What to do:</p>
          <ol className="list-decimal list-inside space-y-1 text-base-300">
            <li>Click <span className="font-medium">Try again</span> below.</li>
            <li>If it still fails, fully quit the app (Cmd+Q on Mac, close window on Windows) and re-launch.</li>
            <li>
              If still failing, click <span className="font-medium">Copy log</span> below
              and paste it to support — we'll diagnose from the log contents directly.
            </li>
          </ol>
        </div>

        <div className="flex gap-3 mt-6">
          <button
            onClick={() => {
              setStatus('checking')
              setLastError('')
              setRetryCount((c) => c + 1)
            }}
            className="flex-1 px-4 py-2.5 rounded-lg bg-white text-base-950 font-medium text-sm hover:bg-base-200 transition-colors"
          >
            Try again
          </button>
          <button
            onClick={copyLogToClipboard}
            className="flex-1 px-4 py-2.5 rounded-lg bg-white/10 hover:bg-white/15 border border-white/15 text-base-100 font-medium text-sm transition-colors flex items-center justify-center gap-2"
          >
            {logCopied ? <Check size={14} /> : <Copy size={14} />}
            {logCopied ? 'Copied!' : 'Copy log'}
          </button>
        </div>
      </div>
    </div>
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
            <Route path="/stats" element={<AnimatedPage><Stats /></AnimatedPage>} />
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

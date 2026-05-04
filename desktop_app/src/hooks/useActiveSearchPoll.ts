/**
 * Global active-search polling hook.
 *
 * Runs at the App root (App.tsx) so the search keeps being polled regardless
 * of which page is mounted. Replaces the per-page polling that lived inside
 * Running.tsx — that polling died when the user clicked "Continue in
 * background" and navigated to Dashboard, leaving them with no way to know
 * when the search finished or to navigate back.
 *
 * Behavior:
 *   - When `activeRunId` is set in the store, polls /search/status/{id}
 *     every 2 seconds.
 *   - On 'completed': pulls /search/results, updates lastResults, fires a
 *     sonner toast ("Search complete · N qualifying roles found"), clears
 *     activeRunId, and surfaces a "View results" CTA.
 *   - On 'failed': fires a destructive toast and clears activeRunId.
 *   - On 'cancelled': fires an info toast and clears activeRunId.
 *   - The latest status (with progress + step + detail) is exposed via the
 *     useActiveSearchStatus() selector so the sidebar indicator and the
 *     Running page can both subscribe to it.
 *
 * Keeps a single source of truth in module-scope state so multiple consumers
 * share the same poll cycle. The hook itself is a no-op when activeRunId is
 * null.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { useAppStore } from '@/stores/appStore'
import { getSearchStatus, getSearchResults, ApiError } from '@/services/api'
import type { SearchStatus } from '@/services/api'

// Module-scope subscribers — every hook call uses the same status snapshot.
let _status: SearchStatus | null = null
const _subscribers = new Set<(s: SearchStatus | null) => void>()

function _setStatus(s: SearchStatus | null) {
  _status = s
  _subscribers.forEach((sub) => {
    try {
      sub(s)
    } catch {
      // swallow individual subscriber errors
    }
  })
}

/** Subscribe to the live status. Used by Sidebar indicator + Running page. */
export function useActiveSearchStatus(): SearchStatus | null {
  const [s, setS] = useState<SearchStatus | null>(_status)
  useEffect(() => {
    _subscribers.add(setS)
    return () => {
      _subscribers.delete(setS)
    }
  }, [])
  return s
}

/**
 * Mount this ONCE at the App root. Polls /search/status as long as
 * activeRunId is set, regardless of route.
 */
export function useActiveSearchPoll(): void {
  const runId = useAppStore((s) => s.activeRunId)
  const setActiveRunId = useAppStore((s) => s.setActiveRunId)
  const setLastResults = useAppStore((s) => s.setLastResults)
  const navigate = useNavigate()
  const completedRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!runId) {
      // Intentionally do NOT clear `_status` here. When a run completes,
      // tick() sets status='completed' and then clears activeRunId. If
      // we cleared status on this effect re-run (triggered by the runId
      // change), Running.tsx briefly sees status=null AND runId=null
      // and races to navigate('/run') — defeating the intended
      // post-completion redirect to /dashboard. Leaving the last status
      // in place lets consumers detect "we just finished" reliably.
      // A new run will overwrite status on the next tick() anyway.
      return
    }
    if (completedRef.current.has(runId)) {
      // Already finalized this run; do nothing.
      return
    }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    const tick = async () => {
      if (cancelled) return
      try {
        const s = await getSearchStatus(runId)
        if (cancelled) return
        _setStatus(s)

        if (s.status === 'completed') {
          completedRef.current.add(runId)
          try {
            const results = await getSearchResults(runId)
            if (!cancelled) {
              setLastResults(results.roles, results.summary)
              toast.success(
                `Search complete · ${results.roles.length} qualifying roles`,
                {
                  // Toast persists until user dismisses or clicks View
                  duration: 10_000,
                  action: {
                    label: 'View results',
                    onClick: () => navigate('/dashboard'),
                  },
                }
              )
            }
          } catch (e) {
            if (!cancelled) {
              toast.error('Search finished but results failed to load', {
                description: e instanceof ApiError ? e.detail : 'Unknown error',
              })
            }
          }
          if (!cancelled) setActiveRunId(null)
          return
        }

        if (s.status === 'failed') {
          completedRef.current.add(runId)
          toast.error('Search failed', {
            description: s.error || 'Unknown error',
            duration: 12_000,
          })
          setActiveRunId(null)
          return
        }

        if (s.status === 'cancelled') {
          completedRef.current.add(runId)
          // No toast here — the cancel handler in Running.tsx already
          // shows one. Just clear.
          setActiveRunId(null)
          return
        }

        // Keep polling
        timer = setTimeout(tick, 2000)
      } catch (e) {
        if (cancelled) return
        // Network blip — back off slightly and try again rather than
        // bouncing the user to an error state.
        timer = setTimeout(tick, 5000)
      }
    }

    tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [runId, navigate, setActiveRunId, setLastResults])
}

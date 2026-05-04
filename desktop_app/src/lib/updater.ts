/**
 * Auto-updater plumbing.
 *
 * Two pieces:
 *
 *   useAppVersion()   — React hook returning the running binary's version
 *                       (read from Tauri's runtime, NOT a hardcoded const).
 *                       Use this everywhere we display a version chip so
 *                       the next install's version flows through automatically.
 *
 *   checkForUpdates() — Pings the Worker manifest endpoint via Tauri's
 *                       updater plugin. If a newer signed bundle is
 *                       available, shows a sonner toast with an Install
 *                       action that downloads + applies + restarts the app.
 *
 * Why a thin wrapper instead of calling the plugin directly: we need to
 * gracefully handle the dev-server case (Tauri APIs aren't available in
 * `npm run dev` outside the webview) and centralize the toast UX.
 */

import { useEffect, useState } from 'react'
import { getVersion } from '@tauri-apps/api/app'
import { check } from '@tauri-apps/plugin-updater'
import { relaunch } from '@tauri-apps/plugin-process'
import { toast } from 'sonner'

// Fallback string used in browser-only environments (Storybook, tests, or
// `vite` running outside the Tauri shell). Never visible in the shipped app.
const FALLBACK_VERSION = '0.0.0-dev'

/**
 * Returns the binary version baked into Cargo.toml / tauri.conf.json /
 * package.json (they're kept in sync by Tauri's release process).
 *
 * On first render we return the fallback briefly while the IPC call
 * resolves, then the real version. In practice this is so fast that
 * components rendering the version chip don't notice a flicker.
 */
export function useAppVersion(): string {
  const [version, setVersion] = useState<string>(FALLBACK_VERSION)
  useEffect(() => {
    let cancelled = false
    getVersion()
      .then((v) => {
        if (!cancelled) setVersion(v)
      })
      .catch(() => {
        // Outside Tauri shell — keep fallback. This branch only fires
        // during pure-Vite preview, which we never ship.
      })
    return () => {
      cancelled = true
    }
  }, [])
  return version
}

/**
 * Polls the manifest endpoint for a newer signed bundle. If found,
 * shows a sonner toast with a single "Install" action. Click → download
 * with progress → apply → relaunch the app on the new version.
 *
 * Called once shortly after app startup (App.tsx), then never again
 * during the same session — users who dismiss the toast see it again
 * on next launch, which matches what most users expect from an updater.
 *
 * Errors are swallowed silently. The point of an updater is "if you
 * happen to be online and a new build exists, you get it" — not "show
 * the user a scary error if their network is flaky." If something's
 * truly broken on the manifest side, we'll see it server-side.
 */
let updateCheckRan = false

export async function checkForUpdates(): Promise<void> {
  if (updateCheckRan) return
  updateCheckRan = true
  try {
    const update = await check()
    if (!update) return // already on latest

    // Tauri 2's check() returns null when no update is available, or an
    // Update object when one exists. The object exposes .version and a
    // downloadAndInstall() method that we hook into the toast action.
    toast(`Update available: v${update.version}`, {
      description:
        update.body ||
        'A new version is ready to install. Click Install to download and restart.',
      duration: Infinity, // user must dismiss or act on it
      action: {
        label: 'Install',
        onClick: async () => {
          // Show progress while the bundle downloads, then relaunch on
          // the new binary. This is the dramatic "your app updated and
          // restarted itself" moment — users see a quick toast, the app
          // closes, and reopens on the new version with state preserved
          // (Zustand's localStorage persist handles state continuity).
          const progressToast = toast.loading('Downloading update...')
          try {
            await update.downloadAndInstall((event) => {
              switch (event.event) {
                case 'Started':
                  toast.loading(`Downloading update — ${formatBytes(event.data.contentLength)}`, {
                    id: progressToast,
                  })
                  break
                case 'Progress':
                  // Tauri 2 emits incremental progress; we don't need to
                  // re-render every chunk — the loading spinner is enough.
                  break
                case 'Finished':
                  toast.success('Update installed — restarting...', { id: progressToast })
                  break
              }
            })
            await relaunch()
          } catch (e) {
            const msg = e instanceof Error ? e.message : 'Update failed'
            toast.error(`Update failed: ${msg}`, { id: progressToast })
          }
        },
      },
    })
  } catch (e) {
    // Manifest fetch failed (offline, Worker hiccup) — ignore. We'll
    // try again next session.
    console.warn('[updater] check failed:', e)
  }
}

function formatBytes(bytes: number | undefined | null): string {
  if (!bytes || bytes <= 0) return '...'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Folder, Clock, RotateCcw, Check } from 'lucide-react'
import { toast } from 'sonner'
import { useAppStore } from '@/stores/appStore'
import { getAuditFolder, setAuditFolder } from '@/services/api'

export default function Settings() {
  const cacheMaxAgeDays = useAppStore((s) => s.cacheMaxAgeDays)
  const setCacheMaxAgeDays = useAppStore((s) => s.setCacheMaxAgeDays)

  const [folderPath, setFolderPath] = useState<string>('')
  const [folderInput, setFolderInput] = useState<string>('')
  const [savingFolder, setSavingFolder] = useState(false)

  useEffect(() => {
    getAuditFolder()
      .then((r) => {
        setFolderPath(r.path)
        setFolderInput(r.path)
      })
      .catch(() => {})
  }, [])

  const saveFolder = async () => {
    if (!folderInput.trim()) return
    setSavingFolder(true)
    try {
      const r = await setAuditFolder(folderInput.trim())
      setFolderPath(r.path)
      toast.success('Audit folder updated')
    } catch (e) {
      toast.error(`Failed to set folder: ${e instanceof Error ? e.message : 'unknown'}`)
    } finally {
      setSavingFolder(false)
    }
  }

  return (
    <div className="px-10 py-8 max-w-3xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
      <p className="text-sm text-base-400 mt-1">Where the app saves its data and how aggressively it caches.</p>

      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="mt-8 space-y-6"
      >
        {/* Audit folder */}
        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-2 mb-3">
            <Folder size={16} className="text-accent-400" />
            <h2 className="text-sm font-medium">Audit folder</h2>
          </div>
          <p className="text-xs text-base-400 mb-4 leading-relaxed">
            All search runs, the role archive (<code className="text-base-200">runs.db</code>), and
            human-readable summaries get saved here. Point this at a folder that syncs to
            OneDrive or Google Drive — that way your audit data is backed up and (if multiple
            testers point to the same parent folder) the app automatically learns from
            everyone's runs.
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={folderInput}
              onChange={(e) => setFolderInput(e.target.value)}
              placeholder="C:\Users\you\OneDrive\JobSearchApp\testers\you"
              className="flex-1 px-3 py-2 rounded-lg bg-white/[0.04] border border-white/[0.10]
                         text-sm text-base-100 placeholder:text-base-600
                         focus:outline-none focus:border-accent-500/50"
            />
            <button
              onClick={saveFolder}
              disabled={savingFolder || !folderInput.trim() || folderInput.trim() === folderPath}
              className="px-4 py-2 rounded-lg bg-white text-base-950 text-sm font-medium
                         hover:bg-base-200 transition-colors
                         disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {savingFolder ? 'Saving…' : 'Save'}
            </button>
          </div>
          {folderPath && (
            <p className="text-[11px] text-base-500 mt-2 flex items-center gap-1.5">
              <Check size={11} className="text-tier-strong" />
              Active: <code className="text-base-300">{folderPath}</code>
            </p>
          )}
        </div>

        {/* Cache duration */}
        <div className="glass rounded-xl p-6">
          <div className="flex items-center gap-2 mb-3">
            <Clock size={16} className="text-accent-400" />
            <h2 className="text-sm font-medium">Cache duration</h2>
          </div>
          <p className="text-xs text-base-400 mb-4 leading-relaxed">
            How long results stay cached for the same profile. Repeat searches with unchanged
            keywords/preferences within this window are returned in seconds instead of
            re-running the full 12–25 minute pipeline. Force-refresh on the dashboard
            bypasses the cache.
          </p>
          <div className="flex gap-2">
            {[1, 3, 7, 14, 30].map((days) => (
              <button
                key={days}
                onClick={() => {
                  setCacheMaxAgeDays(days)
                  toast.success(`Cache window set to ${days} days`)
                }}
                className={`px-4 py-2 rounded-lg text-sm transition-colors ${
                  cacheMaxAgeDays === days
                    ? 'bg-accent-500/20 text-accent-200 border border-accent-500/40'
                    : 'glass-subtle hover:bg-white/[0.06] text-base-300 border border-transparent'
                }`}
              >
                {days}d
              </button>
            ))}
          </div>
          <p className="text-[11px] text-base-500 mt-2">
            Cache invalidates automatically if your profile, keywords, or preferences change.
          </p>
        </div>

        {/* Cross-tester learning info */}
        <div className="glass-subtle rounded-xl p-6">
          <div className="flex items-center gap-2 mb-3">
            <RotateCcw size={16} className="text-accent-400" />
            <h2 className="text-sm font-medium">Cross-tester learning</h2>
          </div>
          <p className="text-xs text-base-400 leading-relaxed">
            When your audit folder shares a parent directory with other testers' folders
            (e.g. a shared OneDrive subfolder), the keyword generator automatically reads
            their <code className="text-base-200">market_contributions.jsonl</code> files
            and learns from titles seen across profiles whose tags overlap with yours by
            at least 20%. No setup — just put each tester's audit folder under a common
            parent.
          </p>
        </div>
      </motion.div>
    </div>
  )
}

import { Bookmark, Plus } from 'lucide-react'
import { motion } from 'framer-motion'

const PRESETS = [
  { name: 'AI Strategy / Advisory', desc: 'Strategy + governance roles', count: 8 },
  { name: 'AI Enablement', desc: 'Adoption + change management', count: 5 },
  { name: 'Federal AI Consulting', desc: 'BAH, Deloitte, PwC, EY federal', count: 3 },
]

export default function SavedSearches() {
  return (
    <div className="px-10 py-8 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Saved Searches</h1>
          <p className="text-sm text-base-400 mt-1">Reusable preset configurations.</p>
        </div>
        <button className="flex items-center gap-2 px-4 py-2 rounded-lg
                           bg-white text-base-950 text-sm font-medium
                           hover:bg-base-200 transition-colors">
          <Plus size={14} />
          New saved search
        </button>
      </div>

      <div className="grid grid-cols-2 gap-3 mt-8">
        {PRESETS.map((p, idx) => (
          <motion.div
            key={p.name}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: idx * 0.05 }}
            className="glass rounded-xl p-5 cursor-pointer hover:border-white/[0.10] transition-colors"
          >
            <div className="flex items-start gap-3">
              <div className="w-9 h-9 rounded-lg bg-accent-500/15 border border-accent-500/25
                              flex items-center justify-center">
                <Bookmark size={14} className="text-accent-400" />
              </div>
              <div className="flex-1">
                <h3 className="text-sm font-medium">{p.name}</h3>
                <p className="text-xs text-base-400 mt-0.5">{p.desc}</p>
                <div className="text-[10px] text-base-500 mt-3 uppercase tracking-wider">
                  Last run: {p.count} qualifying
                </div>
              </div>
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  )
}

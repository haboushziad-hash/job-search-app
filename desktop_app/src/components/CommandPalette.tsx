import { useEffect, useState } from 'react'
import { Command } from 'cmdk'
import { useNavigate } from 'react-router-dom'
import {
  Compass, PlayCircle, Briefcase, History,
  Settings, Sparkles, Filter, Download, FileSpreadsheet, FileText,
} from 'lucide-react'
import { MOD_KEY } from '@/lib/utils'

const sc = (k: string) => (MOD_KEY === '⌘' ? `⌘${k}` : `Ctrl+${k}`)

interface CommandPaletteProps {
  open: boolean
  onClose: () => void
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const navigate = useNavigate()
  const [search, setSearch] = useState('')

  // ⌘K to open globally
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        onClose() // toggles via parent state
      }
      if (e.key === 'Escape' && open) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const go = (path: string) => () => {
    navigate(path)
    onClose()
    setSearch('')
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[18vh] px-4
                 bg-black/60 backdrop-blur-md"
      onClick={onClose}
    >
      <Command
        label="Command palette"
        className="w-full max-w-xl glass-strong rounded-xl overflow-hidden
                   shadow-2xl shadow-black/60"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center px-4 py-3 border-b border-white/[0.06]">
          <Sparkles size={16} className="text-base-400 mr-3" />
          <Command.Input
            value={search}
            onValueChange={setSearch}
            placeholder="Type a command or search..."
            className="flex-1 bg-transparent text-sm placeholder:text-base-500
                       text-base-50 outline-none"
            autoFocus
          />
          <kbd className="px-1.5 py-0.5 text-[10px] font-mono rounded
                          bg-white/[0.06] border border-white/[0.06] text-base-400">
            esc
          </kbd>
        </div>

        <Command.List className="max-h-[420px] overflow-y-auto p-2">
          <Command.Empty className="py-12 text-center text-sm text-base-400">
            No results.
          </Command.Empty>

          <Command.Group heading="Navigate" className="text-[10px] uppercase
                                                       tracking-widest text-base-500
                                                       px-2 py-1.5">
            <Item icon={Compass} label="Dashboard"      shortcut={sc('1')} onSelect={go('/dashboard')} />
            <Item icon={PlayCircle} label="New Search"   shortcut={sc('2')} onSelect={go('/run')} />
            <Item icon={Briefcase} label="Applications"  shortcut={sc('3')} onSelect={go('/tracker')} />
            <Item icon={History} label="Run History"     shortcut={sc('4')} onSelect={go('/history')} />
            <Item icon={Settings} label="Settings"       onSelect={go('/settings')} />
          </Command.Group>

          <Command.Group heading="Actions">
            <Item icon={PlayCircle} label="Run new search now" onSelect={go('/run')} />
            <Item icon={Filter} label="Filter to STRONG only" onSelect={() => onClose()} />
            <Item icon={Filter} label="Filter to remote only" onSelect={() => onClose()} />
          </Command.Group>

          <Command.Group heading="Export">
            <Item icon={FileSpreadsheet} label="Export last run to Excel" onSelect={() => onClose()} />
            <Item icon={FileText} label="Export last run to Word" onSelect={() => onClose()} />
            <Item icon={Download} label="Open last run in Claude" onSelect={() => onClose()} />
          </Command.Group>
        </Command.List>
      </Command>
    </div>
  )
}

interface ItemProps {
  icon: React.ElementType
  label: string
  shortcut?: string
  onSelect: () => void
}

function Item({ icon: Icon, label, shortcut, onSelect }: ItemProps) {
  return (
    <Command.Item
      onSelect={onSelect}
      className="group flex items-center gap-3 px-3 py-2 rounded-lg text-sm
                 text-base-300 cursor-pointer
                 data-[selected=true]:bg-white/[0.06]
                 data-[selected=true]:text-base-50"
    >
      <Icon size={15} />
      <span>{label}</span>
      {shortcut && (
        <kbd className="ml-auto px-1.5 py-0.5 text-[10px] font-mono rounded
                        bg-white/[0.05] border border-white/[0.05] text-base-400">
          {shortcut}
        </kbd>
      )}
    </Command.Item>
  )
}

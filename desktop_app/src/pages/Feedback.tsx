import { useMemo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Send, Loader2, CheckCircle2, AlertCircle, X, Plus, Search,
  Bug, ThumbsDown, ThumbsUp, Layout, Gauge, Wrench, Lightbulb, MessageCircle,
  type LucideIcon,
} from 'lucide-react'
import { toast } from 'sonner'
import { ZMark } from '@/components/ZMark'
import { sendFeedback, ApiError, type FeedbackCategory } from '@/services/api'
import { useAppStore } from '@/stores/appStore'
import { useAppVersion } from '@/lib/updater'
import type { Role } from '@/types'

// Each category gets a short label, an icon, and a one-line example so the
// user knows exactly what kind of detail is most actionable. Order matters —
// "bug" first because it's what we most need from testers, "other" last as
// the catch-all.
interface CategoryDef {
  value: FeedbackCategory
  label: string
  icon: LucideIcon
  hint: string
}

const CATEGORIES: CategoryDef[] = [
  { value: 'bug',            label: 'Bug',            icon: Bug,           hint: 'Something crashed, hung, or did the wrong thing.' },
  { value: 'false-positive', label: 'Bad match',      icon: ThumbsDown,    hint: 'A role surfaced that shouldn\'t have — wrong fit.' },
  { value: 'false-negative', label: 'Missed match',   icon: ThumbsUp,      hint: 'A role you found elsewhere that this should\'ve caught.' },
  { value: 'ui',             label: 'Confusing UI',   icon: Layout,        hint: 'A label, button, or layout that didn\'t make sense.' },
  { value: 'performance',    label: 'Slow / stuck',   icon: Gauge,         hint: 'A step that took way longer than expected.' },
  { value: 'setup',          label: 'Setup friction', icon: Wrench,        hint: 'Install, first-run, or onboarding got in the way.' },
  { value: 'suggestion',     label: 'Suggestion',     icon: Lightbulb,     hint: 'A feature or change you\'d like to see.' },
  { value: 'other',          label: 'Other',          icon: MessageCircle, hint: 'Anything else worth telling us.' },
]

// Concrete examples we show in the "what we're looking for" panel — these
// are the format Ziad finds most useful when triaging tester reports. Each
// is paired with a category so the user can see what good feedback looks
// like across the spectrum.
const EXAMPLES = [
  {
    cat: 'Bad match',
    text: 'Stripe Senior SWE was a STRONG match — I\'m a tax accountant, not a software engineer. Probably picking up on "AI" in my resume keywords.',
  },
  {
    cat: 'Missed match',
    text: 'Anthropic\'s "AI Policy Specialist" role didn\'t show up but it\'s the closest fit I\'ve seen — applied to it via LinkedIn directly.',
  },
  {
    cat: 'Bug',
    text: 'Cancelling a search at 70% progress left the dashboard showing "running" forever — had to restart the app to clear it.',
  },
  {
    cat: 'Confusing UI',
    text: 'After clicking Continue on Keywords, I expected to see results but got the dashboard. Wasn\'t obvious where to start the actual search.',
  },
]

// Stable identifier for a role across "is this in the picker" / "is this
// already chosen" comparisons. Mirrors the same shape the appStore uses
// internally so we don't introduce a divergent identity scheme.
function roleId(r: Role): string {
  return `${(r.company || '').toLowerCase()}|${(r.job_title || '').toLowerCase()}`
}

// Pretty tier label for the picker — keeps the UI scannable so the user
// can find the role they want to flag without opening the dashboard side-
// by-side. Bands match the Dashboard's tier thresholds.
function tierForScore(score?: number | null): { label: string; color: string } {
  const s = score ?? 0
  if (s >= 85) return { label: 'STRONG',  color: 'text-tier-strong' }
  if (s >= 70) return { label: 'GOOD',    color: 'text-tier-good' }
  if (s >= 55) return { label: 'MAYBE',   color: 'text-tier-maybe' }
  return { label: 'STRETCH', color: 'text-base-400' }
}

export default function Feedback() {
  const lastRoles = useAppStore((s) => s.lastRoles)
  // Read the running binary's version dynamically so the feedback report
  // metadata stays in sync with whatever the auto-updater has installed.
  const appVersion = useAppVersion()
  const [category, setCategory] = useState<FeedbackCategory>('bug')
  const [message, setMessage] = useState('')
  const [email, setEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Role-reference state — only relevant for false-positive (bad match)
  // and false-negative (missed match) categories. Cleared when category
  // switches away from those so the references don't get sent for an
  // unrelated bug report.
  const [pickerQuery, setPickerQuery] = useState('')
  const [selectedRoleIds, setSelectedRoleIds] = useState<Set<string>>(new Set())
  const [customRefs, setCustomRefs] = useState<string[]>([])
  const [customRefDraft, setCustomRefDraft] = useState('')

  const showRolePicker = category === 'false-positive'
  const showMissedRoleEntry = category === 'false-negative'

  // Filter the last-run roles by the picker's search query. Match against
  // either company or title; case-insensitive. Sorted by score desc so the
  // strongest matches (most likely to be the false positives the tester
  // saw on the dashboard) are at the top of the list.
  const filteredRoles = useMemo(() => {
    const q = pickerQuery.trim().toLowerCase()
    const all = [...lastRoles].sort(
      (a, b) => (b.final_score || 0) - (a.final_score || 0),
    )
    if (!q) return all.slice(0, 50)
    return all
      .filter((r) =>
        (r.company || '').toLowerCase().includes(q) ||
        (r.job_title || '').toLowerCase().includes(q),
      )
      .slice(0, 50)
  }, [lastRoles, pickerQuery])

  const selectedRoles = useMemo(
    () => lastRoles.filter((r) => selectedRoleIds.has(roleId(r))),
    [lastRoles, selectedRoleIds],
  )

  const toggleRole = (r: Role) => {
    setSelectedRoleIds((prev) => {
      const next = new Set(prev)
      const id = roleId(r)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const addCustomRef = () => {
    const v = customRefDraft.trim()
    if (!v) return
    if (customRefs.includes(v)) return
    setCustomRefs([...customRefs, v])
    setCustomRefDraft('')
  }

  const removeCustomRef = (s: string) => {
    setCustomRefs(customRefs.filter((x) => x !== s))
  }

  // When category changes away from a role-picker category, clear refs so
  // we don't accidentally send stale role references on a bug report.
  const setCategorySafe = (next: FeedbackCategory) => {
    if (next !== 'false-positive' && next !== 'false-negative') {
      setSelectedRoleIds(new Set())
      setCustomRefs([])
      setCustomRefDraft('')
      setPickerQuery('')
    }
    setCategory(next)
  }

  // Build a clean header block listing the referenced roles. Prepended to
  // the message so admin readers see WHICH roles the feedback is about
  // before reading the freeform context. Uses a fenced "ROLES:" section
  // so it's easy to parse out programmatically later if needed.
  const buildRoleRefBlock = (): string => {
    if (showRolePicker && (selectedRoles.length > 0 || customRefs.length > 0)) {
      const lines: string[] = ['ROLES FLAGGED AS BAD MATCHES:']
      for (const r of selectedRoles) {
        const score = r.final_score ?? 0
        const tier = tierForScore(score)
        lines.push(`  • ${r.job_title} @ ${r.company} — ${tier.label} (${Math.round(score)})`)
      }
      for (const c of customRefs) {
        lines.push(`  • ${c}  [custom entry]`)
      }
      return lines.join('\n') + '\n\n'
    }
    if (showMissedRoleEntry && customRefs.length > 0) {
      const lines: string[] = ['ROLES THAT SHOULD HAVE SURFACED:']
      for (const c of customRefs) lines.push(`  • ${c}`)
      return lines.join('\n') + '\n\n'
    }
    return ''
  }

  const charCount = message.trim().length
  const minChars = 10
  const maxChars = 10000
  const tooShort = charCount > 0 && charCount < minChars
  const tooLong = charCount > maxChars
  const canSubmit = !submitting && charCount >= minChars && !tooLong

  const handleSubmit = async () => {
    if (!canSubmit) return
    setError(null)
    setSubmitting(true)
    try {
      const refBlock = buildRoleRefBlock()
      await sendFeedback({
        category,
        message: refBlock + message.trim(),
        email: email.trim() || undefined,
        appVersion,
      })
      setSubmitted(true)
      toast.success('Feedback sent — thank you!')
    } catch (e) {
      const msg = e instanceof ApiError
        ? (e.status === 429 ? 'You\'ve hit the daily limit — try again tomorrow.' : e.detail)
        : (e instanceof Error ? e.message : 'Could not send feedback')
      setError(msg)
      toast.error(msg)
    } finally {
      setSubmitting(false)
    }
  }

  // Reset to a fresh form so the user can submit another piece of feedback
  // without leaving the page.
  const handleReset = () => {
    setSubmitted(false)
    setMessage('')
    setEmail('')
    setError(null)
    setCategory('bug')
    setSelectedRoleIds(new Set())
    setCustomRefs([])
    setCustomRefDraft('')
    setPickerQuery('')
  }

  return (
    <div className="min-h-full px-10 py-8 max-w-3xl mx-auto">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3 }}
      >
        {/* Header */}
        <div className="flex items-center gap-3 mb-2">
          <ZMark size={36} className="shadow-lg shadow-accent-900/40" />
          <h1 className="text-2xl font-semibold tracking-tight">Send feedback</h1>
        </div>
        <p className="text-sm text-base-400 mb-7 max-w-2xl leading-relaxed">
          You're testing pre-release software. The most useful thing you can do is tell us
          what surprised you — good or bad. Every report is read.
        </p>

        <AnimatePresence mode="wait">
          {submitted ? (
            <motion.div
              key="success"
              initial={{ opacity: 0, scale: 0.98 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              className="glass-strong rounded-2xl p-8 text-center"
            >
              <div className="w-12 h-12 mx-auto rounded-full bg-tier-strong/15 border border-tier-strong/40 flex items-center justify-center mb-4">
                <CheckCircle2 size={22} className="text-tier-strong" />
              </div>
              <h2 className="text-lg font-medium mb-1.5">Feedback received</h2>
              <p className="text-sm text-base-400 mb-6 max-w-sm mx-auto leading-relaxed">
                Thanks. If you left an email, we'll reach out if we need a follow-up
                detail.
              </p>
              <button
                onClick={handleReset}
                className="px-5 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.10]
                           text-sm text-base-200 border border-white/[0.08] transition-colors"
              >
                Send another
              </button>
            </motion.div>
          ) : (
            <motion.div
              key="form"
              initial={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="space-y-6"
            >
              {/* Examples panel — concrete format we want */}
              <div className="glass-subtle rounded-xl p-5">
                <div className="text-[10px] uppercase tracking-wider text-accent-400 font-medium mb-3">
                  What good feedback looks like
                </div>
                <ul className="space-y-2.5">
                  {EXAMPLES.map((ex, i) => (
                    <li key={i} className="flex gap-2.5 text-[13px] leading-relaxed">
                      <span className="flex-shrink-0 px-2 py-0.5 rounded-md bg-white/[0.04] border border-white/[0.06] text-[10px] uppercase tracking-wider text-base-400 font-medium h-fit">
                        {ex.cat}
                      </span>
                      <span className="text-base-300">"{ex.text}"</span>
                    </li>
                  ))}
                </ul>
                <p className="text-xs text-base-500 mt-4 leading-relaxed">
                  Specific roles, exact steps, and what you <em className="text-base-400 not-italic">expected vs. saw</em> are
                  what we work from.
                </p>
              </div>

              {/* Category picker — chip grid */}
              <div>
                <label className="block text-xs font-medium text-base-300 mb-2.5">
                  What's this about?
                </label>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                  {CATEGORIES.map((c) => {
                    const Icon = c.icon
                    const active = category === c.value
                    return (
                      <button
                        key={c.value}
                        onClick={() => setCategorySafe(c.value)}
                        title={c.hint}
                        className={`flex items-center gap-2 px-3 py-2.5 rounded-lg text-[12px]
                                    border transition-colors text-left
                                    ${active
                                      ? 'bg-accent-500/15 border-accent-500/40 text-accent-100'
                                      : 'bg-white/[0.03] border-white/[0.06] text-base-300 hover:bg-white/[0.06]'
                                    }`}
                      >
                        <Icon size={13} className={active ? 'text-accent-300' : 'text-base-400'} />
                        <span className="flex-1 truncate">{c.label}</span>
                      </button>
                    )
                  })}
                </div>
                <p className="text-[11px] text-base-500 mt-2 leading-relaxed">
                  {CATEGORIES.find((c) => c.value === category)?.hint}
                </p>
              </div>

              {/* ----------------------------------------------------------
                  Conditional role-reference picker.

                  - "Bad match" (false-positive): show the searchable list
                    of roles from the user's last run, plus a free-text
                    entry for older roles that are no longer in lastRoles
                    (e.g. they re-ran a search and want to flag a role
                    from the previous run).
                  - "Missed match" (false-negative): no list to pick from
                    (we missed it by definition), so just a free-text
                    "Title @ Company" input.

                  Both blocks feed into a single ROLES: header that gets
                  prepended to the freeform message at submit time so the
                  admin reader sees the structured refs before the prose.
                  ---------------------------------------------------------- */}
              {showRolePicker && (
                <div className="rounded-xl border border-amber-500/20 bg-amber-500/[0.04] p-4">
                  <div className="flex items-center gap-2 mb-1">
                    <ThumbsDown size={13} className="text-amber-300" />
                    <h3 className="text-sm font-medium text-base-100">Which role(s) shouldn't have surfaced?</h3>
                  </div>
                  <p className="text-[11px] text-base-400 mb-3 leading-relaxed">
                    Pick from your last run, or type one in if it's from an older search.
                  </p>

                  {/* Selected chips */}
                  {(selectedRoles.length > 0 || customRefs.length > 0) && (
                    <div className="flex flex-wrap gap-1.5 mb-3">
                      <AnimatePresence initial={false}>
                        {selectedRoles.map((r) => {
                          const tier = tierForScore(r.final_score)
                          return (
                            <motion.div
                              key={roleId(r)}
                              layout
                              initial={{ opacity: 0, scale: 0.92 }}
                              animate={{ opacity: 1, scale: 1 }}
                              exit={{ opacity: 0, scale: 0.92 }}
                              className="group flex items-center gap-1.5 pl-2.5 pr-1 py-1
                                         rounded-full bg-white/[0.06] border border-white/[0.10]"
                            >
                              <span className={`text-[10px] font-semibold uppercase tracking-wider ${tier.color}`}>
                                {tier.label}
                              </span>
                              <span className="text-[11px] text-base-100 truncate max-w-[180px]">
                                {r.job_title}
                              </span>
                              <span className="text-[10px] text-base-500">@ {r.company}</span>
                              <button
                                onClick={() => toggleRole(r)}
                                className="p-0.5 rounded-full text-base-500 hover:text-base-100 hover:bg-white/[0.10]"
                                title="Remove"
                              >
                                <X size={10} />
                              </button>
                            </motion.div>
                          )
                        })}
                        {customRefs.map((c) => (
                          <motion.div
                            key={`custom:${c}`}
                            layout
                            initial={{ opacity: 0, scale: 0.92 }}
                            animate={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.92 }}
                            className="group flex items-center gap-1.5 pl-2.5 pr-1 py-1
                                       rounded-full bg-white/[0.06] border border-white/[0.10]"
                          >
                            <span className="text-[10px] font-semibold uppercase tracking-wider text-base-400">
                              Custom
                            </span>
                            <span className="text-[11px] text-base-100 truncate max-w-[260px]">
                              {c}
                            </span>
                            <button
                              onClick={() => removeCustomRef(c)}
                              className="p-0.5 rounded-full text-base-500 hover:text-base-100 hover:bg-white/[0.10]"
                              title="Remove"
                            >
                              <X size={10} />
                            </button>
                          </motion.div>
                        ))}
                      </AnimatePresence>
                    </div>
                  )}

                  {/* Search + list. Hidden if there are no roles in lastRoles
                      (fresh install or after a Reset) — fall back to custom-
                      entry only so the form still works. */}
                  {lastRoles.length > 0 ? (
                    <>
                      <div className="relative">
                        <Search size={12} className="absolute left-3 top-1/2 -translate-y-1/2 text-base-500" />
                        <input
                          type="text"
                          value={pickerQuery}
                          onChange={(e) => setPickerQuery(e.target.value)}
                          placeholder={`Search ${lastRoles.length} roles from your last run...`}
                          className="w-full pl-8 pr-3 py-2 rounded-lg
                                     bg-white/[0.04] border border-white/[0.08]
                                     text-[12px] text-base-100 placeholder:text-base-500
                                     focus:outline-none focus:border-accent-500/50"
                        />
                      </div>
                      {filteredRoles.length > 0 ? (
                        <div className="mt-2 max-h-56 overflow-y-auto rounded-lg
                                        bg-black/20 border border-white/[0.04]">
                          {filteredRoles.map((r) => {
                            const id = roleId(r)
                            const selected = selectedRoleIds.has(id)
                            const tier = tierForScore(r.final_score)
                            return (
                              <button
                                key={id}
                                onClick={() => toggleRole(r)}
                                className={`w-full flex items-center gap-2.5 px-3 py-2 text-left
                                           border-b border-white/[0.03] last:border-b-0
                                           ${selected ? 'bg-accent-500/10' : 'hover:bg-white/[0.03]'}`}
                              >
                                <div className={`w-3.5 h-3.5 rounded-sm border flex-shrink-0 flex items-center justify-center
                                                ${selected
                                                  ? 'bg-accent-500 border-accent-500'
                                                  : 'border-white/[0.20]'}`}>
                                  {selected && <CheckCircle2 size={10} className="text-white" strokeWidth={3} />}
                                </div>
                                <span className={`text-[10px] font-semibold uppercase tracking-wider w-14 flex-shrink-0 ${tier.color}`}>
                                  {tier.label}
                                </span>
                                <span className="text-[12px] text-base-100 truncate flex-1">
                                  {r.job_title}
                                </span>
                                <span className="text-[11px] text-base-400 truncate max-w-[160px]">
                                  {r.company}
                                </span>
                                <span className="text-[10px] text-base-500 tabular-nums w-7 text-right flex-shrink-0">
                                  {Math.round(r.final_score || 0)}
                                </span>
                              </button>
                            )
                          })}
                        </div>
                      ) : (
                        <div className="mt-2 px-3 py-3 rounded-lg bg-black/20 border border-white/[0.04]
                                        text-[11px] text-base-500 text-center">
                          No roles match "{pickerQuery}". Try a different search or use "Add manually" below.
                        </div>
                      )}
                    </>
                  ) : (
                    <div className="px-3 py-2.5 rounded-lg bg-black/20 border border-white/[0.04]
                                    text-[11px] text-base-500">
                      No roles found in your last run yet — type the role manually below.
                    </div>
                  )}

                  {/* Free-text fallback for roles not in lastRoles */}
                  <div className="flex gap-2 mt-3">
                    <input
                      type="text"
                      value={customRefDraft}
                      onChange={(e) => setCustomRefDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addCustomRef()
                        }
                      }}
                      placeholder="Or type manually: AI Strategist @ Anthropic"
                      className="flex-1 px-3 py-2 rounded-lg
                                 bg-white/[0.04] border border-white/[0.08]
                                 text-[12px] text-base-100 placeholder:text-base-500
                                 focus:outline-none focus:border-accent-500/50"
                    />
                    <button
                      onClick={addCustomRef}
                      disabled={!customRefDraft.trim()}
                      className="flex items-center gap-1 px-3 py-2 rounded-lg
                                 bg-white/[0.06] hover:bg-white/[0.10] text-[12px] text-base-200
                                 disabled:opacity-40 disabled:cursor-not-allowed border border-white/[0.06]"
                    >
                      <Plus size={11} /> Add
                    </button>
                  </div>
                </div>
              )}

              {showMissedRoleEntry && (
                <div className="rounded-xl border border-tier-good/20 bg-tier-good/[0.04] p-4">
                  <div className="flex items-center gap-2 mb-1">
                    <ThumbsUp size={13} className="text-tier-good" />
                    <h3 className="text-sm font-medium text-base-100">Which role(s) should have surfaced?</h3>
                  </div>
                  <p className="text-[11px] text-base-400 mb-3 leading-relaxed">
                    Type each one as <span className="text-base-300">Title @ Company</span> and press Enter or Add. Helps us
                    figure out whether it's a keyword gap or a scraper miss.
                  </p>

                  {customRefs.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mb-3">
                      <AnimatePresence initial={false}>
                        {customRefs.map((c) => (
                          <motion.div
                            key={`missed:${c}`}
                            layout
                            initial={{ opacity: 0, scale: 0.92 }}
                            animate={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.92 }}
                            className="flex items-center gap-1.5 pl-2.5 pr-1 py-1
                                       rounded-full bg-white/[0.06] border border-white/[0.10]"
                          >
                            <span className="text-[11px] text-base-100 truncate max-w-[280px]">
                              {c}
                            </span>
                            <button
                              onClick={() => removeCustomRef(c)}
                              className="p-0.5 rounded-full text-base-500 hover:text-base-100 hover:bg-white/[0.10]"
                              title="Remove"
                            >
                              <X size={10} />
                            </button>
                          </motion.div>
                        ))}
                      </AnimatePresence>
                    </div>
                  )}

                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={customRefDraft}
                      onChange={(e) => setCustomRefDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') {
                          e.preventDefault()
                          addCustomRef()
                        }
                      }}
                      placeholder="AI Policy Specialist @ Anthropic"
                      className="flex-1 px-3 py-2 rounded-lg
                                 bg-white/[0.04] border border-white/[0.08]
                                 text-[12px] text-base-100 placeholder:text-base-500
                                 focus:outline-none focus:border-accent-500/50"
                    />
                    <button
                      onClick={addCustomRef}
                      disabled={!customRefDraft.trim()}
                      className="flex items-center gap-1 px-3 py-2 rounded-lg
                                 bg-white/[0.06] hover:bg-white/[0.10] text-[12px] text-base-200
                                 disabled:opacity-40 disabled:cursor-not-allowed border border-white/[0.06]"
                    >
                      <Plus size={11} /> Add
                    </button>
                  </div>
                </div>
              )}

              {/* Message */}
              <div>
                <label className="block text-xs font-medium text-base-300 mb-2.5">
                  Your message
                </label>
                <textarea
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  placeholder="What happened? What were you trying to do?"
                  rows={8}
                  className="w-full px-3.5 py-3 rounded-lg
                             bg-white/[0.03] border border-white/[0.08]
                             text-sm text-base-100 placeholder:text-base-500
                             focus:outline-none focus:border-accent-500/50
                             resize-none leading-relaxed"
                />
                <div className="flex items-center justify-between mt-2 text-[11px]">
                  <span className={`${tooShort ? 'text-amber-400' : tooLong ? 'text-red-400' : 'text-base-500'}`}>
                    {tooShort && `Need at least ${minChars} characters`}
                    {tooLong && `Too long — keep it under ${maxChars}`}
                    {!tooShort && !tooLong && 'Specifics help. Steps to reproduce, role title + company, what you expected.'}
                  </span>
                  <span className="text-base-500 tabular-nums">
                    {charCount} / {maxChars}
                  </span>
                </div>
              </div>

              {/* Optional email */}
              <div>
                <label className="block text-xs font-medium text-base-300 mb-2.5">
                  Email <span className="text-base-500 font-normal">(optional — only if you want a reply)</span>
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  className="w-full px-3.5 py-2.5 rounded-lg
                             bg-white/[0.03] border border-white/[0.08]
                             text-sm text-base-100 placeholder:text-base-500
                             focus:outline-none focus:border-accent-500/50"
                />
              </div>

              {/* Error */}
              {error && (
                <div className="flex items-start gap-2.5 p-3 rounded-lg bg-red-500/10 border border-red-500/30">
                  <AlertCircle size={14} className="text-red-400 flex-shrink-0 mt-0.5" />
                  <p className="text-xs text-base-200 leading-relaxed">{error}</p>
                </div>
              )}

              {/* Submit */}
              <div className="flex items-center justify-between pt-2 border-t border-white/[0.06]">
                <p className="text-[11px] text-base-500 max-w-sm leading-relaxed">
                  Sent over HTTPS to our central server. Your name and resume aren't included.
                </p>
                <button
                  onClick={handleSubmit}
                  disabled={!canSubmit}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-lg
                             bg-white text-base-950 font-medium text-sm
                             hover:bg-base-200 transition-colors
                             disabled:opacity-50 disabled:cursor-not-allowed
                             shadow-lg shadow-black/30"
                >
                  {submitting ? (
                    <>
                      <Loader2 size={14} className="animate-spin" /> Sending...
                    </>
                  ) : (
                    <>
                      <Send size={13} /> Send feedback
                    </>
                  )}
                </button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  )
}

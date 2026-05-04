import { motion } from 'framer-motion'
import {
  MapPin, DollarSign, Calendar, Sparkles,
  Bookmark, BookmarkCheck, EyeOff, Eye, Check, ArrowUpRight, Send,
} from 'lucide-react'
import { toast } from 'sonner'
import { cn, formatCurrency, formatRelativeDate } from '@/lib/utils'
import { useAppStore } from '@/stores/appStore'
import type { Role } from '@/types'

interface RoleCardProps {
  role: Role
  onClick?: () => void
  // When true, render a smaller single-row card for de-emphasized tiers
  // (typically STRETCH) so STRONG/GOOD/MAYBE get more visual weight.
  condensed?: boolean
}

const TIER_PILL = {
  STRONG: 'bg-tier-strong/15 text-tier-strong border-tier-strong/30',
  GOOD: 'bg-tier-good/15 text-tier-good border-tier-good/30',
  MAYBE: 'bg-tier-maybe/15 text-tier-maybe border-tier-maybe/30',
  STRETCH: 'bg-base-700/40 text-base-300 border-base-600/40',
  SKIP: 'bg-base-700/40 text-base-400 border-base-600/40',
}

export function RoleCard({ role, onClick, condensed = false }: RoleCardProps) {
  const tier = (role.final_tier || 'STRETCH') as keyof typeof TIER_PILL
  // Underlying 0-100 score is used internally for sorting/filtering but
  // intentionally NOT displayed: within a tier, score precision is below
  // human signal — we trust the tier label (STRONG/GOOD/MAYBE/STRETCH).

  const status = useAppStore((s) => s.getStatus(role))
  const saveRole = useAppStore((s) => s.saveRole)
  const hideRole = useAppStore((s) => s.hideRole)
  const unsaveRole = useAppStore((s) => s.unsaveRole)
  const markApplied = useAppStore((s) => s.markApplied)

  const isSaved = status?.status === 'saved'
  const isApplied = status?.status === 'applied'
  const isHidden = status?.status === 'hidden'

  // Salary display — explicit "not disclosed" when missing so users know
  // the field was checked rather than overlooked.
  const salaryDisplay = role.salary_min && role.salary_max
    ? `${formatCurrency(role.salary_min, { compact: true })}–${formatCurrency(role.salary_max, { compact: true })}`
    : role.salary_text || null
  const salaryText = salaryDisplay || 'Salary not disclosed'
  const salaryMissing = !salaryDisplay

  // Prefer the AI-generated summary (Stage 3) over stage3_analysis when
  // available. Summary is intentionally crisper for the dashboard preview.
  const previewText =
    (role.summary && role.summary.trim())
    || (role.stage3_analysis && !role.stage3_analysis.startsWith('stage3_error')
        ? role.stage3_analysis
        : null)

  const handleVisitJob = async (e: React.MouseEvent) => {
    e.stopPropagation()
    if (!role.job_url) return
    // In Tauri, window.open is blocked by the webview. Use the shell plugin
    // to open URLs in the user's default browser.
    try {
      const { open } = await import('@tauri-apps/plugin-shell')
      await open(role.job_url)
    } catch {
      // Fallback for non-Tauri environments (browser dev mode)
      window.open(role.job_url, '_blank', 'noopener,noreferrer')
    }
  }

  const handleSave = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (isSaved) {
      unsaveRole(role)
      toast(`Removed from saved`, { description: role.job_title })
    } else {
      saveRole(role)
      toast.success(`Saved`, { description: role.job_title })
    }
  }

  const handleHide = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (isHidden) {
      // Toggle off — clear the hidden status so the role re-appears in
      // the dashboard's default view.
      unsaveRole(role)
      toast('Unhidden', { description: role.job_title })
      return
    }
    hideRole(role)
    toast('Hidden', {
      description: role.job_title,
      action: {
        label: 'Undo',
        onClick: () => unsaveRole(role),
      },
    })
  }

  const handleApply = (e: React.MouseEvent) => {
    e.stopPropagation()
    if (isApplied) {
      // Toggle off — move back to saved (so they can decide later)
      saveRole(role)
      toast(`Marked as saved`, { description: role.job_title })
    } else {
      markApplied(role)
      toast.success(`Marked as applied`, {
        description: role.job_title + ' — track stages on the Applications page',
      })
    }
  }

  // Condensed (STRETCH default) — single-row, lower visual weight
  if (condensed) {
    return (
      <motion.div
        whileHover={{ y: -1 }}
        onClick={onClick}
        className={cn(
          'glass-subtle hover:glass group relative rounded-lg px-4 py-2.5 cursor-pointer',
          'transition-all hover:border-white/[0.10] flex items-center gap-3',
          isApplied && 'border-tier-good/30 bg-tier-good/[0.03]'
        )}
      >
        <div className={cn(
          'flex-shrink-0 px-2 h-9 min-w-[3.25rem] rounded-md flex items-center justify-center border',
          'text-[10px] font-semibold uppercase tracking-wider',
          tier === 'STRONG' && 'bg-tier-strong/15 border-tier-strong/40 text-tier-strong',
          tier === 'GOOD' && 'bg-tier-good/15 border-tier-good/40 text-tier-good',
          tier === 'MAYBE' && 'bg-tier-maybe/15 border-tier-maybe/40 text-tier-maybe',
          tier === 'STRETCH' && 'bg-base-700/40 border-base-600/40 text-base-300',
          tier === 'SKIP' && 'bg-base-700/40 border-base-600/40 text-base-400',
        )}>
          {tier}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-base-100 truncate">
              {role.job_title}
            </span>
            <span className="text-[10px] text-base-500 truncate">
              · {role.company}
            </span>
          </div>
          <div className="flex items-center gap-3 text-[10px] text-base-500 mt-0.5">
            {role.location_type && <span>{role.location_type}</span>}
            <span className={salaryMissing ? 'italic text-base-600' : ''}>{salaryText}</span>
          </div>
        </div>
        <button
          onClick={handleVisitJob}
          disabled={!role.job_url}
          className="flex-shrink-0 px-2.5 py-1 rounded text-[11px]
                     bg-white/[0.04] hover:bg-white/[0.08] text-base-300 disabled:opacity-40
                     border border-white/[0.06]"
        >
          View
        </button>
      </motion.div>
    )
  }

  return (
    <motion.div
      whileHover={{ y: -1 }}
      onClick={onClick}
      className={cn(
        'glass-subtle hover:glass group relative rounded-xl px-5 py-4 cursor-pointer',
        'transition-all hover:border-white/[0.10]',
        // Accent border for STRONG/GOOD — they earn the visual emphasis
        tier === 'STRONG' && 'border-tier-strong/30 bg-tier-strong/[0.02]',
        tier === 'GOOD' && 'border-tier-good/30 bg-tier-good/[0.02]',
        isApplied && 'border-tier-good/30 bg-tier-good/[0.03]'
      )}
    >
      {/* Status badge top-right (only when saved/applied) */}
      {(isSaved || isApplied) && (
        <div className="absolute top-3 right-3 z-10 flex items-center gap-1.5
                        px-2 py-0.5 rounded-full text-[10px] font-mono uppercase tracking-wider
                        border backdrop-blur-md
                        {isApplied ? 'bg-tier-good/15 text-tier-good border-tier-good/40'
                                   : 'bg-accent-500/15 text-accent-300 border-accent-500/40'}">
          {isApplied ? (
            <><Check size={10} /> Applied</>
          ) : (
            <><BookmarkCheck size={10} /> Saved</>
          )}
        </div>
      )}

      <div className="flex items-start gap-4">
        {/* Tier badge (replaces numeric score — tier-level confidence is what
            we trust; the underlying 0-100 score is precise enough to bucket
            but not precise enough to compare 88 vs 92 within a tier) */}
        <div className={cn(
          'flex-shrink-0 w-16 h-14 rounded-lg flex flex-col items-center justify-center border',
          tier === 'STRONG' && 'bg-tier-strong/15 border-tier-strong/40 text-tier-strong',
          tier === 'GOOD' && 'bg-tier-good/15 border-tier-good/40 text-tier-good',
          tier === 'MAYBE' && 'bg-tier-maybe/15 border-tier-maybe/40 text-tier-maybe',
          tier === 'STRETCH' && 'bg-base-700/40 border-base-600/40 text-base-300',
          tier === 'SKIP' && 'bg-base-700/40 border-base-600/40 text-base-400',
        )}>
          <span className="text-[11px] font-semibold uppercase tracking-wider leading-none">
            {tier}
          </span>
          <span className="text-[8px] text-base-400 mt-1 tracking-wider">MATCH</span>
        </div>

        {/* Title + meta */}
        <div className="min-w-0 flex-1">
          <div className="flex items-start gap-2 mb-1.5">
            <h3 className="text-sm font-semibold text-base-50 truncate">
              {role.job_title}
            </h3>
          </div>

          <div className="text-xs text-base-300 mb-2.5 truncate">
            {role.company}
            {role.primary_source && (
              <span className="text-base-500 ml-2">• via {role.primary_source}</span>
            )}
          </div>

          <div className="flex items-center gap-4 text-xs text-base-400 flex-wrap">
            {role.location_type && (
              <span className="flex items-center gap-1">
                <MapPin size={12} />
                {role.location_type}
                {role.location && role.location_type !== 'Remote' && (
                  <span className="text-base-500"> · {role.location}</span>
                )}
              </span>
            )}
            <span className={cn(
              'flex items-center gap-1',
              salaryMissing && 'italic text-base-500'
            )}>
              <DollarSign size={12} />
              {salaryText}
            </span>
            {role.posted_date && (
              <span className="flex items-center gap-1">
                <Calendar size={12} />
                {formatRelativeDate(role.posted_date)}
              </span>
            )}
            {role.matched_keyword && (
              <span className="flex items-center gap-1 text-accent-400" title="Keyword from your profile that surfaced this role">
                <Sparkles size={12} />
                via "{role.matched_keyword}"
              </span>
            )}
          </div>

          {previewText && (
            // line-clamp-4 fits the typical Stage 3 summary (avg 280 chars,
            // max ~360) on screen without truncation. line-clamp-2 was
            // cutting off ~30% of the fit reasoning. STRETCH cards render
            // in the condensed branch above so they don't show this at all.
            <p className="text-xs text-base-400 mt-2.5 line-clamp-4 leading-relaxed">
              {previewText}
            </p>
          )}

          {/* Action row */}
          <div className="flex items-center gap-2 mt-3.5 pt-3 border-t border-white/[0.04]">
            <ActionButton
              onClick={handleVisitJob}
              icon={ArrowUpRight}
              label="Visit Job"
              variant="primary"
              disabled={!role.job_url}
            />
            <ActionButton
              onClick={handleSave}
              icon={isSaved ? BookmarkCheck : Bookmark}
              label={isSaved ? 'Saved' : 'Save'}
              variant={isSaved ? 'active-save' : 'default'}
            />
            <ActionButton
              onClick={handleApply}
              icon={isApplied ? Check : Send}
              label="Applied"
              variant={isApplied ? 'active-apply' : 'default'}
            />
            <ActionButton
              onClick={handleHide}
              icon={isHidden ? Eye : EyeOff}
              label={isHidden ? 'Unhide' : 'Hide'}
              variant="default"
            />
          </div>
        </div>
      </div>
    </motion.div>
  )
}

function ActionButton({
  icon: Icon,
  label,
  onClick,
  variant = 'default',
  disabled = false,
}: {
  icon: React.ElementType
  label: string
  onClick: (e: React.MouseEvent) => void
  variant?: 'default' | 'primary' | 'active-save' | 'active-apply'
  disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium',
        'transition-colors disabled:opacity-40 disabled:cursor-not-allowed',
        variant === 'primary' &&
          'bg-white text-base-950 hover:bg-base-100 shadow-sm',
        variant === 'default' &&
          'bg-white/[0.04] text-base-300 hover:bg-white/[0.08] hover:text-base-100 border border-white/[0.06]',
        variant === 'active-apply' &&
          'bg-tier-good/15 text-tier-good border border-tier-good/30 hover:bg-tier-good/20',
        variant === 'active-save' &&
          'bg-accent-500/15 text-accent-300 border border-accent-500/30 hover:bg-accent-500/20'
      )}
    >
      <Icon size={12} />
      <span>{label}</span>
    </button>
  )
}

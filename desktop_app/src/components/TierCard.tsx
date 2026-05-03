import { motion } from 'framer-motion'
import { cn } from '@/lib/utils'

interface TierCardProps {
  tier: 'STRONG' | 'GOOD' | 'MAYBE' | 'STRETCH'
  count: number
  scoreRange: string
  description: string
  onClick?: () => void
  selected?: boolean
}

const TIER_STYLES = {
  STRONG: {
    color: 'text-tier-strong',
    bg: 'from-tier-strong/15 to-transparent',
    border: 'border-tier-strong/25',
    glow: 'shadow-tier-strong/20',
  },
  GOOD: {
    color: 'text-tier-good',
    bg: 'from-tier-good/15 to-transparent',
    border: 'border-tier-good/25',
    glow: 'shadow-tier-good/20',
  },
  MAYBE: {
    color: 'text-tier-maybe',
    bg: 'from-tier-maybe/15 to-transparent',
    border: 'border-tier-maybe/25',
    glow: 'shadow-tier-maybe/20',
  },
  STRETCH: {
    color: 'text-base-400',
    bg: 'from-base-700/15 to-transparent',
    border: 'border-base-600/25',
    glow: 'shadow-black/40',
  },
}

export function TierCard({ tier, count, scoreRange, description, onClick, selected }: TierCardProps) {
  const style = TIER_STYLES[tier]
  return (
    <motion.button
      onClick={onClick}
      whileHover={{ y: -2 }}
      whileTap={{ scale: 0.99 }}
      className={cn(
        'glass relative overflow-hidden rounded-xl p-5 text-left transition-all',
        'hover:shadow-2xl',
        style.glow,
        selected && 'ring-2 ring-white/20'
      )}
    >
      {/* gradient blob */}
      <div className={cn(
        'absolute -top-1/2 -right-1/4 w-2/3 h-full',
        'bg-gradient-to-br pointer-events-none rounded-full blur-2xl',
        style.bg
      )} />

      <div className="relative">
        <div className="flex items-baseline justify-between mb-1">
          <span className={cn('text-[10px] font-mono tracking-widest uppercase', style.color)}>
            {tier}
          </span>
          <span className="text-[10px] text-base-500 font-mono">{scoreRange}</span>
        </div>
        <div className={cn('text-4xl font-semibold tabular-nums tracking-tight', style.color)}>
          {count}
        </div>
        <div className="text-xs text-base-400 mt-2">{description}</div>
      </div>
    </motion.button>
  )
}

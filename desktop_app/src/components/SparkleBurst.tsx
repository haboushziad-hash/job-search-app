/**
 * Tiny celebratory burst — fires N sparkle particles outward from the
 * center of its container. Uses an integer `trigger` prop as a key so
 * each new value re-mounts the component and replays the animation.
 *
 * Drop inside any `relative`-positioned button or container:
 *
 *   const [burst, setBurst] = useState(0)
 *   <button className="relative overflow-visible" onClick={() => {
 *     setBurst(b => b + 1)
 *     doThing()
 *   }}>
 *     Click me
 *     <SparkleBurst trigger={burst} />
 *   </button>
 *
 * Notes:
 * - Parent must be `position: relative` and ideally `overflow: visible`
 *   so particles aren't clipped.
 * - Particles position themselves at the parent's center via left/top
 *   50% + a transform offset, so they actually emit from the middle
 *   regardless of the button's flex/grid layout.
 */
import { motion, AnimatePresence } from 'framer-motion'

interface SparkleBurstProps {
  trigger: number
  /** Number of particles. Default 24 — visible without being chaotic. */
  count?: number
  /** Max travel distance in px. Default 120. */
  reach?: number
}

// Purple accent palette only — no white particles, since the buttons
// these are mounted on are typically white themselves and white sparkles
// would vanish against the background.
const COLORS = ['#A47CFF', '#C4A6FF', '#7C3AED', '#5B21B6', '#8B5CF6', '#D8B4FE']

export function SparkleBurst({ trigger, count = 24, reach = 120 }: SparkleBurstProps) {
  if (trigger === 0) return null

  return (
    <AnimatePresence mode="wait">
      <motion.span
        key={trigger}
        className="pointer-events-none absolute inset-0"
        aria-hidden="true"
      >
        {/* Expanding glow ring — bright accent halo that ripples outward. */}
        <motion.span
          initial={{ scale: 0.3, opacity: 1 }}
          animate={{ scale: 2.6, opacity: 0 }}
          transition={{ duration: 0.85, ease: [0.16, 1, 0.3, 1] }}
          className="absolute inset-0 rounded-[inherit]"
          style={{
            boxShadow:
              '0 0 0 4px rgba(164, 124, 255, 0.85), ' +
              '0 0 28px 8px rgba(164, 124, 255, 0.7), ' +
              '0 0 60px 16px rgba(124, 58, 237, 0.4)',
          }}
        />
        {/* Secondary inner ring — adds layered depth. */}
        <motion.span
          initial={{ scale: 0.6, opacity: 0.9 }}
          animate={{ scale: 1.8, opacity: 0 }}
          transition={{ duration: 0.6, ease: 'easeOut', delay: 0.05 }}
          className="absolute inset-0 rounded-[inherit] bg-accent-500/30"
        />
        {Array.from({ length: count }).map((_, i) => {
          const angle = (i / count) * Math.PI * 2 + Math.random() * 0.4
          const distance = reach * (0.55 + Math.random() * 0.6)
          const x = Math.cos(angle) * distance
          const y = Math.sin(angle) * distance
          const size = 6 + Math.random() * 8
          const color = COLORS[i % COLORS.length]
          return (
            <motion.span
              key={i}
              initial={{ x: 0, y: 0, opacity: 1, scale: 0.3 }}
              animate={{ x, y, opacity: 0, scale: 1.4 }}
              transition={{
                duration: 0.95 + Math.random() * 0.4,
                ease: [0.22, 1, 0.36, 1],
                delay: Math.random() * 0.08,
              }}
              // Position the particle dead-center of the parent. We use
              // left/top: 50% to anchor, then a margin offset of -size/2
              // for proper centering (canonical CSS centering trick).
              // framer-motion's `x`/`y` animate ON TOP of the base
              // position, so the particle starts centered and flies out.
              className="absolute rounded-full"
              style={{
                left: '50%',
                top: '50%',
                width: size,
                height: size,
                marginLeft: -size / 2,
                marginTop: -size / 2,
                backgroundColor: color,
                boxShadow: `0 0 ${size * 3}px ${color}, 0 0 ${size * 1.5}px ${color}cc`,
              }}
            />
          )
        })}
      </motion.span>
    </AnimatePresence>
  )
}

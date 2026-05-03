/**
 * Animated mesh-gradient background for the Welcome screen.
 *
 * Was originally Vanta WebGL globe, but Vanta's bundled exports don't play
 * nicely with Vite ESM. This pure CSS version is faster, smaller, and still
 * looks premium — animated radial blobs that drift like a lava lamp.
 */
import { motion } from 'framer-motion'

interface Props {
  className?: string
}

export function VantaBackground({ className }: Props) {
  return (
    <div className={`relative overflow-hidden ${className ?? ''}`}>
      {/* Deep base */}
      <div className="absolute inset-0 bg-base-975" />

      {/* Three drifting blobs */}
      <motion.div
        className="absolute w-[60vw] h-[60vw] rounded-full blur-[120px]"
        style={{ background: 'oklch(0.40 0.20 263 / 0.55)' }}
        animate={{
          x: ['-15%', '25%', '-15%'],
          y: ['-10%', '30%', '-10%'],
        }}
        transition={{ duration: 28, repeat: Infinity, ease: 'easeInOut' }}
      />
      <motion.div
        className="absolute w-[55vw] h-[55vw] rounded-full blur-[120px]"
        style={{
          background: 'oklch(0.40 0.18 305 / 0.45)',
          right: 0,
          bottom: 0,
        }}
        animate={{
          x: ['10%', '-20%', '10%'],
          y: ['10%', '-25%', '10%'],
        }}
        transition={{ duration: 32, repeat: Infinity, ease: 'easeInOut' }}
      />
      <motion.div
        className="absolute w-[40vw] h-[40vw] rounded-full blur-[100px]"
        style={{
          background: 'oklch(0.42 0.15 200 / 0.30)',
          left: '40%',
          top: '40%',
        }}
        animate={{
          x: ['0%', '15%', '0%'],
          y: ['0%', '-15%', '0%'],
        }}
        transition={{ duration: 24, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* Subtle grid overlay for tech feel */}
      <div
        className="absolute inset-0 opacity-[0.04]"
        style={{
          backgroundImage:
            'linear-gradient(oklch(1 0 0) 1px, transparent 1px), linear-gradient(90deg, oklch(1 0 0) 1px, transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      {/* Vignette for legibility */}
      <div className="absolute inset-0 bg-gradient-to-b from-transparent via-transparent to-base-975/50" />
    </div>
  )
}

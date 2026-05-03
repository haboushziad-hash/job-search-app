import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Target, Compass, Compass as CompassFill, Sparkles, type LucideIcon } from 'lucide-react'
import { VantaBackground } from '@/components/VantaBackground'
import { useAppStore } from '@/stores/appStore'
import { ZMark } from '@/components/ZMark'

export default function Welcome() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const lastRoles = useAppStore((s) => s.lastRoles)
  const hasPriorRun = !!profile && lastRoles.length > 0

  return (
    <div className="relative h-screen w-screen overflow-hidden">
      {/* Animated Vanta WebGL background */}
      <VantaBackground className="absolute inset-0 z-0" />

      {/* Mesh gradient overlay for color */}
      <div
        className="absolute inset-0 z-[1] pointer-events-none"
        style={{
          background: `
            radial-gradient(at 30% 20%, oklch(0.30 0.18 263 / 0.40) 0%, transparent 55%),
            radial-gradient(at 75% 85%, oklch(0.32 0.15 305 / 0.35) 0%, transparent 50%)
          `,
        }}
      />

      {/* Vignette */}
      <div className="absolute inset-0 z-[1] pointer-events-none
                      bg-gradient-to-b from-transparent via-transparent to-base-975/80" />

      {/* Content — flex layout reserves space for credit at bottom */}
      <div className="relative z-10 h-full flex flex-col px-8 py-12">
        <div className="flex-1 flex flex-col items-center justify-center min-h-0">
        {/* Brand mark — Z mark matching the app icon */}
        <motion.div
          initial={{ opacity: 0, scale: 0.92 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.6, ease: 'easeOut' }}
          className="mb-8"
        >
          <ZMark
            size={64}
            className="rounded-2xl shadow-2xl shadow-accent-900/50 border border-white/10"
          />
        </motion.div>

        {/* Headline — staggered word reveal with DAMN doing a shout
            (scale punch + glow burst). The framer variants below stagger
            each word and the damn span fires its own punch animation. */}
        <motion.h1
          initial="hidden"
          animate="visible"
          variants={{ visible: { transition: { delayChildren: 0.15, staggerChildren: 0.12 } } }}
          className="text-[64px] leading-[1.05] font-semibold tracking-tight text-balance text-center max-w-3xl flex flex-wrap justify-center gap-x-3 gap-y-1"
        >
          {['Find', 'me', 'some'].map((word) => (
            <motion.span
              key={word}
              variants={{
                hidden: { opacity: 0, y: 20 },
                visible: { opacity: 1, y: 0, transition: { duration: 0.5, ease: [0.2, 0.8, 0.2, 1] } },
              }}
              className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
            >
              {word}
            </motion.span>
          ))}
          <motion.span
            variants={{
              hidden: { opacity: 0, scale: 0.4, rotate: -3, filter: 'blur(4px)' },
              visible: {
                opacity: 1,
                scale: [0.4, 1.3, 0.94, 1.05, 1],
                rotate: [-3, 2, -1, 0.5, 0],
                filter: ['blur(4px)', 'blur(0px)', 'blur(0px)', 'blur(0px)', 'blur(0px)'],
                transition: {
                  duration: 1.0,
                  ease: [0.16, 1.2, 0.3, 1.0],
                  delay: 0.4,
                  times: [0, 0.35, 0.6, 0.8, 1],
                },
              },
            }}
            className="bg-gradient-to-r from-accent-300 via-accent-400 to-accent-600 bg-clip-text text-transparent inline-block"
            style={{ textShadow: '0 0 32px rgba(164, 124, 255, 0.4)' }}
          >
            damn
          </motion.span>
          <motion.span
            variants={{
              hidden: { opacity: 0, y: 20 },
              visible: { opacity: 1, y: 0, transition: { duration: 0.5, delay: 1.1, ease: [0.2, 0.8, 0.2, 1] } },
            }}
            className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
          >
            jobz.
          </motion.span>
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.3, ease: 'easeOut' }}
          className="text-lg text-base-300 mt-6 max-w-md text-center text-balance"
        >
          Upload your resume. We scrape, score, and rank thousands of roles
          against your background — in minutes.
        </motion.p>

        {/* Feature pills */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.45, ease: 'easeOut' }}
          className="flex gap-3 mt-10"
        >
          <Pill icon={Target} label="Personalized to You" />
          <Pill icon={Compass} label="Custom Scored" />
          <Pill icon={Sparkles} label="Ranked by Fit" />
        </motion.div>

        {/* CTA */}
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.6, ease: 'easeOut' }}
          className="mt-12"
        >
          <div className="flex items-center gap-3 flex-wrap justify-center">
            <button
              onClick={() => navigate(hasPriorRun ? '/run' : '/how-it-works')}
              className="group relative inline-flex items-center gap-2.5
                         px-7 py-3.5 rounded-full
                         bg-white text-base-950 font-medium text-sm
                         shadow-2xl shadow-black/40
                         hover:shadow-accent-500/30
                         transition-all duration-300 hover:scale-[1.02]
                         active:scale-[0.98]"
            >
              <span>{hasPriorRun ? 'Run a new search' : 'Get started'}</span>
              <ArrowRight
                size={16}
                className="transition-transform duration-300 group-hover:translate-x-0.5"
              />
              <span className="absolute inset-0 rounded-full
                               bg-gradient-to-r from-accent-400/0 via-accent-400/20 to-accent-400/0
                               opacity-0 group-hover:opacity-100 transition-opacity blur-md -z-10" />
            </button>

            {hasPriorRun && (
              <button
                onClick={() => navigate('/dashboard')}
                className="inline-flex items-center gap-2 px-6 py-3.5 rounded-full
                           bg-white/[0.06] border border-white/[0.10] text-base-200 text-sm
                           hover:bg-white/[0.10] transition-colors backdrop-blur"
              >
                <CompassFill size={14} className="text-accent-400" />
                <span>View previous results</span>
              </button>
            )}
          </div>
        </motion.div>

        </div>

        {/* Creator credit — sits in its own flex slot at the bottom */}
        <motion.div
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.8, delay: 0.85 }}
          className="flex items-center justify-center gap-3 pt-8 flex-shrink-0"
        >
          <span className="text-sm text-base-400">Built by</span>
          <span className="text-base font-medium tracking-tight
                           bg-gradient-to-r from-white to-accent-300
                           bg-clip-text text-transparent">
            Ziad Haboush
          </span>
          <span className="px-2 py-0.5 text-[10px] font-mono rounded
                           bg-white/[0.06] border border-white/[0.08] text-base-400">
            v0.1.0
          </span>
        </motion.div>
      </div>
    </div>
  )
}

function Pill({
  icon: Icon,
  label,
}: {
  icon: LucideIcon
  label: string
}) {
  return (
    <div className="glass flex items-center gap-2 px-4 py-2 rounded-full text-xs text-base-200">
      <Icon size={13} className="text-accent-400" />
      <span>{label}</span>
    </div>
  )
}

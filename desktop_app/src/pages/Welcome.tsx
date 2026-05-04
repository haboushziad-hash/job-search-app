import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Target, Compass, Compass as CompassFill, Sparkles, type LucideIcon } from 'lucide-react'
import { VantaBackground } from '@/components/VantaBackground'
import { useAppStore } from '@/stores/appStore'
import { ZMark } from '@/components/ZMark'
import { useAppVersion } from '@/lib/updater'

export default function Welcome() {
  const navigate = useNavigate()
  const profile = useAppStore((s) => s.profile)
  const lastRoles = useAppStore((s) => s.lastRoles)
  const hasPriorRun = !!profile && lastRoles.length > 0
  const version = useAppVersion()

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
          <ZMark size={64} className="shadow-2xl shadow-accent-900/50" />
        </motion.div>

        {/* Headline — staggered word reveal with DAMN doing a shout
            (scale punch + glow burst). The framer variants below stagger
            each word and the damn span fires its own punch animation. */}
        {/* Headline — direct delays per child (no parent variant
            orchestration). Variant orchestration was unreliable; this
            explicit approach is bulletproof: each span has its own
            initial/animate with a hardcoded delay. */}
        <h1 className="text-[64px] leading-[1.05] font-semibold tracking-tight text-balance text-center max-w-3xl flex flex-wrap justify-center gap-x-3 gap-y-1">
          <motion.span
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.9, ease: [0.2, 0.8, 0.2, 1] }}
            className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
          >
            Find
          </motion.span>
          <motion.span
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 1.15, ease: [0.2, 0.8, 0.2, 1] }}
            className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
          >
            me
          </motion.span>
          <motion.span
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 1.40, ease: [0.2, 0.8, 0.2, 1] }}
            className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
          >
            some
          </motion.span>
          {/* DAMN — fires AFTER "some" lands. Punch animation: scale up,
              hold the peak briefly, settle. Explicit opacity keyframes
              match scale array so opacity doesn't decouple mid-animation. */}
          <motion.span
            initial={{ opacity: 0, scale: 0.5, rotate: -3, filter: 'blur(3px)' }}
            animate={{
              opacity: [0, 1, 1, 1, 1],
              scale: [0.5, 1.30, 1.30, 0.97, 1],
              rotate: [-3, 2, 1, 0, 0],
              filter: ['blur(3px)', 'blur(0px)', 'blur(0px)', 'blur(0px)', 'blur(0px)'],
            }}
            transition={{
              duration: 1.1,
              delay: 1.75,
              ease: [0.25, 0.8, 0.35, 1.0],
              times: [0, 0.30, 0.55, 0.80, 1],
            }}
            className="bg-gradient-to-r from-accent-300 via-accent-400 to-accent-600 bg-clip-text text-transparent inline-block"
            style={{ textShadow: '0 0 32px rgba(164, 124, 255, 0.4)' }}
          >
            damn
          </motion.span>
          <motion.span
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 3.0, ease: [0.2, 0.8, 0.2, 1] }}
            className="bg-gradient-to-b from-white to-base-300 bg-clip-text text-transparent"
          >
            jobz.
          </motion.span>
        </h1>

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
            {/* Single primary CTA — always routes through /how-it-works.
                Returning users hit a choice page after to pick between
                re-running with their saved profile or starting fresh.
                First-time users go straight from /how-it-works to /setup. */}
            <button
              onClick={() => navigate('/how-it-works')}
              className="group relative inline-flex items-center gap-2.5
                         px-7 py-3.5 rounded-full
                         bg-white text-base-950 font-medium text-sm
                         shadow-2xl shadow-black/40
                         hover:shadow-accent-500/30
                         transition-all duration-300 hover:scale-[1.02]
                         active:scale-[0.98]"
            >
              <span>{hasPriorRun ? 'Run Search' : 'Get started'}</span>
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
            v{version}
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

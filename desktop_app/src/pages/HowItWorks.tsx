import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import {
  Sparkles, Search, Target, Shield, Zap, Layers,
  ArrowRight, ArrowLeft,
} from 'lucide-react'
import { ZMark } from '@/components/ZMark'
import { useAppStore } from '@/stores/appStore'

export default function HowItWorks() {
  const navigate = useNavigate()
  // Returning users (saved profile + at least one prior run) get
  // forked to /start-search where they pick re-run vs. fresh build.
  // First-timers go straight to /setup for resume upload.
  const profile = useAppStore((s) => s.profile)
  const lastRoles = useAppStore((s) => s.lastRoles)
  const hasPriorRun = !!profile && profile.keywords.length > 0 && lastRoles.length > 0
  const continueDest = hasPriorRun ? '/start-search' : '/setup'

  return (
    <div className="min-h-screen w-full flex items-start justify-center px-6 py-7">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className="glass-strong w-full max-w-3xl rounded-2xl p-8"
      >
        {/* Header */}
        <div className="flex items-center gap-3 mb-1">
          <ZMark size={40} className="shadow-lg shadow-accent-900/40" />
          <h1 className="text-2xl font-semibold tracking-tight">How it works</h1>
        </div>
        <p className="text-sm text-base-400 mb-6">
          A quick walkthrough before we start — so you know exactly what's happening behind the scenes.
        </p>

        {/* Steps */}
        <div className="space-y-3.5">
          <Step
            num="1"
            icon={Sparkles}
            title="You upload your resume(s)"
            body={
              <>
                An AI reads each resume carefully and pulls out your skills, experience, and the
                kinds of work you're targeting. You can also paste extra context — target titles,
                accomplishments, or even job descriptions of roles you've loved.{' '}
                <span className="text-base-100 font-medium">Your data stays on your machine</span>{' '}
                — never used for AI training.
              </>
            }
          />

          <Step
            num="2"
            icon={Search}
            title="We scrape thousands of fresh job listings"
            body={
              <>
                Our search engine queries multiple major job platforms in parallel, pulling
                thousands of recently-posted roles. We respect rate limits and only collect
                publicly available data.
              </>
            }
          />

          <Step
            num="3"
            icon={Layers}
            title="A multi-stage filter narrows the noise"
            body={
              <>
                Before any AI scoring, we apply hard filters (salary, location with smart
                radius matching, posted date, freshness) to drop obvious mismatches cheaply.
                Live URL checks confirm each role is still active.
              </>
            }
          />

          <Step
            num="4"
            icon={Target}
            title="A multi-stage AI cascade scores what's left"
            body={
              <>
                Each surviving role goes through a multi-stage AI evaluation tuned for
                precision. We use frontier-grade language models with custom rubrics built
                specifically for job-fit assessment — not a generic "is this a match" prompt.
              </>
            }
          />

          <Step
            num="5"
            icon={Zap}
            title="You see results bucketed by tier"
            body={
              <>
                Roles are grouped into{' '}
                <span className="text-tier-strong font-medium">STRONG</span>{' '}
                (DAMN! Apply!) ·{' '}
                <span className="text-tier-good font-medium">GOOD</span>{' '}
                (yeah, apply) ·{' '}
                <span className="text-tier-maybe font-medium">MAYBE</span>{' '}
                (hmm, read it) ·{' '}
                <span className="text-base-300 font-medium">STRETCH</span>{' '}
                (ehhhhh) so you know where to focus first.
                Each role's tier reflects fit across function, seniority, skills, salary,
                location, and your stated preferences.
              </>
            }
          />
        </div>

        {/* Privacy + cost callout */}
        <div className="mt-6 p-3.5 rounded-xl bg-accent-500/[0.08] border border-accent-500/[0.20]">
          <div className="flex items-center gap-2 mb-1">
            <Shield size={13} className="text-accent-400" />
            <h3 className="text-sm font-medium text-base-100">Privacy + cost</h3>
          </div>
          <p className="text-xs text-base-300 leading-relaxed">
            Your resume and search results live on your machine. Costs are managed centrally —
            you don't pay per search, and there's no subscription during the test phase.
          </p>
        </div>

        {/* Built by + nav */}
        <div className="flex items-center justify-between mt-6 pt-4 border-t border-white/[0.06]">
          <button
            onClick={() => navigate('/welcome')}
            className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
          >
            <ArrowLeft size={14} /> Back
          </button>

          <div className="text-xs text-base-500">
            Built by{' '}
            <span className="text-base-300 font-medium">Ziad Haboush</span>
          </div>

          <button
            onClick={() => navigate(continueDest)}
            className="flex items-center gap-2 px-6 py-2.5 rounded-lg
                       bg-white text-base-950 font-medium text-sm
                       hover:bg-base-200 transition-colors
                       shadow-lg shadow-black/30"
          >
            Continue <ArrowRight size={14} />
          </button>
        </div>
      </motion.div>
    </div>
  )
}

function Step({
  num,
  icon: Icon,
  title,
  body,
}: {
  num: string
  icon: React.ElementType
  title: string
  body: React.ReactNode
}) {
  return (
    <div className="flex gap-4">
      <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-white/[0.04]
                      border border-white/[0.06]
                      flex flex-col items-center justify-center">
        <Icon size={15} className="text-accent-400" />
      </div>
      <div className="flex-1 min-w-0">
        <h3 className="text-base font-medium text-base-100 mb-1 flex items-center gap-2">
          <span className="text-base-500 text-sm font-mono">{num}</span>
          <span>{title}</span>
        </h3>
        <p className="text-sm text-base-300 leading-relaxed">{body}</p>
      </div>
    </div>
  )
}

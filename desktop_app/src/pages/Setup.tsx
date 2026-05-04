import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import {
  Upload, FileText, ArrowRight, ArrowLeft, X, Check,
  Sparkles, Target, Shield, Zap, MapPin, MessageSquare,
  Lightbulb, Loader2,
} from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { startProfileBuild, ApiError } from '@/services/api'
import { useAppStore } from '@/stores/appStore'

interface UploadedResume {
  file: File         // keep raw File for upload
  filename: string
  size: number
}

interface LocationChip {
  id: string
  name: string
  radius: number
}

// Pre-defined metro presets for one-click setup. Keep US-focused for v1.
// Richmond + Washington at the top because that's where the test cohort
// lives. Other US metros after, alphabetical.
const METRO_PRESETS: { label: string; locations: string[] }[] = [
  { label: 'Richmond, VA',                locations: ['Richmond VA'] },
  { label: 'Washington, DC',              locations: ['Washington DC'] },
  { label: 'DMV (DC + MD + VA)',          locations: ['Washington DC', 'Arlington VA', 'Bethesda MD', 'Tysons VA'] },
  { label: 'Atlanta',                     locations: ['Atlanta GA', 'Alpharetta GA', 'Marietta GA'] },
  { label: 'Austin',                      locations: ['Austin TX', 'Round Rock TX'] },
  { label: 'Boston',                      locations: ['Boston MA', 'Cambridge MA', 'Waltham MA'] },
  { label: 'Chicago',                     locations: ['Chicago IL', 'Evanston IL', 'Schaumburg IL'] },
  { label: 'Dallas / Fort Worth',         locations: ['Dallas TX', 'Plano TX', 'Frisco TX', 'Fort Worth TX'] },
  { label: 'Denver',                      locations: ['Denver CO', 'Boulder CO'] },
  { label: 'Houston',                     locations: ['Houston TX', 'Sugar Land TX'] },
  { label: 'LA / Orange County',          locations: ['Los Angeles CA', 'Pasadena CA', 'Santa Monica CA', 'Irvine CA'] },
  { label: 'Miami / South FL',            locations: ['Miami FL', 'Fort Lauderdale FL', 'Boca Raton FL'] },
  { label: 'NYC Metro',                   locations: ['New York NY', 'Jersey City NJ', 'Newark NJ', 'Stamford CT'] },
  { label: 'Philadelphia',                locations: ['Philadelphia PA', 'King of Prussia PA'] },
  { label: 'Phoenix',                     locations: ['Phoenix AZ', 'Scottsdale AZ', 'Tempe AZ'] },
  { label: 'Raleigh / Durham',            locations: ['Raleigh NC', 'Durham NC', 'Chapel Hill NC'] },
  { label: 'San Diego',                   locations: ['San Diego CA', 'Carlsbad CA'] },
  { label: 'Seattle',                     locations: ['Seattle WA', 'Bellevue WA', 'Redmond WA'] },
  { label: 'SF Bay Area',                 locations: ['San Francisco CA', 'Oakland CA', 'San Jose CA', 'Palo Alto CA'] },
]

// Common US cities for autocomplete — covers all major metros + popular smaller ones
const CITY_AUTOCOMPLETE: string[] = [
  // East Coast / Mid-Atlantic
  'Washington DC', 'Arlington VA', 'Alexandria VA', 'Tysons VA', 'McLean VA', 'Reston VA',
  'Fairfax VA', 'Bethesda MD', 'Rockville MD', 'Silver Spring MD', 'Baltimore MD',
  'Annapolis MD', 'Richmond VA', 'Charlottesville VA', 'Norfolk VA', 'Virginia Beach VA',
  'Raleigh NC', 'Durham NC', 'Charlotte NC', 'Greensboro NC',
  'Atlanta GA', 'Savannah GA', 'Charleston SC', 'Columbia SC',
  // Northeast
  'New York NY', 'Brooklyn NY', 'Manhattan NY', 'Queens NY', 'Bronx NY', 'Long Island NY',
  'Jersey City NJ', 'Newark NJ', 'Hoboken NJ', 'Princeton NJ', 'Stamford CT', 'Hartford CT',
  'Boston MA', 'Cambridge MA', 'Waltham MA', 'Worcester MA',
  'Philadelphia PA', 'Pittsburgh PA', 'King of Prussia PA',
  'Providence RI', 'Portland ME', 'Burlington VT',
  // Florida
  'Miami FL', 'Orlando FL', 'Tampa FL', 'Jacksonville FL', 'Fort Lauderdale FL', 'Tallahassee FL',
  // Midwest
  'Chicago IL', 'Evanston IL', 'Schaumburg IL', 'Naperville IL',
  'Detroit MI', 'Ann Arbor MI', 'Grand Rapids MI',
  'Minneapolis MN', 'St Paul MN', 'Cleveland OH', 'Columbus OH', 'Cincinnati OH',
  'Indianapolis IN', 'Milwaukee WI', 'Madison WI', 'Kansas City MO', 'St Louis MO',
  // South / Texas
  'Austin TX', 'Houston TX', 'Dallas TX', 'San Antonio TX', 'Fort Worth TX', 'Plano TX',
  'Nashville TN', 'Memphis TN', 'New Orleans LA', 'Birmingham AL', 'Louisville KY',
  'Oklahoma City OK', 'Tulsa OK',
  // Mountain / West
  'Denver CO', 'Boulder CO', 'Colorado Springs CO',
  'Salt Lake City UT', 'Phoenix AZ', 'Scottsdale AZ', 'Tucson AZ', 'Las Vegas NV', 'Reno NV',
  'Albuquerque NM', 'Boise ID',
  // West Coast
  'San Francisco CA', 'Oakland CA', 'San Jose CA', 'Palo Alto CA', 'Mountain View CA',
  'Sunnyvale CA', 'Santa Clara CA', 'Berkeley CA', 'Los Angeles CA', 'Santa Monica CA',
  'Pasadena CA', 'Irvine CA', 'San Diego CA', 'Sacramento CA', 'Fresno CA',
  'Seattle WA', 'Bellevue WA', 'Redmond WA', 'Tacoma WA', 'Spokane WA',
  'Portland OR', 'Eugene OR',
  'Honolulu HI', 'Anchorage AK',
  // Remote
  'Remote', 'Remote (US)',
]

export default function Setup() {
  const navigate = useNavigate()
  const [step, setStep] = useState(1)
  // setProfile retained for future use (e.g. clearing the cached profile
  // when the user uploads a new resume). Currently unused — Building.tsx
  // owns the profile-setting flow after the async build completes.
  const _setProfile = useAppStore((s) => s.setProfile); void _setProfile

  // v0.1.4 Phase 15a: persist preferences across profile rebuilds. When
  // the user is rebuilding from a new resume, we pre-populate salary,
  // arrangements, and locations from their PRIOR profile. This prevents
  // the $130K -> $120K -> $100K drift that happens when AI re-infers
  // salary on each rebuild and the user has to re-enter it. Manual user
  // input still wins (this just provides better defaults).
  const priorProfile = useAppStore((s) => s.profile)
  const priorArrangementsSet = new Set(priorProfile?.work_arrangements || [])

  const [resumes, setResumes] = useState<UploadedResume[]>([])
  const [extraContext, setExtraContext] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // Default $100K — reasonable for the test cohort (DC/VA professional roles).
  // User can drag down to $0 (no minimum) or up to $350K.
  // Pre-populate from prior profile if rebuilding (v0.1.4 Phase 15a).
  const [salaryMin, setSalaryMin] = useState(
    priorProfile?.salary_minimum ?? 100_000
  )
  const [arrangements, setArrangements] = useState(
    priorProfile?.work_arrangements && priorProfile.work_arrangements.length > 0
      ? {
          remote: priorArrangementsSet.has('remote'),
          hybrid: priorArrangementsSet.has('hybrid'),
          onsite: priorArrangementsSet.has('on-site') || priorArrangementsSet.has('onsite'),
        }
      : { remote: true, hybrid: true, onsite: false }
  )
  const [locations, setLocations] = useState<LocationChip[]>(
    (priorProfile?.acceptable_locations || []).map((name, idx) => ({
      name,
      radius: priorProfile?.acceptable_location_radii?.[idx] ?? 50,
    }))
  )
  const [locationInput, setLocationInput] = useState('')

  const handleFiles = (files: FileList | null) => {
    if (!files) return
    const newOnes = Array.from(files)
      .filter((f) => /\.(pdf|docx|txt|md)$/i.test(f.name))
      .map((f) => ({ file: f, filename: f.name, size: f.size }))
    setResumes([...resumes, ...newOnes])
  }

  const addLocation = (name: string) => {
    if (!name.trim()) return
    setLocations([
      ...locations,
      { id: crypto.randomUUID(), name: name.trim(), radius: 50 },
    ])
    setLocationInput('')
  }

  const addPreset = (preset: typeof METRO_PRESETS[number]) => {
    const newOnes = preset.locations
      .filter((l) => !locations.find((x) => x.name.toLowerCase() === l.toLowerCase()))
      .map((l) => ({ id: crypto.randomUUID(), name: l, radius: 50 }))
    setLocations([...locations, ...newOnes])
  }

  const updateRadius = (id: string, radius: number) => {
    setLocations(locations.map((l) => (l.id === id ? { ...l, radius } : l)))
  }

  const removeLocation = (id: string) => {
    setLocations(locations.filter((l) => l.id !== id))
  }

  // Show location section only if user wants hybrid or on-site (remote-only doesn't need)
  const showLocationSection = arrangements.hybrid || arrangements.onsite

  const finish = async () => {
    if (resumes.length === 0) {
      toast.error('Please upload at least one resume.')
      return
    }
    setSubmitting(true)
    try {
      const start = await startProfileBuild({
        files: resumes.map((r) => r.file),
        extraContext,
        salaryMinimum: salaryMin,
        workArrangements: Object.entries(arrangements)
          .filter(([, v]) => v)
          .map(([k]) => (k === 'onsite' ? 'on-site' : k)),
        acceptableLocations: locations.map((l) => l.name),
        acceptableLocationRadii: locations.map((l) => l.radius),
      })
      // Pass build_id to /building via history.state — Building page reads it
      navigate('/building', { state: { buildId: start.build_id } })
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.detail
          : e instanceof Error
            ? e.message
            : 'Unknown error'
      toast.error(`Profile build failed: ${msg}`)
      setSubmitting(false)
    }
  }

  // Step 1 is content-heavy — use a wide 2-column layout.
  // Steps 2 & 3 are simpler — keep them narrower for focus.
  const containerWidth = step === 1 ? 'max-w-6xl' : 'max-w-2xl'

  return (
    <div className="min-h-screen w-full flex items-start justify-center px-6 py-6">
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4 }}
        className={cn('glass-strong w-full rounded-2xl p-7', containerWidth)}
      >
        {/* Step indicator */}
        <div className="flex items-center gap-2 mb-6">
          {[1, 2, 3].map((s) => (
            <div
              key={s}
              className={cn(
                'h-1 flex-1 rounded-full transition-colors',
                s <= step ? 'bg-accent-500' : 'bg-white/[0.08]'
              )}
            />
          ))}
        </div>

        {/* ============================================================
            STEP 1 — RESUME UPLOAD (2-column layout)
            ============================================================ */}
        {step === 1 && (
          <>
            <div className="mb-5">
              <h1 className="text-2xl font-semibold tracking-tight mb-1">
                Upload your resume(s)
              </h1>
              <p className="text-sm text-base-300">
                This is the most important step — your resume is what makes the rest personal.
              </p>
            </div>

            {/* 2-column layout — context/tips on LEFT, actions on RIGHT */}
            <div className="grid grid-cols-1 lg:grid-cols-5 gap-5">
              {/* LEFT COLUMN — context cards + tips, stretched to match right column height */}
              <div className="lg:col-span-2 flex flex-col gap-3">
                {/* Why this matters — context cards (grow to fill) */}
                <div className="flex flex-col flex-1 min-h-0">
                  <h3 className="text-[10px] uppercase tracking-wider text-base-400 font-medium mb-2">
                    What we do with this
                  </h3>
                  <div className="flex flex-col gap-1.5 flex-1">
                    <ContextCard
                      icon={Sparkles}
                      title="Generate your keywords"
                      desc="We'll extract 30–50 personalized search terms from your background — far better than generic AI keywords."
                    />
                    <ContextCard
                      icon={Target}
                      title="Score every role"
                      desc="Each scraped role gets compared against your specific skills, seniority, and target functions."
                    />
                    <ContextCard
                      icon={Zap}
                      title="Match to the best version"
                      desc="Upload multiple resumes — we'll tell you which one fits each role best."
                    />
                    <ContextCard
                      icon={Shield}
                      title="Stays on your machine"
                      desc="Processed locally, never used for AI training. Only our own scoring uses it."
                    />
                  </div>
                </div>

                {/* Tips */}
                <div className="glass-subtle rounded-xl p-4 flex-shrink-0">
                  <div className="flex items-center gap-2 mb-2.5">
                    <Lightbulb size={13} className="text-tier-good" />
                    <h3 className="text-[10px] uppercase tracking-wider text-base-300 font-medium">
                      Tips for best results
                    </h3>
                  </div>
                  <div className="space-y-1.5">
                    <Tip
                      bold="Use your most recent resume."
                      text="Older ones miss current job postings."
                    />
                    <Tip
                      bold="Include accomplishments."
                      text={`"Led \$5M AI engagement" beats "Strategy work."`}
                    />
                    <Tip
                      bold="Upload multiple versions."
                      text="Consulting + AI flavored? Upload both."
                    />
                    <Tip
                      bold="PDF works best."
                      text="DOCX with tables can parse imperfectly."
                    />
                  </div>
                </div>
              </div>

              {/* RIGHT COLUMN — actions (upload + textarea), stretched to match left height */}
              <div className="lg:col-span-3 flex flex-col gap-4">
                {/* Upload zone */}
                <div className="flex-shrink-0">
                  <label
                    htmlFor="file-upload"
                    className="flex flex-col items-center justify-center
                               border-2 border-dashed border-white/[0.12]
                               rounded-xl py-7 px-8 cursor-pointer
                               hover:border-accent-400/40 hover:bg-white/[0.02]
                               transition-colors"
                  >
                    <Upload size={26} className="text-base-400 mb-2" />
                    <p className="text-sm text-base-200 font-medium mb-0.5">
                      Drop your resume here or click to browse
                    </p>
                    <p className="text-xs text-base-500">PDF, DOCX, or TXT — up to 5 files</p>
                    <input
                      id="file-upload"
                      type="file"
                      multiple
                      accept=".pdf,.docx,.txt,.md"
                      className="sr-only"
                      onChange={(e) => handleFiles(e.target.files)}
                    />
                  </label>

                  {/* How AI uses your resume */}
                  <div className="mt-2.5 flex items-start gap-2 px-3 py-2 rounded-lg
                                  bg-accent-500/[0.06] border border-accent-500/[0.15]">
                    <Sparkles size={12} className="text-accent-400 flex-shrink-0 mt-0.5" />
                    <p className="text-[11px] text-base-300 leading-snug">
                      <span className="text-base-100 font-medium">How AI uses this:</span>{' '}
                      Extracts your skills, seniority, and target functions to build your profile and
                      generate 30-50 personalized keywords. Used once, never for AI training.
                    </p>
                  </div>

                  {/* Uploaded files */}
                  {resumes.length > 0 && (
                    <div className="mt-4 space-y-2">
                      {resumes.map((r, idx) => (
                        <div key={idx} className="glass-subtle flex items-center gap-3 px-4 py-2.5 rounded-lg">
                          <FileText size={14} className="text-accent-400" />
                          <span className="text-sm flex-1 truncate">{r.filename}</span>
                          <span className="text-xs text-base-500">
                            {(r.size / 1024).toFixed(0)} KB
                          </span>
                          <button
                            onClick={() => setResumes(resumes.filter((_, i) => i !== idx))}
                            className="text-base-500 hover:text-base-200"
                          >
                            <X size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Extra context textarea — grows to fill remaining height */}
                <div className="flex flex-col flex-1 min-h-0">
                  <div className="flex items-center gap-2 mb-2">
                    <MessageSquare size={14} className="text-accent-400" />
                    <label htmlFor="extra-context" className="text-xs uppercase tracking-wider text-base-300 font-medium">
                      Add extra context
                    </label>
                    <span className="text-[10px] text-base-500">(optional, recommended)</span>
                  </div>
                  <p className="text-xs text-base-400 mb-2 leading-snug">
                    Paste anything you'd want a hiring manager to know but isn't fully captured in your resume — accomplishments to emphasize, target job titles, even job descriptions of roles you've loved.
                  </p>

                  {/* How AI uses extra context */}
                  <div className="mb-2.5 flex items-start gap-2 px-3 py-2 rounded-lg
                                  bg-accent-500/[0.06] border border-accent-500/[0.15]">
                    <Sparkles size={12} className="text-accent-400 flex-shrink-0 mt-0.5" />
                    <div className="text-[11px] text-base-300 leading-snug">
                      <span className="text-base-100 font-medium">How AI uses this:</span>{' '}
                      Strongest signal of intent — overrides resume when conflicting.
                      Stated targets become Tier 1 keywords · pasted JDs become match templates · "Avoid X" becomes a score penalty.
                    </div>
                  </div>

                  <textarea
                    id="extra-context"
                    value={extraContext}
                    onChange={(e) => setExtraContext(e.target.value)}
                    maxLength={4000}
                    placeholder={`Examples:
• "I'm targeting roles like 'Operations Manager,' 'Project Manager,' or 'AI Strategy Consultant.'"
• "I grew my territory revenue 44% in 3 years — want this featured."
• "I love roles like this one [paste a JD] — surface anything similar."
• "Avoid pure engineering or hands-on technical roles — I'm strategy/people-focused."`}
                    className="w-full px-4 py-3 rounded-lg
                               bg-white/[0.03] border border-white/[0.08]
                               text-sm text-base-100 placeholder:text-base-600
                               focus:outline-none focus:border-accent-500/50
                               font-sans leading-relaxed resize-none
                               flex-1 min-h-[140px]"
                  />
                  <div className="flex justify-between mt-2 text-[10px]">
                    <span className="text-base-500">
                      Plain text · pastes from PDFs and websites work fine
                    </span>
                    <span className="text-base-500 font-mono tabular-nums">
                      {extraContext.length} / 4000
                    </span>
                  </div>
                </div>
              </div>
            </div>

            <div className="flex justify-between mt-10">
              <button
                onClick={() => navigate('/welcome')}
                className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                onClick={() => setStep(2)}
                disabled={resumes.length === 0}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg
                           bg-white text-base-950 font-medium text-sm
                           disabled:opacity-40 disabled:cursor-not-allowed
                           hover:bg-base-200 transition-colors"
              >
                Continue <ArrowRight size={14} />
              </button>
            </div>
          </>
        )}

        {/* ============================================================
            STEP 2 — PREFERENCES
            ============================================================ */}
        {step === 2 && (
          <>
            <h1 className="text-3xl font-semibold tracking-tight mb-2">Preferences</h1>
            <p className="text-sm text-base-400 mb-8">
              Tell us your hard requirements. Salary is a soft floor — we'll still surface
              roles slightly below if they're great matches.
            </p>

            {/* Salary */}
            <label className="block">
              <span className="text-xs uppercase tracking-wider text-base-400">
                Minimum salary
              </span>
              <div className="mt-2 flex items-center gap-3">
                <input
                  type="range"
                  min={0}
                  max={350000}
                  step={5000}
                  value={salaryMin}
                  onChange={(e) => setSalaryMin(Number(e.target.value))}
                  className="flex-1 accent-accent-500"
                />
                <span className="font-mono text-base-100 text-lg w-28 text-right">
                  {salaryMin === 0 ? 'No min' : `$${(salaryMin / 1000).toFixed(0)}K`}
                </span>
              </div>
              <p className="text-xs text-base-500 mt-1.5">
                {salaryMin === 0
                  ? 'No salary filter applied — all roles included regardless of pay.'
                  : 'Roles within ~15% of this still appear with a small score penalty.'}
              </p>
            </label>

            {/* Work arrangements */}
            <div className="mt-8">
              <span className="text-xs uppercase tracking-wider text-base-400">
                Work arrangements
              </span>
              <div className="mt-3 flex gap-2">
                {(['remote', 'hybrid', 'onsite'] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setArrangements({ ...arrangements, [k]: !arrangements[k] })}
                    className={cn(
                      'px-4 py-2 rounded-lg text-sm capitalize border transition-all',
                      arrangements[k]
                        ? 'bg-accent-500/15 border-accent-500/40 text-accent-300'
                        : 'glass-subtle text-base-400 hover:text-base-200'
                    )}
                  >
                    {arrangements[k] && <Check size={12} className="inline mr-1.5" />}
                    {k === 'onsite' ? 'On-site' : k}
                  </button>
                ))}
              </div>
              <p className="text-xs text-base-500 mt-2">
                {arrangements.remote && !showLocationSection
                  ? 'Remote roles are location-agnostic — we\'ll show roles from anywhere.'
                  : showLocationSection
                  ? 'You\'ll need to specify acceptable locations below for hybrid/on-site roles.'
                  : 'Pick at least one work arrangement to continue.'}
              </p>
            </div>

            {/* Locations — only shown if hybrid or on-site is selected */}
            {showLocationSection && (
              <motion.div
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                className="mt-8"
              >
                <span className="text-xs uppercase tracking-wider text-base-400">
                  Acceptable locations
                </span>
                <p className="text-xs text-base-500 mt-1 mb-3">
                  For hybrid and on-site roles only. Each location includes a radius — we'll
                  match nearby cities automatically.
                </p>

                {/* Quick metro presets */}
                <div className="flex flex-wrap gap-1.5 mb-4">
                  {METRO_PRESETS.map((preset) => (
                    <button
                      key={preset.label}
                      onClick={() => addPreset(preset)}
                      className="text-[11px] px-2.5 py-1 rounded-full
                                 glass-subtle hover:bg-white/[0.08]
                                 text-base-300 hover:text-base-100 transition-colors"
                    >
                      + {preset.label}
                    </button>
                  ))}
                </div>

                {/* Manual add with autocomplete */}
                <LocationAutocomplete
                  value={locationInput}
                  onChange={setLocationInput}
                  onAdd={addLocation}
                  existingLocations={locations.map((l) => l.name.toLowerCase())}
                />

                {/* Location chips with radius sliders */}
                {locations.length > 0 && (
                  <div className="mt-4 space-y-2">
                    {locations.map((loc) => (
                      <motion.div
                        key={loc.id}
                        initial={{ opacity: 0, y: 4 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0 }}
                        className="glass-subtle flex items-center gap-3 px-3 py-2 rounded-lg"
                      >
                        <MapPin size={13} className="text-accent-400 flex-shrink-0" />
                        <span className="text-sm flex-shrink-0 min-w-[140px] truncate">
                          {loc.name}
                        </span>
                        <div className="flex-1 flex items-center gap-2.5">
                          <span className="text-[10px] tracking-wider text-base-500 uppercase">
                            Radius
                          </span>
                          <input
                            type="range"
                            min={5}
                            max={75}
                            step={5}
                            value={loc.radius}
                            onChange={(e) => updateRadius(loc.id, Number(e.target.value))}
                            className="flex-1 accent-accent-500"
                          />
                          <span className="text-xs font-mono text-base-300 w-12 text-right">
                            {loc.radius} mi
                          </span>
                        </div>
                        <button
                          onClick={() => removeLocation(loc.id)}
                          className="text-base-500 hover:text-base-200"
                        >
                          <X size={14} />
                        </button>
                      </motion.div>
                    ))}
                  </div>
                )}
              </motion.div>
            )}

            <div className="flex justify-between mt-10">
              <button
                onClick={() => setStep(1)}
                className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                onClick={() => setStep(3)}
                disabled={showLocationSection && locations.length === 0}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg
                           bg-white text-base-950 font-medium text-sm
                           disabled:opacity-40 disabled:cursor-not-allowed
                           hover:bg-base-200 transition-colors"
              >
                Continue <ArrowRight size={14} />
              </button>
            </div>
          </>
        )}

        {/* ============================================================
            STEP 3 — SUMMARY
            ============================================================ */}
        {step === 3 && (
          <>
            <h1 className="text-3xl font-semibold tracking-tight mb-2">You're all set</h1>
            <p className="text-sm text-base-400 mb-8">
              We'll generate keywords from your resume(s) and run your first search.
              You can always tweak preferences from Settings.
            </p>

            <div className="glass-subtle rounded-xl p-5 space-y-3">
              <SummaryRow
                label="Resumes uploaded"
                value={`${resumes.length} file${resumes.length === 1 ? '' : 's'}`}
              />
              <SummaryRow label="Minimum salary" value={`$${(salaryMin / 1000).toFixed(0)}K`} />
              <SummaryRow
                label="Work arrangements"
                value={
                  Object.entries(arrangements)
                    .filter(([, v]) => v)
                    .map(([k]) => (k === 'onsite' ? 'On-site' : k))
                    .join(', ')
                }
              />
              {showLocationSection && (
                <SummaryRow
                  label="Acceptable locations"
                  value={
                    locations.length === 0
                      ? 'none added'
                      : locations
                          .map((l) => `${formatCityState(l.name)} (${l.radius} Mile Radius)`)
                          .join(', ')
                  }
                />
              )}
              {extraContext.trim().length > 0 && (
                <SummaryRow
                  label="Extra context"
                  value={`${extraContext.trim().length} characters added`}
                />
              )}
            </div>

            <div className="flex justify-between mt-10">
              <button
                onClick={() => setStep(2)}
                className="flex items-center gap-2 px-4 py-2 text-sm text-base-400 hover:text-base-200"
              >
                <ArrowLeft size={14} /> Back
              </button>
              <button
                onClick={finish}
                disabled={submitting}
                className="flex items-center gap-2 px-6 py-2.5 rounded-lg
                           bg-white text-base-950 font-medium text-sm
                           disabled:opacity-60 disabled:cursor-not-allowed
                           hover:bg-base-200 transition-colors"
              >
                {submitting ? (
                  <>
                    <Loader2 size={14} className="animate-spin" /> Building profile...
                  </>
                ) : (
                  <>
                    Finish setup <ArrowRight size={14} />
                  </>
                )}
              </button>
            </div>
          </>
        )}
      </motion.div>
    </div>
  )
}

function ContextCard({
  icon: Icon,
  title,
  desc,
}: {
  icon: React.ElementType
  title: string
  desc: string
}) {
  return (
    <div className="glass-subtle rounded-lg p-4 flex-1 flex flex-col justify-center min-h-0">
      <div className="flex items-center gap-2 mb-1.5">
        <Icon size={14} className="text-accent-400" />
        <h3 className="text-xs font-medium text-base-100 tracking-wide">{title}</h3>
      </div>
      <p className="text-[11px] text-base-400 leading-relaxed">{desc}</p>
    </div>
  )
}

function Tip({ bold, text }: { bold: string; text: string }) {
  return (
    <p className="text-xs text-base-400 leading-relaxed">
      <span className="text-base-100 font-medium">{bold}</span>{' '}
      {text}
    </p>
  )
}

function LocationAutocomplete({
  value,
  onChange,
  onAdd,
  existingLocations,
}: {
  value: string
  onChange: (v: string) => void
  onAdd: (v: string) => void
  existingLocations: string[]
}) {
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [highlightedIdx, setHighlightedIdx] = useState(0)

  const suggestions = value.trim().length >= 1
    ? CITY_AUTOCOMPLETE.filter(
        (city) =>
          city.toLowerCase().includes(value.toLowerCase()) &&
          !existingLocations.includes(city.toLowerCase())
      ).slice(0, 6)
    : []

  const handleAdd = (text: string) => {
    onAdd(text)
    setShowSuggestions(false)
    setHighlightedIdx(0)
  }

  return (
    <div className="relative">
      <div className="flex gap-2">
        <input
          type="text"
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
            setShowSuggestions(true)
            setHighlightedIdx(0)
          }}
          onFocus={() => setShowSuggestions(true)}
          onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              if (suggestions.length > 0 && showSuggestions) {
                handleAdd(suggestions[highlightedIdx])
              } else if (value.trim()) {
                handleAdd(value)
              }
            } else if (e.key === 'ArrowDown') {
              e.preventDefault()
              setHighlightedIdx((i) => Math.min(i + 1, suggestions.length - 1))
            } else if (e.key === 'ArrowUp') {
              e.preventDefault()
              setHighlightedIdx((i) => Math.max(i - 1, 0))
            } else if (e.key === 'Escape') {
              setShowSuggestions(false)
            }
          }}
          placeholder="Type a city — e.g. Richmond VA, Charlotte NC, Denver CO..."
          className="flex-1 px-3 py-2 rounded-lg
                     bg-white/[0.04] border border-white/[0.08]
                     text-sm placeholder:text-base-500
                     focus:outline-none focus:border-accent-500/50"
        />
        <button
          onClick={() => value.trim() && handleAdd(value)}
          disabled={!value.trim()}
          className="px-4 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.10]
                     text-sm text-base-200 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Add
        </button>
      </div>

      {/* Suggestion popover */}
      {showSuggestions && suggestions.length > 0 && (
        <div className="absolute top-full left-0 right-16 mt-1 z-10
                        glass-strong rounded-lg border border-white/[0.10]
                        max-h-64 overflow-y-auto py-1">
          {suggestions.map((city, idx) => (
            <button
              key={city}
              onMouseDown={() => handleAdd(city)}
              onMouseEnter={() => setHighlightedIdx(idx)}
              className={cn(
                'w-full px-3 py-2 text-left text-sm transition-colors',
                idx === highlightedIdx
                  ? 'bg-accent-500/[0.10] text-base-50'
                  : 'text-base-300 hover:text-base-100'
              )}
            >
              <MapPin size={11} className="inline mr-2 text-accent-400" />
              {city}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between items-start gap-4 text-sm">
      <span className="text-base-400 flex-shrink-0">{label}</span>
      <span className="text-base-100 font-medium text-right">{value}</span>
    </div>
  )
}

/** Pretty-format a city name with state code.
 *  "Richmond VA"   -> "Richmond, VA"
 *  "Richmond, VA"  -> "Richmond, VA"  (unchanged)
 *  "Washington DC" -> "Washington, DC"
 *  "San Francisco" -> "San Francisco"  (no state, unchanged)
 */
function formatCityState(name: string): string {
  // If already has a comma, just normalize whitespace + uppercase the state code
  const commaMatch = name.match(/^(.+?),\s*([A-Z]{2})\s*$/i)
  if (commaMatch) {
    return `${commaMatch[1].trim()}, ${commaMatch[2].toUpperCase()}`
  }
  // Otherwise look for a trailing 2-letter state code with no comma
  const trailingMatch = name.match(/^(.+?)\s+([A-Z]{2})\s*$/i)
  if (trailingMatch) {
    return `${trailingMatch[1].trim()}, ${trailingMatch[2].toUpperCase()}`
  }
  return name
}

/**
 * Stats page (v0.2.0)
 *
 * Aggregate analytics for a completed search run. Reads the full audit
 * JSON from the backend and renders charts using recharts. Run selector
 * at the top defaults to the most recent run; user can switch to view
 * stats for any historical run from runs.db.
 *
 * Six sections:
 *   1. Funnel Health     — pipeline drop-off + coverage %
 *   2. Salary Insights   — avg/median + distribution histogram
 *   3. Arrangement Mix   — Remote/Hybrid/On-site/Unknown pie
 *   4. Top Companies     — bar chart of qualifying counts by company
 *   5. Top Keywords      — bar chart of which search terms surfaced
 *                          the most qualifying roles, plus zero-match
 *                          keywords flagged for pruning
 *   6. Sources           — per-board funnel: scraped → qualifying
 *
 * Data is computed client-side from the audit JSON (no server-side
 * aggregation). The /runs/{id}/audit endpoint streams the raw audit
 * file; this page slices it into chart-ready shapes.
 */

import { useEffect, useState, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import {
  BarChart, Bar, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, LabelList,
} from 'recharts'
import {
  ArrowLeft, Loader2, AlertCircle, TrendingDown, DollarSign,
  MapPin, Building2, Search, BarChart3, Briefcase, Calendar,
} from 'lucide-react'
import { listRuns, getRunAudit, ApiError } from '@/services/api'
import type { RunHistoryItem } from '@/services/api'

// Color palette — picks from app's tier colors plus complementary hues.
// Bright/saturated for visual interest, but readable on the dark theme.
const COLORS = {
  strong: '#22c55e',      // green-500 — STRONG tier
  good: '#facc15',        // yellow-400 — GOOD tier
  maybe: '#3b82f6',       // blue-500 — MAYBE tier
  stretch: '#94a3b8',     // slate-400 — STRETCH tier
  accent: '#a78bfa',      // violet-400 — primary accent
  pink: '#f472b6',        // pink-400
  cyan: '#22d3ee',        // cyan-400
  orange: '#fb923c',      // orange-400
  emerald: '#34d399',     // emerald-400
  indigo: '#818cf8',      // indigo-400
  rose: '#fb7185',        // rose-400
}

const ARRANGEMENT_COLORS: Record<string, string> = {
  Remote: COLORS.cyan,
  Hybrid: COLORS.accent,
  'On-site': COLORS.orange,
  Unknown: COLORS.stretch,
}

// ----------------------------------------------------------------------------
// Audit data shape (loose — fields may be missing in older runs)
// ----------------------------------------------------------------------------

interface AuditData {
  run_metadata?: {
    run_id?: string
    date?: string
    duration_seconds?: number
    cost_usd?: number
    cache_hit?: boolean
    app_version?: string
  }
  profile_snapshot?: Record<string, unknown>
  pipeline_funnel?: {
    total_scraped?: number
    after_hard_filters?: number
    qualifying_final?: number
    tier_breakdown?: { STRONG?: number; GOOD?: number; MAYBE?: number; STRETCH?: number }
    coverage?: {
      jd_coverage_pct?: number
      salary_coverage_pct?: number
      location_coverage_pct?: number
    }
    per_source_funnel?: Record<string, {
      raw_scraped?: number
      after_dedup?: number
      after_hard_filters?: number
      qualifying_final?: number
    }>
  }
  per_source_health?: Record<string, { roles?: number; [key: string]: unknown }>
  all_qualifying_roles?: Array<{
    score?: number
    tier?: string
    title?: string
    company?: string
    location?: string
    location_type?: string
    salary_min?: number | null
    salary_max?: number | null
    salary_text?: string
    industry?: string
    matched_keyword?: string
    source?: string
    posted_date?: string
    url?: string
  }>
  company_distribution?: Array<{
    company?: string
    roles?: number
    qualifying?: number
    top_score?: number
  }>
  keyword_effectiveness?: Array<{
    keyword?: string
    search_term?: string
    raw_matches?: number | null
    qualifying_matches?: number | null
  }>
}

// ----------------------------------------------------------------------------
// Stat computation — slice the audit into chart-ready data
// ----------------------------------------------------------------------------

function computeStats(audit: AuditData) {
  const qualifying = audit.all_qualifying_roles || []

  // ---- Funnel ----
  // Prefer the actual qualifying role count over the backend's
  // qualifying_final field. They can differ by 1-2 due to backend
  // accounting (qualifying_final counts roles that scored 40+ but the
  // role list filters further). Using qualifying.length keeps Stats
  // and Dashboard counts aligned (both show the same number).
  const funnel = audit.pipeline_funnel || {}
  const funnelData = [
    { stage: 'Scraped', count: funnel.total_scraped || 0 },
    { stage: 'After Filter', count: funnel.after_hard_filters || 0 },
    { stage: 'Qualifying', count: qualifying.length || 0 },
  ]
  const tierBreakdown = funnel.tier_breakdown || {}
  const tierData = [
    { name: 'STRONG', value: tierBreakdown.STRONG || 0, color: COLORS.strong },
    { name: 'GOOD', value: tierBreakdown.GOOD || 0, color: COLORS.good },
    { name: 'MAYBE', value: tierBreakdown.MAYBE || 0, color: COLORS.maybe },
    { name: 'STRETCH', value: tierBreakdown.STRETCH || 0, color: COLORS.stretch },
  ].filter((t) => t.value > 0)

  // ---- Salary ----
  const salaryRoles = qualifying.filter((r) => r.salary_min || r.salary_max)
  const midpoints = salaryRoles
    .map((r) => ((r.salary_min || 0) + (r.salary_max || 0)) / 2)
    .filter((m) => m > 0)
    .sort((a, b) => a - b)
  const avgMin = salaryRoles.length
    ? Math.round(salaryRoles.reduce((s, r) => s + (r.salary_min || 0), 0) / salaryRoles.length)
    : 0
  const avgMax = salaryRoles.length
    ? Math.round(salaryRoles.reduce((s, r) => s + (r.salary_max || 0), 0) / salaryRoles.length)
    : 0
  const median = midpoints.length ? midpoints[Math.floor(midpoints.length / 2)] : 0
  const salaryCoveragePct = qualifying.length
    ? Math.round((salaryRoles.length / qualifying.length) * 100)
    : 0

  // (Remote % is computed below after arrangementCounts is populated.)

  // Average post age in days — replaces the FRESH percentage with a
  // single value users can grok at a glance: "these roles were posted
  // on average X days ago." Lower = fresher = better, opposite of the
  // other coverage stats.
  const now = Date.now()
  const DAY_MS = 24 * 60 * 60 * 1000
  const ages: number[] = []
  for (const r of qualifying) {
    const posted = (r as any).posted_date
    if (!posted) continue
    const t = Date.parse(posted)
    if (isNaN(t) || t > now) continue
    ages.push(Math.floor((now - t) / DAY_MS))
  }
  const avgPostAgeDays = ages.length
    ? Math.round(ages.reduce((s, a) => s + a, 0) / ages.length)
    : 0

  // Distribution histogram — $20K buckets
  const bucketSize = 20000
  const salaryBuckets: Record<number, number> = {}
  for (const r of salaryRoles) {
    const mid = ((r.salary_min || 0) + (r.salary_max || 0)) / 2
    if (mid <= 0) continue
    const bucket = Math.floor(mid / bucketSize) * bucketSize
    salaryBuckets[bucket] = (salaryBuckets[bucket] || 0) + 1
  }
  const salaryHistogram = Object.entries(salaryBuckets)
    .map(([k, v]) => ({ bucket: parseInt(k), count: v, label: `$${parseInt(k) / 1000}K` }))
    .sort((a, b) => a.bucket - b.bucket)

  // ---- Arrangement breakdown ----
  const arrangementCounts: Record<string, number> = {}
  for (const r of qualifying) {
    const raw = (r.location_type || '').trim()
    const key = !raw
      ? 'Unknown'
      : raw.toLowerCase().includes('hybrid')
      ? 'Hybrid'
      : raw.toLowerCase().includes('remote')
      ? 'Remote'
      : raw.toLowerCase().includes('on-site') || raw.toLowerCase().includes('onsite')
      ? 'On-site'
      : 'Unknown'
    arrangementCounts[key] = (arrangementCounts[key] || 0) + 1
  }
  // v0.2.0: drop the "Unknown" bucket from the chart for a cleaner
  // Remote / Hybrid / On-site presentation. Unknown roles still get
  // counted in the qualifying total elsewhere — they're just not their
  // own slice. The JSearch _classify_from_jd helper now catches most
  // would-be Unknowns at scrape time, so this bucket should be small.
  const arrangementData = Object.entries(arrangementCounts)
    .filter(([name]) => name !== 'Unknown')
    .map(([name, value]) => ({ name, value, color: ARRANGEMENT_COLORS[name] || COLORS.stretch }))
    .sort((a, b) => b.value - a.value)

  // Remote %: replaces v0.1.x location coverage in the FunnelCard.
  // Location coverage was always ~100% (pipeline rejects roles without
  // a location) → meaningless. Remote % aligns with the user preference
  // profile — "what fraction of my qualifying roles are remote?"
  const remotePct = qualifying.length
    ? Math.round(((arrangementCounts['Remote'] || 0) / qualifying.length) * 100)
    : 0

  // ---- States ----
  // Use a whitelist of valid US state codes + DC. Without this, regex
  // false-matches kick in: "AI Lab, Office Of Strategy" matches " OF",
  // "United Kingdom" matches " UK", etc. Remote roles don't typically
  // have state codes (they live in `location` as "Remote" or "USA"),
  // so the resulting count is just the location-pinned subset.
  const US_STATES = new Set([
    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
    'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
    'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
    'VA','WA','WV','WI','WY','DC',
  ])
  const stateCounts: Record<string, number> = {}
  let rolesWithState = 0
  for (const r of qualifying) {
    const loc = (r.location || '').toUpperCase()
    if (!loc) continue
    // Find any 2-letter state code that follows a comma or space
    const matches = loc.matchAll(/[,\s]([A-Z]{2})\b/g)
    const found = new Set<string>()
    for (const m of matches) {
      const code = m[1]
      if (US_STATES.has(code)) {
        found.add(code)
      }
    }
    // Each role contributes once per state code it mentions
    if (found.size > 0) {
      rolesWithState += 1
      for (const code of found) {
        stateCounts[code] = (stateCounts[code] || 0) + 1
      }
    }
  }
  const stateData = Object.entries(stateCounts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 10)

  // ---- Top companies ----
  // v0.2.0: compute client-side from qualifying roles. The audit's
  // company_distribution field was sometimes empty/missing depending on
  // backend version, leaving the card showing "No company data". Counting
  // here from the roles we already have is reliable + matches what users
  // see on the Dashboard list.
  const companyCounts: Record<string, { qualifying: number; topScore: number }> = {}
  for (const r of qualifying) {
    const c = (r.company || '').trim()
    if (!c) continue
    if (!companyCounts[c]) companyCounts[c] = { qualifying: 0, topScore: 0 }
    companyCounts[c].qualifying += 1
    const score = r.score || 0
    if (score > companyCounts[c].topScore) companyCounts[c].topScore = score
  }
  // ---- Soft-match company de-dup (display only — backend untouched).
  // Real-world scraper output has fragmentation: "Careers at Marriott"
  // and "Marriott International, Inc" are the same company; "Booz
  // Allen" and "Booz Allen Hamilton" are too. Without merging, the
  // chart understates concentration and overstates breadth ("116
  // companies" when reality is ~108).
  //
  // Normalization rules (cheap + conservative — false positives are
  // worse than false negatives in user-facing stats):
  //   1. Strip "Careers at " / "Jobs at " / "Work at " prefix
  //   2. Strip leading "[number] " (scraper ID artifacts)
  //   3. Strip trailing legal suffixes: Inc, LLC, Corp, Ltd, GmbH,
  //      Co., Group, Holdings, etc.
  //   4. Strip trailing punctuation + whitespace
  //   5. Lowercase + collapse whitespace for the match key
  //
  // For each cluster, the canonical display name is the variant with
  // the most roles (ties broken by shortest length — usually the
  // cleanest form like "Marriott" over "Marriott International").
  const _normalizeCompany = (name: string): string => {
    let s = name.trim()
    // Prefix strip (case-insensitive)
    s = s.replace(/^(careers at|jobs at|work at|join)\s+/i, '')
    // Leading-number strip ("110 Visa U.S.A.")
    s = s.replace(/^\d+\s+/, '')
    // Punctuation around suffix matching
    s = s.replace(/[,.]\s*(?=$|\s)/g, ' ').trim()
    // Trailing legal/structural suffix strip (loop — handles stacked)
    const suffixRe = /\s+(inc|incorporated|llc|l\.l\.c\.|ltd|limited|corp|corporation|co|gmbh|s\.a\.|sa|ag|plc|holdings|holding|group|company|companies|technologies|technology|tech|systems|solutions|services|labs|industries)\s*\.?$/i
    let prev: string
    do { prev = s; s = s.replace(suffixRe, ''); } while (s !== prev)
    // Final trim + collapse whitespace
    return s.replace(/\s+/g, ' ').trim().toLowerCase()
  }
  // Build clusters keyed by normalized name; track each variant's count.
  const clusters: Record<string, { variants: Record<string, number>; topScore: number }> = {}
  for (const [name, v] of Object.entries(companyCounts)) {
    const key = _normalizeCompany(name) || name.toLowerCase()
    if (!clusters[key]) clusters[key] = { variants: {}, topScore: 0 }
    clusters[key].variants[name] = (clusters[key].variants[name] || 0) + v.qualifying
    if (v.topScore > clusters[key].topScore) clusters[key].topScore = v.topScore
  }
  // Second pass: word-boundary prefix merge. Catches "Booz Allen"
  // + "Booz Allen Hamilton" (one is the start-of-words of the other,
  // separated by a space). Sort short→long so the shorter form is
  // always treated as the canonical key absorbed-into.
  const sortedKeys = Object.keys(clusters).sort((a, b) => a.length - b.length)
  const redirect: Record<string, string> = {}
  for (let i = 0; i < sortedKeys.length; i++) {
    const shortKey = redirect[sortedKeys[i]] || sortedKeys[i]
    for (let j = i + 1; j < sortedKeys.length; j++) {
      const longKey = sortedKeys[j]
      if (redirect[longKey]) continue
      if (longKey === shortKey) continue
      if (longKey.startsWith(shortKey + ' ')) {
        redirect[longKey] = shortKey
      }
    }
  }
  // Apply redirects: fold long-form clusters into their short-form base.
  for (const [longKey, shortKey] of Object.entries(redirect)) {
    if (!clusters[longKey] || !clusters[shortKey]) continue
    for (const [variant, count] of Object.entries(clusters[longKey].variants)) {
      clusters[shortKey].variants[variant] = (clusters[shortKey].variants[variant] || 0) + count
    }
    if (clusters[longKey].topScore > clusters[shortKey].topScore) {
      clusters[shortKey].topScore = clusters[longKey].topScore
    }
    delete clusters[longKey]
  }
  // Pick the canonical display name per cluster.
  const _pickCanonical = (variants: Record<string, number>): string => {
    const entries = Object.entries(variants)
    entries.sort((a, b) => {
      if (b[1] !== a[1]) return b[1] - a[1]      // most roles first
      return a[0].length - b[0].length            // tie → shorter wins
    })
    return entries[0][0]
  }
  const mergedCompanies: Record<string, { qualifying: number; topScore: number }> = {}
  for (const cluster of Object.values(clusters)) {
    const display = _pickCanonical(cluster.variants)
    const total = Object.values(cluster.variants).reduce((s, n) => s + n, 0)
    mergedCompanies[display] = { qualifying: total, topScore: cluster.topScore }
  }
  const totalCompanies = Object.keys(mergedCompanies).length
  // Long-tail count: companies that posted exactly 1 qualifying role.
  // Surfaces the "wow N companies but top 8 are all 3-9 roles, so
  // basically everyone else posted 1" story directly in the subtitle.
  const singleRoleCompanies = Object.values(mergedCompanies).filter((v) => v.qualifying === 1).length
  // No name truncation — MultilineYAxisTick on the chart wraps long
  // company names ("Office of the Director of National Intelligence")
  // to 2 lines on word boundaries. Truncating to 40 chars upstream
  // forced visible "..." even when the chart had room to wrap.
  const companies = Object.entries(mergedCompanies)
    .map(([name, v]) => ({ name, qualifying: v.qualifying, topScore: v.topScore }))
    .sort((a, b) => b.qualifying - a.qualifying)
    .slice(0, 10)

  // ---- Top keywords (from matched_keyword on roles) ----
  const keywordCounts: Record<string, number> = {}
  for (const r of qualifying) {
    const kw = r.matched_keyword
    if (kw) keywordCounts[kw] = (keywordCounts[kw] || 0) + 1
  }
  const topKeywords = Object.entries(keywordCounts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 8)
  // Bottom keywords — the ones that returned zero qualifying
  const allKeywords = audit.keyword_effectiveness || []
  const zeroMatchKeywords = allKeywords
    .filter((k) => (k.qualifying_matches ?? null) === 0)
    .slice(0, 5)
    .map((k) => k.keyword || k.search_term || '')
    .filter(Boolean)

  // v0.2.0: removed Sources breakdown card. The source list is
  // competitive intelligence we don't want testers screenshotting and
  // sharing. Internal diagnostics for source performance still live in
  // the audit JSON files for the operator to inspect directly.
  const sourceData: Array<{ name: string; scraped: number; qualifying: number }> = []

  // ---- Top Industries (v0.2.0) ----
  // Each role has an `industry` field populated by the scrapers /
  // backend classifier. Aggregate, sort, take top 8. Roles with no
  // industry data go in "Unspecified" (visible bucket — not silently
  // dropped — so the chart sums match the qualifying total).
  const industryCounts: Record<string, number> = {}
  for (const r of qualifying) {
    const ind = (r.industry || '').trim() || 'Unspecified'
    industryCounts[ind] = (industryCounts[ind] || 0) + 1
  }
  const industries = Object.entries(industryCounts)
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 8)
  const totalIndustries = Object.keys(industryCounts).length

  // ---- Salary by Arrangement (v0.2.0) ----
  // Avg midpoint salary per Remote/Hybrid/On-site bucket. Lets users
  // see at a glance whether remote roles in their pipeline pay more or
  // less. Only counts roles with disclosed salary (salary_min OR
  // salary_max present).
  const arrSalaryAccum: Record<string, { sum: number; count: number }> = {
    Remote: { sum: 0, count: 0 },
    Hybrid: { sum: 0, count: 0 },
    'On-site': { sum: 0, count: 0 },
  }
  for (const r of qualifying) {
    const mid = ((r.salary_min || 0) + (r.salary_max || 0)) / 2
    if (mid <= 0) continue
    const raw = (r.location_type || '').toLowerCase()
    let key: string | null = null
    if (raw.includes('hybrid')) key = 'Hybrid'
    else if (raw.includes('remote')) key = 'Remote'
    else if (raw.includes('on-site') || raw.includes('onsite')) key = 'On-site'
    if (!key) continue
    arrSalaryAccum[key].sum += mid
    arrSalaryAccum[key].count += 1
  }
  const salaryByArrangement = (['Remote', 'Hybrid', 'On-site'] as const).map((k) => ({
    name: k,
    avg: arrSalaryAccum[k].count ? Math.round(arrSalaryAccum[k].sum / arrSalaryAccum[k].count) : 0,
    count: arrSalaryAccum[k].count,
    color: ARRANGEMENT_COLORS[k] || COLORS.stretch,
  }))

  // ---- Posting Age Buckets (v0.2.0) ----
  // 0-7d / 8-14d / 15-30d / 30+d / No date. Reuses ages[] computed
  // above for AVG AGE. Pie chart in card form.
  const ageBucketCounts = { '0-7d': 0, '8-14d': 0, '15-30d': 0, '30+d': 0, 'No date': 0 }
  for (const r of qualifying) {
    const posted = r.posted_date
    if (!posted) { ageBucketCounts['No date'] += 1; continue }
    const t = Date.parse(posted)
    if (isNaN(t)) { ageBucketCounts['No date'] += 1; continue }
    const days = Math.floor((now - t) / DAY_MS)
    if (days < 0) { ageBucketCounts['No date'] += 1; continue }
    if (days <= 7) ageBucketCounts['0-7d'] += 1
    else if (days <= 14) ageBucketCounts['8-14d'] += 1
    else if (days <= 30) ageBucketCounts['15-30d'] += 1
    else ageBucketCounts['30+d'] += 1
  }
  const AGE_BUCKET_COLORS: Record<string, string> = {
    '0-7d': COLORS.strong,
    '8-14d': COLORS.good,
    '15-30d': COLORS.orange,
    '30+d': COLORS.rose,
    'No date': COLORS.stretch,
  }
  const ageBuckets = Object.entries(ageBucketCounts)
    .map(([name, value]) => ({ name, value, color: AGE_BUCKET_COLORS[name] }))
    .filter((b) => b.value > 0)

  // ---- Title Word Cloud (v0.2.0) ----
  // Tokenize qualifying titles, drop stopwords + very short tokens,
  // count occurrences. Words rendered with font-size scaled to count.
  const STOP_WORDS = new Set([
    'and','the','of','for','a','an','in','on','at','to','with','by','or',
    'as','is','be','from','this','that','it','i','&','-','—','/','II',
    'III','IV','jr','sr','vp','svp','evp','-','ii','iii','iv',
  ])
  const wordCounts: Record<string, number> = {}
  for (const r of qualifying) {
    const title = (r.title || '').toLowerCase()
    if (!title) continue
    // Split on non-letter chars (keeps "ai" but drops punctuation)
    const tokens = title.split(/[^a-z0-9]+/).filter(Boolean)
    for (const t of tokens) {
      if (t.length < 2) continue
      if (STOP_WORDS.has(t)) continue
      wordCounts[t] = (wordCounts[t] || 0) + 1
    }
  }
  // Title-case each word for display, take top 30
  const titleWords = Object.entries(wordCounts)
    .map(([word, count]) => ({
      word: word === 'ai' ? 'AI' : word.charAt(0).toUpperCase() + word.slice(1),
      count,
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 30)

  // ---- Sankey: Tier → Industry → Company (v0.2.0) ----
  // Visualize the flow from quality tiers through industries into the
  // top companies. Limited to top 5 industries × top 8 companies to
  // keep the diagram readable. Recharts Sankey expects:
  //   nodes: [{ name }, ...]
  //   links: [{ source: nodeIdx, target: nodeIdx, value }, ...]
  const sankeyData = (() => {
    if (!qualifying.length) return { nodes: [], links: [] }
    const tiersSet = new Set<string>()
    const indSet = new Set<string>()
    const coSet = new Set<string>()
    // First pass: identify the top 4 industries and top 6 companies
    // (already-deduped via mergedCompanies above) to keep readable.
    const indFromRoles: Record<string, number> = {}
    for (const r of qualifying) {
      const ind = (r.industry || '').trim() || 'Unspecified'
      indFromRoles[ind] = (indFromRoles[ind] || 0) + 1
    }
    const topInds = Object.entries(indFromRoles)
      .sort((a, b) => b[1] - a[1]).slice(0, 4).map(([n]) => n)
    const topCos = companies.slice(0, 6).map((c) => c.name)
    const topIndsSet = new Set(topInds)
    const topCosSet = new Set(topCos)
    // Build a normalized→canonical lookup so role.company maps to the
    // deduped display name we already computed.
    const companyDisplayLookup: Record<string, string> = {}
    for (const cluster of Object.values(clusters)) {
      const canonical = _pickCanonical(cluster.variants)
      for (const variant of Object.keys(cluster.variants)) {
        companyDisplayLookup[variant] = canonical
      }
    }
    // Aggregate links: tier→industry, industry→company.
    const t2i: Record<string, Record<string, number>> = {}
    const i2c: Record<string, Record<string, number>> = {}
    for (const r of qualifying) {
      const tier = (r.tier || '').toUpperCase() || 'UNRATED'
      let ind = (r.industry || '').trim() || 'Unspecified'
      let co = companyDisplayLookup[(r.company || '').trim()] || (r.company || '').trim()
      if (!topIndsSet.has(ind)) ind = 'Other industries'
      if (!topCosSet.has(co)) co = 'Other companies'
      tiersSet.add(tier); indSet.add(ind); coSet.add(co)
      if (!t2i[tier]) t2i[tier] = {}
      if (!i2c[ind]) i2c[ind] = {}
      t2i[tier][ind] = (t2i[tier][ind] || 0) + 1
      i2c[ind][co] = (i2c[ind][co] || 0) + 1
    }
    const tierOrder = ['STRONG','GOOD','MAYBE','STRETCH','UNRATED'].filter((t) => tiersSet.has(t))
    const indOrder = [...topInds, 'Other industries'].filter((i) => indSet.has(i))
    const coOrder = [...topCos, 'Other companies'].filter((c) => coSet.has(c))
    const allNodes = [...tierOrder, ...indOrder, ...coOrder]
    const idx = (name: string) => allNodes.indexOf(name)
    const links: Array<{ source: number; target: number; value: number }> = []
    for (const [t, ms] of Object.entries(t2i)) {
      for (const [i, v] of Object.entries(ms)) {
        if (v > 0) links.push({ source: idx(t), target: idx(i), value: v })
      }
    }
    for (const [i, ms] of Object.entries(i2c)) {
      for (const [c, v] of Object.entries(ms)) {
        if (v > 0) links.push({ source: idx(i), target: idx(c), value: v })
      }
    }
    return { nodes: allNodes.map((name) => ({ name })), links }
  })()

  // ---- Run Highlights (v0.2.0) ----
  // Four headliner stats for the banner at the top of the page. Each
  // is the most-impressive single-role pull on a given dimension.
  const sortedByScore = [...qualifying].sort((a, b) => (b.score || 0) - (a.score || 0))
  const sortedByMaxComp = [...qualifying].sort((a, b) => (b.salary_max || 0) - (a.salary_max || 0))
  const sortedByDate = [...qualifying]
    .filter((r) => r.posted_date && !isNaN(Date.parse(r.posted_date)))
    .sort((a, b) => Date.parse(b.posted_date!) - Date.parse(a.posted_date!))
  const topMatch = sortedByScore[0]
  const highestComp = sortedByMaxComp.find((r) => (r.salary_max || 0) > 0) || null
  const freshestRole = sortedByDate[0]
  const mostPostingsCo = companies[0] || null
  // Helper: format role-age string from posted_date
  const _formatAge = (posted?: string): string => {
    if (!posted) return ''
    const t = Date.parse(posted)
    if (isNaN(t)) return ''
    const ms = now - t
    const hrs = Math.floor(ms / (60 * 60 * 1000))
    const days = Math.floor(ms / DAY_MS)
    if (hrs < 24) return hrs <= 1 ? 'just now' : `${hrs}h ago`
    if (days === 1) return '1 day ago'
    return `${days} days ago`
  }
  const highlights = {
    topMatch: topMatch ? {
      score: topMatch.score || 0,
      title: topMatch.title || 'Untitled',
      company: topMatch.company || '',
    } : null,
    highestComp: highestComp ? {
      min: highestComp.salary_min || 0,
      max: highestComp.salary_max || 0,
      title: highestComp.title || 'Untitled',
      company: highestComp.company || '',
    } : null,
    freshest: freshestRole ? {
      ageLabel: _formatAge(freshestRole.posted_date),
      title: freshestRole.title || 'Untitled',
      company: freshestRole.company || '',
    } : null,
    mostPostings: mostPostingsCo ? {
      company: mostPostingsCo.name,
      count: mostPostingsCo.qualifying,
    } : null,
  }

  return {
    funnelData,
    tierData,
    salary: { avgMin, avgMax, median, salaryCoveragePct, histogram: salaryHistogram, count: salaryRoles.length },
    arrangementData,
    stateData,
    companies,
    totalCompanies,
    singleRoleCompanies,
    topKeywords,
    zeroMatchKeywords,
    sourceData,
    // Coverage values computed CLIENT-SIDE from qualifying roles, same
    // denominator as Salary card stats (was inconsistent before — 48% in
    // funnel vs 71% in salary card pointed at same data).
    coverage: {
      avg_post_age_days: avgPostAgeDays,
      salary_coverage_pct: salaryCoveragePct,
      remote_pct: remotePct,
    },
    metadata: audit.run_metadata || {},
    qualifyingTotal: qualifying.length,
    hybridCount: arrangementCounts['Hybrid'] || 0,
    // For the States card subtitle — distinguishes "X of Y roles have
    // a US state code in their location" vs the full qualifying total.
    rolesWithState,
    // v0.2.0 additions
    industries,
    totalIndustries,
    salaryByArrangement,
    ageBuckets,
    titleWords,
    sankeyData,
    highlights,
  }
}

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

function fmtCurrency(n: number): string {
  if (!n) return '—'
  return `$${(n / 1000).toFixed(0)}K`
}

function fmtRunOption(r: RunHistoryItem): string {
  // v0.2.0: dropped the qualifying count suffix from the dropdown label.
  // The backend's runs.db `qualifying_count` field can differ from the
  // actual `len(all_qualifying_roles)` in the audit JSON by 1-2 (due to
  // post-write filtering), which created a confusing mismatch:
  //   Dropdown said "179 qualifying" but Funnel card said 178.
  // Date alone is unambiguous and avoids the whole inconsistency.
  const date = r.completed_at ? new Date(r.completed_at) : null
  if (!date) return 'Unknown date'
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

// ----------------------------------------------------------------------------
// Custom Y-axis tick — wraps long labels to 2 lines instead of
// truncating. Recharts doesn't natively support multi-line tick text;
// we override `tick` with a function that emits 2 <text> elements when
// the label exceeds maxLineLength characters. Wraps on word boundaries
// so we don't split "Anthropic" into "Anthropi" + "c".
// ----------------------------------------------------------------------------

// FunnelStageTick: renders the stage name as the primary label and a
// smaller, dimmer sublabel below it. Used in the Pipeline Funnel chart
// to give users a parenthetical hint about what each stage represents
// without bloating the main axis text. Sublabels are looked up by the
// stage name from the passed-in map.
const FunnelStageTick = (sublabels: Record<string, string>) => (props: any) => {
  const { x, y, payload } = props
  const value = String(payload.value || '')
  const sub = sublabels[value] || ''
  return (
    <g transform={`translate(${x},${y})`}>
      <text x={0} y={-2} textAnchor="end" fill="#cbd5e1" fontSize={12} fontWeight={500}>
        {value}
      </text>
      {sub && (
        <text x={0} y={12} textAnchor="end" fill="#64748b" fontSize={10}>
          {sub}
        </text>
      )}
    </g>
  )
}

const MultilineYAxisTick = (maxLineLength: number) => (props: any) => {
  const { x, y, payload } = props
  const value = String(payload.value || '')

  if (value.length <= maxLineLength) {
    return (
      <g transform={`translate(${x},${y})`}>
        <text x={0} y={0} dy={4} textAnchor="end" fill="#cbd5e1" fontSize={11}>
          {value}
        </text>
      </g>
    )
  }
  // Split into max 2 lines on word boundaries.
  const words = value.split(/\s+/)
  let line1 = ''
  let line2 = ''
  for (const w of words) {
    const candidate = line1 ? `${line1} ${w}` : w
    if (!line2 && candidate.length <= maxLineLength) {
      line1 = candidate
    } else {
      line2 = line2 ? `${line2} ${w}` : w
    }
  }
  // If line2 is itself too long, truncate it (rare at maxLineLength=28+).
  if (line2.length > maxLineLength + 4) {
    line2 = line2.slice(0, maxLineLength + 1) + '…'
  }
  return (
    <g transform={`translate(${x},${y})`}>
      <text x={0} y={-3} textAnchor="end" fill="#cbd5e1" fontSize={11}>
        {line1}
      </text>
      <text x={0} y={11} textAnchor="end" fill="#cbd5e1" fontSize={11}>
        {line2}
      </text>
    </g>
  )
}

// ----------------------------------------------------------------------------
// Custom recharts tooltip — matches the dark theme
// ----------------------------------------------------------------------------

function CustomTooltip({ active, payload, label }: any) {
  if (!active || !payload || !payload.length) return null
  return (
    <div className="glass-strong rounded-lg px-3 py-2 border border-white/[0.10] text-xs">
      {label && <div className="text-base-300 font-medium mb-1">{label}</div>}
      {payload.map((p: any, idx: number) => (
        <div key={idx} className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: p.color || p.fill }} />
          <span className="text-base-200">{p.name}: <span className="font-semibold">{p.value}</span></span>
        </div>
      ))}
    </div>
  )
}

// ----------------------------------------------------------------------------
// Main component
// ----------------------------------------------------------------------------

export default function Stats() {
  const navigate = useNavigate()
  const [runs, setRuns] = useState<RunHistoryItem[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [audit, setAudit] = useState<AuditData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Load run list on mount AND whenever the window regains focus or
  // the page becomes visible again. v0.2.0: this auto-update behavior
  // means the Stats page shows the newest run as soon as a search
  // completes — without the user having to navigate away and back.
  // Two trigger surfaces:
  //   1. Window 'focus' — user alt-tabs back to the app
  //   2. document 'visibilitychange' — same tab regains visibility
  //      (covers in-app focus shifts on platforms that don't fire
  //      'focus' for our window)
  // Auto-switches to the latest run UNLESS the user has manually
  // selected an older run from the dropdown — that selection is
  // respected (we don't yank them away from a historical run they're
  // examining).
  useEffect(() => {
    let isFirstLoad = true
    let userPickedHistorical = false

    const fetchRuns = () => {
      listRuns()
        .then((r) => {
          const list = r.runs || []
          setRuns(list)
          if (list.length === 0) return
          // Initial load: select the newest. Subsequent refreshes:
          // only switch to a newer run if the user is currently viewing
          // what was previously the newest (i.e. they haven't picked an
          // older one). Otherwise leave their selection alone.
          if (isFirstLoad) {
            setSelectedRunId(list[0].run_id)
            isFirstLoad = false
          } else if (!userPickedHistorical) {
            setSelectedRunId((current) => {
              if (current && current !== list[0].run_id) {
                // Only auto-bump if the current selection IS still the
                // newest known run from the previous fetch — meaning a
                // brand-new run just appeared.
                return list[0].run_id
              }
              return current || list[0].run_id
            })
          }
        })
        .catch((e) => {
          const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'Failed to load runs')
          setError(msg)
        })
    }

    fetchRuns()

    const onFocus = () => fetchRuns()
    const onVisibility = () => {
      if (document.visibilityState === 'visible') fetchRuns()
    }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)

    // Track manual dropdown selection to avoid yanking the user away
    // from a historical run they intentionally picked. We watch the
    // window for our own custom event fired below in the dropdown
    // onChange handler.
    const onUserPick = () => { userPickedHistorical = true }
    window.addEventListener('stats:user-picked-run', onUserPick)

    return () => {
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('stats:user-picked-run', onUserPick)
    }
  }, [])

  // Load audit when selection changes
  useEffect(() => {
    if (!selectedRunId) return
    setLoading(true)
    setError(null)
    getRunAudit(selectedRunId)
      .then((data) => setAudit(data as AuditData))
      .catch((e) => {
        const msg = e instanceof ApiError ? e.detail : (e instanceof Error ? e.message : 'Failed to load audit')
        setError(msg)
        setAudit(null)
      })
      .finally(() => setLoading(false))
  }, [selectedRunId])

  const stats = useMemo(() => (audit ? computeStats(audit) : null), [audit])

  // Empty state — no runs
  if (runs.length === 0 && !loading) {
    return (
      <div className="px-10 py-8 max-w-3xl mx-auto">
        <BackHeader onBack={() => navigate('/dashboard')} />
        <div className="glass-subtle rounded-xl p-12 mt-8 text-center">
          <BarChart3 size={36} className="mx-auto text-base-500 mb-4" />
          <h2 className="text-lg font-medium text-base-200 mb-2">No completed runs yet</h2>
          <p className="text-sm text-base-400 max-w-sm mx-auto">
            Stats appear here after your first search completes. Each run gets its own
            stats view — pick from the dropdown once you have a few runs in the books.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="px-10 py-8 max-w-7xl mx-auto">
      {/* Header — back button + title + run selector */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/dashboard')}
            className="flex items-center gap-1.5 px-3 py-2 rounded-lg
                       glass-subtle hover:bg-white/[0.08]
                       text-sm text-base-300 transition-colors"
          >
            <ArrowLeft size={14} />
            Dashboard
          </button>
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Stats</h1>
            <p className="text-sm text-base-400 mt-0.5">
              Aggregate analytics across your search runs
            </p>
          </div>
        </div>

        {/* Run selector */}
        {runs.length > 0 && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-base-500 uppercase tracking-wider">Run</span>
            {/* color-scheme:dark forces the browser/OS-rendered option list
                to use dark mode styling (otherwise it inherits the system
                default — usually white bg + light gray text on Windows,
                which is unreadable). The class is a Tailwind arbitrary
                value since there's no built-in utility for color-scheme. */}
            <select
              value={selectedRunId || ''}
              onChange={(e) => {
                setSelectedRunId(e.target.value)
                // Mark the user as having explicitly picked a run so
                // the focus/visibility refresh doesn't yank them away
                // from a historical run they're examining.
                window.dispatchEvent(new CustomEvent('stats:user-picked-run'))
              }}
              className="px-3 py-2 rounded-lg
                         bg-white/[0.04] border border-white/[0.10]
                         text-sm text-base-100
                         focus:outline-none focus:border-accent-500/50
                         min-w-[260px]
                         [color-scheme:dark]
                         cursor-pointer"
            >
              {runs.map((r) => (
                <option
                  key={r.run_id}
                  value={r.run_id}
                  className="bg-base-900 text-base-100"
                >
                  {fmtRunOption(r)}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* Loading / error states */}
      {loading && (
        <div className="flex items-center justify-center py-24">
          <Loader2 size={24} className="text-accent-400 animate-spin" />
        </div>
      )}
      {error && !loading && (
        <div className="flex items-start gap-3 p-4 mt-8 rounded-lg bg-red-500/10 border border-red-500/30">
          <AlertCircle size={16} className="text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-sm text-base-100 font-medium">Couldn't load stats</p>
            <p className="text-xs text-base-400 mt-1 leading-relaxed">{error}</p>
          </div>
        </div>
      )}

      {/* v0.2.0: HighlightsBanner removed from layout. The "Top Match"
          and "Highest Comp" callouts could become an emotional reminder
          if a user applied to that role and got rejected. Component +
          data still compiled (HighlightsBanner / highlights in
          computeStats) — uncomment the JSX below to bring it back. */}
      {/* {stats && !loading && <HighlightsBanner stats={stats} />} */}

      {/* Stat cards grid */}
      {stats && !loading && (
        <motion.div
          initial="hidden"
          animate="visible"
          variants={{ visible: { transition: { staggerChildren: 0.07 } } }}
          className="grid grid-cols-1 lg:grid-cols-2 gap-5 mt-8"
        >
          <FunnelCard stats={stats} />
          <ArrangementCard stats={stats} />
          <SalaryCard stats={stats} />
          <SalaryByArrangementCard stats={stats} />
          <KeywordsCard stats={stats} />
          <CompaniesCard stats={stats} />
          <IndustriesCard stats={stats} />
          <StatesCard stats={stats} />
          <PostingAgeCard stats={stats} />
          <TierBreakdownCard stats={stats} />
          {/* v0.2.0: TitleWordCloudCard + SankeyCard removed from layout.
              Word cloud overlapped meaningfully with Top Keywords (same
              underlying signal — what surfaced in titles); Sankey had
              right-edge label clipping and didn't add much over the
              individual Tier / Industry / Company cards. Both components
              still defined below — uncomment the JSX to restore. */}
          {/* <div className="lg:col-span-2"><TitleWordCloudCard stats={stats} /></div> */}
          {/* <div className="lg:col-span-2"><SankeyCard stats={stats} /></div> */}
        </motion.div>
      )}
    </div>
  )
}

function BackHeader({ onBack }: { onBack: () => void }) {
  return (
    <div className="flex items-center gap-3">
      <button
        onClick={onBack}
        className="flex items-center gap-1.5 px-3 py-2 rounded-lg
                   glass-subtle hover:bg-white/[0.08]
                   text-sm text-base-300 transition-colors"
      >
        <ArrowLeft size={14} />
        Dashboard
      </button>
      <h1 className="text-2xl font-semibold tracking-tight">Stats</h1>
    </div>
  )
}

// ----------------------------------------------------------------------------
// Card components
// ----------------------------------------------------------------------------

const cardVariants = {
  hidden: { opacity: 0, y: 12 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.4 } },
}

function Card({
  icon: Icon,
  title,
  subtitle,
  children,
  className = '',
}: {
  icon: any
  title: string
  subtitle?: string
  children: React.ReactNode
  className?: string
}) {
  return (
    <motion.div
      variants={cardVariants}
      className={`glass rounded-xl p-6 ${className}`}
    >
      <div className="flex items-start gap-3 mb-4">
        <div className="w-9 h-9 rounded-lg bg-accent-500/15 border border-accent-500/25
                        flex items-center justify-center flex-shrink-0">
          <Icon size={15} className="text-accent-400" />
        </div>
        <div>
          <h3 className="text-sm font-medium text-base-100">{title}</h3>
          {subtitle && <p className="text-[11px] text-base-500 mt-0.5">{subtitle}</p>}
        </div>
      </div>
      {children}
    </motion.div>
  )
}

function FunnelCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  const m = stats.metadata
  return (
    <Card icon={TrendingDown} title="Pipeline Funnel" subtitle="Where roles dropped off">
      <ResponsiveContainer width="100%" height={210}>
        <BarChart data={stats.funnelData} layout="vertical" margin={{ left: 4, right: 30 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
          <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 11 }} />
          <YAxis
            dataKey="stage"
            type="category"
            tick={FunnelStageTick({
              'Scraped': '(Raw Jobs)',
              'After Filter': '(Salary/Location/Old)',
              'Qualifying': '(After Scoring)',
            })}
            width={150}
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
          <Bar dataKey="count" fill={COLORS.accent} radius={[0, 8, 8, 0]} animationDuration={800}>
            <LabelList dataKey="count" position="right" fill="#cbd5e1" fontSize={11} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-3 gap-3 mt-4 text-center">
        <CoverageStat label="Avg Age" value={stats.coverage.avg_post_age_days} suffix=" days" sublabel="since posted" inverted />
        <CoverageStat label="Salary" value={stats.coverage.salary_coverage_pct} suffix="%" sublabel="disclosed" />
        <CoverageStat label="Remote" value={stats.coverage.remote_pct} suffix="%" sublabel="of qualifying" />
      </div>
      {m.duration_seconds && (
        <div className="mt-4 pt-4 border-t border-white/[0.05] text-[11px] text-base-500 flex flex-wrap gap-x-4 gap-y-1">
          <span>Search Time: <span className="text-base-300">{Math.round(m.duration_seconds / 60)}m {m.duration_seconds % 60}s</span></span>
          {/* v0.2.0: Cost ($) and Cache-hit status removed from the user-
              facing footer. They're operator/dev metrics — Cost reveals
              our per-search API spend (testers shouldn't be reasoning
              about their own ROI), and "Cache hit / Full pipeline" leaks
              pipeline implementation details. Both still live in the
              audit JSON for the operator to inspect directly. */}
        </div>
      )}
    </Card>
  )
}

function CoverageStat({ label, value, suffix = '%', sublabel = 'coverage', inverted = false }: {
  label: string
  value: number | undefined
  suffix?: string
  sublabel?: string
  // For metrics where lower is better (e.g. AVG AGE — fresher postings).
  // Inverts the threshold so a low value = green, high value = blue.
  inverted?: boolean
}) {
  const v = Math.round(value || 0)
  const color = inverted
    ? (v <= 14 ? COLORS.strong : v <= 30 ? COLORS.good : COLORS.maybe)
    : (v >= 80 ? COLORS.strong : v >= 50 ? COLORS.good : COLORS.maybe)
  return (
    <div>
      <div className="text-[10px] text-base-500 uppercase tracking-wider">{label}</div>
      <div className="text-xl font-semibold mt-1" style={{ color }}>{v}{suffix}</div>
      <div className="text-[10px] text-base-500">{sublabel}</div>
    </div>
  )
}

function TierBreakdownCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  return (
    <Card icon={BarChart3} title="Tier Distribution" subtitle="STRONG / GOOD / MAYBE / STRETCH split">
      <div className="flex items-center gap-4">
        <ResponsiveContainer width="60%" height={210}>
          <PieChart>
            <Pie
              data={stats.tierData}
              cx="50%"
              cy="50%"
              innerRadius={45}
              outerRadius={80}
              paddingAngle={3}
              dataKey="value"
              animationDuration={800}
            >
              {stats.tierData.map((entry, idx) => (
                <Cell key={`cell-${idx}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip content={<CustomTooltip />} />
          </PieChart>
        </ResponsiveContainer>
        {/* Custom legend — locks display order to STRONG → GOOD → MAYBE
            → STRETCH (the natural quality ranking). recharts' default
            Legend alphabetizes which buries STRONG (the tier users care
            about most) at the bottom. */}
        <CustomPieLegend items={stats.tierData} />
      </div>
    </Card>
  )
}

// CustomPieLegend renders a vertically-stacked list of {color, name, value}
// in the order provided — no alphabetization, no sort-by-value. Matches
// the visual style of recharts' built-in Legend (circle dot + name +
// dimmed count) but with full ordering control.
function CustomPieLegend({ items }: { items: Array<{ name: string; value: number; color: string }> }) {
  return (
    <ul className="flex flex-col gap-1.5 text-xs">
      {items.map((it) => (
        <li key={it.name} className="flex items-center gap-2">
          <span
            className="w-2.5 h-2.5 rounded-full inline-block flex-shrink-0"
            style={{ backgroundColor: it.color }}
          />
          <span className="text-base-200 font-medium">{it.name}</span>
          <span className="text-base-500">· {it.value}</span>
        </li>
      ))}
    </ul>
  )
}

function ArrangementCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  const hybridIsZero = stats.hybridCount === 0
  return (
    <Card
      icon={MapPin}
      title="Work Arrangement"
      subtitle="Remote / Hybrid / On-site mix"
    >
      <div className="flex items-center gap-4">
        <ResponsiveContainer width="60%" height={210}>
          <PieChart>
            <Pie
              data={stats.arrangementData}
              cx="50%"
              cy="50%"
              innerRadius={45}
              outerRadius={80}
              paddingAngle={3}
              dataKey="value"
              animationDuration={800}
            >
              {stats.arrangementData.map((entry, idx) => (
                <Cell key={`cell-${idx}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip content={<CustomTooltip />} />
          </PieChart>
        </ResponsiveContainer>
        {/* Custom legend matches the data array's order (sorted by
            value descending). Switched away from recharts' built-in
            Legend in v0.2.0 because it alphabetizes entries while the
            formatter callback's `idx` parameter still indexes into the
            ORIGINAL data array — a mismatch that produced wrong
            label/value pairs (e.g., showing "Hybrid · 71" when 71 was
            actually the On-site count). */}
        <CustomPieLegend items={stats.arrangementData} />
      </div>
      {hybridIsZero && stats.qualifyingTotal > 50 && (
        <div className="mt-3 text-[11px] text-base-500 italic">
          Note: zero hybrid roles is unusual for a large run — may indicate scraper-side under-classification.
        </div>
      )}
    </Card>
  )
}

function SalaryCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  const { avgMin, avgMax, median, salaryCoveragePct, histogram, count } = stats.salary
  return (
    <Card
      icon={DollarSign}
      title="Salary Insights"
      subtitle={`${count} roles disclosed comp (${salaryCoveragePct}% coverage)`}
    >
      <div className="grid grid-cols-3 gap-3 mb-4">
        <SalaryStat label="Avg Min" value={fmtCurrency(avgMin)} />
        <SalaryStat label="Median" value={fmtCurrency(median)} accent />
        <SalaryStat label="Avg Max" value={fmtCurrency(avgMax)} />
      </div>
      <ResponsiveContainer width="100%" height={140}>
        <BarChart data={histogram} margin={{ top: 8, right: 8, bottom: 8, left: -16 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
          <XAxis dataKey="label" tick={{ fill: '#94a3b8', fontSize: 10 }} interval={0} angle={-30} textAnchor="end" height={50} />
          <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
          <Bar dataKey="count" fill={COLORS.emerald} radius={[6, 6, 0, 0]} animationDuration={800} />
        </BarChart>
      </ResponsiveContainer>
    </Card>
  )
}

function SalaryStat({ label, value, accent = false }: { label: string; value: string; accent?: boolean }) {
  return (
    <div>
      <div className="text-[10px] text-base-500 uppercase tracking-wider">{label}</div>
      <div className={`text-xl font-semibold mt-1 ${accent ? 'text-accent-300' : 'text-base-100'}`}>{value}</div>
    </div>
  )
}

function StatesCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  // Subtitle clarifies the count basis — remote roles + roles with no
  // structured location data don't have a state code, so they're absent
  // from this chart. Showing the denominator (roles with state data
  // out of total qualifying) prevents the "57 vs 178" confusion.
  const subtitle = stats.rolesWithState > 0
    ? `${stats.rolesWithState} of ${stats.qualifyingTotal} qualifying roles have a state (excludes remote)`
    : 'By qualifying role count'
  return (
    <Card icon={MapPin} title="Top States" subtitle={subtitle}>
      {stats.stateData.length === 0 ? (
        <div className="text-xs text-base-500 italic">Location data sparse for this run.</div>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(180, stats.stateData.length * 28)}>
          <BarChart data={stats.stateData} layout="vertical" margin={{ left: 10, right: 30 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 11 }} />
            <YAxis dataKey="name" type="category" tick={{ fill: '#cbd5e1', fontSize: 12 }} width={45} />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
            <Bar dataKey="value" fill={COLORS.cyan} radius={[0, 6, 6, 0]} animationDuration={800}>
              <LabelList dataKey="value" position="right" fill="#cbd5e1" fontSize={11} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}

function CompaniesCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  // Subtitle: "Top 10 of 116 companies with qualifying roles". Dropped
  // the "X posted just 1 role" tail in v0.2.0 — misleading because
  // each "1 role" is "1 role MATCHING our keywords"; the company may
  // have many other listings that didn't match. Don't want users
  // inferring company size from this stat.
  const subtitle = stats.totalCompanies > stats.companies.length
    ? `Top ${stats.companies.length} of ${stats.totalCompanies} companies with qualifying roles`
    : `${stats.totalCompanies} ${stats.totalCompanies === 1 ? 'company' : 'companies'} with qualifying roles`
  return (
    <Card icon={Building2} title="Top Companies" subtitle={subtitle}>
      {stats.companies.length === 0 ? (
        <div className="text-xs text-base-500 italic">No company data available.</div>
      ) : (
        // Row height 40px for 2-line wrapped labels. Y-axis width
        // tightened from 240 → 180 in v0.2.0: long names like "Office
        // of the Director of National Intelligence" fit cleanly at 180
        // (~25 char × 6.5px = 165px max line) and the saved 60px gives
        // the bars more horizontal real estate. Margin.left 10 → 4
        // tightens the left gutter further.
        <ResponsiveContainer width="100%" height={Math.max(220, stats.companies.length * 40)}>
          <BarChart data={stats.companies} layout="vertical" margin={{ left: 4, right: 30, top: 6, bottom: 6 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 11 }} />
            <YAxis dataKey="name" type="category" tick={MultilineYAxisTick(26)} width={180} interval={0} />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
            <Bar dataKey="qualifying" name="Qualifying" fill={COLORS.accent} radius={[0, 6, 6, 0]} animationDuration={800}>
              <LabelList dataKey="qualifying" position="right" fill="#cbd5e1" fontSize={11} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}

function KeywordsCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  return (
    <Card icon={Search} title="Top Keywords" subtitle="Which search terms surfaced the most roles">
      {stats.topKeywords.length === 0 ? (
        <div className="text-xs text-base-500 italic">Keyword effectiveness data not available for this run.</div>
      ) : (
        // Width tightened from 200 → 160 in v0.2.0 to match Companies
        // card and free up horizontal real estate for the bars. Most
        // keywords are <= 22 chars per line ("Enterprise AI Transformation
        // Lead" wraps to "Enterprise AI" + "Transformation Lead", longest
        // line ~19 chars × 6.5px = 124px, fits in 160 with margin).
        <ResponsiveContainer width="100%" height={Math.max(240, stats.topKeywords.length * 44)}>
          <BarChart data={stats.topKeywords} layout="vertical" margin={{ left: 4, right: 30, top: 6, bottom: 6 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 11 }} />
            <YAxis dataKey="name" type="category" tick={MultilineYAxisTick(22)} width={160} interval={0} />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
            <Bar dataKey="value" fill={COLORS.pink} radius={[0, 6, 6, 0]} animationDuration={800}>
              <LabelList dataKey="value" position="right" fill="#cbd5e1" fontSize={11} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
      {stats.zeroMatchKeywords.length > 0 && (
        <div className="mt-4 pt-4 border-t border-white/[0.05]">
          <div className="text-[10px] text-base-500 uppercase tracking-wider mb-2">
            Zero-match keywords (consider pruning)
          </div>
          <div className="flex flex-wrap gap-1.5">
            {stats.zeroMatchKeywords.map((kw) => (
              <span key={kw} className="text-[11px] px-2 py-0.5 rounded-full
                                          bg-rose-500/10 border border-rose-500/20
                                          text-rose-300">
                {kw}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  )
}

// ----------------------------------------------------------------------------
// v0.2.0 additions: chart cards
// ----------------------------------------------------------------------------
// (HighlightsBanner / HighlightBox were prototyped in early v0.2.0 then
// removed before ship — emotional risk for testers when the "Top Match"
// or "Highest Comp" role gets rejected. See git history for restore.)

// IndustriesCard — bar chart of role counts per industry. Industry data
// comes from the `industry` field on each role (populated by scrapers /
// backend classifier).
function IndustriesCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  const subtitle = stats.totalIndustries > stats.industries.length
    ? `Top ${stats.industries.length} of ${stats.totalIndustries} industries`
    : `${stats.totalIndustries} ${stats.totalIndustries === 1 ? 'industry' : 'industries'} represented`
  return (
    <Card icon={Briefcase} title="Top Industries" subtitle={subtitle}>
      {stats.industries.length === 0 ? (
        <div className="text-xs text-base-500 italic">No industry data available.</div>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(200, stats.industries.length * 36)}>
          <BarChart data={stats.industries} layout="vertical" margin={{ left: 4, right: 30, top: 6, bottom: 6 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
            <XAxis type="number" tick={{ fill: '#94a3b8', fontSize: 11 }} />
            <YAxis dataKey="name" type="category" tick={MultilineYAxisTick(22)} width={160} interval={0} />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
            <Bar dataKey="value" fill={COLORS.indigo} radius={[0, 6, 6, 0]} animationDuration={800}>
              <LabelList dataKey="value" position="right" fill="#cbd5e1" fontSize={11} />
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}

// SalaryByArrangementCard — three stat boxes showing average comp per
// Remote/Hybrid/On-site bucket. Answers "does remote pay less here?"
function SalaryByArrangementCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  return (
    <Card
      icon={DollarSign}
      title="Salary by Arrangement"
      subtitle="Average midpoint comp per work style (disclosed roles only)"
    >
      <div className="grid grid-cols-3 gap-3 mt-2">
        {stats.salaryByArrangement.map((s) => (
          <div
            key={s.name}
            className="rounded-lg p-4 border"
            style={{
              backgroundColor: `${s.color}11`,
              borderColor: `${s.color}33`,
            }}
          >
            <div className="text-[10px] text-base-500 uppercase tracking-wider">{s.name}</div>
            <div className="text-2xl font-semibold mt-1" style={{ color: s.color }}>
              {s.avg ? `$${Math.round(s.avg / 1000)}K` : '—'}
            </div>
            <div className="text-[10px] text-base-500 mt-1">
              {s.count ? `${s.count} role${s.count === 1 ? '' : 's'}` : 'No disclosed comp'}
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}

// PostingAgeCard — pie chart bucketing qualifying roles by age:
// 0-7d / 8-14d / 15-30d / 30+d / No date. Same data backing AVG AGE
// in the FunnelCard but visualized for distribution shape.
function PostingAgeCard({ stats }: { stats: ReturnType<typeof computeStats> }) {
  const total = stats.ageBuckets.reduce((s, b) => s + b.value, 0)
  return (
    <Card icon={Calendar} title="Posting Age" subtitle="How fresh are these roles?">
      {total === 0 ? (
        <div className="text-xs text-base-500 italic">No date data available.</div>
      ) : (
        <div className="flex items-center gap-4">
          <ResponsiveContainer width="60%" height={210}>
            <PieChart>
              <Pie
                data={stats.ageBuckets}
                cx="50%"
                cy="50%"
                innerRadius={45}
                outerRadius={80}
                paddingAngle={3}
                dataKey="value"
                animationDuration={800}
              >
                {stats.ageBuckets.map((entry, idx) => (
                  <Cell key={`age-cell-${idx}`} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip content={<CustomTooltip />} />
            </PieChart>
          </ResponsiveContainer>
          {/* Custom legend locks order to newest → oldest (0-7d → 8-14d
              → 15-30d → 30+d → No date). Default recharts Legend
              alphabetizes which puts 15-30d before 8-14d. */}
          <CustomPieLegend items={stats.ageBuckets} />
        </div>
      )}
    </Card>
  )
}

// (TitleWordCloudCard, SankeyCard, SankeyNode were prototyped in early
// v0.2.0 then removed before ship — Word Cloud overlapped with Top
// Keywords; Sankey labels clipped at chart edges. See git history for
// restore.)

// SourcesCard removed in v0.2.0 — exposing the scraper source list was
// competitive-intelligence leak. Keeping this comment as a marker so future
// maintainers don't add it back without thinking through that.

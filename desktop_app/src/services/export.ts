/**
 * Export run results to Excel (.xlsx), CSV, or plain Markdown.
 *
 * Uses SheetJS (xlsx) for binary formats. CSV/Markdown are hand-rolled
 * so we don't pull in extra deps.
 *
 * Save flow (Tauri-native):
 *   1. Generate the file content in-memory as a Uint8Array (xlsx) or
 *      string (csv / markdown).
 *   2. Show the OS-native Save As dialog so the user picks where it lands
 *      (instead of the previous "silent dump into Downloads" behavior).
 *   3. Write the bytes to the chosen path via the fs plugin.
 *
 * Returns the chosen file path on success, or null if the user cancelled
 * the dialog. The caller uses that to show a meaningful toast (or skip
 * the toast on cancel).
 */

import * as XLSX from 'xlsx'
import { save } from '@tauri-apps/plugin-dialog'
import { writeFile } from '@tauri-apps/plugin-fs'
import type { Role, RunSummary } from '@/types'
import type { RoleStatusEntry } from '@/stores/appStore'
import { stripScoringTags } from '@/lib/utils'

// v0.3.26 (UI1/SC3): clean, user-facing fit analysis for exports — strip
// internal scoring tags and fall back to stage2_reasoning when Stage 3 was
// skipped (otherwise the cell/section is blank for ~26 GOOD roles).
function fitAnalysis(role: Role): string {
  const raw =
    (role.stage3_analysis && !role.stage3_analysis.startsWith('stage3_error')
      ? role.stage3_analysis
      : '') || role.stage2_reasoning || ''
  return stripScoringTags(raw)
}

interface ExportContext {
  roles: Role[]
  summary?: RunSummary | null
  statuses?: Record<string, RoleStatusEntry>
}

// ----------------------------------------------------------------------------
// Shared row builder — turns a Role + status into a flat record
// ----------------------------------------------------------------------------

interface FlatRow {
  Score: number
  Tier: string
  Title: string
  Company: string
  'Location Type': string
  Location: string
  'Salary Min': string
  'Salary Max': string
  'Salary Range': string
  'Posted Date': string
  Source: string
  URL: string
  Status: string
  'Application Stage': string
  'Status Date': string
  'AI Analysis': string
  'Best Resume Match': string
}

function roleToRow(role: Role, status?: RoleStatusEntry): FlatRow {
  const sal = (n: number | null | undefined) => (n != null ? `$${n.toLocaleString()}` : '')
  const sMin = role.salary_min ?? null
  const sMax = role.salary_max ?? null
  const range =
    sMin && sMax ? `${sal(sMin)} – ${sal(sMax)}` : (role.salary_text || '')

  return {
    Score: role.final_score ?? 0,
    Tier: role.final_tier || '',
    Title: role.job_title || '',
    Company: role.company || '',
    'Location Type': role.location_type || '',
    Location: role.location || '',
    'Salary Min': sMin != null ? String(sMin) : '',
    'Salary Max': sMax != null ? String(sMax) : '',
    'Salary Range': range,
    'Posted Date': role.posted_date || '',
    Source: role.primary_source || '',
    URL: role.job_url || '',
    Status: status?.status || '',
    'Application Stage': status?.applicationStage || '',
    'Status Date': status?.date ? new Date(status.date).toLocaleDateString() : '',
    'AI Analysis': fitAnalysis(role),
    'Best Resume Match': role.stage3_best_resume_match || '',
  }
}

// ----------------------------------------------------------------------------
// Excel (.xlsx) export
// ----------------------------------------------------------------------------

export async function exportToExcel(
  ctx: ExportContext,
  defaultFilename = 'job-search-results.xlsx',
): Promise<string | null> {
  const sortedRoles = [...ctx.roles].sort(
    (a, b) => (b.final_score ?? 0) - (a.final_score ?? 0)
  )
  const rows = sortedRoles.map((r) => {
    const key = `${(r.company || '').toLowerCase()}::${(r.job_title || '').toLowerCase()}`
    return roleToRow(r, ctx.statuses?.[key])
  })

  const wb = XLSX.utils.book_new()

  // Main sheet — all roles
  const allSheet = XLSX.utils.json_to_sheet(rows)
  applyColumnWidths(allSheet, rows)
  XLSX.utils.book_append_sheet(wb, allSheet, 'All Roles')

  // Tier-specific sheets for easy filtering
  const tiers: Array<'STRONG' | 'GOOD' | 'MAYBE' | 'STRETCH'> = [
    'STRONG', 'GOOD', 'MAYBE', 'STRETCH',
  ]
  for (const tier of tiers) {
    const tierRows = rows.filter((r) => r.Tier === tier)
    if (tierRows.length === 0) continue
    const tierSheet = XLSX.utils.json_to_sheet(tierRows)
    applyColumnWidths(tierSheet, tierRows)
    XLSX.utils.book_append_sheet(wb, tierSheet, tier)
  }

  // Summary sheet — run metadata
  if (ctx.summary) {
    const summaryRows = [
      ['Run Date', ctx.summary.run_date ? new Date(ctx.summary.run_date).toLocaleString() : ''],
      ['Total Scraped', ctx.summary.roles_scraped],
      ['After Filtering', ctx.summary.roles_after_filter],
      ['Qualifying (≥40)', ctx.summary.roles_qualifying],
      ['STRONG (85+)', ctx.summary.tier_strong],
      ['GOOD (70-84)', ctx.summary.tier_good],
      ['MAYBE (55-69)', ctx.summary.tier_maybe],
      ['STRETCH (40-54)', ctx.summary.tier_stretch],
      ['Duration (sec)', ctx.summary.duration_seconds],
      ['JD Coverage', `${ctx.summary.jd_coverage_pct?.toFixed(0) ?? 0}%`],
      ['Salary Coverage', `${ctx.summary.salary_coverage_pct?.toFixed(0) ?? 0}%`],
    ]
    const summarySheet = XLSX.utils.aoa_to_sheet([['Metric', 'Value'], ...summaryRows])
    summarySheet['!cols'] = [{ wch: 25 }, { wch: 30 }]
    XLSX.utils.book_append_sheet(wb, summarySheet, 'Run Summary')
  }

  // Saved + applied tabs (if any)
  const savedRows = rows.filter((r) => r.Status === 'saved')
  const appliedRows = rows.filter((r) => r.Status === 'applied')
  if (savedRows.length > 0) {
    const sheet = XLSX.utils.json_to_sheet(savedRows)
    applyColumnWidths(sheet, savedRows)
    XLSX.utils.book_append_sheet(wb, sheet, 'Saved')
  }
  if (appliedRows.length > 0) {
    const sheet = XLSX.utils.json_to_sheet(appliedRows)
    applyColumnWidths(sheet, appliedRows)
    XLSX.utils.book_append_sheet(wb, sheet, 'Applied')
  }

  // Generate the .xlsx as an ArrayBuffer and pass to the save-dialog helper.
  // SheetJS supports `bookType: 'xlsx'` with `type: 'array'` returning a
  // Uint8Array-compatible buffer that the fs plugin can write directly.
  const buffer = XLSX.write(wb, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer
  return saveWithDialog({
    defaultFilename,
    bytes: new Uint8Array(buffer),
    filterName: 'Excel Workbook',
    extensions: ['xlsx'],
  })
}

// ----------------------------------------------------------------------------
// CSV export
// ----------------------------------------------------------------------------

export async function exportToCSV(
  ctx: ExportContext,
  defaultFilename = 'job-search-results.csv',
): Promise<string | null> {
  const sortedRoles = [...ctx.roles].sort(
    (a, b) => (b.final_score ?? 0) - (a.final_score ?? 0)
  )
  const rows = sortedRoles.map((r) => {
    const key = `${(r.company || '').toLowerCase()}::${(r.job_title || '').toLowerCase()}`
    return roleToRow(r, ctx.statuses?.[key])
  })
  if (rows.length === 0) return null
  const headers = Object.keys(rows[0]) as (keyof FlatRow)[]
  const escapeCsv = (v: unknown) => {
    const s = String(v ?? '')
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return `"${s.replace(/"/g, '""')}"`
    }
    return s
  }
  const lines = [
    headers.join(','),
    ...rows.map((row) => headers.map((h) => escapeCsv(row[h])).join(',')),
  ]
  return saveWithDialog({
    defaultFilename,
    bytes: new TextEncoder().encode(lines.join('\n')),
    filterName: 'CSV',
    extensions: ['csv'],
  })
}

// ----------------------------------------------------------------------------
// Markdown export — drag into Claude.ai for analysis
// ----------------------------------------------------------------------------

export async function exportToMarkdown(
  ctx: ExportContext,
  defaultFilename = 'job-search-results.md',
): Promise<string | null> {
  const sortedRoles = [...ctx.roles].sort(
    (a, b) => (b.final_score ?? 0) - (a.final_score ?? 0)
  )
  const lines: string[] = []
  lines.push(`# Job Search Results`)
  if (ctx.summary?.run_date) {
    lines.push(`Run on ${new Date(ctx.summary.run_date).toLocaleString()}`)
  }
  lines.push(`${sortedRoles.length} qualifying roles\n`)
  lines.push('---\n')

  const tiers = ['STRONG', 'GOOD', 'MAYBE', 'STRETCH'] as const
  for (const tier of tiers) {
    const tierRoles = sortedRoles.filter((r) => r.final_tier === tier)
    if (tierRoles.length === 0) continue
    lines.push(`## ${tier} (${tierRoles.length})\n`)
    for (const r of tierRoles) {
      lines.push(`### ${r.final_score} — ${r.job_title} @ ${r.company}`)
      const meta: string[] = []
      if (r.location_type) meta.push(r.location_type)
      if (r.location) meta.push(r.location)
      if (r.salary_text) meta.push(r.salary_text)
      if (r.primary_source) meta.push(`via ${r.primary_source}`)
      if (meta.length) lines.push(`*${meta.join(' · ')}*\n`)
      const analysis = fitAnalysis(r)
      if (analysis) {
        lines.push(`${analysis}\n`)
      }
      if (r.job_url) lines.push(`[Apply →](${r.job_url})\n`)
      lines.push('')
    }
  }

  return saveWithDialog({
    defaultFilename,
    bytes: new TextEncoder().encode(lines.join('\n')),
    filterName: 'Markdown',
    extensions: ['md'],
  })
}

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

function applyColumnWidths(sheet: XLSX.WorkSheet, rows: FlatRow[]) {
  if (rows.length === 0) return
  const headers = Object.keys(rows[0])
  sheet['!cols'] = headers.map((h) => {
    const maxLen = Math.max(
      h.length,
      ...rows.map((r) => String((r as unknown as Record<string, unknown>)[h] ?? '').length)
    )
    return { wch: Math.min(60, Math.max(8, maxLen + 2)) }
  })
}

/**
 * Native Save As dialog → writes the bytes to the chosen path.
 *
 * Returns the chosen path on success, or null if the user clicked Cancel
 * (or escaped out of the dialog). Callers distinguish between cancel
 * (don't toast / silent) and success (show "Exported to <path>").
 *
 * Errors during the actual file write throw — caller wraps in try/catch
 * and surfaces a toast.
 */
async function saveWithDialog(opts: {
  defaultFilename: string
  bytes: Uint8Array
  filterName: string
  extensions: string[]
}): Promise<string | null> {
  const filePath = await save({
    defaultPath: opts.defaultFilename,
    filters: [{ name: opts.filterName, extensions: opts.extensions }],
  })
  if (!filePath) return null
  await writeFile(filePath, opts.bytes)
  return filePath
}

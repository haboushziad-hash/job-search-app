import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatCurrency(n: number | null | undefined, opts?: { compact?: boolean }) {
  if (n == null) return '—'
  if (opts?.compact && n >= 1000) {
    return `$${Math.round(n / 1000)}K`
  }
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 0,
  }).format(n)
}

export function formatRelativeDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  const days = Math.floor((Date.now() - d.getTime()) / (1000 * 60 * 60 * 24))
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return `${days}d ago`
  if (days < 30) return `${Math.floor(days / 7)}w ago`
  if (days < 365) return `${Math.floor(days / 30)}mo ago`
  return `${Math.floor(days / 365)}y ago`
}

export function tierColor(tier: string): string {
  switch (tier?.toUpperCase()) {
    case 'STRONG':  return 'tier-strong'
    case 'GOOD':    return 'tier-good'
    case 'MAYBE':   return 'tier-maybe'
    case 'STRETCH': return 'tier-stretch'
    default:        return 'base-500'
  }
}

/** Detect platform once at module load — used for keyboard shortcut labels. */
export const IS_MAC =
  typeof navigator !== 'undefined' &&
  /Mac|iPhone|iPad|iPod/i.test(navigator.platform || navigator.userAgent || '')

/** Symbol shown in the UI for the modifier key (⌘ on Mac, Ctrl elsewhere). */
export const MOD_KEY = IS_MAC ? '⌘' : 'Ctrl'

/** Render a keyboard shortcut for display, e.g. modKey('K') => '⌘K' or 'Ctrl+K'. */
export function modKey(key: string): string {
  return IS_MAC ? `${MOD_KEY}${key}` : `${MOD_KEY}+${key}`
}

/**
 * LocationAutocomplete — shared input for adding cities with type-ahead
 * suggestions from the bundled US_CITIES database (~2,096 cities mirrored
 * from backend's geocoding.py).
 *
 * Used in three places that all need consistent location-add UX:
 *   1. Setup.tsx (full new-search flow, with per-city radius sliders)
 *   2. Run.tsx (Re-Run page "adjust" panel)
 *   3. StartSearch.tsx (welcome → Re-Run path "adjust" panel)
 *
 * Behavior:
 *   - Suggestions filter as you type, top 8 matches shown
 *   - Already-added cities are filtered out
 *   - Comma-tolerant: "Hampton, VA" matches "Hampton VA"
 *   - Prefix matches sorted above substring matches (typing "Hampton"
 *     surfaces "Hampton VA" before "East Hampton VA")
 *   - ↑/↓ navigate, Enter adds, Esc dismisses
 *   - Free-form input still works for cities not in the suggestions list
 *     (autocomplete is typo prevention, not validation — backend handles
 *     unknown cities via state-overlap fallback)
 *
 * Popover is rendered via React Portal to document.body so it escapes
 * any parent `overflow: hidden` (e.g. the Re-Run / StartSearch adjust
 * panels animate height with overflow:hidden, which would otherwise clip
 * the dropdown to the panel's height).
 */
import { useState, useRef, useLayoutEffect, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { MapPin } from 'lucide-react'
import { cn } from '@/lib/utils'
import { US_CITIES } from '@/data/us-cities'

interface LocationAutocompleteProps {
  value: string
  onChange: (v: string) => void
  onAdd: (v: string) => void
  /** Already-added city names — excluded from suggestions (case-insensitive) */
  existingLocations: string[]
  /** Placeholder override. Default tuned for the Setup page. */
  placeholder?: string
  /** Disabled state — used during submit */
  disabled?: boolean
}

// Normalize for matching: strip commas (so "Hampton, VA" matches
// "Hampton VA"), collapse whitespace, lowercase. Same shape used for
// both the user's input and the bundled cities.
function normalize(s: string): string {
  return s.toLowerCase().replace(/,/g, '').replace(/\s+/g, ' ').trim()
}

export function LocationAutocomplete({
  value,
  onChange,
  onAdd,
  existingLocations,
  placeholder = 'Type any US city — Hampton VA, Boise ID, etc. — suggestions cover ~2,000 cities',
  disabled = false,
}: LocationAutocompleteProps) {
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [highlightedIdx, setHighlightedIdx] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number; width: number } | null>(null)

  // Normalize for matching — see top-of-file note.
  const valueNorm = normalize(value)
  const existingNormSet = new Set(existingLocations.map(normalize))

  const suggestions = valueNorm.length >= 1
    ? US_CITIES
        .map((city) => ({ city, norm: normalize(city) }))
        .filter(({ norm }) => norm.includes(valueNorm) && !existingNormSet.has(norm))
        // Prefix matches first, then alphabetical within each tier.
        // Typing "Hampton" surfaces "Hampton VA" before "East Hampton VA".
        .sort((a, b) => {
          const aPrefix = a.norm.startsWith(valueNorm)
          const bPrefix = b.norm.startsWith(valueNorm)
          if (aPrefix && !bPrefix) return -1
          if (!aPrefix && bPrefix) return 1
          return a.norm.localeCompare(b.norm)
        })
        .slice(0, 8)
        .map(({ city }) => city)
    : []

  // Position the portal-rendered popover relative to the input. Recompute
  // synchronously when suggestions change so the popover doesn't lag the
  // input visually. Also recompute on scroll / window resize.
  const updatePos = () => {
    if (!inputRef.current) return
    const rect = inputRef.current.getBoundingClientRect()
    setPopoverPos({
      top: rect.bottom + 4,
      left: rect.left,
      width: rect.width,
    })
  }

  useLayoutEffect(() => {
    if (showSuggestions && suggestions.length > 0) {
      updatePos()
    }
  }, [showSuggestions, suggestions.length, value])

  useEffect(() => {
    if (!showSuggestions) return
    const handle = () => updatePos()
    window.addEventListener('scroll', handle, true)  // capture: catch scroll on any ancestor
    window.addEventListener('resize', handle)
    return () => {
      window.removeEventListener('scroll', handle, true)
      window.removeEventListener('resize', handle)
    }
  }, [showSuggestions])

  const handleAdd = (text: string) => {
    onAdd(text)
    setShowSuggestions(false)
    setHighlightedIdx(0)
  }

  const popover = showSuggestions && suggestions.length > 0 && popoverPos ? (
    <div
      className="fixed z-50
                 glass-strong rounded-lg border border-white/[0.10]
                 max-h-64 overflow-y-auto py-1 shadow-2xl"
      style={{
        top: popoverPos.top,
        left: popoverPos.left,
        width: popoverPos.width,
      }}
    >
      {suggestions.map((city, idx) => (
        <button
          key={city}
          type="button"
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
  ) : null

  return (
    <div className="relative">
      <div className="flex gap-2">
        <input
          ref={inputRef}
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
          disabled={disabled}
          placeholder={placeholder}
          className="flex-1 px-3 py-2 rounded-lg
                     bg-white/[0.04] border border-white/[0.08]
                     text-sm placeholder:text-base-500
                     focus:outline-none focus:border-accent-500/50
                     disabled:opacity-50 disabled:cursor-not-allowed"
        />
        <button
          type="button"
          onClick={() => value.trim() && handleAdd(value)}
          disabled={!value.trim() || disabled}
          className="px-4 py-2 rounded-lg bg-white/[0.06] hover:bg-white/[0.10]
                     text-sm text-base-200 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Add
        </button>
      </div>

      {/* Popover lives in a Portal to escape any parent overflow:hidden
          (e.g. the Re-Run adjust panels animate height with overflow:hidden
          which would otherwise clip the dropdown). */}
      {popover && createPortal(popover, document.body)}
    </div>
  )
}

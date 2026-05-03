// Inline Z mark — matches the app icon (z-mark PNG / icon.icns / icon.ico).
// Used in the Sidebar brand area and anywhere else we need a small Z badge.
//
// Visual matches the rendered PNG: rounded-square gradient + bold Z + sparkle
// accent. Designed to read at 12-32px without losing clarity. Larger uses
// (login splash, etc.) should reach for the actual PNG asset instead.

interface ZMarkProps {
  size?: number
  className?: string
}

export function ZMark({ size = 28, className = '' }: ZMarkProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 64 64"
      width={size}
      height={size}
      className={className}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id="zmark-bg" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#a47cff" />
          <stop offset="0.55" stopColor="#8856ff" />
          <stop offset="1" stopColor="#5a2cd1" />
        </linearGradient>
      </defs>

      {/* Rounded-square background */}
      <rect x="0" y="0" width="64" height="64" rx="14" ry="14" fill="url(#zmark-bg)" />

      {/* Z mark — bold geometric, slight tilt for energy */}
      <g transform="translate(32 32) rotate(-3) translate(-32 -32)">
        {/* Top bar */}
        <path d="M 18 18 L 46 18 L 46 25 L 33 25 Z" fill="#ffffff" />
        {/* Diagonal */}
        <path d="M 46 18 L 46 25 L 25 46 L 18 46 L 18 39 Z" fill="#ffffff" />
        {/* Bottom bar */}
        <path d="M 18 39 L 18 46 L 46 46 L 46 39 Z" fill="#ffffff" />
      </g>

      {/* Sparkle accent (top-right) — small 4-pointed star */}
      <path
        d="M 50 12
           C 50.4 14.4 51 14.8 53.4 15.2
           C 51 15.6 50.4 16 50 18.4
           C 49.6 16 49 15.6 46.6 15.2
           C 49 14.8 49.6 14.4 50 12 Z"
        fill="#ffffff"
        opacity="0.9"
      />
    </svg>
  )
}

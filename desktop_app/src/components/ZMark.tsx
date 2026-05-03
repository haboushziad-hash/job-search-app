// Z mark — uses the same PNG that's the OS app icon (taskbar/dock).
// Source: desktop_app/public/z-mark.png (also referenced as the app icon
// at desktop_app/src-tauri/icons/icon.png after running `tauri icon`).
//
// Renders as <img> for full visual fidelity (animated gradients, depth,
// multiple sparkles) which would be tedious to recreate in inline SVG.

interface ZMarkProps {
  size?: number
  className?: string
}

export function ZMark({ size = 28, className = '' }: ZMarkProps) {
  return (
    <img
      src="/z-mark.png"
      alt="findmesomedamnjobz"
      width={size}
      height={size}
      className={className}
      style={{ display: 'block' }}
    />
  )
}

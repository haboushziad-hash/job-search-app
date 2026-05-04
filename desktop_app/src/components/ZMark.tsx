// Z mark — uses the cleaned Gemini PNG. The earlier export had baked-in
// "transparency" (the checkerboard pattern was actually opaque white/grey
// pixels with full alpha=255), which caused halos at small sizes and
// failed our PIL saturation-mask cleanup. Fix was a flood-fill from the
// canvas corners that converts the fake-transparent region to real
// alpha=0 while preserving the white Z letter (which is enclosed by the
// purple square and never reached by the flood). Final image is auto-
// cropped to its alpha bbox + square-padded so the logo fills the
// canvas — at small sizes (32px in the sidebar) the previous version
// rendered tiny because half the canvas was empty padding.
//
// Source PNG: desktop_app/src-tauri/icons/source/z-mark-clean.png (1024×1024 master)
// Runtime PNG: desktop_app/public/z-mark.png (256×256, downsampled with LANCZOS)

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

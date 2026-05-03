import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// Tauri + Vite config — see https://tauri.app/start/frontend/vite/
const host = process.env.TAURI_DEV_HOST

export default defineConfig({
  plugins: [react(), tailwindcss()],

  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },

  // Tauri-friendly dev settings
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    host: host || false,
    hmr: host ? { protocol: 'ws', host, port: 1421 } : undefined,
    watch: { ignored: ['**/src-tauri/**'] },
  },

  // Tauri uses these env vars to know what features to enable
  envPrefix: ['VITE_', 'TAURI_ENV_*'],

  // Top-level esbuild target — applies to per-file transform pass (TS/JSX).
  // Must match build.target so esbuild doesn't downlevel modern syntax.
  esbuild: {
    target: 'esnext',
  },

  // Vite 8 pre-bundles deps with rolldown (NOT esbuild). The hook is
  // optimizeDeps.rolldownOptions.transform.target, not the legacy
  // optimizeDeps.esbuildOptions.target.
  optimizeDeps: {
    rolldownOptions: {
      transform: {
        target: 'esnext',
      },
    },
  },

  build: {
    // Tauri 2 webviews: WebView2 (Chromium 120+) on Windows, WKWebView
    // on macOS 12+ (Safari 15+), WebKitGTK on Linux. All support every
    // modern syntax we use, so let esbuild emit code as-is rather than
    // downlevel. Earlier safari13/safari14 attempts triggered esbuild's
    // "destructuring not supported yet" error because (a) some
    // destructuring sub-patterns aren't lowered by esbuild yet, and
    // (b) Vite 8's CSS pipeline merged additional older entries into
    // the JS target list, producing the "safari14 + 2 overrides" signature.
    target: 'esnext',
    // Match JS target so lightningcss/postcss don't down-target CSS
    // and pull a multi-entry list back into the JS pipeline.
    cssTarget: 'esnext',
    // Vite 8's default minifier is now 'oxc'. Keep esbuild for now —
    // with target=esnext esbuild does a pure minify pass with no
    // syntax lowering (the source of all our prior failures).
    minify: !process.env.TAURI_ENV_DEBUG ? 'esbuild' : false,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
})

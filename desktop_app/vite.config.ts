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

  build: {
    // safari13 was Tauri's default but esbuild can't transpile modern
    // destructuring (used by react-router etc.) down that far. Tauri 2 ships
    // with WKWebView from macOS 12+ → at least Safari 15, so safari14 is
    // a safe minimum that gives esbuild enough room.
    target:
      process.env.TAURI_ENV_PLATFORM === 'windows' ? 'chrome105' : 'safari14',
    minify: !process.env.TAURI_ENV_DEBUG ? 'esbuild' : false,
    sourcemap: !!process.env.TAURI_ENV_DEBUG,
  },
})

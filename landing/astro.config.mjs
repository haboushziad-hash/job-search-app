import { defineConfig } from 'astro/config';
import tailwindcss from '@tailwindcss/vite';

// Astro config — static output, zero JS by default. Tailwind 4 via the
// official Vite plugin (the v4 way; no postcss config needed).
export default defineConfig({
  output: 'static',
  vite: {
    plugins: [tailwindcss()],
    build: {
      cssCodeSplit: false,
      // Inline small CSS into HTML (Astro does this for us; setting here
      // to be explicit). Goal: <50KB total page weight gzipped.
    },
  },
  // Inline critical stylesheets so first paint < 500ms
  build: {
    inlineStylesheets: 'auto',
  },
  compressHTML: true,
});

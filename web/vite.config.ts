import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    // Build output is committed into the Python package so `pip install
    // hivepilot[webui]` ships it and needs zero Node at runtime — see
    // hivepilot/webui/ (FastAPI serves this directory as static assets).
    outDir: path.resolve(__dirname, '../hivepilot/webui/static'),
    emptyOutDir: true,
  },
})

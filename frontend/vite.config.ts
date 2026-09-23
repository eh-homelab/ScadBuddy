import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: process.env.VITE_MOCK_API
      ? undefined
      : { '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true } },
  },
  preview: { port: 4173 },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // three.js dominates the bundle; the viewer is behind a dynamic import so it
    // lands in its own chunk rather than the entry.
    chunkSizeWarningLimit: 1000,
  },
})

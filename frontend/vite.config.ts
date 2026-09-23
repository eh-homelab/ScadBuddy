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
  // Bind the literal address Playwright polls (`http://127.0.0.1:4173`).
  // Vite's default host is the NAME `localhost`, which Node 17+ resolves
  // `verbatim`; on a host whose /etc/hosts maps it to ::1 first (GitHub
  // runners) vite binds ::1 only and every IPv4 poll is refused (#62).
  preview: { host: '127.0.0.1', port: 4173 },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // three.js dominates the bundle; the viewer is behind a dynamic import so it
    // lands in its own chunk rather than the entry.
    chunkSizeWarningLimit: 1000,
  },
})

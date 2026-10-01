import { readFileSync } from 'node:fs'
import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const backend = process.env.SCADBUDDY_BACKEND_URL ?? 'http://127.0.0.1:8080'
const agent = process.env.SCADBUDDY_AGENT_URL ?? 'http://127.0.0.1:8081'

/**
 * One origin in development, routed the way the ingress routes it in a deployment
 * (AI spec §4.2): the agent's own paths, `/api/v1/ai/*` and `/mcp`, go to the agent
 * sidecar, and every other `/api` path to the backend. Vite tries these in order and
 * the first that matches wins (a key starting with `^` is a RegExp; the others are
 * prefixes), so the agent's entries come first; otherwise
 * `/api` would swallow `/api/v1/ai/*`, the mistake §4.2 warns about. `vite preview`
 * uses the same table (`preview.proxy` defaults to `server.proxy`).
 *
 * Only the agent entries leave out `changeOrigin`, on purpose. The agent's origin check
 * (agent/src/http/origins.ts) accepts a loopback `Origin` only when `Host` names the
 * same loopback origin, so `Host` must reach it as the browser sent it, as it does
 * through the real ingress for the public URL. The backend's WebSocket check
 * (`origin_allowed()`, backend/scadbuddy/api/realtime.py) reads only `Origin`, so its
 * entry keeps `changeOrigin` as it always had.
 * `ws: true` carries the assistant's socket (`/api/v1/ai/chat`) and the backend's
 * `/api/v1/ws`. The backend's entry needs it too, not only for live updates. Vite
 * leaves an upgrade it does not proxy hanging, and a browser keeps at most one
 * WebSocket to a host in the CONNECTING state (RFC 6455 §4.1). A stuck `/api/v1/ws`
 * handshake therefore held the assistant's socket back, as observed with Chromium
 * through `pnpm preview`.
 *
 * The mocked build (`VITE_MOCK_API=1`) proxies nothing: msw answers every route.
 */
const proxy: Record<string, ProxyOptions> = {
  '^/api/v1/ai(?:[/?]|$)': { target: agent, ws: true },
  '^/mcp(?:[/?]|$)': { target: agent },
  '/api': { target: backend, changeOrigin: true, ws: true },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: process.env.VITE_MOCK_API ? undefined : proxy,
  },
  // Bind the literal address Playwright polls (`http://127.0.0.1:4173`).
  // Vite's default host is the NAME `localhost`, which Node 17+ resolves
  // `verbatim`; on a host whose /etc/hosts maps it to ::1 first (GitHub
  // runners) vite binds ::1 only and every IPv4 poll is refused (#62).
  preview: {
    host: '127.0.0.1',
    port: 4173,
    // The backend's PAGE_CSP (backend/scadbuddy/api/static.py), so the mocked e2e run
    // meets the same policy a template UI does in production (spec §9).
    headers: { 'Content-Security-Policy': readFileSync(new URL('./page-csp.txt', import.meta.url), 'utf8').trim() },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // three.js dominates the bundle; the viewer is behind a dynamic import so it
    // lands in its own chunk rather than the entry.
    chunkSizeWarningLimit: 1000,
  },
})

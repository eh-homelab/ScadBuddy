import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { z } from 'zod'
import { installAgentBridge } from './agent'
import { App } from './App'
import { installStaleChunkReload, loadOptionalChunk } from './lib/staleChunks'
import './index.css'

// The page CSP has no 'unsafe-eval'. zod's JIT probes `new Function` on its first parse
// and swallows the refusal, but the browser still reports a violation; jitless skips the
// probe (and the JIT it would enable).
z.config({ jitless: true })

async function start() {
  installStaleChunkReload()
  if (import.meta.env.VITE_MOCK_API === '1') {
    const { worker } = await import('./mocks/browser')
    await worker.start({
      onUnhandledRequest: 'bypass',
      serviceWorker: { url: `${import.meta.env.BASE_URL}mockServiceWorker.js` },
    })
  }

  installAgentBridge()

  const root = document.getElementById('root')
  if (!root) throw new Error('#root is missing from index.html')

  createRoot(root).render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>,
  )
  loadTracingAfterFirstPaint()
}

/**
 * Tracing spec 2026-10-01 §5.3: the SDK is its own chunk, fetched after the first
 * paint (the frame after the next one), so it never delays the page or the 3D viewer.
 * Until it loads, `lib/traceAction.ts` makes no-op spans. The chunk is optional: it
 * loads through `loadOptionalChunk`, so a failure (often a blocker, not a stale deploy)
 * does not trigger `installStaleChunkReload`'s reload; the `catch` keeps the rejection
 * from surfacing, and a page that stays untraced is the fallback.
 */
function loadTracingAfterFirstPaint() {
  requestAnimationFrame(() => {
    setTimeout(() => {
      loadOptionalChunk(() => import('./lib/tracing')).then(({ startTracing }) => startTracing()).catch(() => undefined)
    }, 0)
  })
}

void start()

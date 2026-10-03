import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { z } from 'zod'
import { installAgentBridge } from './agent'
import { App } from './App'
import { installStaleChunkReload } from './lib/staleChunks'
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
 * Until it loads, `lib/traceAction.ts` makes no-op spans. A chunk that fails to load
 * leaves the page untraced; `installStaleChunkReload` handles a stale deploy.
 */
function loadTracingAfterFirstPaint() {
  requestAnimationFrame(() => {
    setTimeout(() => {
      import('./lib/tracing').then(({ startTracing }) => startTracing()).catch(() => undefined)
    }, 0)
  })
}

void start()

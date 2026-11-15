import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import { App } from './App'
import './storefront.css'

/**
 * Where the page's routes start. The edge serves it at `/` on the shop's own host. `vite dev`
 * and the mocked e2e run serve both entries from one server, where `/` is the back office:
 * there the page answers under `/store` (vite.config.ts `storefrontPreview`).
 */
function basename(): string | undefined {
  if (!import.meta.env.DEV && import.meta.env.VITE_MOCK_API !== '1') return undefined
  return location.pathname === '/store' || location.pathname.startsWith('/store/') ? '/store' : undefined
}

async function start() {
  if (import.meta.env.VITE_MOCK_API === '1') {
    const { worker } = await import('../mocks/browser')
    await worker.start({
      onUnhandledFrame: 'bypass',
      serviceWorker: { url: `${import.meta.env.BASE_URL}mockServiceWorker.js` },
    })
  }
  const root = document.getElementById('root')
  if (!root) throw new Error('#root is missing from storefront.html')
  createRoot(root).render(
    <StrictMode>
      <BrowserRouter basename={basename()}>
        <App />
      </BrowserRouter>
    </StrictMode>,
  )
}

void start()

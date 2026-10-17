import '@testing-library/jest-dom/vitest'
import { cleanup, configure, getConfig } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, expect, vi } from 'vitest'
import { resetMockState } from './src/mocks/handlers'
import { server } from './src/mocks/server'
import { resetDisplayUnit } from './src/lib/units'
import { resetRealtime } from './src/lib/realtime'
import { resetAiAvailability } from './src/agent/chat/availability'
import { FakeIntersectionObserver, resetIntersections } from './src/test/intersection'
import { resending } from './src/test/resend'

// A find*/waitFor re-runs its query on every DOM change, and by default each miss
// builds its error with prettyDOM of the whole document: on a page of ~1000 elements
// (Settings) that alone took ~0.2 s of each second the page had to render, so the page
// came in after the 1 s wait under load (#1485). Inside a wait (Testing Library
// disables its "expensive error diagnostics" there) a miss is the message alone; the
// error a wait finally throws still gets the DOM, from waitFor's onTimeout.
const elementError = getConfig().getElementError
configure({
  // As testTimeout in vitest.config.ts: a find*/waitFor on a busy host gets 3 s, not 1 s,
  // to see what a page shows (#1485). A wait that is meant to fail still fails.
  asyncUtilTimeout: 3000,
  getElementError(message, container) {
    if (!(getConfig() as { _disableExpensiveErrorDiagnostics?: boolean })._disableExpensiveErrorDiagnostics) {
      return elementError(message, container)
    }
    const error = new Error(message ?? '')
    error.name = 'TestingLibraryElementError'
    return error
  },
})

// jsdom parses its whole default stylesheet the first time anything asks for a computed
// style (~0.15-0.45 s), and every role query asks. Done here, before the tests, it no
// longer lands inside the first findByRole's 1 s wait in each file (#1485).
getComputedStyle(document.documentElement)

/** `fetch` as MSW patched it, put back once the file is done. */
let mswFetch: typeof fetch | undefined
beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' })
  // MSW lets some requests through to the real network; `resending` sends them again.
  const intercepted = globalThis.fetch
  // The test in the line, as files run in parallel and their stderr interleaves.
  globalThis.fetch = resending(intercepted, (line) => {
    const { testPath, currentTestName } = expect.getState()
    process.stderr.write(`${line} [${testPath ?? 'outside a test'} > ${currentTestName ?? '-'}]\n`)
  })
  mswFetch = intercepted
})
afterEach(async () => {
  // Unmounted here, before the tick below, rather than in Testing Library's own hook,
  // which runs after this one: a page left dirty (Settings, useLeaveGuard) takes its
  // history sentinel off with `history.back()` as it unmounts, and jsdom's popstate for
  // it comes a task later. Waited for here, it cannot reach the next test's page as a
  // Back pressed there (#3671). Under fake timers jsdom's traversal waits on them too,
  // so there is nothing to wait for.
  cleanup()
  if (!vi.isFakeTimers()) await new Promise((resolve) => setTimeout(resolve, 0))
  server.resetHandlers()
  resetMockState()
  resetDisplayUnit()
  resetRealtime()
  resetAiAvailability()
  resetIntersections()
  // #2372 — the assistant panel remembers per tab; every test starts in a fresh one.
  window.sessionStorage.clear()
  // A Radix select or dialog still open when its test ends can leave the body
  // `pointer-events: none`, and the next test could click nothing (#3671).
  document.body.style.pointerEvents = ''
})
afterAll(() => {
  if (mswFetch) globalThis.fetch = mswFetch
  server.close()
})

if (!globalThis.matchMedia) {
  globalThis.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof globalThis.matchMedia
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

if (!URL.createObjectURL) {
  URL.createObjectURL = () => 'blob:mock'
  URL.revokeObjectURL = () => {}
}

// Embla watches which slides are in view, and a catalogue card waits to be near it
// (#558); jsdom lays nothing out, so nothing is until a test says so with `intersect`.
if (!globalThis.IntersectionObserver) {
  globalThis.IntersectionObserver =
    FakeIntersectionObserver as unknown as typeof IntersectionObserver
}

// Radix's Select captures the pointer on open and scrolls its options into view (#3653),
// and cmdk scrolls its active item; jsdom has neither.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}

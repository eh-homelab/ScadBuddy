import '@testing-library/jest-dom/vitest'
import { configure, getConfig } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { resetMockState } from './src/mocks/handlers'
import { server } from './src/mocks/server'
import { resetDisplayUnit } from './src/lib/units'
import { resetRealtime } from './src/lib/realtime'
import { resetAiAvailability } from './src/agent/chat/availability'
import { FakeIntersectionObserver } from './src/test/intersection'

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

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
  resetDisplayUnit()
  resetRealtime()
  resetAiAvailability()
})
afterAll(() => server.close())

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

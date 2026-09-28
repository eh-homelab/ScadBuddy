import '@testing-library/jest-dom/vitest'
import { afterAll, afterEach, beforeAll } from 'vitest'
import { resetMockState } from './src/mocks/handlers'
import { server } from './src/mocks/server'
import { resetDisplayUnit } from './src/lib/units'
import { resetRealtime } from './src/lib/realtime'
import { FakeIntersectionObserver } from './src/test/intersection'

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  resetMockState()
  resetDisplayUnit()
  resetRealtime()
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

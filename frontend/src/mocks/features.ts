import type { RequestHandler, WebSocketHandler } from 'msw'

/**
 * A feature's mock API lives in its own `features/<feature>.ts`, so a feature PR adds
 * a file instead of editing `handlers.ts`, the list every PR touches (#508). A module
 * exports `handlers` and, if it keeps state, `reset`, which `resetMockState` calls.
 */
export type MockFeature = {
  handlers: readonly (RequestHandler | WebSocketHandler)[]
  reset?: () => void
}

const modules = import.meta.glob<MockFeature>(['./features/*.ts', '!./features/*.test.ts'], {
  eager: true,
})

/** Every feature module, in file-name order. */
export const features: MockFeature[] = Object.keys(modules)
  .sort()
  .map((path) => modules[path] as MockFeature)

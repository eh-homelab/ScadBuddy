import type { RequestHandler, WebSocketHandler } from 'msw'

/**
 * A feature's mock API lives in its own `features/<feature>.ts` (or a
 * `features/<feature>/` folder), so a feature PR adds
 * a file instead of editing `handlers.ts`, the list every PR touches (#508). A module
 * exports `handlers` and, if it keeps state, `reset`, which `resetMockState` calls.
 */
export type MockFeature = {
  handlers: readonly (RequestHandler | WebSocketHandler)[]
  reset?: () => void
}

/**
 * Checks each module at the glob boundary: a file that forgets to export `handlers` fails
 * here, naming the file, instead of its mocks silently going missing.
 */
export function toFeatures(modules: Record<string, unknown>): MockFeature[] {
  return Object.keys(modules)
    .sort()
    .map((path) => {
      const module = modules[path] as Partial<MockFeature> | undefined
      if (!Array.isArray(module?.handlers)) {
        throw new Error(`mock feature ${path} must export a \`handlers\` array`)
      }
      return module as MockFeature
    })
}

// Every `.ts` file under `features/`, at any depth, is a module and must export
// `handlers`; colocated tests are skipped.
const modules = import.meta.glob(['./features/**/*.ts', '!./features/**/*.test.ts'], {
  eager: true,
})

/**
 * Every feature module, in file-path order. That order is not meant to decide which
 * mock answers: `features.test.ts` fails if two modules declare the same route.
 */
export const features: MockFeature[] = toFeatures(modules)

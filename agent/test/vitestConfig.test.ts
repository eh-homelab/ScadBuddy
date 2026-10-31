import { describe, expect, it } from 'vitest'
import config from '../vitest.config.js'

// #3608: the Postgres tests' hooks copy and drop a database each, which a loaded
// runner slows as much as a test body (#1746). A hook held to less than a test's
// room times out a setup that did nothing wrong.
describe('vitest config', () => {
  it("gives hooks at least a test's room", () => {
    const { hookTimeout, testTimeout } = config.test ?? {}
    expect(testTimeout).toBeGreaterThan(10_000)
    expect(hookTimeout).toBeGreaterThanOrEqual(testTimeout!)
  })

  // #5030: the leak guard (test/support/leakGuard.ts) walks every process below its
  // worker, so each file needs a process of its own.
  it('runs each file in a process of its own, as the leak guard needs', () => {
    const { pool, isolate, setupFiles } = config.test ?? {}
    expect(setupFiles).toContain('test/support/leakGuard.ts')
    expect(pool).toBe('forks')
    expect(isolate).not.toBe(false)
  })
})

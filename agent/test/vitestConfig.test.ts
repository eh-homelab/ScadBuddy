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
})

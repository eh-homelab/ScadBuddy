import { describe, expect, it } from 'vitest'
import { privateCreate } from './support/temporal.js'

// localTemporal() calls the SDK's type-private TestWorkflowEnvironment.create to pass a
// connect timeout createLocal does not forward. Runs without Temporal, so an SDK bump that
// removes it fails here, by name, rather than in every skipped-locally temporal test.
describe('@temporalio/testing', () => {
  it('still has TestWorkflowEnvironment.create', () => {
    expect(privateCreate()).toBeTypeOf('function')
  })
})

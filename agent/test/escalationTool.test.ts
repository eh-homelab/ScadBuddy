import { describe, expect, it } from 'vitest'
import { flowTools } from '../src/tools/flows.js'

// escalation_respond's description (#2864): a child session's call (`durable:`) takes
// only `deny` or `pass_up`, so the model must read those first, and read that `narrow`
// is for a run's own call (`flow:`) only, before it tries `narrow` on a child's.

describe('escalation_respond', () => {
  const description = flowTools.find((t) => t.name === 'escalation_respond')!.description

  it('leads with deny and pass_up, and says narrow is for a flow: id only', () => {
    expect(description.indexOf('`deny`')).toBeLessThan(description.indexOf('`narrow`'))
    expect(description.indexOf('`pass_up`')).toBeLessThan(description.indexOf('`narrow`'))
    expect(description).toMatch(/`narrow` is only for a `flow:` id/)
    expect(description).toMatch(/`durable:` id, a child session's call, refuses it/)
  })
})

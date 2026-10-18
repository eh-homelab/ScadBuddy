import { describe, expect, it } from 'vitest'
import { respondUpdateId, type RespondArgs } from '../src/gate/durable.js'
import { BROWSER_USER } from '../src/routes/approvals.js'

const base: RespondArgs = { request_id: 'durable:s:r:t', response: { kind: 'approval', decision: 'approve' }, responder: BROWSER_USER, role: 'browser' }

describe('respondUpdateId (#3717)', () => {
  it('is the same for the same body, whatever the key order', () => {
    expect(respondUpdateId({ ...base })).toBe(respondUpdateId({ ...base, response: { decision: 'approve', kind: 'approval' } }))
  })
  it('differs for a different response, responder, role or request', () => {
    const id = respondUpdateId(base)
    expect(respondUpdateId({ ...base, response: { kind: 'approval', decision: 'deny' } })).not.toBe(id)
    expect(respondUpdateId({ ...base, responder: { ...BROWSER_USER, id: 'someone-else' } })).not.toBe(id)
    expect(respondUpdateId({ ...base, role: 'owner' })).not.toBe(id)
    expect(respondUpdateId({ ...base, request_id: 'durable:s:r:u' })).not.toBe(id)
  })
  it('stays short however long the request id is', () => {
    expect(respondUpdateId({ ...base, request_id: 'x'.repeat(5000) }).length).toBeLessThan(100)
  })
})

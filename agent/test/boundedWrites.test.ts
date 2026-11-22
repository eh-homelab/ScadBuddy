import { describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import { WriteTimeout } from '../src/db/bounded.js'
import { SessionResources } from '../src/sessions/touched.js'
import { json } from '../src/tools/registry.js'

// #1076: a tool call never waits long on the rows that record it. A database that
// holds the INSERT (a lock, a stall) costs the call at most `waitMs`, and the wait
// that ran out is reported through onError; the write itself goes on.

describe('bounded record writes', () => {
  it("SessionResources.record returns after waitMs when the write hangs, and says so", async () => {
    const errors: unknown[] = []
    const stalled = { insert: () => new Promise<void>(() => undefined), list: () => Promise.resolve([]) }
    const sink = new SessionResources(stalled, (err) => errors.push(err), { waitMs: 20 })
    const started = performance.now()
    await sink.record({
      sessionId: '00000000-0000-4000-8000-000000000001',
      tool: { name: 'delete_model', risk: 'outward' },
      input: { slug: 'box' },
      result: json({ deleted: 'box' }),
    })
    expect(performance.now() - started).toBeLessThan(1_000)
    expect(errors).toEqual([expect.any(WriteTimeout)])
  })

  it('AuditLog.record returns after waitMs when the INSERT hangs, and says so', async () => {
    const errors: unknown[] = []
    const stalled = { record: () => new Promise<void>(() => undefined), list: () => Promise.reject(new Error('unused')), prune: () => Promise.resolve(0) }
    const audit = new AuditLog({ routes: stalled, hashKey: () => Buffer.alloc(32), onError: (err) => errors.push(err), waitMs: 20 })
    const started = performance.now()
    await audit.record({ kind: 'tool_call', action: 'delete_model', surface: 'harness', actor: { kind: 'browser', id: 'browser', label: 'You' }, outcome: 'ok' })
    expect(performance.now() - started).toBeLessThan(1_000)
    expect(errors).toEqual([expect.any(WriteTimeout)])
  })
})

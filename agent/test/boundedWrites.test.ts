import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { AuditLog } from '../src/audit/log.js'
import { WriteTimeout } from '../src/db/bounded.js'
import { SessionResources } from '../src/sessions/touched.js'
import { json } from '../src/tools/registry.js'

// #1076: a tool call never waits long on the rows that record it. A database that
// holds the INSERT (a lock, a stall) costs the call at most `waitMs`, and the wait
// that ran out is reported through onError; the write itself goes on.

/** A `sql` whose every query hangs: fragments are built, statements never answer. */
function stalledSql(): Sql {
  const sql = (first: unknown) => (Array.isArray(first) && 'raw' in first ? new Promise(() => undefined) : first)
  return sql as unknown as Sql
}

describe('bounded record writes', () => {
  it("SessionResources.record returns after waitMs when the INSERT hangs, and says so", async () => {
    const errors: unknown[] = []
    const sink = new SessionResources(stalledSql(), (err) => errors.push(err), { waitMs: 20 })
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
    const audit = new AuditLog({ sql: stalledSql(), onError: (err) => errors.push(err), waitMs: 20 })
    const started = performance.now()
    await audit.record({ kind: 'tool_call', action: 'delete_model', surface: 'harness', actor: { kind: 'browser', id: 'browser', label: 'You' }, outcome: 'ok' })
    expect(performance.now() - started).toBeLessThan(1_000)
    expect(errors).toEqual([expect.any(WriteTimeout)])
  })
})

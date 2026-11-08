import { randomUUID } from 'node:crypto'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import postgres from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { createLockClient, type Database } from '../src/db.js'
import { WriteTimeout } from '../src/db/bounded.js'
import type { Risk } from '../src/tools/registry.js'
import { OrderCosts, type AssistantCostReport, type ReportAnswer } from '../src/sessions/orderCosts.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The assistant's spend on an order (#5330, job cost spec 2026-11-05 §4.4): a session that
// gives an order item a design revision is reported to the backend's CRUD route as one
// `assistant` cost entry per order, carrying the spend since the session's last report.

const OK: CallToolResult = { content: [{ type: 'text', text: '{}' }] }
const REVISE: { name: string; risk: Risk } = { name: 'order_item_revision_add', risk: 'write' }

describe.skipIf(!TEST_DATABASE_URL)(`order costs in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let url: string
  let posted: AssistantCostReport[]
  let timeouts: number[]
  let answer: ReportAnswer
  let clock: number
  let postTakes: number
  let costs: OrderCosts
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop, url } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    posted = []
    timeouts = []
    answer = 'recorded'
    clock = 0
    postTakes = 0
    failures.length = 0
    costs = new OrderCosts({
      sql: db.sql,
      locks: db.locks,
      post: async (report, timeoutMs) => {
        posted.push(report)
        timeouts.push(timeoutMs)
        clock += postTakes
        return answer
      },
      onError: (err) => failures.push(err),
      now: () => clock,
    })
  })
  afterEach(async () => {
    expect(failures).toEqual([])
    await drop()
  })

  async function session(costUsd: number): Promise<string> {
    const id = randomUUID()
    await db.sql`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, cost_usd)
      VALUES (${id}, 'chat', 'browser', 'person:owner', 'Owner', 'browser', 'person:owner', 'idle', 10, 5, ${costUsd})`
    return id
  }
  const spend = (id: string, costUsd: number) => db.sql`UPDATE ai_sessions SET cost_usd = ${costUsd} WHERE id = ${id}`
  const revise = (sessionId: string, order: string, item: string, ok = true, tool = REVISE) =>
    costs.record({ sessionId, tool, input: { order_id: order, item_id: item }, result: OK, ok })

  it('records only a revision that succeeded', async () => {
    const id = await session(1)
    await revise(id, 'o1', 'i1', false)
    await revise(id, 'o1', 'i2', true, { name: 'order_item_update', risk: 'write' })
    await costs.record({ sessionId: id, tool: REVISE, input: { order_id: 'o1' }, result: OK })
    await costs.report(id)
    expect(posted).toEqual([])
    await revise(id, 'o1', 'i1', true, { name: 'order_item_revision_add_after_acceptance', risk: 'outward' as const })
    await costs.report(id)
    expect(posted).toEqual([{ orderId: 'o1', orderItemId: 'i1', sessionId: id, principal: 'person:owner', costUsd: '1.00000000' }])
  })

  it('splits the spend between the orders and reports each once', async () => {
    const id = await session(1.5)
    await revise(id, 'a', 'a1')
    await revise(id, 'b', 'b1')
    await revise(id, 'b', 'b2')
    await costs.report(id)
    expect(posted).toEqual([
      { orderId: 'a', orderItemId: 'a1', sessionId: id, principal: 'person:owner', costUsd: '0.75000000' },
      { orderId: 'b', orderItemId: null, sessionId: id, principal: 'person:owner', costUsd: '0.75000000' },
    ])
    posted.length = 0
    await costs.report(id)
    // Another revision of an order already reported adds nothing.
    await revise(id, 'a', 'a2')
    await spend(id, 2)
    await revise(id, 'c', 'c1')
    await costs.report(id)
    expect(posted).toEqual([{ orderId: 'c', orderItemId: 'c1', sessionId: id, principal: 'person:owner', costUsd: '0.50000000' }])
  })

  it('keeps a report that failed for the next, and leaves a refused share unattributed', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    answer = 'failed'
    await costs.report(id)
    answer = 'recorded'
    await costs.report(id)
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([
      ['a', '1.00000000'],
      ['a', '1.00000000'],
    ])
    posted.length = 0
    await revise(id, 'gone', 'g1')
    await spend(id, 1.25)
    answer = 'refused'
    await costs.report(id)
    // The refused share is no other order's: the next order gets only the spend since.
    answer = 'recorded'
    await revise(id, 'c', 'c1')
    await spend(id, 1.5)
    await costs.report(id)
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([
      ['gone', '0.25000000'],
      ['c', '0.25000000'],
    ])
  })

  it('sends a share whose answer was lost again as it was, whatever the spend since', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    answer = 'failed'
    await costs.report(id)
    await spend(id, 3)
    await revise(id, 'b', 'b1')
    answer = 'recorded'
    await costs.report(id)
    // `a` keeps the 1.00 it was sent; `b` gets the 2.00 spent since.
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([
      ['a', '1.00000000'],
      ['a', '1.00000000'],
      ['b', '2.00000000'],
    ])
    const [row] = await db.sql<{ total: number }[]>`SELECT SUM(reported_usd) AS total FROM ai_session_order_costs WHERE session_id = ${id}`
    expect(Number(row!.total)).toBeCloseTo(3)
  })

  it('runs one report of a session at a time, across replicas', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    const slow = new OrderCosts({
      sql: db.sql,
      locks: db.locks,
      post: async (report) => {
        posted.push(report)
        await held
        return 'recorded'
      },
    })
    const first = slow.report(id)
    await new Promise((resolve) => setTimeout(resolve, 300))
    // Another replica's OrderCosts finds the session's lock taken and sends nothing.
    await costs.report(id)
    release()
    await first
    expect(posted.map((p) => p.orderId)).toEqual(['a'])
    await costs.report(id)
    expect(posted.map((p) => p.orderId)).toEqual(['a'])
  })

  it('sweeps the orders left to report of a session no turn is running', async () => {
    const idle = await session(1)
    const busy = await session(1)
    const fresh = await session(1)
    await revise(idle, 'a', 'a1')
    await revise(busy, 'b', 'b1')
    await revise(fresh, 'c', 'c1')
    await db.sql`UPDATE ai_session_order_costs SET at = now() - interval '1 hour' WHERE session_id IN (${idle}, ${busy})`
    await db.sql`UPDATE ai_sessions SET status = 'running' WHERE id = ${busy}`
    await costs.sweep()
    expect(posted.map((p) => p.orderId)).toEqual(['a'])
  })

  it('carries only what it sent when a revision of the order lands while its report runs', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    // The second item's revision is recorded while the report of `a` waits for the backend.
    const racing = new OrderCosts({
      sql: db.sql,
      locks: db.locks,
      post: async (report) => {
        posted.push(report)
        await revise(id, 'a', 'a2')
        return 'recorded'
      },
    })
    await racing.report(id)
    expect(posted.map((p) => [p.orderId, p.orderItemId, p.costUsd])).toEqual([['a', 'a1', '1.00000000']])
    const rows = await db.sql<{ order_item_id: string; reported_usd: number; reported: boolean }[]>`
      SELECT order_item_id, reported_usd, reported_at IS NOT NULL AS reported FROM ai_session_order_costs
      WHERE session_id = ${id} ORDER BY order_item_id`
    expect(rows.map((r) => [r.order_item_id, Number(r.reported_usd), r.reported])).toEqual([
      ['a1', 1, true],
      ['a2', 0, true],
    ])
  })

  it('holds no connection of the stores while a post waits', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    const one = postgres(url, { max: 1, onnotice: () => {} })
    const locks = createLockClient((max) => postgres(url, { max, onnotice: () => {} }))
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    let posting!: () => void
    const posted1 = new Promise<void>((resolve) => (posting = resolve))
    const slow = new OrderCosts({
      sql: one,
      locks,
      post: async () => {
        posting()
        await held
        return 'recorded'
      },
    })
    const running = slow.report(id)
    await posted1
    // The one connection of the stores still answers while the post waits.
    const answered = await Promise.race([one`SELECT 1 AS ok`.then(() => true), new Promise((r) => setTimeout(() => r(false), 2_000))])
    release()
    await running
    await Promise.all([one.end({ timeout: 5 }), locks.end()])
    expect(answered).toBe(true)
  })

  it('reports nothing when every lock connection is out', async () => {
    const id = await session(1)
    await revise(id, 'a', 'a1')
    const full = new OrderCosts({ sql: db.sql, locks: { take: async () => undefined, end: async () => {} }, post: async (r) => (posted.push(r), 'recorded') })
    await full.report(id)
    expect(posted).toEqual([])
  })

  it('never holds a revision call up on its row', async () => {
    const id = await session(1)
    const late: unknown[] = []
    const bounded = new OrderCosts({ sql: db.sql, locks: db.locks, post: async () => 'recorded', onError: (err) => late.push(err), waitMs: 200 })
    // Another transaction holds the table: the insert stalls.
    const blocker = postgres(url, { max: 1, onnotice: () => {} })
    let unlock!: () => void
    const unlocked = new Promise<void>((resolve) => (unlock = resolve))
    let locked!: () => void
    const isLocked = new Promise<void>((resolve) => (locked = resolve))
    const tx = blocker.begin(async (t) => {
      await t`LOCK TABLE ai_session_order_costs IN ACCESS EXCLUSIVE MODE`
      locked()
      await unlocked
    })
    await isLocked
    const started = Date.now()
    await bounded.record({ sessionId: id, tool: REVISE, input: { order_id: 'a', item_id: 'a1' }, result: OK })
    expect(Date.now() - started).toBeLessThan(2_000)
    expect(late.some((e) => e instanceof WriteTimeout)).toBe(true)
    unlock()
    await tx
    await blocker.end({ timeout: 5 })
  })

  it('sweeps at most its limit of sessions at once', async () => {
    const ids = [await session(1), await session(1)]
    for (const [n, id] of ids.entries()) await revise(id, `o${n}`, `i${n}`)
    await db.sql`UPDATE ai_session_order_costs SET at = now() - interval '1 hour'`
    const capped = new OrderCosts({ sql: db.sql, locks: db.locks, post: async (r) => (posted.push(r), 'recorded'), sweepLimit: 1 })
    await capped.sweep()
    expect(posted).toHaveLength(1)
  })

  it('reports nothing for a session with no spend since', async () => {
    const id = await session(0)
    await revise(id, 'a', 'a1')
    await costs.report(id)
    expect(posted).toEqual([])
  })

  it('keeps a share the backend found under one minor unit for the next report', async () => {
    const id = await session(0.003)
    await revise(id, 'a', 'a1')
    answer = 'deferred'
    await costs.report(id)
    answer = 'recorded'
    await spend(id, 0.5)
    await costs.report(id)
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([
      ['a', '0.00300000'],
      ['a', '0.50000000'],
    ])
  })

  it('sends every report of one turn within one deadline, the rest at the next', async () => {
    const id = await session(4)
    for (const order of ['a', 'b', 'c', 'd']) await revise(id, order, `${order}1`)
    postTakes = 6_000
    await costs.report(id)
    // 15 s in all: two whole posts, a third with the 3 s left, then none.
    expect(posted.map((p) => p.orderId)).toEqual(['a', 'b', 'c'])
    expect(timeouts).toEqual([5_000, 5_000, 3_000])
    posted.length = 0
    await costs.report(id)
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([['d', '1.00000000']])
  })
})

import { randomUUID } from 'node:crypto'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
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
  let posted: AssistantCostReport[]
  let answer: ReportAnswer
  let costs: OrderCosts
  const failures: unknown[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    posted = []
    answer = 'recorded'
    failures.length = 0
    costs = new OrderCosts(
      db.sql,
      async (report) => {
        posted.push(report)
        return answer
      },
      (err) => failures.push(err),
    )
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

  it('keeps a report that failed for the next, and drops one refused for good', async () => {
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
    // A refused order carried none of the spend: the next order gets it.
    answer = 'recorded'
    await revise(id, 'c', 'c1')
    await costs.report(id)
    expect(posted.map((p) => [p.orderId, p.costUsd])).toEqual([
      ['gone', '0.25000000'],
      ['c', '0.25000000'],
    ])
  })

  it('reports nothing for a session with no spend since', async () => {
    const id = await session(0)
    await revise(id, 'a', 'a1')
    await costs.report(id)
    expect(posted).toEqual([])
  })
})

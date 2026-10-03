import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PROTOCOL_VERSION } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { PARENT_SPAN_ID, resetTracing, TRACE_ID, TRACEPARENT, testTracing, waitForSpan } from './support/tracing.js'

// The turn's trace against a real SessionManager, without the SDK: the chat
// socket's first frame carries the browser's traceparent (spec 2026-10-01 §4),
// and an orphan approved after a restart resumes under its decision.

const spans = testTracing()

describe.skipIf(!TEST_DATABASE_URL)(`turn tracing${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let runs: unknown[]

  beforeEach(async () => {
    await resetTracing()
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    runs = scripted.runs
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner, pollMs: 20 })
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  const frame = (extra: Record<string, unknown>) =>
    JSON.stringify({ v: PROTOCOL_VERSION, type: 'user.message', text: 'hi', context: { route: '/' }, ...extra })

  it('a chat turn is the child of the traceparent in its first frame', async () => {
    const connection = new ChatConnection(m, () => {})
    await connection.open()
    await connection.receive(frame({ traceparent: TRACEPARENT }))
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    connection.close()
    expect(turn.spanContext().traceId).toBe(TRACE_ID)
    expect(turn.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    expect(turn.attributes).toMatchObject({ 'scadbuddy.segment': 0, 'scadbuddy.outcome': 'success' })
  })

  it('a garbage traceparent is ignored: the message is handled and the turn is a root', async () => {
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    await connection.receive(frame({ traceparent: 'not-a-traceparent' }))
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    connection.close()
    expect(turn.parentSpanContext).toBeUndefined()
    expect(runs).toHaveLength(1)
    expect(out.filter((e) => e.type === 'error')).toEqual([])
  })

  it('a traceparent that is too long or not a string is ignored, never refused', async () => {
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    await connection.receive(frame({ traceparent: 'x'.repeat(5000) }))
    await connection.receive(frame({ traceparent: 42 }))
    const turns = () => spans.getFinishedSpans().filter((s) => s.name === 'agent.turn')
    await expect.poll(() => turns().length, { timeout: 10_000 }).toBe(2)
    connection.close()
    expect(runs).toHaveLength(2)
    expect(out.filter((e) => e.type === 'error')).toEqual([])
    expect(turns().map((s) => s.parentSpanContext)).toEqual([undefined, undefined])
  })

  it('an orphan approved after a restart resumes as a new turn under its decision', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
      traceparent: TRACEPARENT,
    })
    await m.approvals.decide(browser, approval.id, true)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    expect(turn.parentSpanContext?.spanId).toBe(decision.spanContext().spanId)
    expect(decision.links[0]?.context.spanId).toBe(PARENT_SPAN_ID)
  })
})

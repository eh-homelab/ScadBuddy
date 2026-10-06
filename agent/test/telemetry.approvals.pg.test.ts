import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { GateTrace } from '../src/approvals/service.js'
import type { Database } from '../src/db.js'
import { approvalView } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { tracer } from '../src/telemetry/trace.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { PARENT_SPAN_ID, resetTracing, TRACE_ID, TRACEPARENT, testTracing, waitForSpan } from './support/tracing.js'

// Approvals end and link (spec 2026-10-01 §5.4): the parked span's context is
// stored on the row, and the decision is its own trace linked to it.

const spans = testTracing()

describe.skipIf(!TEST_DATABASE_URL)(`approval tracing${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager

  beforeEach(async () => {
    await resetTracing()
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, approvalPollMs: 20 })
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  async function orphan(traceparent?: string) {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
      ...(traceparent ? { traceparent } : {}),
    })
    return { session, approval }
  }

  it('stores the parked span’s context, and never shows it in the approval view', async () => {
    const { approval } = await orphan(TRACEPARENT)
    expect(approval.traceparent).toBe(TRACEPARENT)
    expect(approval.decisionTraceparent).toBeNull()
    expect(Object.keys(approvalView(approval))).not.toContain('traceparent')
    expect(JSON.stringify(approvalView(approval))).not.toContain(TRACE_ID)
  })

  it('a human decision is a child of the request that made it, linked to the parked span', async () => {
    const { approval } = await orphan(TRACEPARENT)
    let requestSpanId = ''
    await tracer().startActiveSpan('POST /api/v1/ai/approvals/:id/deny', async (request) => {
      requestSpanId = request.spanContext().spanId
      await m.approvals.decide(browser, approval.id, false)
      request.end()
    })
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.parentSpanContext?.spanId).toBe(requestSpanId)
    expect(decision.links.map((l) => [l.context.traceId, l.context.spanId])).toEqual([[TRACE_ID, PARENT_SPAN_ID]])
    expect(decision.attributes).toMatchObject({
      'scadbuddy.approval_id': approval.id,
      'scadbuddy.outcome': 'denied',
      'scadbuddy.tool': 'mcp__stub__print',
      'scadbuddy.tier': 'outward',
      'scadbuddy.decided_by_kind': 'browser',
    })
    expect(decision.attributes['scadbuddy.wait_seconds']).toBeGreaterThanOrEqual(0)
    const row = await m.approvals.get(approval.id, browser)
    expect(row.decisionTraceparent).toBe(`00-${decision.spanContext().traceId}-${decision.spanContext().spanId}-01`)
  })

  it('an expiry is a trace of its own, linked to the parked span', async () => {
    const { approval } = await orphan(TRACEPARENT)
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    expect(await m.approvals.expireDue()).toBe(1)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.parentSpanContext).toBeUndefined()
    expect(decision.attributes['scadbuddy.outcome']).toBe('expired')
    expect(decision.links[0]?.context.spanId).toBe(PARENT_SPAN_ID)
  })

  it('a prepare evicted by a newer one is a cancelled root decision, linked to its parked span', async () => {
    const prepare = (traceparent: string, n: number) =>
      m.approvals.createPrepared(
        { toolUseId: `prep_${n}`, tool: 'mcp__stub__print', input: { job: `box${n}.3mf` }, tier: 'outward', requestedBy: agentA, traceparent },
        { perPrincipal: 1, total: 100, evictReason: 'superseded' },
      )
    const otherParent = 'b7ad6b7169203331'
    const first = await prepare(TRACEPARENT, 1)
    await prepare(`00-${TRACE_ID}-${otherParent}-01`, 2)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.parentSpanContext).toBeUndefined()
    expect(decision.links.map((l) => [l.context.traceId, l.context.spanId])).toEqual([[TRACE_ID, PARENT_SPAN_ID]])
    expect(decision.attributes).toMatchObject({ 'scadbuddy.approval_id': first!.id, 'scadbuddy.outcome': 'cancelled' })
    const [row] = await db.sql<{ decision: string; decision_traceparent: string | null }[]>`
      SELECT decision, decision_traceparent FROM ai_approvals WHERE id = ${first!.id}`
    expect(row).toEqual({
      decision: 'cancelled',
      decision_traceparent: `00-${decision.spanContext().traceId}-${decision.spanContext().spanId}-01`,
    })
  })

  it('a row with no stored context (written before the migration) is decided without a link or an error', async () => {
    const { approval } = await orphan()
    expect(approval.traceparent).toBeNull()
    await m.approvals.decide(browser, approval.id, false)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.links).toEqual([])
  })

  it('the gate stores the park’s traceparent and hands the decision back to the turn’s trace', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const turnId = randomUUID()
    // The turn is live on this session, so the decision is a parked call's, not an orphan's.
    await db.sql`UPDATE ai_sessions SET turn_id = ${turnId}, lease_until = now() + interval '1 minute' WHERE id = ${session.id}`
    const calls: string[] = []
    let decidedWith: string | null = null
    const trace: GateTrace = {
      park: (toolUseId, toolName) => {
        calls.push(`park ${toolUseId} ${toolName}`)
        return {
          traceparent: TRACEPARENT,
          parked: (approvalId) => calls.push(`parked ${approvalId}`),
          decided: (approval, runs) => {
            calls.push(`decided ${approval.decision} ${runs}`)
            decidedWith = approval.decisionTraceparent
          },
          abandoned: () => calls.push('abandoned'),
        }
      },
    }
    const stop = new AbortController()
    const gate = m.approvals.gate({ sessionId: session.id, turnId, requestedBy: agentA, secrets: () => [], signal: stop.signal, trace })
    const verdict = gate({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_g', tier: 'outward', signal: stop.signal })
    await expect.poll(async () => (await m.approvals.list(browser, { sessionId: session.id, pending: true })).length).toBe(1)
    const [pending] = await m.approvals.list(browser, { sessionId: session.id, pending: true })
    expect(pending!.traceparent).toBe(TRACEPARENT)
    await m.approvals.decide(browser, pending!.id, true)
    expect(await verdict).toMatchObject({ approved: true })
    expect(calls).toEqual([`park toolu_g mcp__stub__print`, `parked ${pending!.id}`, 'decided approved true'])
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decidedWith).toBe(`00-${decision.spanContext().traceId}-${decision.spanContext().spanId}-01`)
  })

  it('a row that cannot be written abandons the park, so the turn’s count is undone', async () => {
    const calls: string[] = []
    const trace: GateTrace = {
      park: (toolUseId) => {
        calls.push(`park ${toolUseId}`)
        return {
          traceparent: TRACEPARENT,
          parked: () => calls.push('parked'),
          decided: () => calls.push('decided'),
          abandoned: () => calls.push('abandoned'),
        }
      },
    }
    const stop = new AbortController()
    // No such session: the insert breaks its foreign key.
    const gate = m.approvals.gate({ sessionId: randomUUID(), turnId: randomUUID(), requestedBy: agentA, secrets: () => [], signal: stop.signal, trace })
    await expect(gate({ toolName: 'mcp__stub__print', input: {}, toolUseId: 'toolu_g', tier: 'outward', signal: stop.signal })).rejects.toThrow()
    expect(calls).toEqual(['park toolu_g', 'abandoned'])
  })
})

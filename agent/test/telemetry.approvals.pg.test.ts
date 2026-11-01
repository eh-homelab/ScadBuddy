import { afterEach, beforeEach, describe, expect, it } from 'vitest'
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
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner })
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

  it('a decision whose transaction rolls back ends its span as rolled_back, not as the decision', async () => {
    const { approval } = await orphan(TRACEPARENT)
    // The span exists by the time decision_traceparent is written; that write then fails.
    await db.sql.unsafe(`
      CREATE FUNCTION refuse() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'refused'; END $$;
      CREATE TRIGGER refuse BEFORE UPDATE OF decision_traceparent ON ai_approvals FOR EACH ROW EXECUTE FUNCTION refuse();`)
    await expect(m.approvals.decide(browser, approval.id, false)).rejects.toThrow()
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.attributes['scadbuddy.outcome']).toBe('rolled_back')
    expect(decision.attributes['scadbuddy.failure_class']).toBeDefined()
    expect((await m.approvals.get(approval.id, browser)).decision).toBeNull()
  })

  it('an eviction whose prepare rolls back ends the cancelled row’s span as rolled_back', async () => {
    const prepare = (n: number) =>
      m.approvals.createPrepared(
        { toolUseId: `prep_${n}`, tool: 'mcp__stub__print', input: { job: `box${n}.3mf` }, tier: 'outward', requestedBy: agentA, traceparent: TRACEPARENT },
        { perPrincipal: 1, total: 100, evictReason: 'superseded' },
      )
    const first = await prepare(1)
    // The eviction's span is started; then the new row's insert fails.
    await db.sql.unsafe(`
      CREATE FUNCTION refuse() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'refused'; END $$;
      CREATE TRIGGER refuse BEFORE INSERT ON ai_approvals FOR EACH ROW EXECUTE FUNCTION refuse();`)
    await expect(prepare(2)).rejects.toThrow()
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.attributes).toMatchObject({ 'scadbuddy.approval_id': first!.id, 'scadbuddy.outcome': 'rolled_back' })
    const [row] = await db.sql<{ decision: string | null }[]>`SELECT decision FROM ai_approvals WHERE id = ${first!.id}`
    expect(row?.decision).toBeNull()
  })

  it('a row with no stored context (written before the migration) is decided without a link or an error', async () => {
    const { approval } = await orphan()
    expect(approval.traceparent).toBeNull()
    await m.approvals.decide(browser, approval.id, false)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.links).toEqual([])
  })
})

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import type { CheckedPlugin } from '../src/plugins/registry.js'
import { SpanStatusCode } from '@opentelemetry/api'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PROTOCOL_VERSION } from '../src/sessions/protocol.js'
import { WARM_SPAN } from '../src/telemetry/turn.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { PARENT_SPAN_ID, resetTracing, TRACE_ID, TRACEPARENT, testTracing, waitForSpan } from './support/tracing.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// The turn's trace against a real SessionManager, without the SDK: the chat
// socket's first frame carries the browser's traceparent (spec 2026-10-01 §4).

const spans = testTracing()

describe.skipIf((!TEST_DATABASE_URL) || AGENT_SESSIONS_SKIP !== '')(`turn tracing${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
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

  it('the steps before Claude Code starts are spans under the turn (#2375)', async () => {
    const { session } = await m.start(browser, { origin: 'chat' })
    await (await m.send(session.id, browser, 'hi')).done
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    const steps = spans
      .getFinishedSpans()
      .filter((s) => s.name.startsWith('agent.turn.') && s.parentSpanContext?.spanId === turn.spanContext().spanId)
      .map((s) => s.name)
    expect(steps).toEqual(expect.arrayContaining(['agent.turn.credentials', 'agent.turn.session']))
    // Only when the turn has them: none of these is configured here (#3321).
    for (const absent of ['agent.turn.remote_plugins', 'agent.turn.memory', 'agent.turn.packages']) {
      expect(steps).not.toContain(absent)
    }
  })

  it('the remote plugins and the automatic memory are spans too, when the turn has them (#2981 review)', async () => {
    m.abortAll()
    const hindsight: CheckedPlugin = {
      plugin: { name: 'hindsight', url: 'http://hindsight.invalid:1/mcp/b/', toolTiers: {}, disabledTools: [] },
      address: '127.0.0.1',
    }
    m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scriptedRunner(() => ({ reply: 'ok' })).runner,
      pollMs: 20,
      remotePlugins: () => Promise.resolve({ plugins: [], problems: [], secrets: [], hindsight, terminate: async () => {}, release: () => {} }),
    })
    const { session } = await m.start(browser, { origin: 'chat' })
    await (await m.send(session.id, browser, 'hi')).done
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    const steps = spans
      .getFinishedSpans()
      .filter((s) => s.parentSpanContext?.spanId === turn.spanContext().spanId)
      .map((s) => s.name)
    expect(steps).toEqual(expect.arrayContaining(['agent.turn.remote_plugins', 'agent.turn.memory']))
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

  it('a cleanup step that throws still ends the turn’s span, failed with its class', async () => {
    m.abortAll()
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({
      sql: db.sql,
      paths: await tempPaths(),
      run: scripted.runner,
      pollMs: 20,
      remotePlugins: () =>
        Promise.resolve({
          plugins: [],
          problems: [],
          secrets: [],
          terminate: async () => {},
          release: () => {
            throw new RangeError('release failed')
          },
        }),
    })
    // The first turn's assembly stays with its warm process (sessions/warm.ts); the
    // second turn's is released as the cache reuses that process, and the throw is its.
    const { session, turn: first } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    expect(await first!.done).toMatchObject({ kind: 'result' })
    const connection = new ChatConnection(m, () => {})
    await connection.open()
    await connection.receive(frame({ sessionId: session.id }))
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn' && s.attributes['scadbuddy.outcome'] === 'failed')
    connection.close()
    expect(turn.status.code).toBe(SpanStatusCode.ERROR)
    expect(turn.attributes['scadbuddy.failure_class']).toBe('RangeError')
  })

  it('the warm cache’s acquire is a span under the turn: a miss on the first, a hit on the next', async () => {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
    await turn!.done
    await (await m.send(session.id, browser, 'again')).done
    const turns = spans.getFinishedSpans().filter((s) => s.name === 'agent.turn')
    const spawns = spans.getFinishedSpans().filter((s) => s.name === WARM_SPAN)
    expect(turns).toHaveLength(2)
    expect(spawns.map((s) => s.attributes['warm.outcome'])).toEqual(['miss', 'hit'])
    expect(spawns.map((s) => s.parentSpanContext?.spanId)).toEqual(turns.map((s) => s.spanContext().spanId))
    expect(spawns.map((s) => s.attributes['scadbuddy.session_id'])).toEqual([session.id, session.id])
  })
})

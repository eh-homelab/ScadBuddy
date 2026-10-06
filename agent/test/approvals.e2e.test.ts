import { fixedCredentials } from './support/fixedCredentials.js'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { ApprovalError, type ApprovalRecord } from '../src/approvals/service.js'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { RiskTier } from '../src/harness/permissions.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import type { SessionManager, SessionManagerDeps } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts, frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, collectUntil, manager, tempPaths } from './support/sessions.js'

// Approvals (#258) end to end: session turns on the real Agent SDK and its
// bundled Claude Code binary (pointed at the local fake Anthropic endpoint),
// with sessions and approvals in Postgres. An outward stub tool stands in for
// #251's registry. Each "replica" is its own state dir and connection pool.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const TOKEN = 'gw-approvals-e2e-token-4444555566667777'
const tiers: Record<string, RiskTier> = { mcp__stub__print: 'outward', mcp__stub__echo: 'read' }
const RESUME_PHRASE = 'Make that same call again'
const HASH_KEY = Buffer.alloc(32, 9)

/** The UI through the TLS ingress (routes/guard.ts). */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

describe.skipIf(skip !== undefined)(`approvals against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let stop: AbortController
  let printed: string[]
  const pools: Database[] = []

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
    printed = []
  })
  afterEach(async () => {
    stop.abort()
    await fake.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  function stubServer() {
    const print = tool('print', 'Send to the printer', { job: z.string() }, (args) => {
      printed.push(args.job)
      return Promise.resolve({ content: [{ type: 'text' as const, text: `printing ${args.job}` }] })
    })
    return createSdkMcpServer({ name: 'stub', tools: [print] })
  }

  async function replica(extra: Partial<SessionManagerDeps> = {}): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    return manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      tierOf: (name) => tiers[name],
      mcpServers: () => ({ stub: stubServer() }),
      approvalPollMs: 50,
      // Replicas share the KEK, so they share the input-hash key (approvals/service.ts approvalHashKey).
      approvalHashKey: HASH_KEY,
      ...extra,
    })
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  /** The model prints `job` (again when asked to resume), and reports what the tool said. */
  function printing(job: () => string) {
    return (r: RecordedRequest): Reply => {
      const last = lastContent(r)
      if (last.includes(RESUME_PHRASE)) return { toolUse: { name: 'mcp__stub__print', input: { job: job() } } }
      if (last.includes('tool_result')) return { text: last.includes('printing') ? 'Printed.' : `Not printed: ${last.slice(0, 300)}` }
      return { toolUse: { name: 'mcp__stub__print', input: { job: job() } } }
    }
  }

  /** Starts a session whose first turn parks on an approval; resolves with it. */
  async function parked(m: SessionManager, owner: Owner = agentA) {
    const { session, turn } = await m.start(owner, { origin: 'mcp', prompt: 'print the box' })
    const events = await m.attach(session.id, owner, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required')
    const required = seen.at(-1)!.event
    if (required.type !== 'approval.required') throw new Error('unreachable')
    return { session, turn: turn!, approvalId: required.id, toolUseId: required.tool }
  }

  const allEvents = async (m: SessionManager, id: string): Promise<LoggedEvent[]> => m.events.read(id, 0, 10_000)
  const types = (events: LoggedEvent[]) => events.map((e) => e.event.type)

  function routes(m: SessionManager): Hono {
    const app = new Hono()
    registerApprovalRoutes(app, {
      approvals: m.approvals,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    return app
  }

  it('parks the turn in waiting_approval until the browser approves over HTTP, then runs the call', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId, toolUseId } = await parked(m)

    // Parked: the session says so, nothing ran, and the model is not being asked anything.
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'waiting_approval', turnActive: true })
    const calls = fake.messageCalls().length
    await new Promise((r) => setTimeout(r, 1500))
    expect(printed).toEqual([])
    expect(fake.messageCalls()).toHaveLength(calls)

    const app = routes(m)
    const list = (await (await app.request(`/api/v1/ai/approvals?session=${session.id}&pending=true`, { headers: UI })).json()) as {
      approvals: { id: string; tool: string; tool_use_id: string; input_summary: string; input_hash: string; decision: null }[]
    }
    expect(list.approvals).toHaveLength(1)
    expect(list.approvals[0]).toMatchObject({
      id: approvalId,
      tool: 'mcp__stub__print',
      tool_use_id: toolUseId,
      input_summary: '{"job":"box.3mf"}',
      input_hash: m.approvals.hash('mcp__stub__print', { job: 'box.3mf' }),
      decision: null,
    })

    // A write without the UI's origin is refused before anything is decided.
    const bare = await app.request(`/api/v1/ai/approvals/${approvalId}/approve`, { method: 'POST' })
    expect(bare.status).toBe(403)

    const res = await app.request(`/api/v1/ai/approvals/${approvalId}/approve`, {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ input_hash: list.approvals[0]!.input_hash }),
    })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ decision: 'approved', decided_by: browser })

    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    const [row] = await m.approvals.list(browser, { sessionId: session.id })
    expect(row).toMatchObject({ decision: 'approved', consumedAt: expect.any(String) })

    const events = await allEvents(m, session.id)
    await expectPanelAccepts(events.map((e) => e.event))
    const approvalPart = types(events).filter((t) => t.startsWith('approval') || t === 'session.status' || t === 'tool.call' || t === 'tool.result')
    expect(approvalPart).toEqual([
      'session.status', // idle (start)
      'session.status', // running
      'tool.call',
      'approval.required',
      'session.status', // waiting_approval
      'approval.resolved',
      'session.status', // running
      'tool.result',
      'session.status', // idle
    ])
    const statuses = events.flatMap((e) => (e.event.type === 'session.status' ? [e.event.status] : []))
    expect(statuses).toEqual(['idle', 'running', 'waiting_approval', 'running', 'idle'])
    const required = events.find((e) => e.event.type === 'approval.required')!.event
    const call = events.find((e) => e.event.type === 'tool.call')!.event
    if (required.type !== 'approval.required' || call.type !== 'tool.call') throw new Error('unreachable')
    expect(required.tool).toBe(call.id)
  }, 60_000)

  it('logs the call before the approval that asks about it, however far behind the turn is in logging it (#881)', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    // The turn logs what the SDK streams one append at a time, while the SDK asks
    // canUseTool as soon as Claude Code does. Hold the turn's append of the call
    // until the approval exists, or 3 s have passed if it never will before the call.
    const append = m.events.append.bind(m.events)
    m.events.append = async (sessionId, events, tx) => {
      if (tx === undefined && events.some((e) => e.type === 'tool.call')) {
        const deadline = Date.now() + 3000
        while (Date.now() < deadline && (await m.approvals.list(browser, { sessionId })).length === 0) {
          await new Promise((r) => setTimeout(r, 50))
        }
      }
      return append(sessionId, events, tx)
    }
    const { session, turn, approvalId } = await parked(m)
    await m.approvals.decide(browser, approvalId, true)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])

    const order = types(await allEvents(m, session.id)).filter((t) => t === 'tool.call' || t.startsWith('approval'))
    expect(order).toEqual(['tool.call', 'approval.required', 'approval.resolved'])
  }, 60_000)

  it('the panel’s own approval.decision message decides it (the socket seam); a denial reaches the model', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)

    const { clientMessage, parseClientMessage } = await frontendClientMessages()
    const message = clientMessage({ type: 'approval.decision', sessionId: session.id, id: approvalId, approve: false })
    expect(parseClientMessage(message).ok).toBe(true)
    await m.approvals.decision(browser, message as never)

    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    const last = lastContent(fake.messageCalls().at(-1)!)
    expect(last).toContain('The user denied mcp__stub__print')
    const events = await allEvents(m, session.id)
    await expectPanelAccepts(events.map((e) => e.event))
    expect(events.map((e) => e.event)).toContainEqual(
      expect.objectContaining({ type: 'approval.resolved', id: approvalId, approved: false, by: browser }),
    )
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
  }, 60_000)

  it('only the browser user, or ANOTHER principal with a grant, may decide; never the requester itself', async () => {
    script = printing(() => 'box.3mf')
    const granted = new Set<string>()
    const m = await replica({ approvalGrants: (p) => Promise.resolve(granted.has(`${p.kind}:${p.id}`)) })
    const { turn, approvalId } = await parked(m)

    // The owning agent, without a grant: forbidden. Another agent cannot even see it.
    await expect(m.approvals.decide(agentA, approvalId, true)).rejects.toMatchObject({ code: 'forbidden', status: 403 })
    await expect(m.approvals.decide(agentB, approvalId, true)).rejects.toMatchObject({ code: 'not_found' })
    const anonymous: Owner = { kind: 'anonymous', id: 'anonymous', label: 'Anonymous' }
    await expect(m.approvals.decide(anonymous, approvalId, true)).rejects.toBeInstanceOf(ApprovalError)

    // A grant never allows self-approval: the requester (session owner and creator) is refused.
    granted.add(`${agentA.kind}:${agentA.id}`)
    await expect(m.approvals.decide(agentA, approvalId, true)).rejects.toMatchObject({
      code: 'forbidden',
      message: expect.stringMatching(/its own outward actions/),
    })
    expect((await m.approvals.get(approvalId, browser)).decision).toBeNull()

    // Another agent with a grant may see and decide it.
    granted.add(`${agentB.kind}:${agentB.id}`)
    expect(await m.approvals.decide(agentB, approvalId, true)).toMatchObject({ decision: 'approved', decidedBy: agentB })
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])
    // Decided once: a second decision is a conflict.
    await expect(m.approvals.decide(browser, approvalId, false)).rejects.toMatchObject({ code: 'conflict' })
  }, 60_000)

  it('refuses a decision bound to a different input', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { turn, approvalId } = await parked(m)
    const other = m.approvals.hash('mcp__stub__print', { job: 'other.3mf' })
    await expect(m.approvals.decide(browser, approvalId, true, { inputHash: other })).rejects.toMatchObject({
      code: 'input_mismatch',
      status: 409,
    })
    expect((await m.approvals.get(approvalId, browser)).decision).toBeNull()
    await m.approvals.decide(browser, approvalId, true, { inputHash: m.approvals.hash('mcp__stub__print', { job: 'box.3mf' }) })
    expect(await turn.done).toMatchObject({ kind: 'result' })
    expect(printed).toEqual(['box.3mf'])
  }, 60_000)

  it('interrupt cancels the pending approval and stops the turn without running the call', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)
    const callsWhenParked = fake.messageCalls().length
    // Any watcher may interrupt (spec §8.6); here the browser user.
    expect(await m.interrupt(session.id, browser)).toBe(true)
    // The turn ends there (#1168): the model is not called again to reply to the abort.
    expect(await turn.done).toEqual({ kind: 'interrupted' })
    expect(fake.messageCalls()).toHaveLength(callsWhenParked)
    expect(printed).toEqual([])
    expect(await m.approvals.get(approvalId, browser)).toMatchObject({
      decision: 'cancelled',
      reason: 'interrupted by You',
      decidedBy: null,
    })
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    await expect(m.approvals.decide(browser, approvalId, true)).rejects.toMatchObject({ code: 'conflict' })
    const events = await allEvents(m, session.id)
    await expectPanelAccepts(events.map((e) => e.event))
    expect(events.map((e) => e.event)).toContainEqual({ v: 1, type: 'approval.resolved', sessionId: session.id, id: approvalId, approved: false })
    expect(events.some((e) => e.event.type === 'assistant.text.done')).toBe(false)
    expect(events.map((e) => e.event).slice(-2)).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: 'interrupted', message: 'the turn was interrupted' },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
  }, 60_000)

  it('handoff cancels the pending approval: the parked call is refused and the turn ends', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)
    await m.handoff(session.id, agentA, agentB).then(() => m.acceptHandoff(session.id, agentB))
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    expect(await m.approvals.get(approvalId, browser)).toMatchObject({
      decision: 'cancelled',
      reason: 'the session was handed off to another MCP token',
    })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('was cancelled (the session was handed off to another MCP token)')
  }, 60_000)

  it('expires a parked approval at its time, and the model is told', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)
    // Bring its time forward rather than wait out the minimum window.
    await db.sql`UPDATE ai_approvals SET expires_at = now() WHERE id = ${approvalId}`
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    expect(await m.approvals.get(approvalId, browser)).toMatchObject({ decision: 'expired' })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('before it expired')
    await expect(m.approvals.decide(browser, approvalId, true)).rejects.toMatchObject({ code: 'expired', status: 410 })
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
  }, 60_000)

  it('a pending approval survives a restart; approving it on another replica resumes the session and runs the call once', async () => {
    script = printing(() => 'box.3mf')
    const a = await replica()
    const { session, turn, approvalId } = await parked(a)

    // Shut replica A down: its turn stops, and the approval stays pending.
    a.abortAll()
    expect(['interrupted', 'result']).toContain((await turn.done).kind)
    const b = await replica()
    expect(await b.get(session.id, agentA)).toMatchObject({ status: 'waiting_approval', turnActive: false })
    const pending: ApprovalRecord[] = await b.approvals.list(browser, { pending: true })
    expect(pending.map((p) => p.id)).toEqual([approvalId])
    expect(printed).toEqual([])

    await b.approvals.decide(browser, approvalId, true)
    const events = await b.attach(session.id, agentA, { signal: stop.signal })
    await collectUntil(events, (e) => e.event.type === 'session.result')
    await expect.poll(async () => (await b.get(session.id, agentA)).status, { timeout: 10_000 }).toBe('idle')

    expect(printed).toEqual(['box.3mf'])
    expect(await b.approvals.get(approvalId, browser)).toMatchObject({ decision: 'approved', consumedAt: expect.any(String) })
    // Used once: no second approval was asked for.
    expect(await b.approvals.list(browser, { sessionId: session.id })).toHaveLength(1)
    const all = (await allEvents(b, session.id)).map((e) => e.event)
    await expectPanelAccepts(all)
    expect(all).toContainEqual(expect.objectContaining({ type: 'user.turn', author: browser, text: expect.stringContaining(RESUME_PHRASE) }))
  }, 90_000)

  it('an approval that could not resume is not inherited: after a handoff, the new owner’s identical call asks again', async () => {
    script = printing(() => 'box.3mf')
    const a = await replica()
    const { session, turn, approvalId } = await parked(a)
    a.abortAll()
    await turn.done
    const b = await replica()
    // Approved while another replica holds the session, so it cannot resume.
    await db.sql`UPDATE ai_sessions SET turn_id = gen_random_uuid(), lease_until = now() + interval '1 minute' WHERE id = ${session.id}`
    await b.approvals.decide(browser, approvalId, true)
    await db.sql`UPDATE ai_sessions SET turn_id = NULL, lease_until = NULL WHERE id = ${session.id}`
    expect((await b.approvals.get(approvalId, browser)).revokedAt).not.toBeNull()

    await b.handoff(session.id, agentA, agentB).then(() => b.acceptHandoff(session.id, agentB))
    await b.send(session.id, agentB, 'print the box')
    const events = await b.attach(session.id, agentB, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required' && e.event.id !== approvalId)
    expect(seen.at(-1)!.event).toMatchObject({ type: 'approval.required', summary: expect.stringContaining('box.3mf') })
    expect(printed).toEqual([])
    await b.interrupt(session.id, browser)
    await expect.poll(async () => (await b.get(session.id, agentB)).status, { timeout: 10_000 }).toBe('idle')
    expect(printed).toEqual([])
  }, 90_000)

  it('after a restart, an approval does not cover a call with a different input', async () => {
    let job = 'box.3mf'
    script = printing(() => job)
    const a = await replica()
    const { session, turn, approvalId } = await parked(a)
    a.abortAll()
    await turn.done

    const b = await replica()
    job = 'swapped.3mf'
    await b.approvals.decide(browser, approvalId, true)
    const events = await b.attach(session.id, agentA, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required' && e.event.id !== approvalId)
    const second = seen.at(-1)!.event
    if (second.type !== 'approval.required') throw new Error('unreachable')
    expect(second.summary).toContain('swapped.3mf')
    expect(printed).toEqual([])
    // The first approval stays approved and unused; the new one waits.
    expect(await b.approvals.get(approvalId, browser)).toMatchObject({ decision: 'approved', consumedAt: null })
    expect(await b.approvals.get(second.id, browser)).toMatchObject({ decision: null })
    await b.interrupt(session.id, browser)
    await expect.poll(async () => (await b.get(session.id, agentA)).status, { timeout: 10_000 }).toBe('idle')
    expect(printed).toEqual([])
  }, 90_000)
})

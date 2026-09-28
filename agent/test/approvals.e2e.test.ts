import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { ApprovalError, type ApprovalRecord, inputHash } from '../src/approvals/service.js'
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
      credential: () => Promise.resolve({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      tierOf: (name) => tiers[name],
      mcpServers: () => ({ stub: stubServer() }),
      approvalPollMs: 50,
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
    const list = (await (await app.request(`/api/v1/ai/approvals?session=${session.id}&pending=true`)).json()) as {
      approvals: { id: string; tool: string; tool_use_id: string; input_summary: string; input_hash: string; decision: null }[]
    }
    expect(list.approvals).toHaveLength(1)
    expect(list.approvals[0]).toMatchObject({
      id: approvalId,
      tool: 'mcp__stub__print',
      tool_use_id: toolUseId,
      input_summary: '{"job":"box.3mf"}',
      input_hash: inputHash('mcp__stub__print', { job: 'box.3mf' }),
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

  it('only the browser user, or a principal with a grant, may decide', async () => {
    script = printing(() => 'box.3mf')
    const granted = new Set<string>()
    const m = await replica({ approvalGrants: (p) => Promise.resolve(granted.has(`${p.kind}:${p.id}`)) })
    const { turn, approvalId } = await parked(m)

    // The owning agent, without a grant: forbidden. Another agent cannot even see it.
    await expect(m.approvals.decide(agentA, approvalId, true)).rejects.toMatchObject({ code: 'forbidden', status: 403 })
    await expect(m.approvals.decide(agentB, approvalId, true)).rejects.toMatchObject({ code: 'not_found' })
    const anonymous: Owner = { kind: 'anonymous', id: 'anonymous', label: 'Anonymous' }
    await expect(m.approvals.decide(anonymous, approvalId, true)).rejects.toBeInstanceOf(ApprovalError)
    // Another agent WITH a grant still cannot decide in a session it cannot see.
    granted.add(`${agentB.kind}:${agentB.id}`)
    await expect(m.approvals.decide(agentB, approvalId, true)).rejects.toMatchObject({ code: 'not_found' })
    expect((await m.approvals.get(approvalId, browser)).decision).toBeNull()

    // With a per-token grant, the owner may.
    granted.add(`${agentA.kind}:${agentA.id}`)
    expect(await m.approvals.decide(agentA, approvalId, true)).toMatchObject({ decision: 'approved', decidedBy: agentA })
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])
    // Decided once: a second decision is a conflict.
    await expect(m.approvals.decide(browser, approvalId, false)).rejects.toMatchObject({ code: 'conflict' })
  }, 60_000)

  it('refuses a decision bound to a different input', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { turn, approvalId } = await parked(m)
    const other = inputHash('mcp__stub__print', { job: 'other.3mf' })
    await expect(m.approvals.decide(browser, approvalId, true, { inputHash: other })).rejects.toMatchObject({
      code: 'input_mismatch',
      status: 409,
    })
    expect((await m.approvals.get(approvalId, browser)).decision).toBeNull()
    await m.approvals.decide(browser, approvalId, true, { inputHash: inputHash('mcp__stub__print', { job: 'box.3mf' }) })
    expect(await turn.done).toMatchObject({ kind: 'result' })
    expect(printed).toEqual(['box.3mf'])
  }, 60_000)

  it('interrupt cancels the pending approval and stops the turn without running the call', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)
    // Any watcher may interrupt (spec §8.6); here the browser user.
    expect(await m.interrupt(session.id, browser)).toBe(true)
    // Either outcome (manager.ts finish): the abort may land before or after
    // Claude Code reports the failed permission request to the model.
    expect(['interrupted', 'result']).toContain((await turn.done).kind)
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
  }, 60_000)

  it('handoff cancels the pending approval: the parked call is refused and the turn ends', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, approvalId } = await parked(m)
    await m.handoff(session.id, agentA, agentB)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    expect(await m.approvals.get(approvalId, browser)).toMatchObject({
      decision: 'cancelled',
      reason: 'the session was handed off to Agent B',
    })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('was cancelled (the session was handed off to Agent B)')
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

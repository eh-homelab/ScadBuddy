import { fixedCredentials } from './support/fixedCredentials.js'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { SETTING_APPROVAL_EXPIRY_SECONDS } from '../src/approvals/service.js'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import type { RiskTier } from '../src/harness/permissions.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { registerPendingInputRoutes, respond, RespondError } from '../src/routes/pendingInput.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import type { SessionManager, SessionManagerDeps } from '../src/sessions/manager.js'
import type { InputEntry, Owner } from '../src/sessions/protocol.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, browser, collectUntil, manager, stopManagers, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// Approvals (#258) end to end: session turns on the real Agent SDK and its bundled Claude
// Code binary (pointed at the local fake Anthropic endpoint), each an AgentSession's turn
// (plan Step 6.3, support/agentSessions.ts), its outward calls parked in the gate's
// ai_pending_input and answered through the pending-input route. An outward stub tool
// stands in for #251's registry. Each "replica" is its own state dir and connection pool.
//
// A parked approval that outlives its turn's process (a restart) is the session workflow's
// to hold and re-place, never an orphan another replica resumes: sessionRelease.temporal and
// sessionAffinity.temporal cover that.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const TOKEN = 'gw-approvals-e2e-token-4444555566667777'
const tiers: Record<string, RiskTier> = { mcp__stub__print: 'outward', mcp__stub__echo: 'read' }
const HASH_KEY = Buffer.alloc(32, 9)

/** The UI through the TLS ingress (routes/guard.ts). */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

describe.skipIf(skip !== undefined || AGENT_SESSIONS_SKIP !== '')(`approvals against the real SDK${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let url: string
  let drop: () => Promise<void>
  let stop: AbortController
  let printed: string[]
  const pools: Database[] = []

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
    printed = []
  })
  afterEach(async () => {
    stop.abort()
    await stopManagers()
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
    const pool = connectDatabase(url, { searchPath: schema })
    pools.push(pool)
    return manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      tierOf: (name) => tiers[name],
      mcpServers: () => ({ stub: stubServer() }),
      approvalHashKey: HASH_KEY,
      ...extra,
    })
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')

  /** The model prints `job`, and reports what the tool said. */
  function printing(job: () => string) {
    return (r: RecordedRequest): Reply => {
      const last = lastContent(r)
      if (last.includes('tool_result')) return { text: last.includes('printing') ? 'Printed.' : `Not printed: ${last.slice(0, 300)}` }
      return { toolUse: { name: 'mcp__stub__print', input: { job: job() } } }
    }
  }

  /** Starts a session whose first turn parks on an approval; resolves with its entry's request id. */
  async function parked(m: SessionManager, owner: Owner = agentA) {
    const { session, turn } = await m.start(owner, { origin: 'mcp', prompt: 'print the box' })
    const events = await m.attach(session.id, owner, { signal: stop.signal })
    const seen = await collectUntil(events, (e) => e.event.type === 'approval.required')
    const required = seen.at(-1)!.event
    if (required.type !== 'approval.required') throw new Error('unreachable')
    return { session, turn: turn!, requestId: required.id, toolUseId: required.tool }
  }

  const allEvents = async (m: SessionManager, id: string): Promise<LoggedEvent[]> => m.events.read(id, 0, 10_000)
  const types = (events: LoggedEvent[]) => events.map((e) => e.event.type)
  /** How the entry ended (ai_input_responses), or undefined while it is pending. */
  const response = async (requestId: string) =>
    (await db.sql<{ outcome: string; reason: string | null; responder: Owner | null }[]>`
      SELECT outcome, reason, responder FROM ai_input_responses WHERE request_id = ${requestId}`)[0]
  const pendingRow = async (requestId: string) =>
    (await db.sql<{ input_hash: string | null }[]>`SELECT input_hash FROM ai_pending_input WHERE request_id = ${requestId}`)[0]
  /** A respond's refusal, or undefined when it was taken. */
  const refusal = (p: Promise<unknown>) => p.then(() => undefined, (err: unknown) => err)

  function routes(m: SessionManager): Hono {
    const app = new Hono()
    registerPendingInputRoutes(app, {
      sessions: m,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    return app
  }

  it('parks the turn in waiting_approval until the browser approves over HTTP, then runs the call', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, requestId, toolUseId } = await parked(m)

    // Parked: the session says so, nothing ran, and the model is not being asked anything.
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'waiting_approval', turnActive: true })
    const calls = fake.messageCalls().length
    await new Promise((r) => setTimeout(r, 1500))
    expect(printed).toEqual([])
    expect(fake.messageCalls()).toHaveLength(calls)

    const app = routes(m)
    const list = (await (await app.request(`/api/v1/ai/sessions/${session.id}/pending-input`, { headers: UI_READ })).json()) as {
      entries: InputEntry[]
    }
    expect(list.entries).toHaveLength(1)
    expect(list.entries[0]).toMatchObject({
      id: requestId,
      kind: 'approval',
      tool: 'mcp__stub__print',
      summary: expect.stringContaining('box.3mf'),
      input_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      requested_by: expect.objectContaining({ kind: agentA.kind, id: agentA.id }),
    })
    expect(list.entries[0]!.input_hash).toBe((await pendingRow(requestId))?.input_hash)

    // A write without the UI's origin is refused before anything is decided.
    const bare = await app.request(`/api/v1/ai/pending-input/${encodeURIComponent(requestId)}`, { method: 'POST' })
    expect(bare.status).toBe(403)
    expect(await response(requestId)).toBeUndefined()

    const res = await app.request(`/api/v1/ai/pending-input/${encodeURIComponent(requestId)}`, {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'approval', decision: 'approve', input_hash: list.entries[0]!.input_hash }),
    })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ id: requestId, outcome: 'approved' })

    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    expect(await response(requestId)).toMatchObject({ outcome: 'approved', responder: expect.objectContaining({ kind: 'browser' }) })
    expect(await pendingRow(requestId)).toBeUndefined()

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
    expect(required.tool).toBe(toolUseId)
  }, 60_000)

  it('logs the call before the approval that asks about it, however far behind the turn is in logging it (#881)', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    // The turn logs what the SDK streams one append at a time, while the SDK asks
    // canUseTool as soon as Claude Code does. Hold the turn's append of the call
    // until the entry exists, or 3 s have passed if it never will before the call.
    const append = m.events.append.bind(m.events)
    m.events.append = async (sessionId, events, tx) => {
      if (tx === undefined && events.some((e) => e.type === 'tool.call')) {
        const deadline = performance.now() + 3000
        while (performance.now() < deadline && (await db.sql`SELECT 1 FROM ai_pending_input WHERE session_id = ${sessionId}`).length === 0) {
          await new Promise((r) => setTimeout(r, 50))
        }
      }
      return append(sessionId, events, tx)
    }
    const { session, turn, requestId } = await parked(m)
    expect(await respond(m, browser, requestId, { kind: 'approval', decision: 'approve' })).toMatchObject({ outcome: 'approved' })
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])

    const order = types(await allEvents(m, session.id)).filter((t) => t === 'tool.call' || t.startsWith('approval'))
    expect(order).toEqual(['tool.call', 'approval.required', 'approval.resolved'])
  }, 60_000)

  it('the panel’s decision (the pending-input route) decides it; a denial reaches the model', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, requestId } = await parked(m)

    const res = await routes(m).request(`/api/v1/ai/pending-input/${encodeURIComponent(requestId)}`, {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'approval', decision: 'deny' }),
    })
    expect(res.status).toBe(200)

    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    const last = lastContent(fake.messageCalls().at(-1)!)
    expect(last).toContain('The user denied mcp__stub__print')
    const events = await allEvents(m, session.id)
    await expectPanelAccepts(events.map((e) => e.event))
    expect(events.map((e) => e.event)).toContainEqual(
      expect.objectContaining({ type: 'approval.resolved', id: requestId, approved: false, decision: 'denied', by: expect.objectContaining({ kind: 'browser' }) }),
    )
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
  }, 60_000)

  it('only the browser user, or ANOTHER principal with a grant, may decide; never the requester itself', async () => {
    script = printing(() => 'box.3mf')
    const granted = new Set<string>()
    const m = await replica({ approvalGrants: (p) => Promise.resolve(granted.has(`${p.kind}:${p.id}`)) })
    const { turn, requestId } = await parked(m)
    const approve = (who: Owner) => respond(m, who, requestId, { kind: 'approval', decision: 'approve' })

    // The owning agent, without a grant: forbidden. Another agent without one: forbidden too.
    expect(await refusal(approve(agentA))).toMatchObject({ status: 403 })
    expect(await refusal(approve(agentB))).toMatchObject({ status: 403 })
    const anonymous: Owner = { kind: 'anonymous', id: 'anonymous', label: 'Anonymous' }
    expect(await refusal(approve(anonymous))).toBeInstanceOf(RespondError)

    // A grant never allows self-approval: the requester (session owner and creator) is refused.
    granted.add(`${agentA.kind}:${agentA.id}`)
    expect(await refusal(approve(agentA))).toMatchObject({ status: 403, message: expect.stringMatching(/never your own/) })
    expect(await response(requestId)).toBeUndefined()

    // Another agent with a grant may decide it.
    granted.add(`${agentB.kind}:${agentB.id}`)
    expect(await approve(agentB)).toMatchObject({ outcome: 'approved' })
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual(['box.3mf'])
    expect(await response(requestId)).toMatchObject({ outcome: 'approved', responder: expect.objectContaining({ kind: agentB.kind, id: agentB.id }) })
    // Decided once: a second, different decision is refused and changes nothing. (Its status, 409
    // with how it ended, is respond.pg.test.ts's, skipped there on a defect: it answers 404.)
    expect(await refusal(respond(m, browser, requestId, { kind: 'approval', decision: 'deny' }))).toBeInstanceOf(RespondError)
    expect(await response(requestId)).toMatchObject({ outcome: 'approved', responder: expect.objectContaining({ kind: agentB.kind, id: agentB.id }) })
  }, 60_000)

  it('refuses a decision bound to a different input', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { turn, requestId } = await parked(m)
    const hash = (await pendingRow(requestId))!.input_hash!
    const other = hash.replace(/^./, (c) => (c === '0' ? '1' : '0'))
    expect(await refusal(respond(m, browser, requestId, { kind: 'approval', decision: 'approve', input_hash: other }))).toMatchObject({ status: 409 })
    expect(await response(requestId)).toBeUndefined()
    expect(await respond(m, browser, requestId, { kind: 'approval', decision: 'approve', input_hash: hash })).toMatchObject({ outcome: 'approved' })
    expect(await turn.done).toMatchObject({ kind: 'result' })
    expect(printed).toEqual(['box.3mf'])
  }, 60_000)

  it('interrupt cancels the pending approval and stops the turn without running the call', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, requestId } = await parked(m)
    const callsWhenParked = fake.messageCalls().length
    // Any watcher may interrupt (spec §8.6); here the browser user.
    expect(await m.interrupt(session.id, browser)).toBe(true)
    // The turn ends there (#1168): the model is not called again to reply to the abort.
    expect(await turn.done).toEqual({ kind: 'interrupted' })
    expect(fake.messageCalls()).toHaveLength(callsWhenParked)
    expect(printed).toEqual([])
    expect(await response(requestId)).toMatchObject({ outcome: 'cancelled', reason: 'interrupted by You' })
    await expect.poll(async () => (await m.get(session.id, agentA)).status).toBe('idle')
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    expect(await refusal(respond(m, browser, requestId, { kind: 'approval', decision: 'approve' }))).toBeInstanceOf(RespondError)
    const events = await allEvents(m, session.id)
    await expectPanelAccepts(events.map((e) => e.event))
    expect(events.map((e) => e.event)).toContainEqual({
      v: 1,
      type: 'approval.resolved',
      sessionId: session.id,
      id: requestId,
      approved: false,
      decision: 'cancelled',
      reason: 'interrupted by You',
    })
    expect(events.filter((e) => e.event.type === 'approval.resolved' && e.event.id === requestId)).toHaveLength(1)
    expect(events.some((e) => e.event.type === 'assistant.text.done')).toBe(false)
    expect(printed).toEqual([])
  }, 60_000)

  it('handoff cancels the pending approval: the parked call is refused and the turn ends', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    const { session, turn, requestId } = await parked(m)
    await m.handoff(session.id, agentA, agentB).then(() => m.acceptHandoff(session.id, agentB))
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    expect(await response(requestId)).toMatchObject({ outcome: 'cancelled', reason: 'the session was handed off to another MCP token' })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('was cancelled (the session was handed off to another MCP token)')
  }, 60_000)

  it('expires a parked approval at its time, and the model is told', async () => {
    script = printing(() => 'box.3mf')
    const m = await replica()
    // The shortest window Settings takes, read by the gate when the call parks.
    await db.sql`INSERT INTO ai_settings (key, value) VALUES (${SETTING_APPROVAL_EXPIRY_SECONDS}, ${db.sql.json(10)})`
    const { session, turn, requestId } = await parked(m)
    expect(await turn.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(printed).toEqual([])
    expect(await response(requestId)).toMatchObject({ outcome: 'expired' })
    expect(lastContent(fake.messageCalls().at(-1)!)).toContain('before it expired')
    expect(await refusal(respond(m, browser, requestId, { kind: 'approval', decision: 'approve' }))).toBeInstanceOf(RespondError)
    expect(await m.get(session.id, agentA)).toMatchObject({ status: 'idle' })
  }, 90_000)
})

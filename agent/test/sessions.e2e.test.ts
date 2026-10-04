import { fixedCredentials } from './support/fixedCredentials.js'
import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createApp } from '../src/app.js'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import type { SessionManager, SessionManagerDeps } from '../src/sessions/manager.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, collectUntil, manager, tempPaths } from './support/sessions.js'

// Sessions end to end: the real Agent SDK and its bundled Claude Code binary,
// pointed at the local fake Anthropic endpoint as a gateway (as
// test/run.test.ts does), with the Postgres SessionStore. Each "replica" is its
// own state directory (so its own CLAUDE_CONFIG_DIR and local JSONL files) and
// its own connection pool; only Postgres is shared.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

/** Every text the fake was sent in its last request's messages. */
function conversation(request: RecordedRequest | undefined): string {
  return JSON.stringify(request?.body?.messages ?? [])
}

describe.skipIf(skip !== undefined)(`sessions against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let stop: AbortController
  const pools: Database[] = []

  beforeEach(async () => {
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
  })
  afterEach(async () => {
    stop.abort()
    await fake.close()
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  /** A replica: a fresh state dir and its own pool on the shared schema. */
  async function replica(extra: Partial<SessionManagerDeps> = {}): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    return manager({
      sql: pool.sql,
      paths,
      credentials: fixedCredentials({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-sessions-test-token' }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      ...extra,
    })
  }

  const allEvents = async (m: SessionManager, id: string): Promise<LoggedEvent[]> => m.events.read(id, 0, 10_000)

  it('resumes a session on another replica through the Postgres store, with its history', async () => {
    let n = 0
    script = () => ({ text: `answer ${++n}` })
    const a = await replica()
    const b = await replica()

    const { session, turn } = await a.start(agentA, { origin: 'mcp', prompt: 'remember the word teapot' })
    const first = await turn!.done
    expect(first).toMatchObject({ kind: 'result', subtype: 'success', turns: 1 })
    // The SDK session id is ours.
    expect(await a.store.exists(session.id)).toBe(true)

    // Turn 2 on replica B: nothing of the session exists on its disk.
    const second = await (await b.send(session.id, agentA, 'what was the word?')).done
    expect(second).toMatchObject({ kind: 'result', subtype: 'success', turns: 2 })
    const sent = conversation(fake.messageCalls().at(-1))
    expect(sent).toContain('remember the word teapot')
    expect(sent).toContain('answer 1')
    expect(sent).toContain('what was the word?')

    // total_cost_usd of the resumed query already includes turn 1: the fake
    // prices both turns alike, so the session total doubles (manager.ts).
    if (first.kind !== 'result' || second.kind !== 'result') throw new Error('unreachable')
    expect(first.costUsd).toBeGreaterThan(0)
    expect(second.costUsd).toBeCloseTo(first.costUsd * 2, 10)
    expect(await b.get(session.id, agentA)).toMatchObject({ status: 'idle', turns: 2, turnActive: false })

    const events = (await allEvents(a, session.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.map((e) => e.type)).toEqual([
      'session.started',
      'session.status',
      'user.turn',
      'session.status',
      'assistant.text.delta',
      'assistant.text.done',
      'session.result',
      'session.status',
      'user.turn',
      'session.status',
      'assistant.text.delta',
      'assistant.text.done',
      'session.result',
      'session.status',
    ])
  })

  it('falls back to the next credential when the first is refused, resuming through the Postgres store (#1093)', async () => {
    const TOKEN_A = 'gw-sessions-revoked-aaaa'
    const TOKEN_B = 'gw-sessions-working-bbbb'
    let revoked = true
    script = (r) =>
      r.headers.authorization === `Bearer ${TOKEN_A}` && revoked
        ? { error: { status: 401, type: 'authentication_error', message: 'invalid token' } }
        : { text: conversation(r).includes('second') ? 'second answer' : 'first answer' }
    const outcomes: string[] = []
    const pooled = (id: string, secret: string) => ({
      id,
      epoch: 0,
      label: id,
      credential: { kind: 'gateway' as const, baseUrl: fake.url, secret },
    })
    const m = await replica({
      credentials: {
        candidates: () => Promise.resolve([pooled('a', TOKEN_A), pooled('b', TOKEN_B)]),
        reporter: () => (attempt, outcome) => {
          outcomes.push(`${attempt.id}:${outcome.class}`)
          return Promise.resolve()
        },
      },
    })

    // The session's first turn: credential A fails before Claude Code has
    // answered anything, and B picks the new session up from the store.
    const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'make a box' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })
    expect(outcomes).toEqual(['a:permanent', 'b:ok'])
    const firstSent = conversation(fake.messageCalls().at(-1))
    expect(firstSent.split('make a box').length - 1).toBe(1)

    // A later turn on A, now working again, sees the whole conversation.
    revoked = false
    expect(await (await m.send(session.id, agentA, 'second question')).done).toMatchObject({ kind: 'result', subtype: 'success' })
    const sent = conversation(fake.messageCalls().at(-1))
    expect(fake.messageCalls().at(-1)?.headers.authorization).toBe(`Bearer ${TOKEN_A}`)
    expect(sent).toContain('make a box')
    expect(sent).toContain('first answer')
    expect(sent).toContain('second question')

    const events = (await allEvents(m, session.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.filter((e) => e.type === 'error')).toEqual([])
    expect(JSON.stringify(events)).not.toMatch(/API Error|Not logged in|gw-sessions/)
  }, 60_000)

  // #1101: Claude Code reports a request the API refused as a `success`
  // result with `is_error: true`, and its text as a synthetic assistant
  // message. Neither a 429 with a long retry-after nor a 400 is retried, so
  // the single credential's attempt ends with exactly those. A 401 is
  // retried, and fallback.ts stops the attempt at the first retry and throws.
  it.each([
    {
      status: 429,
      type: 'rate_limit_error',
      message: 'This request would exceed your rate limit',
      headers: { 'retry-after': '120' },
      code: 'api_error',
      says: /rate limited \(HTTP 429\); try again later: .*exceed your rate limit/,
    },
    {
      status: 400,
      type: 'invalid_request_error',
      message: 'messages: text content blocks must be non-empty (sent by gw-sessions-test-token)',
      code: 'api_error',
      says: /refused the request \(HTTP 400\): .*text content blocks must be non-empty/,
    },
    {
      status: 400,
      type: 'invalid_request_error',
      message: 'Your credit balance is too low to access the Anthropic API',
      code: 'api_error',
      says: /the Claude credential was rejected \(HTTP 400\); check it under Settings → AI: .*credit balance is too low/i,
    },
    // A guard for the #1093 path: fallback.ts stops a 401 at its first retry and throws.
    {
      status: 401,
      type: 'authentication_error',
      message: 'invalid x-api-key',
      code: 'turn_failed',
      says: /the Claude credential \(.*\) was refused: HTTP 401/,
    },
  ])('a $status ($type) from the API ends the turn as an error ($code), not as a reply, and counts no turn (#1101)', async (c) => {
    script = (r) =>
      conversation(r).includes('second')
        ? { text: 'second answer' }
        : { error: { status: c.status, type: c.type, message: c.message, headers: c.headers ?? {} } }
    const m = await replica({
      probe: () =>
        Promise.resolve(
          c.status === 401
            ? { verdict: 'refused', reason: 'the probe was refused (HTTP 401)' }
            : { verdict: 'unknown', until: new Date(Date.now() + 60_000) },
        ),
    })

    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    const done = await turn!.done
    expect(done).toMatchObject({ kind: 'failed', message: expect.stringMatching(c.says) })
    // An API message that echoes the credential is redacted from the outcome too.
    expect(JSON.stringify(done)).not.toContain('gw-sessions-test-token')
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'failed', turns: 0, turnActive: false })

    const events = (await allEvents(m, session.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.filter((e) => e.type.startsWith('assistant.'))).toEqual([])
    const errors = events.filter((e) => e.type === 'error')
    expect(errors).toEqual([expect.objectContaining({ code: c.code, message: expect.stringMatching(c.says) })])
    expect(JSON.stringify(events)).not.toContain('gw-sessions-test-token')
    expect(events.at(-1)).toMatchObject({ type: 'session.status', status: 'failed' })

    // The session is not spent: the next message is answered as usual.
    expect(await (await m.send(session.id, browser, 'second question')).done).toMatchObject({
      kind: 'result',
      subtype: 'success',
      turns: 1,
    })
  }, 60_000)

  it('a refusal on every credential counts no turn for any of them, and names the last one’s failure (#1101)', async () => {
    const TOKEN_A = 'gw-sessions-limited-aaaa'
    const TOKEN_B = 'gw-sessions-refused-bbbb'
    script = (r) =>
      r.headers.authorization === `Bearer ${TOKEN_A}`
        ? { error: { status: 429, type: 'rate_limit_error', message: 'slow down', headers: { 'retry-after': '120' } } }
        : { error: { status: 400, type: 'invalid_request_error', message: 'messages: text content blocks must be non-empty' } }
    const pooled = (id: string, secret: string) => ({
      id,
      epoch: 0,
      label: id,
      credential: { kind: 'gateway' as const, baseUrl: fake.url, secret },
    })
    const m = await replica({
      credentials: {
        candidates: () => Promise.resolve([pooled('a', TOKEN_A), pooled('b', TOKEN_B)]),
        reporter: () => () => Promise.resolve(),
      },
      probe: () => Promise.resolve({ verdict: 'unknown', until: new Date(Date.now() + 60_000) }),
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    expect(await turn!.done).toMatchObject({ kind: 'failed', message: expect.stringMatching(/refused the request \(HTTP 400\)/) })
    expect(fake.messageCalls().map((c) => c.headers.authorization)).toEqual([`Bearer ${TOKEN_A}`, `Bearer ${TOKEN_B}`])
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'failed', turns: 0 })
    const events = (await allEvents(m, session.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events.filter((e) => e.type.startsWith('assistant.'))).toEqual([])
    expect(events.filter((e) => e.type === 'error')).toEqual([expect.objectContaining({ code: 'api_error' })])
  }, 60_000)

  it('a refusal the probe finds is not the credential’s says so, and does not send the user to Settings (#1101)', async () => {
    script = () => ({ error: { status: 403, type: 'permission_error', message: 'blocked by policy' } })
    const m = await replica({ probe: () => Promise.resolve({ verdict: 'answered', until: new Date(Date.now() + 1000) }) })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    const done = await turn!.done
    expect(done).toMatchObject({ kind: 'failed', message: expect.stringMatching(/refused this request \(HTTP 403\); the credential itself works/) })
    expect(JSON.stringify(done)).not.toContain('Settings')
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'failed', turns: 0 })
    const events = (await allEvents(m, session.id)).map((e) => e.event)
    expect(events.filter((e) => e.type.startsWith('assistant.'))).toEqual([])
  }, 60_000)

  it('a refused request on one credential is not a turn when the next one answers (#1101)', async () => {
    const TOKEN_A = 'gw-sessions-limited-aaaa'
    const TOKEN_B = 'gw-sessions-working-bbbb'
    script = (r) =>
      r.headers.authorization === `Bearer ${TOKEN_A}`
        ? { error: { status: 429, type: 'rate_limit_error', message: 'slow down', headers: { 'retry-after': '120' } } }
        : { text: 'a box' }
    const pooled = (id: string, secret: string) => ({
      id,
      epoch: 0,
      label: id,
      credential: { kind: 'gateway' as const, baseUrl: fake.url, secret },
    })
    const m = await replica({
      credentials: {
        candidates: () => Promise.resolve([pooled('a', TOKEN_A), pooled('b', TOKEN_B)]),
        reporter: () => () => Promise.resolve(),
      },
      probe: () => Promise.resolve({ verdict: 'unknown', until: new Date(Date.now() + 60_000) }),
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box' })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success', turns: 1 })
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'idle', turns: 1 })
  }, 60_000)

  it('a refusal after a tool call keeps what the turn did and counts the round trip that finished (#1101)', async () => {
    script = (r) =>
      conversation(r).includes('tool_result')
        ? { error: { status: 400, type: 'invalid_request_error', message: 'messages: text content blocks must be non-empty' } }
        : { toolUse: { name: 'mcp__stub__lookup', input: { q: 'box' } } }
    const lookup = tool('lookup', 'Look something up', { q: z.string() }, (args) =>
      Promise.resolve({ content: [{ type: 'text' as const, text: `found ${args.q}` }] }),
    )
    const m = await replica({
      mcpServers: () => ({ stub: createSdkMcpServer({ name: 'stub', tools: [lookup] }) }),
      tierOf: (name) => (name === 'mcp__stub__lookup' ? 'read' : undefined),
    })
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'find a box' })
    expect(await turn!.done).toMatchObject({ kind: 'failed', message: expect.stringMatching(/refused the request \(HTTP 400\)/) })
    // Two model requests: the tool call, which counts, and the refused one, which does not.
    expect(fake.messageCalls()).toHaveLength(2)
    expect(await m.get(session.id, browser)).toMatchObject({ status: 'failed', turns: 1 })
    const events = (await allEvents(m, session.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    // The panel's count follows the row's.
    expect(events.filter((e) => e.type === 'session.result')).toEqual([expect.objectContaining({ turns: 1 })])
    expect(events.map((e) => e.type).filter((t) => t.startsWith('tool.') || t.startsWith('assistant.') || t === 'error')).toEqual([
      'tool.call',
      'tool.result',
      'error',
    ])
  }, 60_000)

  it('continues a spent session in a new chat from the panel: POST …/fork, a fresh budget, the transcript', async () => {
    script = (r) => ({ text: conversation(r).includes('go on') ? 'carrying on' : 'a box, 20 mm' })
    const m = await replica({ newSessions: { max: 3, windowMs: 60_000 } })
    const { session: parent, turn } = await m.start(browser, { origin: 'chat', prompt: 'make a box', title: 'box' })
    await turn!.done
    // Spent, as the panel finds it (manager.ts whyNotClaimed).
    await db.sql`UPDATE ai_sessions SET cost_usd = budget_usd + 0.016 WHERE id = ${parent.id}`
    await expect(m.send(parent.id, browser, 'more')).rejects.toMatchObject({ code: 'budget_exhausted' })

    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      sessions: m,
    })
    const ui = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
    const res = await app.request(`/api/v1/ai/sessions/${parent.id}/fork`, { method: 'POST', headers: ui })
    expect(res.status).toBe(201)
    const { session: child } = (await res.json()) as {
      session: { id: string; parent_id: string; owner: { kind: string }; budget_usd: number; cost_usd: number; title: string }
    }
    expect(child).toMatchObject({ parent_id: parent.id, owner: { kind: 'browser' }, budget_usd: 1, cost_usd: 0, title: 'box (fork)' })
    const events = (await allEvents(m, child.id)).map((e) => e.event)
    await expectPanelAccepts(events)
    expect(events[0]).toMatchObject({ type: 'session.started', sessionId: child.id, budgetUsd: 1 })
    expect(events.find((e) => e.type === 'user.turn')).toMatchObject({ text: 'make a box' })

    await (await m.send(child.id, browser, 'go on')).done
    expect(conversation(fake.messageCalls().at(-1))).toContain('make a box')

    // The fork is a new session: it counts against the owner's limit (3 here, with the parent and child).
    const named = await app.request(`/api/v1/ai/sessions/${parent.id}/fork`, {
      method: 'POST',
      headers: { ...ui, 'content-type': 'application/json' },
      body: JSON.stringify({ title: 'box, part 2' }),
    })
    expect(((await named.json()) as { session: { title: string } }).session.title).toBe('box, part 2')
    const limited = await app.request(`/api/v1/ai/sessions/${parent.id}/fork`, { method: 'POST', headers: ui })
    expect(limited.status).toBe(429)
  })

  it('forks a session: the child records its parent, has the history, and diverges', async () => {
    script = (r) => ({ text: conversation(r).includes('left') ? 'went left' : conversation(r).includes('right') ? 'went right' : 'at the fork' })
    const a = await replica()
    const b = await replica()
    const { session: parent, turn } = await a.start(agentA, { origin: 'mcp', prompt: 'walk to the fork', title: 'walk' })
    await turn!.done

    const child = await b.fork(parent.id, browser)
    expect(child).toMatchObject({ parentId: parent.id, owner: browser, origin: 'mcp', title: 'walk (fork)', status: 'idle' })
    expect(child.id).not.toBe(parent.id)

    await (await b.send(child.id, browser, 'go left')).done
    await (await a.send(parent.id, agentA, 'go right')).done
    const calls = fake.messageCalls()
    const childCall = conversation(calls.at(-2))
    const parentCall = conversation(calls.at(-1))
    expect(childCall).toContain('walk to the fork')
    expect(childCall).toContain('go left')
    expect(parentCall).toContain('walk to the fork')
    expect(parentCall).not.toContain('go left')

    // Attach on the child replays its history first, re-addressed to it.
    const childEvents = (await allEvents(b, child.id)).map((e) => e.event)
    await expectPanelAccepts(childEvents)
    expect(childEvents.every((e) => !('sessionId' in e) || e.sessionId === child.id)).toBe(true)
    expect(childEvents.slice(0, 5).map((e) => e.type)).toEqual([
      'session.started',
      'user.turn',
      'assistant.text.delta',
      'assistant.text.done',
      'session.status',
    ])
    // Three Claude Code runs; past vitest's 5 s default on a loaded machine.
  }, 60_000)

  it('keeps a turn stopped by a restart in the transcript, so the next turn on another replica has it', async () => {
    let n = 0
    script = (r) => (conversation(r).includes('kettle') && ++n === 1 ? { hang: true } : { text: `answer ${n}` })
    const a = await replica()
    const b = await replica()
    const { session, turn } = await a.start(agentA, { origin: 'mcp', prompt: 'remember the word teapot' })
    await turn!.done
    // Turn 2 is still with the "model" when the restart comes (2026-09-30).
    const cut = await a.send(session.id, agentA, 'and the second word is kettle')
    for (let i = 0; i < 200 && !conversation(fake.messageCalls().at(-1)).includes('kettle'); i++) {
      await new Promise((r) => setTimeout(r, 50))
    }
    await a.stopTurns({ graceMs: 0, abortWaitMs: 10_000 })
    expect(await cut.done).toEqual({ kind: 'interrupted' })
    expect(await b.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })

    // Replica B resumes from Postgres: the cut-off turn's prompt is there,
    // because stopTurns waited for the aborted query to end (and flush).
    await (await b.send(session.id, agentA, 'what were the words?')).done
    const sent = conversation(fake.messageCalls().at(-1))
    expect(sent).toContain('remember the word teapot')
    expect(sent).toContain('and the second word is kettle')
    expect(sent).toContain('what were the words?')
  }, 60_000)

  it('interrupts a running turn through the abort signal', async () => {
    script = () => ({ hang: true })
    const a = await replica()
    const { session } = await a.start(agentA, { origin: 'mcp' })
    const turn = await a.send(session.id, agentA, 'think forever')
    // Wait until the request reached the "model".
    for (let i = 0; i < 200 && fake.messageCalls().length === 0; i++) await new Promise((r) => setTimeout(r, 50))
    expect(fake.messageCalls().length).toBeGreaterThan(0)
    const started = Date.now()
    expect(await a.interrupt(session.id, browser)).toBe(true)
    expect(await turn.done).toEqual({ kind: 'interrupted' })
    expect(Date.now() - started).toBeLessThan(10_000)
    expect(await a.get(session.id, agentA)).toMatchObject({ status: 'idle', turnActive: false })
    const last = (await allEvents(a, session.id)).map((e) => e.event).slice(-2)
    expect(last).toEqual([
      { v: 1, type: 'error', sessionId: session.id, code: 'interrupted', message: 'the turn was interrupted' },
      { v: 1, type: 'session.status', sessionId: session.id, status: 'idle' },
    ])
  })

  it('streams tool calls and results, and a late attach replays the same sequence a live watcher saw', async () => {
    script = (r) =>
      conversation(r).includes('tool_result')
        ? { text: 'Found it.' }
        : { toolUse: { name: 'mcp__stub__lookup', input: { q: 'box' } } }
    const lookup = tool('lookup', 'Look something up', { q: z.string() }, (args) =>
      Promise.resolve({ content: [{ type: 'text' as const, text: `found ${args.q}` }] }),
    )
    const withTools = await replica({
      mcpServers: () => ({ stub: createSdkMcpServer({ name: 'stub', tools: [lookup] }) }),
      tierOf: (name) => (name === 'mcp__stub__lookup' ? 'read' : undefined),
    })
    const b = await replica()
    const { session } = await withTools.start(browser, { origin: 'chat' })

    const live = collectUntil(await b.attach(session.id, browser, { signal: stop.signal }), (e) =>
      e.event.type === 'session.status' && e.event.status === 'idle' && e.seq > 2,
    30_000)
    const outcome = await (await withTools.send(session.id, browser, 'find a box')).done
    expect(outcome).toMatchObject({ kind: 'result', subtype: 'success' })
    const seen = await live

    const replay = await collectUntil(await b.attach(session.id, browser, { signal: stop.signal }), (e) => e.seq === seen.at(-1)!.seq)
    expect(replay).toEqual(seen)
    const events = seen.map((e) => e.event)
    await expectPanelAccepts(events)
    const call = events.find((e) => e.type === 'tool.call')
    const result = events.find((e) => e.type === 'tool.result')
    expect(call).toMatchObject({ name: 'mcp__stub__lookup', input: { q: 'box' }, risk: 'read' })
    expect(result).toMatchObject({ id: call && call.type === 'tool.call' ? call.id : '', ok: true, summary: 'found box' })
    expect(events.map((e) => e.type)).toEqual([
      'session.started',
      'session.status',
      'user.turn',
      'session.status',
      'tool.call',
      'tool.result',
      'assistant.text.delta',
      'assistant.text.done',
      'session.result',
      'session.status',
    ])
  }, 60_000)
})

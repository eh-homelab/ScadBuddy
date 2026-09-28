import { createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import type { LoggedEvent } from '../src/sessions/eventLog.js'
import type { SessionManager, SessionManagerDeps } from '../src/sessions/manager.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
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
      credential: () => Promise.resolve({ kind: 'gateway', baseUrl: fake.url, secret: 'gw-sessions-test-token' }),
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
  })

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

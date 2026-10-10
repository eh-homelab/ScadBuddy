import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { type Principal, tiersUpTo } from '../src/auth/principal.js'
import { TabHub, type TabConnection } from '../src/bridge/hub.js'
import { PostgresPairingStore } from '../src/bridge/pairings.js'
import type { AgentFrame, CallOutcome } from '../src/bridge/protocol.js'
import { PgTabRelay } from '../src/bridge/relay.js'
import { PostgresSessionTabStore } from '../src/bridge/sessionTabs.js'
import type { Database } from '../src/db.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #1916: two agent replicas on one database (bridge/relay.ts). Each has its own
// listener connection, relay and hub, as main.ts wires them; a call made on
// replica A for a tab whose socket is on replica B reaches it through Postgres.

const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const browser: Principal = { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') }
const anonymous: Principal = { id: 'anonymous:session-1', kind: 'anonymous', tiers: ['read', 'write'] }

type Replica = { hub: TabHub; relay: PgTabRelay; listener: PgEventListener }

/** A tab on `hub`; `answer` decides each call's outcome (undefined: never answers). */
async function tab(hub: TabHub, answer: (frame: Extract<AgentFrame, { type: 'call' }>) => CallOutcome | undefined) {
  const calls: Extract<AgentFrame, { type: 'call' }>[] = []
  const conn: TabConnection = hub.open((frame) => {
    if (frame.type !== 'call') return
    calls.push(frame)
    const outcome = answer(frame)
    if (outcome) queueMicrotask(() => void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome })))
  })
  await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId: TAB, route: '/m/box', live: ['snapshot', 'navigate'] }))
  return { conn, calls }
}

describe.skipIf(!TEST_DATABASE_URL)(`browser calls across replicas${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let url: string
  let drop: () => Promise<void>
  let a: Replica
  let b: Replica

  function replica(): Replica {
    const listener = new PgEventListener(url, { searchPath: 'public', log: () => {} })
    const relay = new PgTabRelay(db.sql, { listen: (c, f) => listener.listenAlso(c, f), ackTimeoutMs: 500, log: () => {} })
    listener.start()
    const hub = new TabHub({
      pairings: new PostgresPairingStore(db.sql),
      relay,
      sessionTabs: new PostgresSessionTabStore(db.sql),
      callTimeoutMs: 2_000,
      log: () => {},
    })
    return { hub, relay, listener }
  }

  beforeAll(async () => {
    ;({ db, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  beforeEach(async () => {
    await db.sql`TRUNCATE ai_browser_pairings, ai_bridge_messages, ai_session_tabs`
    a = replica()
    b = replica()
    await Promise.all([a.listener.ready(), b.listener.ready()])
  })
  afterEach(async () => {
    for (const r of [a, b]) {
      r.hub.close()
      await r.listener.close()
    }
  })
  afterAll(async () => {
    await drop()
  })

  const signal = () => new AbortController().signal

  it("runs a session's call on the replica holding its tab, and carries a result too large for a NOTIFY", async () => {
    const big = 'x'.repeat(100_000)
    const t = await tab(b.hub, (frame) => ({ ok: true, result: { tool: frame.tool, args: frame.args, big } }))
    a.hub.pairSession('s1', TAB)
    const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', { depth: 2 }, { signal: signal() })
    expect(outcome).toEqual({ ok: true, result: { tool: 'snapshot', args: { depth: 2 }, big } })
    expect(t.calls).toHaveLength(1)
    // The request and the answer were each taken by the replica they were for.
    expect(await db.sql`SELECT id FROM ai_bridge_messages`).toHaveLength(0)
  })

  it('reaches the tab an MCP client paired by code, and reports its state', async () => {
    await tab(b.hub, () => ({ ok: true, result: 'ok' }))
    const store = new PostgresPairingStore(db.sql)
    const request = await store.request(anonymous)
    expect((await store.accept(request.id, request.code, TAB)).ok).toBe(true)
    expect(await a.hub.call({ principal: anonymous }, 'navigate', { to: '/' }, { signal: signal() })).toEqual({ ok: true, result: 'ok' })
    expect(await a.hub.status({ principal: anonymous })).toMatchObject({ attached: true, via: 'pairing', route: '/m/box', live: ['snapshot', 'navigate'] })
  })

  it('answers not connected, and leaves no row, when no replica holds the tab', async () => {
    a.hub.pairSession('s1', TAB)
    const started = performance.now()
    const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
    expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser', message: expect.stringMatching(/tab is not connected/) } })
    expect(performance.now() - started).toBeLessThan(2_000)
    expect(await a.hub.status({ principal: browser, sessionId: 's1' })).toMatchObject({ attached: false })
    expect(await db.sql`SELECT id FROM ai_bridge_messages`).toHaveLength(0)
  })

  it("times a silent tab out on its own replica, with the call's own timeout", async () => {
    await tab(b.hub, () => undefined)
    a.hub.pairSession('s1', TAB)
    const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal(), timeoutMs: 300 })
    expect(outcome).toMatchObject({ ok: false, error: { code: 'no_answer', message: expect.stringMatching(/did not answer snapshot within/) } })
  })

  it('stops waiting when the call is cancelled', async () => {
    await tab(b.hub, () => undefined)
    a.hub.pairSession('s1', TAB)
    const stop = new AbortController()
    const call = a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: stop.signal })
    setTimeout(() => stop.abort(), 200)
    await expect(call).rejects.toMatchObject({ name: 'AbortError' })
  })

  // #2086: a durable session's tool call is an activity on `agent-tools`, which
  // any replica's worker may take, so it can run where the session's chat
  // socket never paired it. The pairing is read from Postgres there.
  async function chatSession(): Promise<string> {
    const [row] = await db.sql<{ id: string }[]>`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (gen_random_uuid(), 'chat', 'browser', 'browser', 'you', 'browser', 'browser', 'idle', 10, 1)
      RETURNING id`
    return row!.id
  }
  const until = async (check: () => Promise<boolean>) => {
    const deadline = performance.now() + 3_000
    while (!(await check())) {
      if (performance.now() > deadline) throw new Error('timed out')
      await new Promise((r) => setTimeout(r, 10))
    }
  }

  it("runs a durable session's call on a replica its messages never reached, on the tab it paired", async () => {
    const t = await tab(b.hub, () => ({ ok: true, result: 'from b' }))
    const session = await chatSession()
    // The chat socket (and the tab) are on B; the activity runs on A.
    b.hub.pairSession(session, TAB)
    await until(async () => (await db.sql`SELECT 1 FROM ai_session_tabs WHERE session_id = ${session}`).length === 1)
    expect(await a.hub.call({ principal: browser, sessionId: session }, 'snapshot', {}, { signal: signal() })).toEqual({
      ok: true,
      result: 'from b',
    })
    expect(t.calls).toHaveLength(1)
    expect(await a.hub.status({ principal: browser, sessionId: session })).toMatchObject({ attached: true, via: 'session' })
  })

  it('follows the session to the tab the user sends from next, whatever the replica', async () => {
    await tab(b.hub, () => ({ ok: true, result: 'old tab' }))
    const session = await chatSession()
    b.hub.pairSession(session, TAB)
    b.hub.pairSession(session, 'tab-bbbbbbbbbbbbbbbbbbbbbb')
    await until(async () => {
      const [row] = await db.sql<{ tab_id: string }[]>`SELECT tab_id FROM ai_session_tabs WHERE session_id = ${session}`
      return row?.tab_id === 'tab-bbbbbbbbbbbbbbbbbbbbbb'
    })
    // That tab is connected nowhere: not the old one's answer.
    const outcome = await a.hub.call({ principal: browser, sessionId: session }, 'snapshot', {}, { signal: signal() })
    expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser' } })
  })

  it('keeps a tab connected here local: nothing goes through the database', async () => {
    const t = await tab(a.hub, () => ({ ok: true, result: 1 }))
    a.hub.pairSession('s1', TAB)
    const forward = vi.spyOn(a.relay, 'forward')
    expect(await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })).toEqual({ ok: true, result: 1 })
    expect(t.calls).toHaveLength(1)
    expect(forward).not.toHaveBeenCalled()
  })
})

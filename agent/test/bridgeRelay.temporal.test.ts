import { Client } from '@temporalio/client'
import { historyToJSON } from '@temporalio/common/lib/proto-utils.js'
import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { mkdtemp, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { type Principal, tiersUpTo } from '../src/auth/principal.js'
import { TabHub, type TabConnection } from '../src/bridge/hub.js'
import { PostgresPairingStore } from '../src/bridge/pairings.js'
import type { AgentFrame, CallOutcome } from '../src/bridge/protocol.js'
import { ACK_TIMEOUT_MS, TemporalTabRelay } from '../src/bridge/relay.js'
import { PostgresSessionTabStore } from '../src/bridge/sessionTabs.js'
import { PostgresTabOwnerStore } from '../src/bridge/tabOwners.js'
import type { Database } from '../src/db.js'
import { dropPayloadKey, FilePayloadStore, type PgPayloadKeys, subjectDataConverter } from '../src/temporal/payloadCodec.js'
import { testPayloadKeys } from './support/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// #4780: browser_* calls between agent replicas go through Temporal to the replica that
// holds the tab (bridge/relay.ts). Each replica here has its own hub, relay and host
// worker on a queue of its own (main.ts HOST_QUEUE), as main.ts wires them; one shared
// worker runs the BridgeCall workflow, as every replica's `agent-tools` worker does.

const WORKFLOWS = fileURLToPath(new URL('../src/temporal/bridgeCall.ts', import.meta.url))
const TAB = 'tab-aaaaaaaaaaaaaaaaaaaaaa'
const OTHER_TAB = 'tab-bbbbbbbbbbbbbbbbbbbbbb'
/** frontend src/agent/link.ts MAX_RESULT_BYTES: the largest result a tab sends. */
const MAX_RESULT_BYTES = 200_000
const browser: Principal = { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') }
const anonymous: Principal = { id: 'anonymous:session-1', kind: 'anonymous', tiers: ['read', 'write'] }

type Replica = { hub: TabHub; relay: TemporalTabRelay; queue: string; worker: Worker; running: Promise<void> }

/** A tab on `hub`; `answer` decides each call's outcome (undefined: never answers). */
async function tab(
  hub: TabHub,
  answer: (frame: Extract<AgentFrame, { type: 'call' }>) => CallOutcome | undefined,
  tabId = TAB,
) {
  const calls: Extract<AgentFrame, { type: 'call' }>[] = []
  const conn: TabConnection = hub.open((frame) => {
    if (frame.type !== 'call') return
    calls.push(frame)
    const outcome = answer(frame)
    if (outcome) queueMicrotask(() => void conn.receive(JSON.stringify({ v: 1, type: 'result', id: frame.id, outcome })))
  })
  await conn.receive(JSON.stringify({ v: 1, type: 'hello', tabId, route: '/m/box', live: ['snapshot', 'navigate'] }))
  return { conn, calls }
}

describe.skipIf(!TEST_DATABASE_URL || !TEMPORAL_CLI)(
  `browser calls across replicas${TEST_DATABASE_URL ? TEMPORAL_SKIP : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let env: TestWorkflowEnvironment
    let db: Database
    let drop: () => Promise<void>
    let workflowQueue: string
    let workflowWorker: Worker
    let workflowRunning: Promise<void>
    let replicas: Replica[] = []
    // Every client and worker seals bridge payloads, as main.ts's do with the KEK.
    let payloadDir: string
    let keys: PgPayloadKeys
    let dataConverter: ReturnType<typeof subjectDataConverter>
    let client: Client
    /** The BridgeCall workflows started, by ID. */
    let started: string[] = []

    async function replica(name: string, options: { ackTimeoutMs?: number; sealed?: boolean } = {}): Promise<Replica> {
      const queue = `agent-tools.${name}-${randomUUID().slice(0, 8)}`
      const relay = new TemporalTabRelay({
        owners: new PostgresTabOwnerStore(db.sql),
        hostQueue: queue,
        workflowQueue,
        ...(options.sealed === false
          ? {}
          : {
              client: {
                client: recording(client),
                dropKey: (subject: string) => dropPayloadKey(db.sql, keys, subject),
              },
            }),
        ...(options.ackTimeoutMs !== undefined ? { ackTimeoutMs: options.ackTimeoutMs } : {}),
        log: () => {},
      })
      const hub = new TabHub({
        pairings: new PostgresPairingStore(db.sql),
        relay,
        sessionTabs: new PostgresSessionTabStore(db.sql),
        callTimeoutMs: 2_000,
        log: () => {},
      })
      const worker = await Worker.create({
        connection: env.nativeConnection,
        taskQueue: queue,
        activities: relay.activities(),
        dataConverter,
      })
      const r: Replica = { hub, relay, queue, worker, running: worker.run() }
      replicas.push(r)
      return r
    }

    /** `client`, recording each BridgeCall it starts in `started`. */
    function recording(base: Client): Client {
      const start = base.workflow.start.bind(base.workflow)
      const workflow = Object.create(base.workflow) as Client['workflow']
      workflow.start = (async (...args: Parameters<typeof start>) => {
        const handle = await start(...args)
        started.push(handle.workflowId)
        return handle
      }) as typeof start
      return Object.assign(Object.create(base) as Client, { workflow })
    }

    /** The replica's pod is gone: its worker stops polling, and nothing told the database. */
    async function kill(r: Replica): Promise<void> {
      r.worker.shutdown()
      await r.running
    }

    /** The owner rows are written in the background, as a tab attaches: wait for `queue`'s. */
    async function owned(tabId: string, queue: string): Promise<void> {
      await vi.waitFor(async () => {
        const [row] = await db.sql<{ queue: string }[]>`SELECT queue FROM ai_tab_owners WHERE tab_id = ${tabId}`
        expect(row?.queue).toBe(queue)
      })
    }

    beforeAll(async () => {
      ;[env, { db, drop }] = await Promise.all([localTemporal(), throwawayDatabase()])
      expect(await db.ready()).toBe(true)
      payloadDir = await mkdtemp(join(tmpdir(), 'bridge-payloads-'))
      keys = testPayloadKeys(db.sql)
      dataConverter = subjectDataConverter(keys, new FilePayloadStore(payloadDir))
      client = new Client({ connection: env.connection, namespace: env.namespace ?? 'default', dataConverter })
      workflowQueue = `agent-tools-bridge-${randomUUID()}`
      workflowWorker = await Worker.create({
        connection: env.nativeConnection,
        taskQueue: workflowQueue,
        workflowsPath: WORKFLOWS,
        dataConverter,
      })
      workflowRunning = workflowWorker.run()
    }, 120_000)
    beforeEach(async () => {
      await db.sql`TRUNCATE ai_browser_pairings, ai_session_tabs, ai_tab_owners`
      started = []
    })
    afterEach(async () => {
      for (const r of replicas) {
        r.hub.close()
        if (r.worker.getState() === 'RUNNING') r.worker.shutdown()
        await r.running
      }
      replicas = []
    })
    afterAll(async () => {
      workflowWorker?.shutdown()
      await workflowRunning
      await env?.teardown()
      await drop?.()
      if (payloadDir) await rm(payloadDir, { recursive: true, force: true })
    })

    const signal = () => new AbortController().signal

    it('runs a call from replica A on the replica holding the tab, once, and on no other', async () => {
      const [a, b, c] = [await replica('a'), await replica('b'), await replica('c')]
      // The same tab id reached C first, then B (a reconnect): B holds it now.
      const onC = await tab(c.hub, () => ({ ok: true, result: 'from c' }))
      await owned(TAB, c.queue)
      const onB = await tab(b.hub, (frame) => ({ ok: true, result: { tool: frame.tool, args: frame.args } }))
      await owned(TAB, b.queue)
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', { depth: 2 }, { signal: signal() })
      expect(outcome).toEqual({ ok: true, result: { tool: 'snapshot', args: { depth: 2 } } })
      expect(onB.calls).toHaveLength(1)
      expect(onC.calls).toHaveLength(0)
    })

    it('answers not connected within the ack timeout when the replica holding the tab is gone', async () => {
      const a = await replica('a')
      const b = await replica('b')
      await tab(b.hub, () => ({ ok: true, result: 'never' }))
      await owned(TAB, b.queue)
      await kill(b)
      a.hub.pairSession('s1', TAB)
      const started = performance.now()
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser', message: expect.stringMatching(/tab is not connected/) } })
      expect(performance.now() - started).toBeLessThan(ACK_TIMEOUT_MS + 1_000)
      // The dead replica's row is dropped, so the next call does not wait for it again.
      await vi.waitFor(async () => expect(await db.sql`SELECT 1 FROM ai_tab_owners WHERE tab_id = ${TAB}`).toHaveLength(0))
    })

    it('reaches the new owner when the tab reconnects to another replica mid-session', async () => {
      const [a, b, c] = [await replica('a'), await replica('b'), await replica('c')]
      const onB = await tab(b.hub, () => ({ ok: true, result: 'from b' }))
      await owned(TAB, b.queue)
      a.hub.pairSession('s1', TAB)
      expect(await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })).toEqual({
        ok: true,
        result: 'from b',
      })
      // The tab reconnects to C, and B notices its old socket closed only afterwards.
      const onC = await tab(c.hub, () => ({ ok: true, result: 'from c' }))
      await owned(TAB, c.queue)
      onB.conn.close()
      await new Promise((r) => setTimeout(r, 200))
      await owned(TAB, c.queue)
      expect(await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })).toEqual({
        ok: true,
        result: 'from c',
      })
      expect(onB.calls).toHaveLength(1)
      expect(onC.calls).toHaveLength(1)
    })

    it('brings a result of MAX_RESULT_BYTES back whole, inline: nothing in the payload store', async () => {
      const a = await replica('a')
      const b = await replica('b')
      // A JSON result of exactly MAX_RESULT_BYTES, as link.ts measures it.
      const text = 'x'.repeat(MAX_RESULT_BYTES - JSON.stringify({ text: '' }).length)
      const result = { text }
      expect(new TextEncoder().encode(JSON.stringify(result)).length).toBe(MAX_RESULT_BYTES)
      await tab(b.hub, () => ({ ok: true, result }))
      await owned(TAB, b.queue)
      const session = randomUUID()
      a.hub.pairSession(session, TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: session }, 'get_editor_text', {}, { signal: signal() })
      expect(outcome).toEqual({ ok: true, result })
      // Over OFFLOAD_BYTES, yet sealed inline: a bridge call leaves no file on the share (#4780 review).
      expect(await readdir(payloadDir)).toEqual([])
    })

    it('reaches the tab an MCP client paired by code, and reports its state', async () => {
      const a = await replica('a')
      const b = await replica('b')
      await tab(b.hub, () => ({ ok: true, result: 'ok' }))
      await owned(TAB, b.queue)
      const store = new PostgresPairingStore(db.sql)
      const request = await store.request(anonymous)
      expect((await store.accept(request.id, request.code, TAB)).ok).toBe(true)
      expect(await a.hub.call({ principal: anonymous }, 'navigate', { to: '/' }, { signal: signal() })).toEqual({ ok: true, result: 'ok' })
      expect(await a.hub.status({ principal: anonymous })).toMatchObject({
        attached: true,
        via: 'pairing',
        route: '/m/box',
        live: ['snapshot', 'navigate'],
      })
    })

    it('answers not connected at once when no replica ever held the tab', async () => {
      const a = await replica('a')
      a.hub.pairSession('s1', TAB)
      const started = performance.now()
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser', message: expect.stringMatching(/tab is not connected/) } })
      expect(performance.now() - started).toBeLessThan(1_000)
      expect(await a.hub.status({ principal: browser, sessionId: 's1' })).toMatchObject({ attached: false })
    })

    it('forgets the owner when the tab disconnects', async () => {
      const a = await replica('a')
      const b = await replica('b')
      const onB = await tab(b.hub, () => ({ ok: true, result: 'from b' }))
      await owned(TAB, b.queue)
      onB.conn.close()
      await vi.waitFor(async () => expect(await db.sql`SELECT 1 FROM ai_tab_owners WHERE tab_id = ${TAB}`).toHaveLength(0))
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser' } })
    })

    it("times a silent tab out on its own replica, with the call's own timeout", async () => {
      const a = await replica('a')
      const b = await replica('b')
      await tab(b.hub, () => undefined)
      await owned(TAB, b.queue)
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal(), timeoutMs: 300 })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_answer', message: expect.stringMatching(/did not answer snapshot within/) } })
    })

    it('stops waiting when the call is cancelled', async () => {
      const a = await replica('a')
      const b = await replica('b')
      await tab(b.hub, () => undefined)
      await owned(TAB, b.queue)
      a.hub.pairSession('s1', TAB)
      const stop = new AbortController()
      const call = a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: stop.signal })
      setTimeout(() => stop.abort(), 300)
      await expect(call).rejects.toMatchObject({ name: 'AbortError' })
    })

    it('answers not connected when the tab left the owner before the call ran there', async () => {
      const a = await replica('a')
      const b = await replica('b')
      await tab(b.hub, () => ({ ok: true, result: 'never' }))
      await owned(TAB, b.queue)
      b.relay.serve({ holds: () => true, run: async () => ({ op: 'gone' }) })
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser', message: expect.stringMatching(/tab is not connected/) } })
    })

    it('gives up on an owner that took the call and never answered', async () => {
      const a = await replica('a', { ackTimeoutMs: 500 })
      const b = await replica('b')
      await tab(b.hub, () => ({ ok: true, result: 'never' }))
      await owned(TAB, b.queue)
      // Answers only once the caller gave up, so B's worker can stop after the test.
      let release = () => {}
      b.relay.serve({ holds: () => true, run: () => new Promise((resolve) => (release = () => resolve({ op: 'gone' }))) })
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal(), timeoutMs: 300 })
      release()
      expect(outcome).toMatchObject({
        ok: false,
        error: { code: 'no_answer', message: expect.stringMatching(/replica holding the tab did not answer/) },
      })
    })

    it("leaves no plaintext in any bridge workflow's history, and deletes each call's key", async () => {
      const a = await replica('a')
      const b = await replica('b')
      const SECRET = `form-contents-${randomUUID()}`
      await tab(b.hub, (frame) => ({ ok: true, result: { echoed: frame.args, page: `${SECRET} on the page` } }))
      await owned(TAB, b.queue)
      // One call for a session, one for an /mcp caller with no session.
      const session = randomUUID()
      a.hub.pairSession(session, TAB)
      expect(await a.hub.call({ principal: browser, sessionId: session }, 'fill', { value: SECRET }, { signal: signal() })).toMatchObject({ ok: true })
      const store = new PostgresPairingStore(db.sql)
      const request = await store.request(anonymous)
      expect((await store.accept(request.id, request.code, TAB)).ok).toBe(true)
      expect(await a.hub.call({ principal: anonymous }, 'fill', { value: SECRET }, { signal: signal() })).toMatchObject({ ok: true })
      expect(started).toHaveLength(2)
      for (const id of started) {
        expect(id).toMatch(/^bridge-[0-9a-f-]{36}$/)
        // The raw history, read without the codec.
        const history = historyToJSON(await env.client.workflow.getHandle(id).fetchHistory())
        expect(history).not.toContain(SECRET)
        const datas = [...history.matchAll(/"data":\s*"([A-Za-z0-9+/=]+)"/g)].map((m) => Buffer.from(m[1]!, 'base64').toString('latin1'))
        expect(datas.length).toBeGreaterThan(0)
        for (const data of datas) expect(data).not.toContain(SECRET)
      }
      // Each call's key went with its answer: the history cannot be opened again.
      await vi.waitFor(async () => expect(await db.sql`SELECT subject FROM ai_payload_keys WHERE subject = ANY(${started})`).toEqual([]))
    })

    it('refuses to send a call through Temporal unsealed', async () => {
      const a = await replica('a', { sealed: false })
      const b = await replica('b')
      const onB = await tab(b.hub, () => ({ ok: true, result: 'never' }))
      await owned(TAB, b.queue)
      a.hub.pairSession('s1', TAB)
      const outcome = await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })
      expect(outcome).toMatchObject({ ok: false, error: { code: 'no_browser', message: expect.stringMatching(/SCADBUDDY_SECRET_KEY_FILE/) } })
      expect(onB.calls).toHaveLength(0)
      expect(started).toHaveLength(0)
    })

    // #2086: a durable session's tool call is an activity on `agent-tools`, which any
    // replica's worker may take, so it can run where the session's chat socket never
    // paired it. The pairing is read from Postgres there.
    async function chatSession(): Promise<string> {
      const [row] = await db.sql<{ id: string }[]>`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
        VALUES (gen_random_uuid(), 'chat', 'browser', 'browser', 'you', 'browser', 'browser', 'idle', 10, 1)
        RETURNING id`
      return row!.id
    }
    const storedTab = async (session: string) =>
      (await db.sql<{ tab_id: string }[]>`SELECT tab_id FROM ai_session_tabs WHERE session_id = ${session}`)[0]?.tab_id

    it("runs a durable session's call on a replica its messages never reached, on the tab it paired", async () => {
      const a = await replica('a')
      const b = await replica('b')
      const t = await tab(b.hub, () => ({ ok: true, result: 'from b' }))
      await owned(TAB, b.queue)
      const session = await chatSession()
      b.hub.pairSession(session, TAB)
      await vi.waitFor(async () => expect(await storedTab(session)).toBe(TAB))
      expect(await a.hub.call({ principal: browser, sessionId: session }, 'snapshot', {}, { signal: signal() })).toEqual({
        ok: true,
        result: 'from b',
      })
      expect(t.calls).toHaveLength(1)
      expect(await a.hub.status({ principal: browser, sessionId: session })).toMatchObject({ attached: true, via: 'session' })
    })

    it("prefers another replica's newer pairing over this replica's own older one", async () => {
      const a = await replica('a')
      const b = await replica('b')
      const t1 = await tab(a.hub, () => ({ ok: true, result: 'T1 on a' }))
      const t2 = await tab(b.hub, () => ({ ok: true, result: 'T2 on b' }), OTHER_TAB)
      await owned(OTHER_TAB, b.queue)
      const session = await chatSession()
      a.hub.pairSession(session, TAB)
      await vi.waitFor(async () => expect(await storedTab(session)).toBe(TAB))
      b.hub.pairSession(session, OTHER_TAB)
      await vi.waitFor(async () => expect(await storedTab(session)).toBe(OTHER_TAB))
      expect(await a.hub.call({ principal: browser, sessionId: session }, 'snapshot', {}, { signal: signal() })).toEqual({
        ok: true,
        result: 'T2 on b',
      })
      expect(t1.calls).toHaveLength(0)
      expect(t2.calls).toHaveLength(1)
    })

    it('keeps a tab connected here local: nothing goes through Temporal', async () => {
      const a = await replica('a')
      const t = await tab(a.hub, () => ({ ok: true, result: 1 }))
      a.hub.pairSession('s1', TAB)
      const forward = vi.spyOn(a.relay, 'forward')
      expect(await a.hub.call({ principal: browser, sessionId: 's1' }, 'snapshot', {}, { signal: signal() })).toEqual({ ok: true, result: 1 })
      expect(t.calls).toHaveLength(1)
      expect(forward).not.toHaveBeenCalled()
    })
  },
)

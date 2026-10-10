import { setTimeout as sleep } from 'node:timers/promises'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { ResourceUpdatedNotificationSchema } from '@modelcontextprotocol/sdk/types.js'
import { delay, http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { AgentApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { MemoryEventSource } from '../src/events/bus.js'
import { PgEventListener } from '../src/events/pgListener.js'
import { PgMcpSessionRelay, sessionHash } from '../src/mcp/sessionRelay.js'
import { ResourceHub } from '../src/resources/hub.js'
import { BACKEND, LOOPBACK, MCP_URL, testApp } from './helpers/mcp.js'
import { InMemoryTokenStore } from './support/memoryTokens.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2086: two agent replicas on one database, as main.ts wires them: each its own
// listener connection, relay, resource hub and app. A session opened on one is
// used through the other, the way a Service without affinity spreads one
// client's requests across pods.

const MODELS = [{ slug: 'keychain', name: 'Keychain', origin: 'mine', has_thumbnail: false, has_readme: false, updated_at: '2026-09-28T00:00:00Z' }]

const backend = setupServer(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json(MODELS)))
beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

type Replica = { app: AgentApp; relay: PgMcpSessionRelay; listener: PgEventListener; bus: MemoryEventSource }

const BEAT_MS = 100
const ACK_TIMEOUT_MS = 1_000

describe.skipIf(!TEST_DATABASE_URL)(`/mcp sessions across replicas${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let url: string
  let drop: () => Promise<void>
  let tokens: InMemoryTokenStore
  let a: Replica
  let b: Replica
  const clients: Client[] = []

  /** A replica; `hears` drops the relay notifications it returns false for. */
  function replica(mcp: { maxSessionsPerCaller?: number } = {}, hears: (payload: string) => boolean = () => true): Replica {
    const listener = new PgEventListener(url, { searchPath: 'public', log: () => {} })
    const relay = new PgMcpSessionRelay(db.sql, {
      listen: (c, f) => listener.listenAlso(c, (payload) => (hears(payload) ? f(payload) : undefined)),
      ackTimeoutMs: ACK_TIMEOUT_MS,
      beatMs: BEAT_MS,
      log: () => {},
    })
    listener.start()
    const bus = new MemoryEventSource()
    const { app } = testApp({ tokens, mcp: { relay, resources: new ResourceHub(bus, { minIntervalMs: 0 }), ...mcp } })
    return { app, relay, listener, bus }
  }

  /** A client whose requests go to `route(method)`: one replica or the other, never both for one request. */
  async function connect(token: string, route: (method: string) => Replica): Promise<Client> {
    const fetchVia: typeof fetch = async (input, init) => {
      const request = new Request(input, init)
      request.headers.set('host', new URL(request.url).host)
      request.headers.set('authorization', `Bearer ${token}`)
      return route(request.method).app.fetch(request, { incoming: { socket: { remoteAddress: LOOPBACK } } })
    }
    const client = new Client({ name: 'replicas-test', version: '0' })
    await client.connect(new StreamableHTTPClientTransport(new URL(MCP_URL), { fetch: fetchVia }))
    clients.push(client)
    return client
  }

  /** A raw request to `replica` in `sessionId`. */
  function post(replica: Replica, token: string, sessionId: string, body: unknown): Promise<Response> {
    const request = new Request(MCP_URL, {
      method: 'POST',
      headers: {
        host: new URL(MCP_URL).host,
        authorization: `Bearer ${token}`,
        'mcp-session-id': sessionId,
        'mcp-protocol-version': '2025-06-18',
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
      },
      body: JSON.stringify(body),
    })
    return Promise.resolve(replica.app.fetch(request, { incoming: { socket: { remoteAddress: LOOPBACK } } }))
  }

  const sessionOf = (client: Client) => (client.transport as unknown as StreamableHTTPClientTransport).sessionId!

  async function until(check: () => boolean | Promise<boolean>, what: string, timeoutMs = 5000): Promise<void> {
    const deadline = performance.now() + timeoutMs
    while (!(await check())) {
      if (performance.now() > deadline) throw new Error(`timed out waiting for ${what}`)
      await sleep(10)
    }
  }

  beforeAll(async () => {
    ;({ db, url, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  beforeEach(async () => {
    await db.sql`TRUNCATE ai_mcp_sessions, ai_mcp_relay_messages`
    tokens = new InMemoryTokenStore()
    a = replica()
    b = replica()
    await Promise.all([a.listener.ready(), b.listener.ready()])
  })
  afterEach(async () => {
    await Promise.all(clients.splice(0).map((c) => c.close().catch(() => {})))
    for (const r of [a, b]) {
      await r.app.close()
      await r.listener.close()
    }
  })
  afterAll(async () => {
    await drop()
  })

  it('serves a session opened on one replica from another: tools/list, a tool call and DELETE', async () => {
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    // initialize (and its `initialized`) on A; everything after on B.
    let initialized = false
    const client = await connect(token, () => (initialized ? b : a))
    initialized = true
    const id = sessionOf(client)
    expect(await db.sql`SELECT replica FROM ai_mcp_sessions WHERE id_hash = ${sessionHash(id)}`).toEqual([
      { replica: a.relay.replica },
    ])
    // The directory never holds the id itself.
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions WHERE id_hash = ${id}`).toHaveLength(0)

    const { tools } = await client.listTools()
    expect(tools.map((t) => t.name)).toContain('list_models')
    const result = await client.callTool({ name: 'list_models', arguments: {} })
    expect(result.isError).toBeFalsy()
    expect(JSON.stringify(result.content)).toContain('keychain')

    await (client.transport as StreamableHTTPClientTransport).terminateSession()
    await until(async () => (await db.sql`SELECT 1 FROM ai_mcp_sessions`).length === 0, 'the directory row to go')
    // Ended on its owner: A no longer has it either.
    expect((await post(a, token, id, { jsonrpc: '2.0', id: 1, method: 'tools/list' })).status).toBe(404)
    // Every relay message was taken by the replica it was for.
    expect(await db.sql`SELECT id FROM ai_mcp_relay_messages`).toHaveLength(0)
  })

  it("refuses another caller on someone else's session through any replica", async () => {
    const { token } = await tokens.mint({ name: 'owner', tier: 'read' })
    const other = await tokens.mint({ name: 'other', tier: 'read' })
    const client = await connect(token, () => a)
    const res = await post(b, other.token, sessionOf(client), { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    expect(res.status).toBe(403)
  })

  it('answers 404 at once for a session no replica holds', async () => {
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const started = performance.now()
    const res = await post(b, token, 'no-such-session', { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    expect(res.status).toBe(404)
    expect(performance.now() - started).toBeLessThan(ACK_TIMEOUT_MS)
  })

  it("delivers the owner's resource notifications on a GET stream that reached another replica", async () => {
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    // POSTs to A (the owner); the standing GET stream to B.
    const client = await connect(token, (method) => (method === 'GET' ? b : a))
    const updated: string[] = []
    client.setNotificationHandler(ResourceUpdatedNotificationSchema, (n) => {
      updated.push(n.params.uri)
    })
    await client.subscribeResource({ uri: 'scadbuddy://jobs/j1' })
    // The GET stream opens after `initialized`; it is relayed, so wait for the owner's beat on it.
    await sleep(BEAT_MS * 3)
    a.bus.emit({ id: 'e1', kind: 'job.running', job_id: 'j1', slug: 'keychain' })
    await until(() => updated.includes('scadbuddy://jobs/j1'), 'the update through B')
  })

  it('counts the per-caller limit across replicas', async () => {
    for (const r of [a, b]) {
      await r.app.close()
      await r.listener.close()
    }
    a = replica({ maxSessionsPerCaller: 1 })
    b = replica({ maxSessionsPerCaller: 1 })
    await Promise.all([a.listener.ready(), b.listener.ready()])
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    await connect(token, () => a)
    await expect(connect(token, () => b)).rejects.toMatchObject({ code: 429 })
  })

  it('answers 404 when the owner is gone, and forgets the session', async () => {
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const id = sessionOf(client)
    // A dies without ending its sessions: it hears nothing any more.
    await a.listener.close()
    const res = await post(b, token, id, { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    expect(res.status).toBe(404)
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions WHERE id_hash = ${sessionHash(id)}`).toHaveLength(0)
  })

  /** Replaces replica `which` with one built by `make`. */
  async function swap(which: 'a' | 'b', make: () => Replica): Promise<void> {
    const old = which === 'a' ? a : b
    await old.app.close()
    await old.listener.close()
    const fresh = make()
    await fresh.listener.ready()
    if (which === 'a') a = fresh
    else b = fresh
  }

  it('never writes the bearer token or the session id to the relay', async () => {
    // A never hears the request, so its row waits out the ack timeout where it can be read.
    await swap('a', () => replica({}, (payload) => JSON.parse(payload).t !== 'req'))
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const id = sessionOf(client)
    const pending = post(b, token, id, { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    let rows: { body: unknown }[] = []
    await until(async () => {
      rows = await db.sql<{ body: unknown }[]>`SELECT body FROM ai_mcp_relay_messages`
      return rows.length > 0
    }, 'the relayed request row')
    const written = JSON.stringify(rows)
    expect(written).toContain(sessionHash(id))
    expect(written).not.toContain(token)
    expect(written).not.toContain(id)
    expect(written.toLowerCase()).not.toContain('authorization')
    expect((await pending).status).toBe(404)
  })

  it('keeps waiting, and keeps the session, when the owner took a request whose ack was lost', async () => {
    // B never hears an ack: its timeout finds the row taken, so the owner has it.
    await swap('b', () => replica({}, (payload) => JSON.parse(payload).t !== 'ack'))
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const id = sessionOf(client)
    const res = await post(b, token, id, { jsonrpc: '2.0', id: 1, method: 'tools/list' })
    expect(res.status).toBe(200)
    expect(await res.text()).toContain('list_models')
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions WHERE id_hash = ${sessionHash(id)}`).toHaveLength(1)
  })

  it('ends a relayed answer with an error when the owner dies mid-call', async () => {
    let reached!: () => void
    const backendReached = new Promise<void>((r) => (reached = r))
    backend.use(
      http.get(`${BACKEND}/api/v1/models`, async () => {
        reached()
        await delay('infinite')
        return HttpResponse.json(MODELS)
      }),
    )
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const pending = post(b, token, sessionOf(client), {
      jsonrpc: '2.0',
      id: 7,
      method: 'tools/call',
      params: { name: 'list_models', arguments: {} },
    })
    await backendReached
    // A stops hearing and beating: its relay is closed with its listener.
    a.relay.close()
    await a.listener.close()
    // The call answers as SSE, so its head (200) left before the tool finished;
    // the body then breaks off once the owner misses its beats.
    const res = await pending
    expect(res.status).toBe(200)
    const started = performance.now()
    await expect(res.text()).rejects.toThrow(/stopped answering/)
    expect(performance.now() - started).toBeLessThan(BEAT_MS * 30)
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions WHERE id_hash = ${sessionHash(sessionOf(client))}`).toHaveLength(0)
  })
})

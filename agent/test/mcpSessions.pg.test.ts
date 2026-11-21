import { execFileSync } from 'node:child_process'
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
import { CrudRelayRows, PgRelayRows, RELAY_SEND_MAX_BYTES, type RelayRows } from '../src/mcp/relayRows.js'
import { SharedMcpSessionRelay, type SessionOwner, sessionHash } from '../src/mcp/sessionRelay.js'
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
beforeAll(() => backend.listen({ onUnhandledFrame: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

/** A relay whose owner, armed, stalls its process once the first chunk of its next relayed answer has gone. */
class StallingRelay extends SharedMcpSessionRelay {
  stallAfterFirstChunk: (() => void) | undefined

  override serve(owner: SessionOwner): void {
    super.serve({
      run: async (...args) => {
        const res = await owner.run(...args)
        const stall = this.stallAfterFirstChunk
        if (!stall || !res.body) return res
        this.stallAfterFirstChunk = undefined
        const reader = res.body.getReader()
        let sent = false
        // highWaterMark 0: pulled only when the relay reads, so a second pull means
        // the chunk before has been sent on, NOTIFY and all.
        const body = new ReadableStream<Uint8Array>(
          {
            async pull(controller) {
              if (sent) stall()
              const { done, value } = await reader.read()
              if (done) return controller.close()
              controller.enqueue(value)
              sent = true
            },
            cancel: (reason) => reader.cancel(reason),
          },
          { highWaterMark: 0 },
        )
        return new Response(body, { status: res.status, headers: res.headers })
      },
    })
  }
}

type Replica = { app: AgentApp; relay: SharedMcpSessionRelay; listener: PgEventListener; bus: MemoryEventSource }

type Liveness = { beatMs: number; ackTimeoutMs: number }
/**
 * The relay's liveness in most tests. Both replicas and the client share one process and one
 * Postgres: on a loaded CI runner an event-loop stall, or a NOTIFY's round trip, can outlast the
 * quick window below (a peer is gone after 3 silent beats, about 400 ms), and the relaying replica
 * then ends a live owner's answer (#6521, reproduced with six runs pinned to two cores). Production
 * beats every 10 s and waits 3 s for an ack (mcp/sessionRelay.ts).
 */
const LIVE: Liveness = { beatMs: 1_000, ackTimeoutMs: 3_000 }
/** Only for the tests that make a peer silent or stall on purpose (`quickLiveness`). */
const BEAT_MS = 100
const QUICK: Liveness = { beatMs: BEAT_MS, ackTimeoutMs: 1_000 }

/**
 * The backend's `mcp-relay` CRUD routes (api/crud_agent_mcp.py, whose SQL its pytest checks), as a
 * fetch that answers each route with the agent's own SQL: the relay over `CrudRelayRows` sends
 * every call through the wire shape, ids and all in the body.
 */
/** Every body `CrudRelayRows` sent, for the check against the API's own routes. */
const sentToApi: { path: string; body: Record<string, unknown> }[] = []

const hasUv = (): boolean => {
  try {
    execFileSync('uv', ['--version'], { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

function crudRoutes(rows: RelayRows): typeof fetch {
  return async (input, init) => {
    // The API's JSON gate (api/limits.py MAX_TEXT_BODY_BYTES), refused before the route runs.
    if (Buffer.byteLength(String(init?.body ?? '')) > RELAY_SEND_MAX_BYTES) return new Response(null, { status: 413 })
    const path = new URL(String(input)).pathname.replace(/^.*\/crud\/agent\/mcp-relay/, '')
    const b = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
    sentToApi.push({ path: `/mcp-relay${path}`, body: b })
    const s = (k: string) => b[k] as string
    const n = (k: string) => b[k] as number
    const answers: Record<string, () => Promise<unknown>> = {
      '/sessions': async () => (await rows.register(s('hash'), s('replica'), s('caller_key')), { done: true }),
      '/sessions/touch': async () => (await rows.touch(s('hash')), { done: true }),
      '/sessions/remove': async () => ({ done: await rows.remove(s('hash'), b.replica === undefined ? undefined : s('replica')) }),
      '/sessions/owner': async () => ({ replica: (await rows.owner(s('hash'))) ?? null }),
      '/counts': async () => await rows.counts(s('caller_key'), n('idle_seconds')),
      '/send': async () => {
        const header = b.header as { row?: string }
        await rows.send(header, b.body === undefined ? undefined : { row: header.row!, value: b.body })
        return { done: true }
      },
      '/rows/take': async () => ({ body: (await rows.take(s('row'))) ?? null }),
      '/rows/withdraw': async () => ({ done: await rows.withdraw(s('row')) }),
      '/sweep': async () => (await rows.sweep(n('idle_seconds')), { done: true }),
    }
    const answer = answers[path]
    if (!answer) return new Response(null, { status: 404 })
    return Response.json(await answer(), { headers: { 'x-scadbuddy-crud': '1' } })
  }
}

describe.skipIf(!TEST_DATABASE_URL).each(['sql', 'crud'] as const)(`/mcp sessions across replicas, over %s${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, (over) => {
  let db: Database
  let url: string
  let drop: () => Promise<void>
  let tokens: InMemoryTokenStore
  let a: Replica
  let b: Replica
  const clients: Client[] = []
  let liveness: Liveness = LIVE

  /** A replica; `hears` drops the relay notifications it returns false for. */
  function replica(
    mcp: { maxSessionsPerCaller?: number } = {},
    hears: (payload: string) => boolean = () => true,
    log: (message: string) => void = () => {},
    Relay: typeof SharedMcpSessionRelay = SharedMcpSessionRelay,
  ): Replica {
    const listener = new PgEventListener(url, { searchPath: 'public', log: () => {} })
    const sql = new PgRelayRows(db.sql)
    const rows = over === 'sql' ? sql : new CrudRelayRows({ baseUrl: BACKEND, fetch: crudRoutes(sql) })
    const relay = new Relay(rows, {
      listen: (c, f) => listener.listenAlso(c, (payload) => (hears(payload) ? f(payload) : undefined)),
      ackTimeoutMs: liveness.ackTimeoutMs,
      beatMs: liveness.beatMs,
      log,
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
    liveness = LIVE
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
    expect(performance.now() - started).toBeLessThan(liveness.ackTimeoutMs)
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
    // The GET stream opens after `initialized` and is relayed, so it may not be up yet: an update
    // sent before it is lost, as it would be to a client with no stream. Sent again until one
    // arrives, not after a fixed sleep a loaded runner can outlast (#6521).
    let n = 0
    await until(() => {
      a.bus.emit({ id: `e${++n}`, kind: 'job.running', job_id: 'j1', slug: 'keychain' })
      return updated.includes('scadbuddy://jobs/j1')
    }, 'the update through B')
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

  /** Both replicas again, with the quick liveness: for a test that silences or stalls a peer. */
  async function quickLiveness(): Promise<void> {
    liveness = QUICK
    await swap('a', () => replica())
    await swap('b', () => replica())
  }

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

  it("does not sweep or uncount a live session's row inside the touch interval (#2213)", async () => {
    // `last_seen` is written at most every 30 s, so a row a few seconds past the
    // idle limit can belong to a session its owner still holds.
    await a.relay.register('live-hash', 'caller')
    await db.sql`UPDATE ai_mcp_sessions SET last_seen = now() - interval '5 seconds' WHERE id_hash = 'live-hash'`
    expect(await a.relay.counts('caller', 1_000)).toEqual({ total: 1, caller: 1 })
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions WHERE id_hash = 'live-hash'`).toHaveLength(1)
  })

  it('says so when an owner that took no request loses its session from the directory (#2213)', async () => {
    const logged: string[] = []
    await swap('a', () => replica({}, (payload) => JSON.parse(payload).t !== 'req'))
    await swap('b', () => replica({}, () => true, (m) => logged.push(m)))
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const id = sessionOf(client)
    expect((await post(b, token, id, { jsonrpc: '2.0', id: 1, method: 'tools/list' })).status).toBe(404)
    expect(logged.join('\n')).toMatch(new RegExp(`dropped its session ${sessionHash(id).slice(0, 12)} from the directory`))
  })

  it('counts a session whose directory row could not be written against the limits (#2213)', async () => {
    await swap('a', () => replica({ maxSessionsPerCaller: 1 }))
    a.relay.register = async () => {
      throw new Error('register failed')
    }
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    await connect(token, () => a)
    expect(await db.sql`SELECT 1 FROM ai_mcp_sessions`).toHaveLength(0)
    await expect(connect(token, () => a)).rejects.toMatchObject({ code: 429 })
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

  /** A raw GET (the standalone SSE stream) to `replica` in `sessionId`; its body is cancelled once the status is read. */
  async function openStream(replica: Replica, token: string, sessionId: string): Promise<number> {
    const request = new Request(MCP_URL, {
      method: 'GET',
      headers: {
        host: new URL(MCP_URL).host,
        authorization: `Bearer ${token}`,
        'mcp-session-id': sessionId,
        'mcp-protocol-version': '2025-06-18',
        accept: 'text/event-stream',
      },
    })
    const res = await replica.app.fetch(request, { incoming: { socket: { remoteAddress: LOOPBACK } } })
    await res.body?.cancel().catch(() => {})
    return res.status
  }

  it("lets go of a relayed GET stream when the relaying replica dies, so the client's next one opens", async () => {
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    // Opened by hand, so no GET stream is open yet: the SDK client would open one.
    const init = await a.app.fetch(
      new Request(MCP_URL, {
        method: 'POST',
        headers: {
          host: new URL(MCP_URL).host,
          authorization: `Bearer ${token}`,
          'content-type': 'application/json',
          accept: 'application/json, text/event-stream',
        },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 0,
          method: 'initialize',
          params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } },
        }),
      }),
      { incoming: { socket: { remoteAddress: LOOPBACK } } },
    )
    const id = init.headers.get('mcp-session-id')!
    await init.body?.cancel().catch(() => {})
    expect((await post(a, token, id, { jsonrpc: '2.0', method: 'notifications/initialized' })).status).toBe(202)
    // The session's one standalone stream, opened through B and held there.
    const getVia = (r: Replica) =>
      r.app.fetch(
        new Request(MCP_URL, {
          method: 'GET',
          headers: {
            host: new URL(MCP_URL).host,
            authorization: `Bearer ${token}`,
            'mcp-session-id': id,
            'mcp-protocol-version': '2025-06-18',
            accept: 'text/event-stream',
          },
        }),
        { incoming: { socket: { remoteAddress: LOOPBACK } } },
      )
    let held: Response | undefined
    await until(async () => {
      const res = await getVia(b)
      if (res.status === 200) held = res
      else await res.body?.cancel().catch(() => {})
      return held !== undefined
    }, 'the stream through B')
    // While B lives, a second stream is refused: the transport allows one.
    expect(await openStream(a, token, id)).toBe(409)
    // B dies without a word: no cancel, no more beats.
    b.relay.close()
    await b.listener.close()
    const c = replica()
    await c.listener.ready()
    try {
      await until(async () => (await openStream(c, token, id)) === 200, 'a new stream through C', liveness.beatMs * 10)
    } finally {
      await c.app.close()
      await c.listener.close()
    }
  })

  it('relays answers of every size whole, however much their JSON escapes', async () => {
    // Names full of quotes: a tool result is JSON inside a JSON string inside a
    // NOTIFY payload, so each quote is escaped twice on the way.
    const models = (n: number) =>
      Array.from({ length: n }, (_, i) => ({ ...MODELS[0], slug: `m${i}`, name: `${'"'.repeat(40)}${'\\'.repeat(20)} ${i}` }))
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    let via: Replica = a
    const client = await connect(token, () => via)
    // Each call well inside the test's 20 s, so a call that never answers (#4273) fails
    // naming its count and its way, not as the test's bare timeout.
    const call = (n: number, way: string) =>
      client.callTool({ name: 'list_models', arguments: { limit: 100 } }, undefined, { timeout: 5_000 }).catch((err: unknown) => {
        throw new Error(`${n} models, ${way}: ${err instanceof Error ? err.message : String(err)}`)
      })
    // Every count up to 60: past some size each answer goes in a row, below it
    // in the NOTIFY, and the boundary is where escaping can push a chunk over.
    for (let n = 1; n <= 60; n++) {
      const listed = models(n)
      backend.use(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json(listed)))
      via = a
      const direct = await call(n, 'direct')
      via = b
      const relayed = await call(n, 'relayed')
      expect(relayed.isError, `${n} models`).toBeFalsy()
      expect(relayed.content, `${n} models`).toEqual(direct.content)
    }
  })

  // The fake routes above answer with the agent's own SQL, so this checks the wire itself: every
  // body CrudRelayRows sent is one the API's routes take (backend api/crud_agent_mcp.py, run with
  // uv), and each NOTIFY the API would send is the header the agent sent.
  it.skipIf(over === 'sql' || !hasUv())("sends only bodies the API's routes take, and they NOTIFY what was sent", { timeout: 120_000 }, async () => {
    // Only this test's own calls: others write made-up hashes straight through the rows.
    sentToApi.length = 0
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    let initialized = false
    const client = await connect(token, () => (initialized ? b : a))
    initialized = true
    await client.listTools()
    await (client.transport as StreamableHTTPClientTransport).terminateSession()
    const sent = sentToApi.splice(0)
    const paths = new Set(sent.map((s) => s.path))
    for (const path of ['/mcp-relay/sessions', '/mcp-relay/counts', '/mcp-relay/send', '/mcp-relay/rows/take']) expect(paths).toContain(path)
    const script = new URL('./support/relay_backend_parity.py', import.meta.url).pathname
    const cwd = new URL('../../backend/', import.meta.url).pathname
    const out = execFileSync('uv', ['run', '--frozen', 'python', script], { cwd, input: JSON.stringify(sent), maxBuffer: 1 << 28 })
    const results = JSON.parse(out.toString()) as { ok: boolean; error?: string; payload?: string }[]
    expect(results).toHaveLength(sent.length)
    results.forEach((result, i) => {
      expect(result, `${sent[i]!.path}: ${result.error ?? ''}`).toMatchObject({ ok: true })
      if (sent[i]!.path === '/mcp-relay/send') expect(JSON.parse(result.payload!)).toEqual(sent[i]!.body.header)
    })
  })

  it('relays an answer past the API gate whole, in rows under it', async () => {
    // One SSE event of about 9 MiB: a single row would be refused at the gate. Plain letters, so
    // nothing escapes ("relays answers of every size whole" covers escaping): a quote doubles at
    // each JSON layer, and 6 MiB of them took past 15 s a call on a loaded CI runner (#6521).
    const big = Array.from({ length: 9 }, (_, i) => ({ ...MODELS[0], slug: `big${i}`, name: 'x'.repeat(1 << 20) }))
    backend.use(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json(big)))
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    let via: Replica = a
    const client = await connect(token, () => via)
    // About 0.5 s a call on an idle machine; the margin is for a runner running the whole suite.
    const call = () => client.callTool({ name: 'list_models', arguments: { limit: 100 } }, undefined, { timeout: 45_000 })
    const direct = await call()
    via = b
    const relayed = await call()
    expect(relayed.isError).toBeFalsy()
    expect(JSON.stringify(relayed.content).length).toBeGreaterThan(RELAY_SEND_MAX_BYTES)
    expect(relayed.content).toEqual(direct.content)
  }, 120_000)

  it('answers 413 over the CRUD routes for a request whose relay message is past the API gate, sending nothing', async () => {
    // A 4 MiB body (the MCP SDK's limit) of backslashes doubles when the row escapes it: past
    // the API's 8 MiB JSON gate, which over SQL never applied.
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const head = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_models","arguments":{"x":"'
    const tail = '"}}}'
    const fill = 4 * 1024 * 1024 - head.length - tail.length
    const raw = head + '\\\\'.repeat(Math.floor(fill / 2)) + 'x'.repeat(fill % 2) + tail
    expect(Buffer.byteLength(raw)).toBe(4 * 1024 * 1024)
    const sent = (await db.sql`SELECT count(*)::int AS n FROM ai_mcp_relay_messages`)[0]!.n as number
    const res = await b.app.fetch(
      new Request(MCP_URL, {
        method: 'POST',
        headers: {
          host: new URL(MCP_URL).host,
          authorization: `Bearer ${token}`,
          'mcp-session-id': sessionOf(client),
          'mcp-protocol-version': '2025-06-18',
          'content-type': 'application/json',
          accept: 'application/json, text/event-stream',
        },
        body: raw,
      }),
      { incoming: { socket: { remoteAddress: LOOPBACK } } },
    )
    if (over === 'sql') {
      // Postgres takes the row: the owner answers it.
      expect(res.status).toBe(200)
      return
    }
    expect(res.status).toBe(413)
    expect(await res.json()).toMatchObject({ error: { message: 'Request body too large to relay' } })
    expect((await db.sql`SELECT count(*)::int AS n FROM ai_mcp_relay_messages`)[0]!.n).toBe(sent)
  })

  /** Busy for longer than the beats a peer may miss: nothing else in this process runs. */
  function stall(): void {
    const until = performance.now() + BEAT_MS * 6
    while (performance.now() < until) {
      // busy
    }
  }

  /** A tools/call relayed through B, read whole: it rejects if the answer ended as an error. */
  async function callThroughB(token: string, sessionId: string): Promise<string> {
    const res = await post(b, token, sessionId, {
      jsonrpc: '2.0',
      id: 7,
      method: 'tools/call',
      params: { name: 'list_models', arguments: {} },
    })
    const text = await res.text()
    expect(res.status, text).toBe(200)
    return text
  }

  // #4552: after a stall (CPU starvation, a long GC) the liveness timers run before
  // the messages that arrived meanwhile are read. Timed by the clock, each side took
  // the other, still talking, for gone: the owner stopped the call, and the relaying
  // replica dropped the owner's directory row, so the client's next call met a 404.
  it('keeps a live owner through a stall while the call waits for its head (#4552)', async () => {
    await quickLiveness()
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    backend.use(
      http.get(`${BACKEND}/api/v1/models`, async () => {
        await delay(BEAT_MS) // B has heard the ack
        stall()
        return HttpResponse.json(MODELS)
      }),
    )
    expect(await callThroughB(token, sessionOf(client))).toContain('keychain')
    backend.resetHandlers()
    expect(await callThroughB(token, sessionOf(client))).toContain('keychain')
  })

  it('keeps a live owner, and its session, through a stall between the answer and its end (#4552)', async () => {
    await quickLiveness()
    await swap('a', () => replica({}, undefined, undefined, StallingRelay))
    const owner = a.relay as StallingRelay
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    owner.stallAfterFirstChunk = stall
    expect(await callThroughB(token, sessionOf(client))).toContain('keychain')
    expect(await callThroughB(token, sessionOf(client))).toContain('keychain')
    expect(await db.sql`SELECT replica FROM ai_mcp_sessions WHERE id_hash = ${sessionHash(sessionOf(client))}`).toEqual([
      { replica: a.relay.replica },
    ])
  })

  it('ends an answer with an error, not as whole, when its owner stops it for the relaying replica going silent', async () => {
    await quickLiveness()
    // #4384: A never hears B's beats, so it takes B for gone once the call's head has left.
    await swap('a', () => replica({}, (payload) => JSON.parse(payload).t !== 'beat'))
    backend.use(
      http.get(`${BACKEND}/api/v1/models`, async () => {
        await delay(BEAT_MS * 6)
        return HttpResponse.json(MODELS)
      }),
    )
    const { token } = await tokens.mint({ name: 't', tier: 'read' })
    const client = await connect(token, () => a)
    const res = await post(b, token, sessionOf(client), {
      jsonrpc: '2.0',
      id: 7,
      method: 'tools/call',
      params: { name: 'list_models', arguments: {} },
    })
    expect(res.status).toBe(200)
    await expect(res.text()).rejects.toThrow(/failed while answering/)
  })

  it('ends a relayed answer with an error when the owner dies mid-call', async () => {
    await quickLiveness()
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

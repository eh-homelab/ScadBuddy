import type { Server as HttpServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { setTimeout as sleep } from 'node:timers/promises'
import { serve } from '@hono/node-server'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { shutdown } from '../src/shutdown.js'
import { appFetch, BACKEND, connect, MCP_URL, testApp } from './helpers/mcp.js'

// /mcp session lifecycle: per-caller limits, the timer-driven idle sweep,
// graceful shutdown with standing SSE streams, and Last-Event-ID resumption
// against the real mounted endpoint (review 5861456943).

const backend = setupServer()
beforeAll(() => backend.listen({ onUnhandledRequest: 'error' }))
afterEach(() => backend.resetHandlers())
afterAll(() => backend.close())

const clients: Client[] = []
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.close().catch(() => {})))
})

describe('/mcp session limits', () => {
  it('caps sessions per caller without taking the allowance from other callers', async () => {
    const t = testApp({ mcp: { maxSessionsPerCaller: 2 } })
    const a = await t.tokens.mint({ name: 'a', tier: 'read' })
    const b = await t.tokens.mint({ name: 'b', tier: 'read' })
    const asA = { headers: { authorization: `Bearer ${a.token}` } }
    clients.push(await connect(t.app, asA), await connect(t.app, asA))
    await expect(connect(t.app, asA)).rejects.toMatchObject({ code: 429 })
    const other = await connect(t.app, { headers: { authorization: `Bearer ${b.token}` } })
    clients.push(other)
    expect((await other.listTools()).tools.length).toBeGreaterThan(0)
  })

  it('counts anonymous sessions by client address in disabled mode', async () => {
    const t = testApp({ settings: { mode: 'disabled' }, mcp: { maxSessionsPerCaller: 1 } })
    clients.push(await connect(t.app, { address: '127.0.0.1' }))
    await expect(connect(t.app, { address: '127.0.0.1' })).rejects.toMatchObject({ code: 429 })
    clients.push(await connect(t.app, { address: '127.0.0.2' }))
  })

  it('keeps an active session when the wall clock steps forward (#1485)', async () => {
    const t = testApp({ settings: { mode: 'disabled' }, mcp: { idleSessionMs: 60_000, sweepIntervalMs: 10 } })
    const client = await connect(t.app)
    clients.push(client)
    const id = (client.transport as unknown as StreamableHTTPClientTransport).sessionId!
    // Let the client's standing GET stream open first: it counts as activity too.
    await sleep(50)
    const now = Date.now
    const stepped = vi.spyOn(Date, 'now').mockImplementation(() => now.call(Date) + 3_600_000)
    try {
      await sleep(100)
    } finally {
      stepped.mockRestore()
    }
    const res = await appFetch(t.app, {
      headers: { 'mcp-session-id': id, 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    })(MCP_URL, { method: 'POST', body: JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/list' }) })
    expect(res.status).toBe(200)
  })

  it('sweeps an idle session on its timer, with no other session opening', async () => {
    const t = testApp({ settings: { mode: 'disabled' }, mcp: { idleSessionMs: 30, sweepIntervalMs: 10 } })
    const client = await connect(t.app)
    clients.push(client)
    const id = (client.transport as unknown as StreamableHTTPClientTransport).sessionId!
    await sleep(150)
    const res = await appFetch(t.app, {
      headers: { 'mcp-session-id': id, 'content-type': 'application/json', accept: 'application/json, text/event-stream' },
    })(MCP_URL, { method: 'POST', body: JSON.stringify({ jsonrpc: '2.0', id: 9, method: 'tools/list' }) })
    expect(res.status).toBe(404)
  })
})

describe('graceful shutdown with open /mcp sessions', () => {
  // These talk to a real socket on loopback, and msw's fetch interception
  // buffers the standing SSE stream, so it is switched off around them.
  beforeAll(() => backend.close())
  afterAll(() => backend.listen({ onUnhandledRequest: 'error' }))

  async function listening(app: ReturnType<typeof testApp>['app']) {
    const server = await new Promise<ReturnType<typeof serve>>((resolve) => {
      const s = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0 }, () => resolve(s))
    })
    const url = new URL(`http://127.0.0.1:${(server.address() as AddressInfo).port}/mcp`)
    const client = new Client({ name: 'shutdown-test', version: '0' })
    await client.connect(new StreamableHTTPClientTransport(url))
    clients.push(client)
    // The client opens its standing GET stream after `initialized`; give it a moment.
    await sleep(100)
    const closeServer = () =>
      new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
    return { closeServer, server }
  }

  it('ends the sessions first, so the server drains and the exit is clean', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const { closeServer } = await listening(app)
    const started = performance.now()
    expect(await shutdown({ closeSessions: () => app.close(), closeServer, timeoutMs: 3000 })).toBe('clean')
    expect(performance.now() - started).toBeLessThan(3000)
  })

  it('without ending them, a standing SSE stream holds the drain until the deadline', async () => {
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const { closeServer, server } = await listening(app)
    expect(await shutdown({ closeServer, timeoutMs: 300 })).toBe('timed out')
    await app.close()
    ;(server as HttpServer).closeAllConnections()
  })
})

/** Reads SSE events off a stream until `done(event)` says stop, then cancels it. */
async function readEvents(
  body: ReadableStream<Uint8Array>,
  done: (event: { id?: string; data?: unknown }) => boolean,
): Promise<{ id?: string; data?: unknown }[]> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  const events: { id?: string; data?: unknown }[] = []
  let buffer = ''
  for (;;) {
    const { done: end, value } = await reader.read()
    if (end) break
    buffer += decoder.decode(value, { stream: true })
    let cut: number
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      const event: { id?: string; data?: unknown } = {}
      for (const line of raw.split('\n')) {
        if (line.startsWith('id:')) event.id = line.slice(3).trim()
        if (line.startsWith('data:') && line.slice(5).trim()) event.data = JSON.parse(line.slice(5))
      }
      events.push(event)
      if (done(event)) {
        await reader.cancel().catch(() => {})
        return events
      }
    }
  }
  return events
}

describe('Last-Event-ID resumption on the mounted /mcp', () => {
  it('replays what a dropped stream missed, including the final result', async () => {
    let released = false
    backend.use(
      http.get(`${BACKEND}/api/v1/models/keychain/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/keychain/render`, () =>
        HttpResponse.json({ job_id: 'j1', status_url: '' }, { status: 202 }),
      ),
      http.get(`${BACKEND}/api/v1/jobs/j1`, () =>
        HttpResponse.json({ id: 'j1', slug: 'keychain', created_at: '', status: released ? 'done' : 'running' }),
      ),
    )
    const { app } = testApp({ settings: { mode: 'disabled' } })
    const base = { 'content-type': 'application/json', accept: 'application/json, text/event-stream' }
    const post = (body: unknown, headers: Record<string, string> = {}) =>
      appFetch(app, { headers: { ...base, ...headers } })(MCP_URL, { method: 'POST', body: JSON.stringify(body) })

    const init = await post({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'resume-test', version: '0' } },
    })
    const session = init.headers.get('mcp-session-id')!
    await init.body?.cancel()
    await post({ jsonrpc: '2.0', method: 'notifications/initialized' }, { 'mcp-session-id': session })

    const call = await post(
      {
        jsonrpc: '2.0',
        id: 2,
        method: 'tools/call',
        params: { name: 'render_model', arguments: { slug: 'keychain' }, _meta: { progressToken: 'p' } },
      },
      { 'mcp-session-id': session },
    )
    expect(call.headers.get('content-type')).toContain('text/event-stream')
    // Read the first progress event with an id, then drop the connection.
    const first = await readEvents(call.body!, (e) => e.id !== undefined && e.data !== undefined)
    const lastId = first.at(-1)!.id!
    expect((first.at(-1)!.data as { method?: string }).method).toBe('notifications/progress')

    released = true
    await sleep(150) // the handler polls, sees `done` and sends its result into the event store

    const resumed = await appFetch(app, {
      headers: { accept: 'text/event-stream', 'mcp-session-id': session, 'last-event-id': lastId },
    })(MCP_URL, { method: 'GET' })
    expect(resumed.status).toBe(200)
    const replay = await readEvents(resumed.body!, (e) => (e.data as { id?: number } | undefined)?.id === 2)
    const result = replay.find((e) => (e.data as { id?: number } | undefined)?.id === 2)
    expect(result).toBeDefined()
    const text = (result!.data as { result: { content: { text: string }[] } }).result.content[0]!.text
    expect(JSON.parse(unwrapUntrusted(text))).toMatchObject({ job_id: 'j1', status: 'done' })
  })
})

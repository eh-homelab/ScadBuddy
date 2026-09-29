import { randomBytes } from 'node:crypto'
import { request as httpRequest } from 'node:http'
import type { AddressInfo } from 'node:net'
import { serve } from '@hono/node-server'
import { Hono } from 'hono'
import type { UpgradeWebSocket, WSContext, WSEvents } from 'hono/ws'
import { describe, expect, it } from 'vitest'
import { type AiStatusView, type AppDeps, createApp, statusReason } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import { MAX_QUEUED_FRAMES, registerChatRoute } from '../src/routes/chat.js'
import { JSON_BODY_MAX } from '../src/routes/guard.js'
import { kekFromBase64 } from '../src/secrets.js'
import {
  CONTEXT_MAX,
  MESSAGE_MAX,
  parseClientFrame,
  renderPageContext,
} from '../src/sessions/clientProtocol.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// The chat socket's pieces that need no database: the client-message parser
// against the panel's own builder, the page context the model gets, and
// GET /api/v1/ai/status, which the UI's gate reads. The socket itself is
// test/chat.e2e.test.ts.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const fakeSessions = {} as SessionManager
const fakeUpgrade = (() => () => Promise.resolve()) as unknown as UpgradeWebSocket

function deps(overrides: Partial<AppDeps> = {}): AppDeps {
  return {
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    sessions: fakeSessions,
    upgradeWebSocket: fakeUpgrade,
    ...overrides,
  }
}

/** What the ingress forwards for a page opened at the public URL. */
const VIA_INGRESS = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https' }

async function status(
  app: ReturnType<typeof createApp>,
  headers: Record<string, string> = VIA_INGRESS,
): Promise<AiStatusView> {
  const res = await app.request('/api/v1/ai/status', { headers })
  expect(res.status).toBe(200)
  expect(res.headers.get('cache-control')).toBe('no-store')
  expect(res.headers.get('x-scadbuddy-service')).toBe('agent')
  return (await res.json()) as AiStatusView
}

describe('client messages', () => {
  it('accepts every message the panel builds, and nothing malformed', async () => {
    const { clientMessage } = await frontendClientMessages()
    const built = [
      clientMessage({ type: 'user.message', text: 'hi', context: { route: '/', tools: ['navigate'], page: { a: 1 } } }),
      clientMessage({ type: 'user.message', sessionId: 's1', text: 'hi', context: { route: '/models/x', modelSlug: 'x' } }),
      clientMessage({ type: 'approval.decision', sessionId: 's1', id: 'a1', approve: true }),
      clientMessage({ type: 'session.interrupt', sessionId: 's1' }),
      clientMessage({ type: 'session.handoff', sessionId: 's1' }),
      clientMessage({ type: 'session.attach', sessionId: 's1' }),
    ]
    for (const message of built) expect(parseClientFrame(JSON.stringify(message))).toMatchObject({ ok: true, value: message })

    expect(parseClientFrame('not json')).toEqual({ ok: false, error: 'frame is not JSON' })
    expect(parseClientFrame(JSON.stringify({ v: 2, type: 'session.attach', sessionId: 's' }))).toMatchObject({
      ok: false,
      error: 'unsupported protocol version 2',
    })
    expect(parseClientFrame(JSON.stringify({ v: 1, type: 'user.message', text: '', context: { route: '/' } })).ok).toBe(false)
    const huge = { v: 1, type: 'user.message', text: 'x'.repeat(MESSAGE_MAX + 1), context: { route: '/' } }
    expect(parseClientFrame(JSON.stringify(huge)).ok).toBe(false)
  })

  it('renders the page context as labelled data, capped', () => {
    const text = renderPageContext({ route: '/models/x', modelSlug: 'x' })
    expect(text).toContain('<page_context>\n{"route":"/models/x","modelSlug":"x"}\n</page_context>')
    expect(text).toMatch(/not instructions/)
    const big = renderPageContext({ route: '/', page: { blob: 'y'.repeat(CONTEXT_MAX * 2) } })
    expect(big.length).toBeLessThan(CONTEXT_MAX + 400)
    expect(big).toContain('(truncated)')
  })
})

describe('GET /api/v1/ai/status', () => {
  it('is available when AI is enabled and the chat socket exists', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: 'sk-ant-status-test-0000' }, kek)
    expect(await status(createApp(deps({ credentials })))).toEqual({ available: true, state: 'enabled', ai: 'enabled' })
  })

  it('names what is missing: no credential, no database, no key, unreachable', async () => {
    expect(await status(createApp(deps()))).toEqual({
      available: false,
      state: 'disabled',
      ai: 'disabled (no Claude credential)',
      reason: 'No Claude credential is configured yet.',
    })
    expect(await status(createApp(deps({ database: undefined, credentials: undefined, sessions: undefined })))).toMatchObject({
      available: false,
      state: 'disabled',
      reason: expect.stringMatching(/SCADBUDDY_DATABASE_URL/),
    })
    expect(await status(createApp(deps({ kek: { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' } })))).toMatchObject({
      state: 'disabled',
      reason: expect.stringMatching(/key-encryption key.*SCADBUDDY_SECRET_KEY_FILE is not set/),
    })
    expect(await status(createApp(deps({ database: { ping: () => Promise.resolve(false), ready: () => Promise.resolve(true) } })))).toEqual({
      available: false,
      state: 'unavailable',
      ai: 'unavailable (database unreachable)',
      reason: 'The agent service is unavailable: database unreachable.',
    })
  })

  it('is not available without the chat socket, even with AI enabled', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: 'sk-ant-status-test-0000' }, kek)
    expect(await status(createApp(deps({ credentials, upgradeWebSocket: undefined })))).toMatchObject({
      available: false,
      state: 'enabled',
      reason: expect.stringMatching(/chat socket/),
    })
  })

  it('says the socket would be refused for a page not opened at the public HTTPS URL', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: 'sk-ant-status-test-0000' }, kek)
    const app = createApp(deps({ credentials }))
    const plainHttp = await status(app, { host: 'scadbuddy.example', 'x-forwarded-proto': 'http' })
    expect(plainHttp).toMatchObject({ available: false, state: 'enabled', chat: 'refused' })
    expect(plainHttp.reason).toMatch(/HTTPS ingress.*public HTTPS address/)
    const byLanIp = await status(app, { host: '10.0.0.5', 'x-forwarded-proto': 'https' })
    expect(byLanIp).toMatchObject({ available: false, chat: 'refused', reason: expect.stringMatching(/public URL/) })
    // A setup problem is named first; the chat verdict still rides along.
    expect(await status(createApp(deps()), { host: '10.0.0.5', 'x-forwarded-proto': 'https' })).toMatchObject({
      reason: 'No Claude credential is configured yet.',
      chat: 'refused',
    })
  })

  it('has a reason for every non-enabled status', () => {
    expect(statusReason('enabled')).toBeUndefined()
    expect(statusReason('unavailable (stored credential is in an outdated format; save it again)')).toBe(
      'The agent service is unavailable: stored credential is in an outdated format; save it again.',
    )
  })
})

describe('the chat socket route', () => {
  it('refuses a handshake from another origin before upgrading, and plain GETs', async () => {
    const app = createApp(deps())
    const evil = await app.request('/api/v1/ai/chat', {
      headers: { host: 'scadbuddy.example', origin: 'https://evil.example', 'x-forwarded-proto': 'https', upgrade: 'websocket' },
    })
    expect(evil.status).toBe(403)
    const plain = await app.request('/api/v1/ai/chat', {
      headers: { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' },
    })
    expect(plain.status).toBe(426)
    const noDb = createApp(deps({ sessions: undefined }))
    const res = await noDb.request('/api/v1/ai/chat', {
      headers: { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', upgrade: 'websocket' },
    })
    expect(res.status).toBe(503)
  })
})

describe('the chat socket handlers', () => {
  /** The route's handlers on a socket that records what it is sent, with the manager's snapshot answered. */
  async function openSocket(log: (m: string) => void) {
    let events: WSEvents | undefined
    const capture = ((create: (c: unknown) => WSEvents | Promise<WSEvents>) => async (c: unknown) => {
      events = await create(c)
      return new Response()
    }) as unknown as UpgradeWebSocket
    const app = new Hono()
    const sessions = {
      snapshot: () => Promise.resolve({ v: 1, type: 'sessions.snapshot', sessions: [] }),
    } as unknown as SessionManager
    registerChatRoute(app, {
      sessions,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      upgradeWebSocket: capture,
      log,
    })
    const res = await app.request('/api/v1/ai/chat', {
      headers: { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', upgrade: 'websocket' },
    })
    expect(res.status).toBe(200)
    const sent: { type: string; code?: string; message?: string }[] = []
    let closed: [number | undefined, string | undefined] | undefined
    const ws = {
      raw: { bufferedAmount: 0 },
      send: (data: string) => sent.push(JSON.parse(data) as (typeof sent)[number]),
      close: (code?: number, reason?: string) => {
        closed = [code, reason]
      },
    } as unknown as WSContext
    events!.onOpen?.(new Event('open'), ws)
    await new Promise((r) => setTimeout(r, 20))
    expect(sent.map((f) => f.type)).toEqual(['sessions.snapshot'])
    return { events: events!, ws, sent, closed: () => closed }
  }

  it('runs a binary frame through the connection, so the queue cap covers it', async () => {
    const { events, ws, sent } = await openSocket(() => {})
    for (let i = 0; i < MAX_QUEUED_FRAMES + 10; i++) {
      events.onMessage?.(new MessageEvent('message', { data: new ArrayBuffer(4) }), ws)
    }
    await new Promise((r) => setTimeout(r, 20))
    const codes = sent.filter((f) => f.type === 'error').map((f) => f.code)
    expect(codes.filter((c) => c === 'invalid')).toHaveLength(MAX_QUEUED_FRAMES)
    expect(codes.filter((c) => c === 'busy')).toHaveLength(10)
    expect(sent.find((f) => f.code === 'invalid')?.message).toBe('frames must be JSON text')
  })

  it('logs a transport error, then closes the connection', async () => {
    const logged: string[] = []
    const { events, ws } = await openSocket((m) => logged.push(m))
    const evt = Object.assign(new Event('error'), { error: new Error('ECONNRESET: peer went away') })
    events.onError?.(evt, ws)
    expect(logged).toHaveLength(1)
    expect(logged[0]).toMatch(/^chat: socket error: Error: ECONNRESET: peer went away/)
    // Closed: a frame after the error is dropped rather than handled.
    events.onMessage?.(new MessageEvent('message', { data: 'not json' }), ws)
    await new Promise((r) => setTimeout(r, 20))
    expect(logged).toHaveLength(1)
  })
})

describe('body limits on the UI write routes', () => {
  // Every method throws: a refused body must never reach the manager or the approvals.
  const untouchable = new Proxy(
    {},
    {
      get: () => {
        throw new Error('the route ran')
      },
    },
  ) as SessionManager
  const UI_WRITE = {
    host: 'scadbuddy.example',
    origin: 'https://scadbuddy.example',
    'x-forwarded-proto': 'https',
    'content-type': 'application/json',
  }
  const big = JSON.stringify({ text: 'x'.repeat(JSON_BODY_MAX) })
  const paths = [
    '/api/v1/ai/sessions',
    '/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/messages',
    '/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/interrupt',
    '/api/v1/ai/approvals/a1/approve',
  ]

  it('answers 413 to an oversized body by Content-Length, unread', async () => {
    const app = createApp(deps({ sessions: untouchable, approvals: untouchable as never }))
    for (const path of paths) {
      const res = await app.request(path, {
        method: 'POST',
        headers: { ...UI_WRITE, 'content-length': String(big.length) },
        body: big,
      })
      expect(res.status, path).toBe(413)
      expect(await res.json()).toEqual({ detail: `request body is larger than ${JSON_BODY_MAX} bytes` })
    }
  })

  it('answers 413 to a chunked body with no Content-Length once it passes the cap', async () => {
    const app = createApp(deps({ sessions: untouchable, approvals: untouchable as never }))
    for (const path of paths) {
      const chunk = new TextEncoder().encode('x'.repeat(16 * 1024))
      let sent = 0
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          // An endless body: the limit must stop reading it.
          sent += chunk.length
          controller.enqueue(chunk)
        },
      })
      const res = await app.request(path, { method: 'POST', headers: UI_WRITE, body, duplex: 'half' } as RequestInit)
      expect(res.status, path).toBe(413)
      expect(sent).toBeLessThan(JSON_BODY_MAX * 2)
    }
  })

  it('refuses a real chunked upload over HTTP with 413', async () => {
    const app = createApp(deps({ sessions: untouchable, approvals: untouchable as never }))
    const server = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0 })
    await new Promise<void>((resolve) => server.once('listening', () => resolve()))
    const { port } = server.address() as AddressInfo
    try {
      const status = await new Promise<number>((resolve, reject) => {
        // No Content-Length: node sends Transfer-Encoding: chunked.
        const req = httpRequest(
          { host: '127.0.0.1', port, method: 'POST', path: '/api/v1/ai/sessions', headers: UI_WRITE },
          (res) => {
            res.resume()
            resolve(res.statusCode ?? 0)
          },
        )
        req.on('error', (err) => {
          // The server may close after answering while we are still writing.
          if ((err as NodeJS.ErrnoException).code !== 'EPIPE' && (err as NodeJS.ErrnoException).code !== 'ECONNRESET') reject(err)
        })
        for (let i = 0; i < Math.ceil(JSON_BODY_MAX / (16 * 1024)) + 2; i++) req.write('x'.repeat(16 * 1024))
        req.end()
      })
      expect(status).toBe(413)
    } finally {
      await new Promise<void>((resolve) => {
        if ('closeAllConnections' in server) server.closeAllConnections()
        server.close(() => resolve())
      })
    }
  })
})

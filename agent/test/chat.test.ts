import { randomBytes } from 'node:crypto'
import type { UpgradeWebSocket } from 'hono/ws'
import { describe, expect, it } from 'vitest'
import { type AiStatusView, type AppDeps, createApp, statusReason } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
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

async function status(app: ReturnType<typeof createApp>): Promise<AiStatusView> {
  const res = await app.request('/api/v1/ai/status')
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

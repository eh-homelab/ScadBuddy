import { randomBytes } from 'node:crypto'
import type { UpgradeWebSocket } from 'hono/ws'
import { describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { AuditEntry, AuditRepo } from '../src/audit/log.js'
import { type Credential, MAX_CREDENTIALS, TOO_MANY_MESSAGE } from '../src/credentials.js'
import { originPolicy } from '../src/http/origins.js'
import type { CredentialEntryView, CredentialListView, CredentialView } from '../src/routes/credentials.js'
import type { AiStatusView } from '../src/routes/status.js'
import { kekFromBase64, type KekStatus } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// The routes for several credentials (#1093), and the single-credential
// routes (#255) acting on the first of them. test/app.test.ts covers the
// single-credential routes as Settings uses them today.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const withKek: KekStatus = { ok: true, kek }
const KEY_A = 'sk-ant-api03-entries-first-aaaa'
const KEY_B = 'sk-ant-api03-entries-second-bbbb'
const GW = 'gw-entries-third-token-cccc'

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const JSON_UI = { ...UI, 'content-type': 'application/json' }
const BASE = '/api/v1/ai/credentials'

function setup(overrides: Partial<AppDeps> = {}) {
  const credentials = new MemoryCredentials()
  const tested: Credential[] = []
  const audited: AuditEntry[] = []
  const app = createApp({
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: withKek,
    credentials,
    testConnection: (credential) => {
      tested.push(credential)
      return Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' })
    },
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    resolveHost: () => Promise.resolve(['203.0.113.10']),
    testCooldownMs: 0,
    audit: { record: (e: AuditEntry) => Promise.resolve(void audited.push(e)) } as unknown as AuditRepo,
    sessions: {} as SessionManager,
    upgradeWebSocket: (() => () => Promise.resolve()) as unknown as UpgradeWebSocket,
    ...overrides,
  })
  const call = async <T>(method: string, path: string, body?: unknown, headers: Record<string, string> = JSON_UI) => {
    const res = await app.request(`${BASE}${path}`, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    return { status: res.status, body: (await res.json()) as T }
  }
  const create = async (body: unknown) => {
    const res = await call<CredentialEntryView>('POST', '/entries', body)
    expect(res.status).toBe(201)
    return res.body
  }
  return { app, credentials, tested, audited, call, create }
}

describe('/api/v1/ai/credentials/entries (#1093)', () => {
  it('lists nothing, then each created credential in order, never with its secret', async () => {
    const { call, create } = setup()
    expect((await call<CredentialListView>('GET', '/entries')).body).toEqual({
      credentials: [],
      usable_now: false,
      recovers_at: null,
      can_save: true,
      cannot_save_reason: null,
    })
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    expect(a).toEqual({
      id: a.id,
      priority: 0,
      kind: 'anthropic_api_key',
      base_url: null,
      last4: 'aaaa',
      updated_at: expect.any(String),
      usable: true,
      status: 'active',
      cooldown_until: null,
      last_error: null,
      last_error_at: null,
      last_used_at: null,
    })
    await create({ kind: 'gateway', base_url: 'https://llm.example/', secret: GW })
    const list = await call<CredentialListView>('GET', '/entries')
    expect(list.body.credentials.map((c) => [c.priority, c.kind, c.base_url])).toEqual([
      [0, 'anthropic_api_key', null],
      [1, 'gateway', 'https://llm.example'],
    ])
    expect(list.body.usable_now).toBe(true)
    expect(JSON.stringify(list.body)).not.toMatch(/entries-first|third-token/)
  })

  it('refuses a create without a secret, without JSON, from another origin, or with no key', async () => {
    const { call } = setup()
    expect((await call('POST', '/entries', { kind: 'anthropic_api_key' })).status).toBe(400)
    expect((await call('POST', '/entries', { kind: 'gateway', secret: GW })).status).toBe(400)
    expect((await call('POST', '/entries', { kind: 'anthropic_api_key', secret: KEY_A }, { ...UI, 'content-type': 'text/plain' })).status).toBe(415)
    expect(
      (await call('POST', '/entries', { kind: 'anthropic_api_key', secret: KEY_A }, { ...JSON_UI, origin: 'https://evil.example' })).status,
    ).toBe(403)
    const noKey = setup({ kek: { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' } })
    expect((await noKey.call('POST', '/entries', { kind: 'anthropic_api_key', secret: KEY_A })).status).toBe(503)
  })

  it('refuses a credential past the most that can be stored', async () => {
    const { call, credentials } = setup()
    for (let i = 0; i < MAX_CREDENTIALS; i++) await credentials.create({ kind: 'anthropic_api_key', secret: `${KEY_A}${i}` }, kek)
    const res = await call<{ detail: string }>('POST', '/entries', { kind: 'anthropic_api_key', secret: KEY_B })
    expect(res.status).toBe(409)
    expect(res.body.detail).toBe(TOO_MANY_MESSAGE)
    expect((await call<CredentialListView>('GET', '/entries')).body.credentials).toHaveLength(MAX_CREDENTIALS)
  })

  it('refuses a gateway host the egress rules refuse', async () => {
    const { call } = setup({ resolveHost: () => Promise.resolve(['169.254.169.254']) })
    const res = await call<{ detail: string }>('POST', '/entries', { kind: 'gateway', base_url: 'https://meta.example', secret: GW })
    expect(res.status).toBe(400)
    expect(res.body.detail).toMatch(/link-local/)
  })

  it('reorders, and the single-credential routes follow the first', async () => {
    const { call, create, tested } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    const ordered = await call<CredentialListView>('PUT', '/order', { ids: [b.id, a.id] })
    expect(ordered.status).toBe(200)
    expect(ordered.body.credentials.map((c) => [c.id, c.priority])).toEqual([
      [b.id, 0],
      [a.id, 1],
    ])
    expect((await call<CredentialView>('GET', '')).body).toMatchObject({ configured: true, last4: 'bbbb' })
    expect((await call('POST', '/test', undefined, UI)).status).toBe(200)
    expect(tested.at(-1)).toEqual({ kind: 'anthropic_api_key', secret: KEY_B })
    // The single-credential DELETE removes the first and answers with the one that moved up.
    expect((await call<CredentialView>('DELETE', '', undefined, UI)).body).toMatchObject({ configured: true, last4: 'aaaa' })
    expect((await call<CredentialView>('GET', '')).body).toMatchObject({ configured: true, last4: 'aaaa' })
    expect((await call<CredentialView>('DELETE', '', undefined, UI)).body).toMatchObject({ configured: false })
  })

  it('refuses an order that does not name every credential exactly once', async () => {
    const { call, create } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    for (const ids of [[a.id], [a.id, b.id, 'x'], [a.id, a.id]]) {
      expect((await call('PUT', '/order', { ids })).status).toBe(409)
    }
    expect((await call('PUT', '/order', { order: [a.id] })).status).toBe(400)
  })

  it('saves one credential by id, keeping its secret or taking a new one', async () => {
    const { call, create, credentials } = setup()
    const a = await create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW })
    await credentials.record(a.id, 0, { kind: 'disabled', reason: 'HTTP 401: authentication_failed' })
    const kept = await call<CredentialEntryView>('PUT', `/entries/${a.id}`, { kind: 'gateway', base_url: 'https://llm.example' })
    expect(kept.body).toMatchObject({ status: 'disabled', last_error: 'HTTP 401: authentication_failed' })
    const moved = await call('PUT', `/entries/${a.id}`, { kind: 'gateway', base_url: 'https://other.example' })
    expect(moved.status).toBe(409)
    const saved = await call<CredentialEntryView>('PUT', `/entries/${a.id}`, {
      kind: 'gateway',
      base_url: 'https://llm.example',
      secret: `${GW}-new`,
    })
    expect(saved.body).toMatchObject({ status: 'active', last4: '-new', last_error: null, last_error_at: null })
    expect((await call('PUT', '/entries/nope', { kind: 'anthropic_api_key', secret: KEY_A })).status).toBe(404)
  })

  it('resets a disabled or cooling credential', async () => {
    const { call, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    await credentials.record(a.id, 0, { kind: 'cooling_down', until: new Date(Date.now() + 60_000), reason: 'HTTP 429' })
    const reset = await call<CredentialEntryView>('POST', `/entries/${a.id}/reset`, undefined, UI)
    expect(reset.status).toBe(200)
    expect(reset.body).toMatchObject({ status: 'active', cooldown_until: null })
    expect((await call('POST', '/entries/nope/reset', undefined, UI)).status).toBe(404)
  })

  it('deletes one credential by id and answers with the rest', async () => {
    const { call, create } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    const res = await call<CredentialListView>('DELETE', `/entries/${a.id}`, undefined, UI)
    expect(res.body.credentials.map((c) => [c.id, c.priority])).toEqual([[b.id, 0]])
    expect((await call('DELETE', `/entries/${a.id}`, undefined, UI)).status).toBe(404)
  })

  it('tests one credential by id', async () => {
    const { call, create, tested } = setup()
    await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'gateway', base_url: 'https://llm.example', secret: GW })
    const res = await call<{ ok: boolean }>('POST', `/entries/${b.id}/test`, undefined, UI)
    expect(res.body.ok).toBe(true)
    expect(tested).toEqual([{ kind: 'gateway', baseUrl: 'https://llm.example', secret: GW }])
    expect((await call('POST', '/entries/nope/test', undefined, UI)).status).toBe(404)
  })

  it('shows each credential’s health and when the first is usable again', async () => {
    const { call, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    const until = new Date(Date.now() + 120_000)
    await credentials.record(a.id, 0, { kind: 'cooling_down', until, reason: 'API Error: Request rejected (429)' })
    await credentials.record(b.id, 0, { kind: 'disabled', reason: 'Credit balance is too low' })
    const list = (await call<CredentialListView>('GET', '/entries')).body
    expect(list.usable_now).toBe(false)
    expect(list.recovers_at).toBe(until.toISOString())
    expect(list.credentials).toMatchObject([
      { status: 'cooling_down', cooldown_until: until.toISOString(), last_error: 'API Error: Request rejected (429)' },
      { status: 'disabled', cooldown_until: null, last_error: 'Credit balance is too low', last_error_at: expect.any(String) },
    ])
  })

  it('audits every write', async () => {
    const { call, create, audited } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    await call('PUT', '/order', { ids: [a.id] })
    await call('PUT', `/entries/${a.id}`, { kind: 'anthropic_api_key' })
    await call('POST', `/entries/${a.id}/reset`, undefined, UI)
    await call('POST', `/entries/${a.id}/test`, undefined, UI)
    await call('DELETE', `/entries/${a.id}`, undefined, UI)
    await call('PUT', '', { kind: 'anthropic_api_key', secret: KEY_B })
    expect(audited.map((e) => [e.kind, e.action])).toEqual([
      ['credential', 'create'],
      ['credential', 'reorder'],
      ['credential', 'save'],
      ['credential', 'reset'],
      ['credential', 'delete'],
      ['credential', 'save'],
    ])
    expect(JSON.stringify(audited)).not.toMatch(/entries-(first|second)/)
  })
})

describe('GET /api/v1/ai/status with several credentials (#1093)', () => {
  const status = async (app: ReturnType<typeof createApp>) => {
    const res = await app.request('/api/v1/ai/status', { headers: { host: 'scadbuddy.example', 'x-forwarded-proto': 'https' } })
    return (await res.json()) as AiStatusView
  }

  it('is enabled while any credential is usable', async () => {
    const { app, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    await create({ kind: 'anthropic_api_key', secret: KEY_B })
    await credentials.record(a.id, 0, { kind: 'disabled', reason: 'HTTP 401' })
    expect(await status(app)).toEqual({ available: true, state: 'enabled', ai: 'enabled' })
  })

  it('says when the first is usable again when every credential is rate limited or disabled', async () => {
    const { app, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    const soon = new Date(Date.now() + 60_000)
    await credentials.record(a.id, 0, { kind: 'cooling_down', until: new Date(Date.now() + 600_000), reason: 'HTTP 429' })
    await credentials.record(b.id, 0, { kind: 'cooling_down', until: soon, reason: 'HTTP 429' })
    expect(await status(app)).toEqual({
      available: false,
      state: 'unavailable',
      ai: 'unavailable (every Claude credential is rate limited)',
      reason: `Every Claude credential is rate limited; the first is usable again at ${soon.toISOString()}.`,
      recovers_at: soon.toISOString(),
    })
    // Once that passes, it is usable again with nothing written.
    credentials.now = () => soon.getTime() + 1
    expect(await status(app)).toMatchObject({ available: true, ai: 'enabled' })
  })

  it('does not call a mix of disabled and rate-limited credentials all rate limited', async () => {
    const { app, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    const b = await create({ kind: 'anthropic_api_key', secret: KEY_B })
    const soon = new Date(Date.now() + 60_000)
    await credentials.record(a.id, 0, { kind: 'disabled', reason: 'HTTP 401' })
    await credentials.record(b.id, 0, { kind: 'cooling_down', until: soon, reason: 'HTTP 429' })
    expect(await status(app)).toEqual({
      available: false,
      state: 'unavailable',
      ai: 'unavailable (no Claude credential is usable now)',
      reason:
        'No Claude credential is usable now: some need attention in Settings (disabled, or sealed with another ' +
        `key-encryption key or an older format, which need saving again), and the first rate-limited one is usable again at ${soon.toISOString()}.`,
      recovers_at: soon.toISOString(),
    })
  })

  it('does not call credentials disabled when one cannot be opened with the mounted key, which a reset would not fix', async () => {
    const { app, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    await credentials.record(a.id, 0, { kind: 'disabled', reason: 'HTTP 401' })
    await credentials.create({ kind: 'anthropic_api_key', secret: KEY_B }, kekFromBase64(randomBytes(32).toString('base64')))
    const body = await status(app)
    expect(body).toMatchObject({ available: false, state: 'unavailable', ai: 'unavailable (no Claude credential is usable now)' })
    expect(body.recovers_at).toBeUndefined()
    expect(body.reason).toMatch(/another key-encryption key/)
  })

  it('says a person must act when every credential is disabled', async () => {
    const { app, create, credentials } = setup()
    const a = await create({ kind: 'anthropic_api_key', secret: KEY_A })
    await credentials.record(a.id, 0, { kind: 'disabled', reason: 'HTTP 401' })
    expect(await status(app)).toMatchObject({
      available: false,
      state: 'unavailable',
      ai: 'unavailable (every Claude credential is disabled)',
      reason: expect.stringMatching(/reset one/),
    })
  })
})

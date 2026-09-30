import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { auditedTokenStore, REFUSAL_WINDOW_MS, RefusalCoalescer, UI_ACTOR, UNVERIFIED_ACTOR } from '../src/audit/writes.js'
import type { Sql } from 'postgres'
import { cap } from '../src/audit/log.js'
import type { TokenStore } from '../src/auth/tokens.js'
import { SettingsStore } from '../src/credentials.js'
import { originPolicy } from '../src/http/origins.js'
import { kekFromBase64 } from '../src/secrets.js'
import { MemoryAudit } from './support/memoryAudit.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// /api/v1/ai/audit and the write auditing (#258) without a database: the
// guard, validation and wiring. The log itself against Postgres is
// test/audit.pg.test.ts.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
/** The UI through the TLS ingress (routes/guard.ts). */
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
/** A same-origin GET sends no Origin (Fetch standard). */
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

function app(overrides: Partial<AppDeps> = {}) {
  return createApp({
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    testPlugin: vi.fn(),
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    ...overrides,
  })
}

describe('GET /api/v1/ai/audit', () => {
  it('answers the UI with a page and the retention', async () => {
    const audit = new MemoryAudit()
    await audit.record({ kind: 'tool_call', action: 'get_readme', surface: 'harness', actor: UI_ACTOR, outcome: 'ok' })
    const res = await app({ audit }).request('/api/v1/ai/audit?kind=tool_call&outcome=ok&limit=10', { headers: UI_READ })
    expect(res.status).toBe(200)
    const body = (await res.json()) as { entries: { action: string }[]; next: string | null; retention_days: number }
    expect(body).toMatchObject({ next: null, retention_days: 90 })
    expect(body.entries.map((e) => e.action)).toEqual(['get_readme'])
    expect(audit.filters.at(-1)).toEqual({ kind: 'tool_call', outcome: 'ok', limit: 10 })
  })

  it.each([
    ['another origin', { ...UI_READ, origin: 'https://evil.example' }],
    ['a cross-site fetch', { ...UI_READ, 'sec-fetch-site': 'cross-site' }],
    ['plain HTTP through the ingress', { ...UI_READ, 'x-forwarded-proto': 'http' }],
    ['another host', { ...UI_READ, host: 'evil.example' }],
  ])('refuses %s (uiReadProblem)', async (_what, headers) => {
    const audit = new MemoryAudit()
    const res = await app({ audit }).request('/api/v1/ai/audit', { headers })
    expect(res.status).toBe(403)
    expect(audit.filters).toEqual([])
  })

  it.each([
    'kind=everything',
    'outcome=maybe',
    'session=not-a-uuid',
    'since=yesterday',
    'before=abc',
    'limit=0',
    'limit=1000',
    'unknown=1',
  ])('refuses a bad query (%s)', async (query) => {
    const res = await app({ audit: new MemoryAudit() }).request(`/api/v1/ai/audit?${query}`, { headers: UI_READ })
    expect(res.status).toBe(400)
  })

  it('answers 503 without a database', async () => {
    const res = await app({ database: undefined, credentials: undefined }).request('/api/v1/ai/audit', { headers: UI_READ })
    expect(res.status).toBe(503)
  })
})

describe('PUT /api/v1/ai/audit/settings', () => {
  const put = (a: ReturnType<typeof app>, body: unknown, headers: Record<string, string> = UI) =>
    a.request('/api/v1/ai/audit/settings', {
      method: 'PUT',
      headers: { ...headers, 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })

  it('stores the retention as the browser user', async () => {
    const audit = new MemoryAudit()
    const res = await put(app({ audit }), { retention_days: 30 })
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ retention_days: 30 })
    expect(audit.retentionWrites).toEqual([{ days: 30, context: { actor: UI_ACTOR, surface: 'http', clientIp: '10.0.0.7' } }])
  })

  it.each([{ retention_days: 0 }, { retention_days: 4000 }, { retention_days: 1.5 }, {}, { retention_days: 30, extra: 1 }])(
    'refuses %j',
    async (body) => {
      const audit = new MemoryAudit()
      expect((await put(app({ audit }), body)).status).toBe(400)
      expect(audit.retentionWrites).toEqual([])
    },
  )

  it('refuses a write that is not from the UI', async () => {
    const audit = new MemoryAudit()
    expect((await put(app({ audit }), { retention_days: 30 }, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
    expect(audit.retentionWrites).toEqual([])
  })
})

describe('credential and plugin writes are audited', () => {
  it('records a credential delete as the browser user, and a refused save as unverified', async () => {
    const audit = new MemoryAudit()
    const a = app({ audit })
    expect((await a.request('/api/v1/ai/credentials', { method: 'DELETE', headers: UI })).status).toBe(200)
    const refused = await a.request('/api/v1/ai/credentials', {
      method: 'PUT',
      headers: { host: UI.host, 'x-forwarded-proto': 'https', 'content-type': 'application/json' },
      body: JSON.stringify({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-SECRET-never-logged-0000' }),
    })
    expect(refused.status).toBe(403)
    // Reads are not writes.
    await a.request('/api/v1/ai/credentials', { headers: UI_READ })
    expect(audit.entries).toEqual([
      expect.objectContaining({ kind: 'credential', action: 'delete', surface: 'http', actor: UI_ACTOR, outcome: 'ok', clientIp: '10.0.0.7' }),
      expect.objectContaining({ kind: 'credential', action: 'save', actor: UNVERIFIED_ACTOR, outcome: 'refused' }),
    ])
    expect(JSON.stringify(audit.entries)).not.toContain('SECRET')
  })

  it('records plugin writes, but not connection tests', async () => {
    const audit = new MemoryAudit()
    const a = app({ audit })
    await a.request('/api/v1/ai/plugins/mem', { method: 'DELETE', headers: UI })
    await a.request('/api/v1/ai/plugins/mem/test', { method: 'POST', headers: UI })
    await a.request('/api/v1/ai/plugins', {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'mem', url: 'https://hs.example/mcp' }),
    })
    expect(audit.entries.map((e) => [e.kind, e.action, e.outcome])).toEqual([
      // No plugin registry here (no database behind it): 503, recorded as an error.
      ['plugin', 'delete', 'error'],
      ['plugin', 'create', 'error'],
    ])
    expect(audit.entries[0]?.detail).toBe('DELETE /api/v1/ai/plugins/mem → 503')
  })
})

describe('refused writes are coalesced, so they cannot grow the log without bound', () => {
  const refusedPut = (a: ReturnType<typeof app>) =>
    a.request('/api/v1/ai/credentials', { method: 'PUT', headers: { host: UI.host, 'x-forwarded-proto': 'https' } })

  it('records the first refusal per peer at once and the rest as one counted row per window', async () => {
    vi.useFakeTimers()
    try {
      const audit = new MemoryAudit()
      let peer = '10.0.0.7'
      const a = app({ audit, remoteAddress: () => peer })
      for (let i = 0; i < 50; i++) expect((await refusedPut(a)).status).toBe(403)
      peer = '10.0.0.8'
      await refusedPut(a)
      expect(audit.entries.map((e) => [e.action, e.outcome, e.clientIp])).toEqual([
        ['save', 'refused', '10.0.0.7'],
        ['save', 'refused', '10.0.0.8'],
      ])
      await vi.advanceTimersByTimeAsync(REFUSAL_WINDOW_MS)
      expect(audit.entries).toHaveLength(3)
      expect(audit.entries[2]).toMatchObject({ outcome: 'refused', actor: UNVERIFIED_ACTOR, clientIp: '10.0.0.7' })
      expect(audit.entries[2]?.detail).toMatch(/^49 more refused save requests from this peer/)
      // A new window starts over.
      peer = '10.0.0.7'
      await refusedPut(a)
      expect(audit.entries).toHaveLength(4)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shares one row among peers beyond the cap', async () => {
    vi.useFakeTimers()
    try {
      const audit = new MemoryAudit()
      const refusals = new RefusalCoalescer(audit, 1000, 2)
      const entry = (ip: string) => ({ kind: 'credential' as const, action: 'save', surface: 'http' as const, actor: UNVERIFIED_ACTOR, outcome: 'refused' as const, clientIp: ip })
      for (let i = 0; i < 100; i++) await refusals.record(entry(`10.1.0.${i}`))
      expect(audit.entries.map((e) => e.clientIp)).toEqual(['10.1.0.0', '10.1.0.1', undefined])
      await vi.advanceTimersByTimeAsync(1000)
      expect(audit.entries).toHaveLength(4)
      expect(audit.entries[3]?.detail).toMatch(/^97 more refused save requests from other peers/)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('MCP token writes', () => {
  it('records a failed mint once, with the client address', async () => {
    const audit = new MemoryAudit()
    const store: TokenStore = {
      verify: () => Promise.resolve(null),
      list: () => Promise.resolve([]),
      mint: () => Promise.reject(new Error('database down')),
      revoke: () => Promise.resolve(false),
      approvalGrant: () => Promise.resolve(false),
      liveTier: () => Promise.resolve(null),
    }
    const tokens = auditedTokenStore(store, audit)
    const a = app({ audit, tokens })
    const minted = await a.request('/api/v1/ai/mcp-tokens', {
      method: 'POST',
      headers: { ...UI, 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'Desk', tier: 'read' }),
    })
    expect(minted.status).toBeGreaterThanOrEqual(500)
    const revoked = await a.request('/api/v1/ai/mcp-tokens/nope', { method: 'DELETE', headers: UI })
    expect(revoked.status).toBe(404)
    expect(audit.entries.map((e) => [e.kind, e.action, e.outcome, e.clientIp])).toEqual([
      ['token', 'mint', 'error', '10.0.0.7'],
      ['token', 'revoke', 'error', '10.0.0.7'],
    ])
  })
})

describe('cap', () => {
  it('cuts between code points, so no lone surrogate reaches a row', () => {
    expect(cap('abc', 3)).toBe('abc')
    expect(cap('abcd', 3)).toBe('ab…')
    // 'ab😀x' is five UTF-16 units; a cut after three would keep half the emoji.
    expect(cap('ab😀x', 4)).toBe('ab…')
    expect(cap('a😀', 3)).toBe('a😀')
    expect(cap('a😀bc', 4)).toBe('a😀…')
  })
})

describe('SettingsStore', () => {
  /** A `sql` whose transactions run and write nothing. */
  const quietSql = () =>
    Object.assign(() => Promise.resolve([]), {
      json: (value: unknown) => value,
      begin: (fn: (tx: unknown) => Promise<unknown>) => fn(Object.assign(() => Promise.resolve([]), { json: (v: unknown) => v })),
    }) as unknown as Sql

  it('audits each key setMany writes, as whoever the context names (#831)', async () => {
    const audit = new MemoryAudit()
    const settings = new SettingsStore(quietSql(), audit)
    const context = { actor: UI_ACTOR, surface: 'http' as const, clientIp: '10.0.0.7' }
    expect(await settings.setMany({ mcp_auth_mode: 'disabled', mcp_anonymous_cap: 'read' }, async () => true, context)).toBe(true)
    expect(audit.entries).toEqual([
      expect.objectContaining({ kind: 'settings', action: 'mcp_auth_mode', surface: 'http', actor: UI_ACTOR, clientIp: '10.0.0.7', outcome: 'ok' }),
      expect.objectContaining({ kind: 'settings', action: 'mcp_anonymous_cap', surface: 'http', actor: UI_ACTOR, outcome: 'ok' }),
    ])
  })

  it('records nothing when the setMany check refuses the write', async () => {
    const audit = new MemoryAudit()
    const settings = new SettingsStore(quietSql(), audit)
    expect(await settings.setMany({ mcp_auth_mode: 'disabled' }, async () => false)).toBe(false)
    expect(audit.entries).toEqual([])
  })

  it('audits a failed write whose rejection is not an Error, and rethrows it as it was', async () => {
    const audit = new MemoryAudit()
    const sql = Object.assign(() => Promise.reject('connection reset'), { json: (value: unknown) => value }) as unknown as Sql
    const settings = new SettingsStore(sql, audit)
    await expect(settings.set('model', 'claude-sonnet-4-5')).rejects.toBe('connection reset')
    expect(audit.entries).toEqual([
      expect.objectContaining({
        kind: 'settings',
        action: 'model',
        outcome: 'error',
        detail: 'model = "claude-sonnet-4-5" (failed: connection reset)',
      }),
    ])
  })
})

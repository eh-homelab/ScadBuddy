import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import { testAndList } from '../src/routes/plugins.js'
import type { StoredTool, ToolListings } from '../src/access/listings.js'
import type { PluginRepo, RemotePlugin } from '../src/plugins/registry.js'
import type { PluginTest } from '../src/plugins/testConnection.js'
import { kekFromBase64 } from '../src/secrets.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// The guard on the plugin routes (#297; src/routes/guard.ts), without a
// database: a refused request must never reach the registry, so every repo
// method here fails the test if called. The registry behind the guard is
// test/plugins.pg.test.ts.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function untouchable(): PluginRepo & { calls: string[] } {
  const calls: string[] = []
  const fail = (name: string) => () => {
    calls.push(name)
    return Promise.reject(new Error(`${name} must not be called`))
  }
  return {
    calls,
    list: fail('list'),
    get: fail('get'),
    create: fail('create'),
    update: fail('update'),
    setToolAccess: fail('setToolAccess'),
    delete: fail('delete'),
    reveal: fail('reveal'),
  }
}

function app(plugins: PluginRepo | undefined, overrides: Partial<AppDeps> = {}) {
  return createApp({
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    plugins,
    testPlugin: vi.fn(),
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    resolveHost: () => Promise.resolve(['203.0.113.10']),
    ...overrides,
  })
}

const WRITES: [string, string, unknown][] = [
  ['POST', '/api/v1/ai/plugins', { name: 'mem', url: 'https://hs.example/mcp' }],
  ['PATCH', '/api/v1/ai/plugins/mem', { enabled: true }],
  ['DELETE', '/api/v1/ai/plugins/mem', undefined],
  ['POST', '/api/v1/ai/plugins/mem/test', undefined],
]

describe('plugin write routes are guarded', () => {
  it.each(WRITES)('%s %s without an Origin is refused before the registry', async (method, path, body) => {
    const repo = untouchable()
    const { host, 'x-forwarded-proto': proto } = UI
    const res = await app(repo).request(path, {
      method,
      headers: { host, 'x-forwarded-proto': proto, 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    expect(res.status).toBe(403)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/^plugin changes must come from the ScadBuddy UI/)
    expect(repo.calls).toEqual([])
  })

  it.each(WRITES)('%s %s from another origin is refused', async (method, path, body) => {
    const repo = untouchable()
    const res = await app(repo).request(path, {
      method,
      headers: { ...UI, origin: 'https://evil.example', 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    expect(res.status).toBe(403)
    expect(repo.calls).toEqual([])
  })

  it.each(WRITES)('%s %s over plain HTTP from a LAN peer is refused', async (method, path, body) => {
    const repo = untouchable()
    const res = await app(repo, { remoteAddress: () => '192.168.1.50' }).request(path, {
      method,
      headers: { ...UI, 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    expect(res.status).toBe(403)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/HTTPS ingress/)
    expect(repo.calls).toEqual([])
  })

  it('refuses a form-encoded POST or PATCH body (a cross-origin form cannot send JSON)', async () => {
    const repo = untouchable()
    for (const [method, path] of [
      ['POST', '/api/v1/ai/plugins'],
      ['PATCH', '/api/v1/ai/plugins/mem'],
    ] as const) {
      const res = await app(repo).request(path, {
        method,
        headers: { ...UI, 'content-type': 'application/x-www-form-urlencoded' },
        body: 'name=mem&url=https://hs.example/mcp',
      })
      expect(res.status).toBe(403)
    }
    expect(repo.calls).toEqual([])
  })

  it('answers 503 on a read without a database', async () => {
    const none = await app(undefined).request('/api/v1/ai/plugins', { headers: SAME_ORIGIN_GET })
    expect(none.status).toBe(503)
  })
})

/** A same-origin fetch() GET through the ingress: browsers send no Origin on it. */
const SAME_ORIGIN_GET = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

describe('plugin reads are guarded (uiReadProblem)', () => {
  const READS = ['/api/v1/ai/plugins', '/api/v1/ai/plugins/mem']

  function readable() {
    const list = vi.fn(() => Promise.resolve([]))
    const get = vi.fn(() => Promise.resolve(undefined))
    return { repo: { ...untouchable(), list, get }, list, get }
  }

  it.each(READS)('allows a same-origin GET of %s through the ingress, with or without Origin', async (path) => {
    const { repo } = readable()
    for (const headers of [SAME_ORIGIN_GET, { ...SAME_ORIGIN_GET, origin: 'https://scadbuddy.example' }]) {
      const res = await app(repo).request(path, { headers })
      expect([200, 404]).toContain(res.status) // 404: no plugin "mem" in the stub
    }
    expect(repo.calls).toEqual([])
  })

  it('allows a loopback GET (local development), and a navigation (Sec-Fetch-Site: none)', async () => {
    const { repo, list } = readable()
    const loopback = await app(repo, { remoteAddress: () => '127.0.0.1' }).request('/api/v1/ai/plugins', {
      headers: { host: '127.0.0.1:8081' },
    })
    expect(loopback.status).toBe(200)
    const typed = await app(repo).request('/api/v1/ai/plugins', {
      headers: { ...SAME_ORIGIN_GET, 'sec-fetch-site': 'none' },
    })
    expect(typed.status).toBe(200)
    expect(list).toHaveBeenCalledTimes(2)
  })

  it.each(READS)('refuses a cross-site GET of %s before the registry', async (path) => {
    const cases: Record<string, string>[] = [
      // a page on another site, no Origin (e.g. an <img> or no-cors fetch)
      { ...SAME_ORIGIN_GET, 'sec-fetch-site': 'cross-site' },
      { ...SAME_ORIGIN_GET, 'sec-fetch-site': 'same-site' },
      // a CORS fetch from another origin
      { ...SAME_ORIGIN_GET, origin: 'https://evil.example', 'sec-fetch-site': 'cross-site' },
      // DNS rebinding: the attacker's name as Host
      { host: 'evil.example', 'x-forwarded-proto': 'https' },
      // plain HTTP through the proxy
      { host: 'scadbuddy.example', 'x-forwarded-proto': 'http' },
    ]
    for (const headers of cases) {
      const { repo, list, get } = readable()
      const res = await app(repo).request(path, { headers })
      expect(res.status, JSON.stringify(headers)).toBe(403)
      expect(((await res.json()) as { detail: string }).detail).toMatch(/^plugin reads must/)
      expect(list).not.toHaveBeenCalled()
      expect(get).not.toHaveBeenCalled()
    }
  })

  it('refuses a GET from a LAN peer that is not the trusted proxy', async () => {
    const { repo, list } = readable()
    const res = await app(repo, { remoteAddress: () => '192.168.1.50' }).request('/api/v1/ai/plugins', {
      headers: SAME_ORIGIN_GET,
    })
    expect(res.status).toBe(403)
    expect(list).not.toHaveBeenCalled()
  })
})

describe('the connection test stores its listing (#2171)', () => {
  const plugin: RemotePlugin = { name: 'mem', url: 'https://hs.example/mcp', toolTiers: {}, disabledTools: [] }
  const stored: StoredTool[] = [{ raw: { name: 'recall', x_new: 1 }, omitted: [] }]
  const result = (ok: boolean): PluginTest => ({
    ok,
    detail: ok ? 'connected; 1 tool(s)' : 'the MCP endpoint answered HTTP 500',
    duration_ms: 1,
    server: null,
    tools: [],
    truncated: false,
    stored: ok ? stored : [],
  })
  function setup(testPlugin: AppDeps['testPlugin'], overrides: Partial<AppDeps> = {}) {
    const repo = { ...untouchable(), reveal: vi.fn(() => Promise.resolve(plugin)) }
    const put = vi.fn((_url: string, _tools: readonly StoredTool[], _options: { truncated?: boolean }) => Promise.resolve(true))
    const exclusive = vi.fn(async <T>(_name: string, fn: (p: typeof put) => Promise<T>) => ({ value: await fn(put) }))
    const listings = { exclusive } as unknown as ToolListings
    return { put, repo, listings, a: app(repo, { testPlugin, listings, ...overrides }) }
  }
  const test = (a: ReturnType<typeof app>) => a.request('/api/v1/ai/plugins/mem/test', { method: 'POST', headers: UI })

  it('a good connection test stores its listing', async () => {
    const { put, a } = setup(() => Promise.resolve(result(true)))
    const res = await test(a)
    expect(res.status).toBe(200)
    expect(put).toHaveBeenCalledWith(plugin.url, stored, { truncated: false })
    expect(await res.json()).not.toHaveProperty('stored')
  })

  it('a truncated test stores its listing as incomplete (#2171 re-review N1)', async () => {
    const { put, a } = setup(() => Promise.resolve({ ...result(true), truncated: true }))
    expect((await test(a)).status).toBe(200)
    expect(put).toHaveBeenCalledWith(plugin.url, stored, { truncated: true })
  })

  it('a failed connection test stores nothing', async () => {
    const { put, a } = setup(() => Promise.resolve(result(false)))
    const res = await test(a)
    expect(res.status).toBe(200)
    expect(((await res.json()) as PluginTest).ok).toBe(false)
    expect(put).not.toHaveBeenCalled()
  })

  it('a second test of one plugin while one runs answers 429', async () => {
    let finish: ((t: PluginTest) => void) | undefined
    let calls = 0
    const { a } = setup(() =>
      calls++ === 0 ? new Promise<PluginTest>((resolve) => (finish = resolve)) : Promise.resolve(result(true)),
    )
    const first = test(a)
    await vi.waitFor(() => expect(finish).toBeDefined())
    const second = await test(a)
    expect(second.status).toBe(429)
    expect(second.headers.get('Retry-After')).toBe('10')
    finish!(result(true))
    expect((await first).status).toBe(200)
    expect((await test(a)).status).toBe(200)
  })

  it("a test while another replica holds the plugin's test lock answers 429 and connects nowhere (#4994)", async () => {
    const testPlugin = vi.fn(() => Promise.resolve(result(true)))
    const { a, listings } = setup(testPlugin)
    vi.mocked(listings.exclusive).mockResolvedValueOnce('busy')
    const res = await test(a)
    expect(res.status).toBe(429)
    expect(res.headers.get('Retry-After')).toBe('10')
    expect(testPlugin).not.toHaveBeenCalled()
    expect((await test(a)).status).toBe(200)
  })

  it('a test while this replica runs its cap of tests answers 429 (#4994)', async () => {
    const testPlugin = vi.fn(() => Promise.resolve(result(true)))
    const { a, listings } = setup(testPlugin)
    vi.mocked(listings.exclusive).mockResolvedValueOnce('full')
    const res = await test(a)
    expect(res.status).toBe(429)
    expect(res.headers.get('Retry-After')).toBe('10')
    expect(((await res.json()) as { detail: string }).detail).toMatch(/too many connection tests/)
    expect(testPlugin).not.toHaveBeenCalled()
  })

  it("a test whose lock client cannot reach Postgres answers 503, the ready check's answer, and connects nowhere (#4994)", async () => {
    const testPlugin = vi.fn(() => Promise.resolve(result(true)))
    const { a, listings } = setup(testPlugin)
    vi.mocked(listings.exclusive).mockResolvedValueOnce('unreachable')
    const res = await test(a)
    expect(res.status).toBe(503)
    expect(res.headers.get('Retry-After')).toBeNull()
    expect(((await res.json()) as { detail: string }).detail).toMatch(/AI database is unreachable/)
    expect(testPlugin).not.toHaveBeenCalled()
  })

  it('one test at a time per plugin, whichever caller started it (#2171 review M3)', async () => {
    let finish: ((t: PluginTest) => void) | undefined
    const testPlugin = vi.fn(() => new Promise<PluginTest>((resolve) => (finish = resolve)))
    const { a, repo, listings } = setup(testPlugin)
    const first = test(a)
    await vi.waitFor(() => expect(finish).toBeDefined())
    const direct = await testAndList({ plugins: repo, kek: { ok: true, kek }, testPlugin, listings, ready: () => Promise.resolve(true) }, 'mem')
    expect(direct.status).toBe(429)
    finish!(result(true))
    expect((await first).status).toBe(200)
  })
})

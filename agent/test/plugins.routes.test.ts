import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import type { PluginRepo } from '../src/plugins/registry.js'
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

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

  it('lets reads through without the write guard, and answers 503 without a database', async () => {
    const list = vi.fn(() => Promise.resolve([]))
    const repo = { ...untouchable(), list }
    const res = await app(repo).request('/api/v1/ai/plugins')
    expect(res.status).toBe(200)
    expect(list).toHaveBeenCalledOnce()
    const none = await app(undefined).request('/api/v1/ai/plugins')
    expect(none.status).toBe(503)
  })
})

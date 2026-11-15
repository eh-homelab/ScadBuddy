import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { afterEach, describe, expect, it } from 'vitest'
import { type AgentApp, createApp } from '../src/app.js'
import { CrudPathError } from '../src/api/crudStores.js'
import { originPolicy } from '../src/http/origins.js'
import { CrudOAuthRows, PluginOAuth, refreshLease } from '../src/plugins/oauthStore.js'
import { CrudPluginStore, PluginError } from '../src/plugins/registry.js'
import type { PluginView } from '../src/routes/plugins.js'
import { SealError } from '../src/secrets.js'
import { type FakeAuthServer, startFakeAuthServer, startProtectedMcp } from './support/fakeOAuth.js'
import type { FakeMcp } from './support/fakeMcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// The remote plugins and their sign-ins through the API's CRUD routes (#5299 step 5, area
// `plugins`), against a fake API that keeps them in memory as backend
// api/crud_agent_plugins.py does (the backend's tests/api/test_crud_agent_plugins.py covers
// the routes, the sealing and the lease on Postgres). Nothing here holds a KEK or SQL: the
// app is made with no key and the stores with no database.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const CALLBACK = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' }
const NO_SQL = new Proxy({}, { get: () => { throw new Error('no SQL in this test') } }) as Sql

type Plugin = {
  name: string
  url: string
  auth_header: string | null
  secret: string | null
  access: Map<string, string>
}
type Grant = Record<string, unknown> & { plugin: string; plugin_url: string; grant_id: string; secret: Record<string, string>; status: string }

/** The API's side, in memory: what each route answers, with no SQL and no key here. */
class FakeApi {
  seen: string[] = []
  plugins = new Map<string, Plugin>()
  flows = new Map<string, Record<string, unknown>>()
  grants = new Map<string, Grant>()
  leases = new Map<string, string>()
  /** Runs before a refresh is stored: a test takes the lease away there. */
  beforeRefreshed?: () => void
  evictions = 0

  fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : {}
    const path = decodeURIComponent(url.pathname.slice(PREFIX.length))
    this.seen.push(`${method} ${path}`)
    return this.#route(method, path, url.searchParams, body)
  }

  #json(value: unknown, status = 200): Response {
    return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } })
  }

  #row(p: Plugin) {
    const at = '2026-11-15T10:00:00.123456Z'
    const grant = this.grants.get(p.name)
    return {
      name: p.name,
      kind: 'remote_mcp',
      url: p.url,
      auth_header: p.auth_header,
      kek_id: p.secret ? 'api-kek' : null,
      last4: p.secret ? p.secret.slice(-4) : null,
      created_at: at,
      updated_at: at,
      access_rows: [...p.access].map(([item, access]) => ({ source: `remote:${p.name}`, item, access, updated_by: 'system' })),
      oauth: grant
        ? {
            status: grant.status,
            last_error: grant.last_error ?? null,
            issuer: grant.issuer,
            scope: grant.scope,
            registration: grant.registration,
            has_refresh: grant.has_refresh,
            access_expires_at: grant.access_expires_at,
            connected_at: at,
            grant_id: grant.grant_id,
            plugin_url: grant.plugin_url,
          }
        : null,
      opens: true,
    }
  }

  #opened(p: Plugin) {
    return { ...this.#row(p), header: p.secret && p.auth_header ? { name: p.auth_header, value: p.secret } : null, seal_error: null }
  }

  #route(method: string, path: string, q: URLSearchParams, b: Record<string, unknown>): Response {
    let m: RegExpMatchArray | null
    if ((m = path.match(/^\/leases\/(.+)$/))) {
      const key = m[1]!
      if (method === 'POST') {
        const held = this.leases.get(key)
        if (held && held !== b.token) return this.#json({ detail: 'busy', reason: 'busy' }, 409)
        this.leases.set(key, b.token as string)
        return this.#json({ token: b.token, expires_at: '2026-11-15T10:02:00Z' })
      }
      if (method === 'PUT') {
        return this.leases.get(key) === b.token ? this.#json({ token: b.token, expires_at: 'x' }) : this.#json({ detail: 'lost', reason: 'lease-lost' }, 409)
      }
      if (this.leases.get(key) === q.get('token')) this.leases.delete(key)
      return new Response(null, { status: 204, headers: { 'x-scadbuddy-crud': '1' } })
    }
    if (method === 'GET' && path === '/plugins') return this.#json({ plugins: [...this.plugins.values()].map((p) => this.#row(p)) })
    if (method === 'POST' && path === '/plugins') {
      if (this.plugins.has(b.name as string)) return this.#json({ detail: `a plugin named "${String(b.name)}" already exists`, reason: 'plugin-refused' }, 409)
      const access = new Map<string, string>([['*', 'off'], ...Object.entries(b.tools as Record<string, string>)])
      const p: Plugin = { name: b.name as string, url: b.url as string, auth_header: (b.auth_header as string) ?? null, secret: (b.secret as string) ?? null, access }
      this.plugins.set(p.name, p)
      this.evictions++
      return this.#json(this.#row(p), 201)
    }
    if (method === 'POST' && path === '/plugins/open-enabled') {
      return this.#json({ plugins: [...this.plugins.values()].filter((p) => p.access.get('*') === 'on').map((p) => this.#opened(p)) })
    }
    if ((m = path.match(/^\/plugins\/([^/]+)(?:\/([a-z-]+))?$/))) {
      const p = this.plugins.get(m[1]!)
      const verb = m[2]
      if (method === 'GET' && !verb) return this.#json({ plugin: p ? this.#row(p) : null })
      if (verb === 'reveal') return this.#json({ plugin: p ? this.#opened(p) : null })
      if (!p) return this.#json({ detail: `no plugin named "${m[1]!}"`, reason: 'plugin-refused' }, 404)
      if (verb === 'update') {
        if (b.url !== undefined && b.url !== p.url && p.secret && b.secret === undefined) {
          return this.#json({ detail: 'changing url or auth_header needs the secret again', reason: 'plugin-refused' }, 409)
        }
        if (b.url !== undefined) p.url = b.url as string
        if (b.enabled !== undefined) p.access.set('*', b.enabled ? 'on' : 'off')
        this.evictions++
        return this.#json(this.#row(p))
      }
      if (verb === 'delete') {
        this.plugins.delete(p.name)
        return this.#json({ done: true })
      }
    }
    if (method === 'POST' && path === '/plugin-oauth/flows') {
      if (!this.plugins.has(b.plugin as string)) return this.#json({ detail: 'gone', reason: 'plugin-gone' }, 409)
      this.flows.set(b.state_hash as string, { ...b, binding_hash: null })
      return this.#json({ done: true }, 201)
    }
    if (method === 'POST' && path === '/plugin-oauth/flows/authorize') {
      const flow = [...this.flows.values()].find((f) => f.ticket_hash === b.ticket_hash && f.binding_hash === null)
      if (!flow) return this.#json({ flow: null })
      flow.ticket_hash = null
      flow.binding_hash = b.binding_hash
      return this.#json({ flow: { plugin: flow.plugin, plugin_url: flow.plugin_url, meta: flow.meta, secret: flow.secret, seal_error: null } })
    }
    if (method === 'POST' && path === '/plugin-oauth/flows/take') {
      const flow = this.flows.get(b.state_hash as string)
      if (!flow || flow.binding_hash !== b.binding_hash) return this.#json({ flow: null, held: flow !== undefined })
      this.flows.delete(b.state_hash as string)
      return this.#json({ flow: { plugin: flow.plugin, plugin_url: flow.plugin_url, meta: flow.meta, secret: flow.secret, seal_error: null, created_by: flow.created_by, expired: false }, held: false })
    }
    if (method === 'POST' && path === '/plugin-oauth/grants') {
      const p = this.plugins.get(b.plugin as string)
      if (!p || p.url !== b.plugin_url) return this.#json({ stored: false })
      this.grants.set(p.name, { ...b, status: 'connected', grant_id: randomUUID() } as Grant)
      this.evictions++
      return this.#json({ stored: true })
    }
    if (method === 'POST' && path === '/plugin-oauth/stored-secrets') return this.#json({ secrets: [...this.grants.values()].map((g) => g.secret) })
    if ((m = path.match(/^\/plugin-oauth\/grants\/([^/]+)\/([a-z]+)$/))) {
      const grant = this.grants.get(m[1]!)
      if (m[2] === 'open') {
        if (!grant) return this.#json({ grant: null, fail: 'it is not signed in' })
        if (grant.status === 'error') return this.#json({ grant: null, fail: grant.last_error })
        if (grant.grant_id !== b.grant) return this.#json({ grant: null, fail: 'it was signed in again or out since this run loaded it' })
        return this.#json({ grant, fail: null })
      }
      if (m[2] === 'refreshed') {
        this.beforeRefreshed?.()
        if (this.leases.get(refreshLease(m[1]!)) !== b.lease_token) return this.#json({ detail: 'lost', reason: 'lease-lost' }, 409)
        if (!grant || grant.grant_id !== b.grant) return this.#json({ stored: false })
        Object.assign(grant, { secret: b.secret, has_refresh: b.has_refresh, access_expires_at: b.access_expires_at })
        return this.#json({ stored: true })
      }
      if (m[2] === 'mark') {
        if (grant) Object.assign(grant, { status: 'error', last_error: b.reason })
        return this.#json({ done: grant !== undefined })
      }
      if (m[2] === 'forget') return this.#json({ done: this.grants.delete(m[1]!) })
    }
    return this.#json({ detail: `no fake route ${method} ${path}` }, 404)
  }
}

describe('the remote plugins and their sign-ins through CRUD routes (#5299 step 5)', () => {
  let auth: FakeAuthServer | undefined
  let mcp: FakeMcp | undefined
  afterEach(async () => {
    await mcp?.close()
    await auth?.close()
  })

  function stores(api: FakeApi, now: () => Date = () => new Date()) {
    const target = { baseUrl: BASE, fetch: api.fetch }
    const oauth = new PluginOAuth(NO_SQL, { now, remote: { crud: new CrudOAuthRows(target), use: () => true } })
    return { oauth, plugins: new CrudPluginStore(target, { oauth }) }
  }

  it('registers a plugin with a secret, reveals and loads it, with no KEK here', async () => {
    const api = new FakeApi()
    const { plugins } = stores(api)
    const made = await plugins.create({ name: 'docs', url: 'https://mcp.example/mcp/', auth_header: 'X-Key', secret: 'sk-secret-value-1234' }, undefined)
    expect(made).toMatchObject({ name: 'docs', enabled: false, auth_header: 'X-Key', secret_last4: '1234', opens: true })
    // Only checked values reach the API: the URL normalised, the tool rows built.
    expect(api.seen).toEqual(['POST /plugins'])
    await expect(plugins.create({ name: 'Bad_Name', url: 'https://x.example/' }, undefined)).rejects.toThrow(PluginError)
    await expect(plugins.create({ name: 'docs', url: 'https://x.example/' }, undefined)).rejects.toMatchObject({ status: 409 })
    expect((await plugins.reveal('docs', undefined))?.header).toEqual({ name: 'X-Key', value: 'sk-secret-value-1234' })
    expect(await plugins.enabled(undefined)).toEqual({ plugins: [], problems: [] })
    await plugins.update('docs', { enabled: true }, undefined)
    const loaded = await plugins.enabled(undefined)
    expect(loaded.plugins.map((p) => p.header?.value)).toEqual(['sk-secret-value-1234'])
    await expect(plugins.update('docs', { url: 'https://other.example/' }, undefined)).rejects.toMatchObject({ status: 409 })
    expect(await plugins.get('Not A Name')).toBeUndefined()
    expect(await plugins.delete('docs')).toBe(true)
  })

  it("turns a row the API cannot open into the SealError the SQL store throws", async () => {
    const api = new FakeApi()
    const { plugins } = stores(api)
    await plugins.create({ name: 'docs', url: 'https://mcp.example/mcp/', secret: 'sk-secret-value-1234' }, undefined)
    const opened = api.fetch
    api.fetch = async (input, init) => {
      const res = await opened(input, init)
      if (!String(input).endsWith('/reveal')) return res
      const body = (await res.json()) as { plugin: Record<string, unknown> }
      return new Response(JSON.stringify({ plugin: { ...body.plugin, header: null, seal_error: 'wrong key' } }), {
        headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' },
      })
    }
    await expect(stores(api).plugins.reveal('docs', undefined)).rejects.toThrow(SealError)
  })

  it('signs a plugin in, sends its token and renews it under the refresh lease, with no KEK here', async () => {
    const api = new FakeApi()
    let clock = new Date('2026-11-01T12:00:00Z')
    const { oauth, plugins } = stores(api, () => clock)
    auth = await startFakeAuthServer({ registration: true, expiresIn: 3600 })
    mcp = await startProtectedMcp(auth)
    const app: AgentApp = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'not set' },
      credentials: new MemoryCredentials(),
      plugins,
      pluginOAuth: oauth,
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    await plugins.create({ name: 'github', url: mcp.url }, undefined)
    await plugins.update('github', { enabled: true }, undefined)

    const start = await app.request('/api/v1/ai/plugins/github/oauth/start', { method: 'POST', headers: { ...UI, 'content-type': 'application/json' }, body: '{}' })
    expect(start.status, await start.clone().text()).toBe(200)
    const { authorize_url } = (await start.json()) as { authorize_url: string }
    const opened = await app.request(authorize_url, { headers: READ })
    expect(opened.status).toBe(302)
    const cookie = (opened.headers.get('set-cookie') ?? '').split(';')[0] ?? ''
    const answered = await fetch(opened.headers.get('location') ?? '', { redirect: 'manual' })
    const back = new URL(answered.headers.get('location') ?? '')
    const done = await app.request(`${back.pathname}${back.search}`, { headers: { ...CALLBACK, cookie } })
    expect(done.status, await done.clone().text()).toBe(200)
    // The tokens went to the API whole, for it to seal; the flow is gone.
    expect(api.flows.size).toBe(0)
    const grant = api.grants.get('github')!
    expect(grant.secret).toMatchObject({ access_token: auth.issued[0], refresh_token: expect.any(String) as unknown })

    const view = (await (await app.request('/api/v1/ai/plugins/github', { headers: READ })).json()) as PluginView
    expect(view).toMatchObject({ usable: true, oauth: { status: 'connected', refreshes: true } })

    const source = oauth.source('github', mcp.url, grant.grant_id, undefined)
    expect((await source.header()).value).toBe(`Bearer ${auth.issued[0]}`)
    // An hour on: renewed under the lease, kept, and the lease released.
    clock = new Date(clock.getTime() + 3600_000)
    const renewed = await source.header()
    expect(renewed.value).toBe(`Bearer ${auth.issued[1]}`)
    expect(api.seen).toContain(`POST /leases/${refreshLease('github')}`)
    expect(api.leases.size).toBe(0)
    expect(api.grants.get('github')!.secret.access_token).toBe(auth.issued[1])
    expect(await oauth.storedSecrets(undefined)).toContain(auth.issued[1])

    // Another replica holds the lease: this one waits on the API, then refuses, sends nothing.
    clock = new Date(clock.getTime() + 3600_000)
    const other = stores(api, () => clock).oauth
    api.leases.set(refreshLease('github'), randomUUID())
    await expect(other.source('github', mcp.url, grant.grant_id, undefined).header()).rejects.toThrow(/another replica is renewing/)
    expect(auth.tokenRequests).toHaveLength(2)
    api.leases.clear()

    // The lease lost while the token endpoint answered: the rotated refresh token is not kept,
    // and the stored one is spent, so the answer says a new sign-in may be needed.
    api.beforeRefreshed = () => api.leases.set(refreshLease('github'), randomUUID())
    await expect(other.source('github', mcp.url, grant.grant_id, undefined).header()).rejects.toThrow(/a new sign-in may be needed/)
    api.beforeRefreshed = undefined
    api.leases.clear()

    // A revoked refresh marks the grant on the API: sign in again.
    auth.revokeAll()
    await expect(other.source('github', mcp.url, grant.grant_id, undefined).header()).rejects.toThrow(/sign in again/)
    expect(api.grants.get('github')).toMatchObject({ status: 'error' })
    expect(await oauth.forget('github')).toBe(true)
  })
  it('refuses a plugin name outside the rule before any call: no path reaches another route', async () => {
    const api = new FakeApi()
    const { oauth, plugins } = stores(api)
    for (const bad of ['../x', '..', 'a/b', '%2e%2e']) {
      expect(await plugins.get(bad)).toBeUndefined()
      expect(await plugins.reveal(bad, undefined)).toBeUndefined()
      expect(await plugins.delete(bad)).toBe(false)
      await expect(plugins.update(bad, { url: 'https://x.example/' }, undefined)).rejects.toMatchObject({ status: 404 })
      await expect(plugins.setToolAccess(bad, 't', 'read')).rejects.toMatchObject({ status: 404 })
      await expect(oauth.forget(bad)).rejects.toBeInstanceOf(CrudPathError)
      await expect(oauth.mark(bad, 'why')).rejects.toBeInstanceOf(CrudPathError)
      await expect(oauth.source(bad, 'https://x.example/', randomUUID(), undefined).header()).rejects.toBeInstanceOf(CrudPathError)
    }
    // The sign-out route refuses it too.
    const app: AgentApp = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'not set' },
      credentials: new MemoryCredentials(),
      plugins,
      pluginOAuth: oauth,
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    const out = await app.request('/api/v1/ai/plugins/..%2Fx/oauth', { method: 'DELETE', headers: UI })
    expect(out.status).toBe(404)
    expect(api.seen).toEqual([])
  })
})

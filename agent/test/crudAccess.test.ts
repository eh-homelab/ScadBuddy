import { describe, expect, it } from 'vitest'
import { CrudToolListings, listingLeaseKey, ToolListings } from '../src/access/listings.js'
import { AccessStore, CrudAccessRows } from '../src/access/store.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { SOURCES_PER_CALL } from '../src/api/crudStores.js'
import type { AccessRow } from '../src/access/model.js'
import type { AuditEntry } from '../src/audit/log.js'
import { type LockClient, TEST_LOCK_CONNECTIONS } from '../src/db.js'
import { CrudPluginSessions, endOwnSessions, sweepDeadReplicas } from '../src/plugins/pluginSessions.js'

// The tool access, the tool listings and the plugin sessions through the API's CRUD routes
// (#5299 step 5), against a fake API that keeps them in memory as backend
// api/crud_agent_access.py does (the backend's tests/api/test_crud_agent_access.py covers the
// routes, the lease and the advisory lock on Postgres). None of these stores takes a KEK or a
// database: the SQL handles given here throw if they are touched.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'

const noSql = new Proxy(() => undefined, {
  get: () => {
    throw new Error('the SQL store was used')
  },
  apply: () => {
    throw new Error('the SQL store was used')
  },
}) as never
const noLocks: LockClient = {
  take: () => {
    throw new Error('the lock client was used')
  },
} as never

type Seen = { method: string; path: string; body: Record<string, unknown> }

class FakeApi {
  seen: Seen[] = []
  rows: AccessRow[] = []
  listings = new Map<string, { tools: unknown; listed_at: string; truncated: boolean }>()
  leases = new Map<string, string>()
  plugins = new Map<string, string>([['files', 'https://mcp.example/']])
  sessions: { plugin_id: string; upstream_session_id: string; replica: string }[] = []

  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : {}
    const path = url.pathname.slice(PREFIX.length)
    this.seen.push({ method, path: path + url.search, body })
    return this.#route(method, path, url.searchParams, body)
  }

  #json(value: unknown, status = 200): Response {
    return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } })
  }

  #route(method: string, path: string, q: URLSearchParams, b: Record<string, unknown>): Response {
    let m: RegExpMatchArray | null
    if (method === 'POST' && path === '/access/rows') {
      const sources = b.sources as string[]
      return this.#json({ rows: this.rows.filter((r) => sources.includes(r.source)) })
    }
    if (method === 'GET' && path === '/access/row') {
      return this.#json({ row: this.rows.find((r) => r.source === q.get('source') && r.item === q.get('item')) ?? null })
    }
    if (method === 'POST' && path === '/access/write') {
      const edits = b.edits as { source: string; kind: string; items?: Record<string, string | null>; tools?: Record<string, string> }[]
      const applied: number[] = []
      edits.forEach((e, i) => {
        const before = JSON.stringify(this.rows.filter((r) => r.source === e.source).map((r) => [r.item, r.access]))
        if (e.kind === 'forget') this.rows = this.rows.filter((r) => r.source !== e.source)
        if (e.kind === 'replace') {
          this.rows = this.rows.filter((r) => r.source !== e.source || r.item === '*')
          for (const [item, access] of Object.entries(e.tools!)) this.rows.push({ source: e.source, item, access, updated_by: b.by as string })
        }
        if (e.kind === 'items') {
          for (const [item, access] of Object.entries(e.items!)) {
            this.rows = this.rows.filter((r) => !(r.source === e.source && r.item === item))
            if (access !== null) this.rows.push({ source: e.source, item, access, updated_by: b.by as string })
          }
        }
        const after = JSON.stringify(this.rows.filter((r) => r.source === e.source).map((r) => [r.item, r.access]))
        if (!b.skip_unchanged || before !== after) applied.push(i)
      })
      return this.#json({ applied })
    }
    if (method === 'POST' && path === '/tool-listings/read') {
      const sources = b.sources as string[]
      return this.#json({ listings: sources.flatMap((source) => (this.listings.has(source) ? [{ source, ...this.listings.get(source)! }] : [])) })
    }
    if ((m = path.match(/^\/leases\/(.+)$/))) {
      const key = decodeURIComponent(m[1]!)
      if (method === 'POST') {
        const holder = this.leases.get(key)
        if (holder && holder !== b.token) return this.#json({ detail: 'Another holder keeps the lease.', reason: 'busy' }, 409)
        this.leases.set(key, b.token as string)
        return this.#json({ token: b.token, expires_at: '2026-11-15T10:22:00Z' })
      }
      if (method === 'PUT') {
        if (this.leases.get(key) !== b.token) return this.#json({ detail: 'lost', reason: 'lease-lost' }, 409)
        return this.#json({ token: b.token, expires_at: '2026-11-15T10:24:00Z' })
      }
      if (method === 'DELETE') {
        if (this.leases.get(key) === q.get('token')) this.leases.delete(key)
        return new Response(null, { status: 204, headers: { 'x-scadbuddy-crud': '1' } })
      }
    }
    if (method === 'POST' && path === '/tool-listings/plugin') {
      const source = `remote:${b.name as string}`
      if (this.leases.get(listingLeaseKey(source)) !== b.lease_token) return this.#json({ detail: 'lost', reason: 'lease-lost' }, 409)
      if (this.plugins.get(b.name as string) !== b.url) return this.#json({ stored: false })
      this.listings.set(source, { tools: b.tools, listed_at: '2026-11-15T10:20:00.123456Z', truncated: b.truncated as boolean })
      return this.#json({ stored: true })
    }
    if (method === 'POST' && path === '/plugin-sessions/seen') {
      this.sessions.push({ plugin_id: b.plugin as string, upstream_session_id: b.id as string, replica: b.replica as string })
      return this.#json({ ok: true })
    }
    if (method === 'POST' && path === '/plugin-sessions/ended') {
      this.sessions = this.sessions.filter((s) => !(s.plugin_id === b.plugin && s.upstream_session_id === b.id))
      return this.#json({ ok: true })
    }
    if (method === 'GET' && path === '/plugin-sessions/mark') return this.#json({ mark: '2026-11-15 10:20:00.123456+00' })
    if (method === 'POST' && path === '/plugin-sessions/dead') {
      const live = b.live as string[]
      return this.#json({
        rows: this.sessions.filter((s) => !live.includes(s.replica)).map((s) => ({ plugin_id: s.plugin_id, upstream_session_id: s.upstream_session_id, old: false })),
      })
    }
    if (method === 'POST' && path === '/plugin-sessions/own') {
      return this.#json({
        rows: this.sessions.filter((s) => s.replica === b.replica).map((s) => ({ plugin_id: s.plugin_id, upstream_session_id: s.upstream_session_id, old: false })),
      })
    }
    return this.#json({ detail: `no route ${method} ${path}` }, 404)
  }
}

const target = (api: FakeApi) => ({ baseUrl: BASE, fetch: api.fetch })

describe('the plugin stores through CRUD routes (#5299 step 5)', () => {
  it('writes tool access on the API, audits it here, and skips a write that changes nothing', async () => {
    const api = new FakeApi()
    const audit: AuditEntry[] = []
    const store = new AccessStore(noSql, { record: async (e: AuditEntry) => void audit.push(e) } as never, {
      crud: new CrudAccessRows(target(api)),
      use: () => true,
    })
    await store.setEnabled('remote:files', true)
    await store.replaceTools('remote:files', { search: 'read', write: 'off' })
    expect(await store.rows(['remote:files'])).toEqual([
      { source: 'remote:files', item: '*', access: 'on', updated_by: 'system' },
      { source: 'remote:files', item: 'search', access: 'read', updated_by: 'system' },
      { source: 'remote:files', item: 'write', access: 'off', updated_by: 'system' },
    ])
    expect((await store.row('remote:files', 'search'))?.access).toBe('read')
    expect(api.seen.find((s) => s.path.startsWith('/access/row?'))?.path).toBe('/access/row?source=remote%3Afiles&item=search')
    expect(audit.map((e) => e.detail)).toEqual(['remote:files/* = "on"', 'remote:files/* tools = {"search":"read","write":"off"}'])
    // The same tools again: the API writes nothing, so nothing is audited.
    await store.replaceToolsOf({ 'remote:files': { search: 'read', write: 'off' } })
    expect(audit).toHaveLength(2)
    expect(api.seen.at(-1)?.body).toMatchObject({ skip_unchanged: true })
    // The model is checked here, before any call.
    await expect(store.setItem('remote:files', 'search', 'on')).rejects.toThrow(/takes off, read, write or outward/)
  })

  it('holds a lease for a connection test, stores the listing under it, and releases it', async () => {
    const api = new FakeApi()
    const listings = new ToolListings(noSql, noLocks, { crud: new CrudToolListings(target(api)), use: () => true })
    const held = await listings.exclusive('files', async (put) => {
      expect(api.leases.has('tool-listing:remote:files')).toBe(true)
      // Another replica's test of the same plugin is busy meanwhile.
      const other = new ToolListings(noSql, noLocks, { crud: new CrudToolListings(target(api)), use: () => true })
      expect(await other.exclusive('files', async () => 'never')).toBe('busy')
      expect(await put('https://mcp.example/', [{ raw: { name: 'search' }, omitted: [] }], { truncated: true })).toBe(true)
      expect(await put('https://moved.example/', [], {})).toBe(false)
      return 'tested'
    })
    expect(held).toEqual({ value: 'tested' })
    expect(api.leases.size).toBe(0)
    const listing = await listings.get('remote:files')
    expect(listing?.truncated).toBe(true)
    expect(listing?.tools.map((t) => t.name)).toEqual(['search'])
    expect((await listings.getMany(['remote:files', 'remote:none'])).size).toBe(1)
  })

  it('answers unreachable when the API does not answer the acquire, and runs nothing', async () => {
    const down: typeof fetch = () => Promise.reject(Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } }))
    const listings = new ToolListings(noSql, noLocks, { crud: new CrudToolListings({ baseUrl: BASE, fetch: down }), use: () => true })
    let ran = false
    expect(await listings.exclusive('files', async () => (ran = true))).toBe('unreachable')
    expect(ran).toBe(false)
  })

  it("passes on a 4xx the acquire's route answered as it is, and a 5xx as unreachable", async () => {
    const answering =
      (status: number): typeof fetch =>
      async (_input, init) =>
        (init?.method ?? 'GET') === 'DELETE'
          ? new Response(null, { status: 204, headers: { 'x-scadbuddy-crud': '1' } })
          : new Response(JSON.stringify({ detail: 'That is not a lease key.' }), {
              status,
              headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' },
            })
    let ran = false
    const refused = new ToolListings(noSql, noLocks, { crud: new CrudToolListings({ baseUrl: BASE, fetch: answering(422) }), use: () => true })
    const err = await refused.exclusive('files', async () => (ran = true)).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(CrudReadError)
    expect((err as CrudReadError).status).toBe(422)
    const failing = new ToolListings(noSql, noLocks, { crud: new CrudToolListings({ baseUrl: BASE, fetch: answering(503) }), use: () => true })
    expect(await failing.exclusive('files', async () => (ran = true))).toBe('unreachable')
    expect(ran).toBe(false)
  })

  it('runs at most TEST_LOCK_CONNECTIONS tests at once on this replica, as the lock client does', async () => {
    const api = new FakeApi()
    const listings = new ToolListings(noSql, noLocks, { crud: new CrudToolListings(target(api)), use: () => true })
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    const names = Array.from({ length: TEST_LOCK_CONNECTIONS }, (_, i) => `p${i}`)
    let started = 0
    const running = names.map((n) => listings.exclusive(n, async () => (started++, await gate, n)))
    await expect.poll(() => started).toBe(TEST_LOCK_CONNECTIONS)
    expect(await listings.exclusive('one-more', async () => 'never')).toBe('full')
    release()
    expect(await Promise.all(running)).toEqual(names.map((n) => ({ value: n })))
    expect(await listings.exclusive('one-more', async () => 'ran')).toEqual({ value: 'ran' })
  })

  it('reads more sources than one call takes in runs of SOURCES_PER_CALL', async () => {
    const api = new FakeApi()
    const sources = Array.from({ length: SOURCES_PER_CALL + 1 }, (_, i) => `remote:s${i}`)
    api.rows = [{ source: sources.at(-1)!, item: '*', access: 'on', updated_by: 'system' }]
    api.listings.set(sources.at(-1)!, { tools: [], listed_at: new Date().toISOString(), truncated: false })
    expect(await new CrudAccessRows(target(api)).rows(sources)).toHaveLength(1)
    expect((await new CrudToolListings(target(api)).read(sources)).size).toBe(1)
    const sent = api.seen.filter((s) => s.path === '/access/rows' || s.path === '/tool-listings/read').map((s) => (s.body.sources as string[]).length)
    expect(sent).toEqual([SOURCES_PER_CALL, 1, SOURCES_PER_CALL, 1])
  })

  it("refuses a test's write once its lease is lost: an error of the test's own", async () => {
    const api = new FakeApi()
    const listings = new ToolListings(noSql, noLocks, { crud: new CrudToolListings(target(api)), use: () => true })
    const run = listings.exclusive('files', async (put) => {
      api.leases.set('tool-listing:remote:files', 'someone-else')
      return put('https://mcp.example/', [], {})
    })
    await expect(run).rejects.toThrow(/lost/)
  })

  it('records and sweeps upstream MCP sessions through the API', async () => {
    const api = new FakeApi()
    const rows = new CrudPluginSessions(target(api), 'me')
    await rows.seen('pkg:srv', 's1')
    await rows.seen('pkg:srv', 's2')
    api.sessions.push({ plugin_id: 'pkg:srv', upstream_session_id: 's3', replica: 'gone' })
    const deps = { plugins: { enabled: async () => ({ plugins: [], problems: [] }) }, kek: undefined, graceMs: 0 }
    const mark = await rows.mark()
    expect(mark).toBe('2026-11-15 10:20:00.123456+00')
    // A package's server is never looked up: its sessions are dropped without a DELETE.
    expect(await sweepDeadReplicas(rows, new Set(['me']), deps)).toBe(1)
    expect(await endOwnSessions(rows, 'me', deps, mark)).toBe(2)
    expect(api.sessions).toEqual([])
    expect(api.seen.find((s) => s.path === '/plugin-sessions/own')?.body).toEqual({ replica: 'me', mark })
  })
})

import { randomBytes, randomUUID } from 'node:crypto'
import type { UpgradeWebSocket } from 'hono/ws'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudAreas, SECRET_AREAS } from '../src/api/crudAreas.js'
import { aiStatus } from '../src/aiStatus.js'
import { createApp } from '../src/app.js'
import type { AuditEntry, AuditRepo } from '../src/audit/log.js'
import {
  type CredentialKind,
  CredentialError,
  CrudCredentials,
  CrudSettings,
  gatedCredentials,
  type CredentialRepo,
  SETTINGS_CAS_TRIES,
  SETTINGS_READ_MAX,
  SettingsStore,
} from '../src/credentials.js'
import { CredentialPool } from '../src/harness/fallback.js'
import { originPolicy } from '../src/http/origins.js'
import { toolSecrets } from '../src/plugins/secrets.js'
import type { CredentialEntryView, CredentialListView } from '../src/routes/credentials.js'
import { type KekStatus, SealError } from '../src/secrets.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { gateSettings } from '../src/temporal/session/gateActivities.js'
import { CrudPayloadKeys, gatedPayloadKeys, PayloadKeyUnavailable, SubjectForgotten } from '../src/temporal/payloadCodec.js'

// The agent's settings, Claude credentials and payload data keys through the API's CRUD routes
// (#5299 step 4, D5; #5342), against a fake API that keeps them in memory as the backend's
// api/crud_agent_secrets.py does (the backend's tests/crud/test_crud_agent_secrets.py covers the
// routes themselves). Every store here runs with no KEK in this process: the API seals and opens.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const NO_KEK: KekStatus = { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' }
const API_KEK_ID = 'a1b2c3d4e5f60718'

type Stored = {
  id: string
  priority: number
  kind: CredentialKind
  name: string | null
  base_url: string | null
  last4: string
  updated_at: string
  status: 'active' | 'cooling_down' | 'disabled'
  cooldown_until: string | null
  last_error: string | null
  last_error_at: string | null
  last_used_at: string | null
  epoch: number
  kek_id: string
  legacy_format: boolean
  secret: string
}

/** The API's side, in memory: what each route does, with no SQL. */
class FakeApi {
  settings = new Map<string, unknown>()
  credentials: Stored[] = []
  keys = new Map<string, Buffer>()
  forgotten = new Set<string>()
  standing = new Set<string>()
  hasKek = true
  /** Each request, as `METHOD path` with its JSON body. */
  seen: { method: string; path: string; body: unknown }[] = []
  /** Reveals and releases the API audited. */
  audited: string[] = []
  /** Before each settings write: a stand-in for another writer. */
  beforeWrite: (() => void) | undefined

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = url.pathname.slice(PREFIX.length)
    const body = request.method === 'POST' ? ((await request.json()) as Record<string, unknown>) : undefined
    this.seen.push({ method: request.method, path: `${path}${url.search}`, body })
    const answer = this.#route(request.method, path, url.searchParams, body ?? {})
    return new Response(JSON.stringify(answer.body), { status: answer.status ?? 200, headers: { 'x-scadbuddy-crud': '1' } })
  }

  #wire(c: Stored): Omit<Stored, 'secret'> {
    const { secret: _secret, ...rest } = c
    return rest
  }

  #opened(c: Stored) {
    return { kind: c.kind, base_url: c.base_url, secret: c.secret }
  }

  #refused(status: number, detail: string, reason = 'credential-refused') {
    return { status, body: { detail, reason } }
  }

  #route(method: string, path: string, query: URLSearchParams, body: Record<string, unknown>): { status?: number; body: unknown } {
    if (method === 'GET' && path === '/settings') {
      const values: Record<string, unknown> = {}
      for (const key of query.getAll('key')) if (this.settings.has(key)) values[key] = this.settings.get(key)
      return { body: { values } }
    }
    if (method === 'POST' && path === '/settings') {
      this.beforeWrite?.()
      const expect = body.expect as { key: string; present: boolean; value?: unknown }[] | undefined
      const holds = (expect ?? []).every((e) =>
        e.present ? JSON.stringify(this.settings.get(e.key)) === JSON.stringify(e.value) : !this.settings.has(e.key),
      )
      if (!holds) return { body: { written: false } }
      for (const [k, v] of Object.entries(body.values as Record<string, unknown>)) this.settings.set(k, v)
      return { body: { written: true } }
    }
    if (path === '/credentials/key') {
      return { body: this.hasKek ? { ok: true, kek_id: API_KEK_ID, reason: null } : { ok: false, kek_id: null, reason: 'the API has no key-encryption key' } }
    }
    if (method === 'GET' && path === '/credentials') return { body: { credentials: this.credentials.map((c) => this.#wire(c)) } }
    if (method === 'GET' && path === '/credentials/one') {
      const id = query.get('id')
      const found = id === null ? this.credentials[0] : this.credentials.find((c) => c.id === id)
      return { body: { credential: found ? this.#wire(found) : null } }
    }
    if (method === 'POST' && path === '/credentials') {
      if (!this.hasKek) return this.#refused(503, 'credentials cannot be saved: no key-encryption key is configured on the API')
      const secret = body.secret as string
      const now = new Date().toISOString()
      const row: Stored = {
        id: randomUUID(),
        priority: this.credentials.length,
        kind: body.kind as CredentialKind,
        name: (body.name as string | undefined) ?? null,
        base_url: (body.base_url as string | undefined) ?? null,
        last4: secret.length < 12 ? '' : secret.slice(-4),
        updated_at: now,
        status: 'active',
        cooldown_until: null,
        last_error: null,
        last_error_at: null,
        last_used_at: null,
        epoch: 0,
        kek_id: API_KEK_ID,
        legacy_format: false,
        secret,
      }
      this.credentials.push(row)
      return { status: 201, body: { credential: this.#wire(row) } }
    }
    if (method === 'POST' && path === '/credentials/save') {
      const found = body.id === undefined ? this.credentials[0] : this.credentials.find((c) => c.id === body.id)
      if (!found) return this.#refused(404, `no credential ${String(body.id)}`)
      const update = body.update as { kind: CredentialKind; secret?: string }
      if (update.secret === undefined && update.kind !== found.kind) return this.#refused(409, 'changing kind or base_url needs the secret again')
      if (update.secret !== undefined) Object.assign(found, { secret: update.secret, epoch: found.epoch + 1 })
      return { body: { credential: this.#wire(found) } }
    }
    if (method === 'POST' && path === '/credentials/delete') {
      const before = this.credentials.length
      this.credentials = this.credentials.filter((c) => c.id !== body.id)
      this.credentials.forEach((c, i) => (c.priority = i))
      return { body: { deleted: this.credentials.length < before } }
    }
    if (method === 'POST' && path === '/credentials/order') {
      const ids = body.ids as string[]
      if (ids.length !== this.credentials.length) return this.#refused(409, 'the order must name every stored credential exactly once')
      this.credentials = ids.map((id, i) => Object.assign(this.credentials.find((c) => c.id === id) as Stored, { priority: i }))
      return { body: { credentials: this.credentials.map((c) => this.#wire(c)) } }
    }
    if (method === 'POST' && path === '/credentials/name') {
      const found = this.credentials.find((c) => c.id === body.id)
      if (found) found.name = body.name as string | null
      return { body: { credential: found ? this.#wire(found) : null } }
    }
    if (method === 'POST' && path === '/credentials/reset') {
      const found = this.credentials.find((c) => c.id === body.id)
      if (found) Object.assign(found, { status: 'active', epoch: found.epoch + 1, last_error: null })
      return { body: { credential: found ? this.#wire(found) : null } }
    }
    if (method === 'POST' && path === '/credentials/events') {
      const found = this.credentials.find((c) => c.id === body.id && c.epoch === body.epoch)
      if (!found) return { body: { transition: null } }
      const event = body.event as { kind: string; reason?: string; until?: string }
      const before = found.status
      if (event.kind === 'disabled') Object.assign(found, { status: 'disabled', last_error: event.reason })
      if (event.kind === 'cooling_down') Object.assign(found, { status: 'cooling_down', cooldown_until: event.until, last_error: event.reason })
      return { body: { transition: { before, after: found.status, recovered: false } } }
    }
    if (method === 'POST' && path === '/credentials/reveal') {
      if (!this.hasKek) return this.#refused(503, 'The API has no key-encryption key.', 'kek-missing')
      this.audited.push(`reveal.${String(body.purpose)}`)
      if (body.all) return { body: { credentials: this.credentials.map((c) => ({ stored: this.#wire(c), credential: this.#opened(c) })) } }
      const found = body.id === undefined ? this.credentials[0] : this.credentials.find((c) => c.id === body.id)
      if (found?.kek_id === 'elsewhere') return this.#refused(409, 'secret was sealed with key elsewhere', 'credential-sealed')
      return { body: { credential: found ? this.#opened(found) : null } }
    }
    const subject = /^\/payload-keys\/(.+)$/.exec(path)?.[1]
    if (method === 'POST' && subject) {
      if (!this.hasKek) return this.#refused(503, 'The API has no key-encryption key.', 'kek-missing')
      let key = this.keys.get(subject)
      const standingOk = !body.only_for_standing_session || this.standing.has(subject)
      if (!key && body.create && !this.forgotten.has(subject) && standingOk) {
        key = randomBytes(32)
        this.keys.set(subject, key)
      }
      if (!key) return this.#refused(410, `${subject} has no payload key`, 'subject-forgotten')
      this.audited.push(`payload_key ${subject}`)
      return { body: { data_key: key.toString('base64') } }
    }
    return { status: 404, body: { detail: 'no such route' } }
  }
}

/** A database this process must never reach: every store here goes through the API. */
const NO_SQL = new Proxy(() => {}, {
  apply() {
    throw new Error('the store reached SQL')
  },
  get() {
    throw new Error('the store reached SQL')
  },
}) as unknown as Sql

const setup = () => {
  const api = new FakeApi()
  return { api, fetch: api.fetch }
}

describe('the step 4 areas', () => {
  it('are the API agent areas the agent waits for', () => {
    expect([...SECRET_AREAS]).toEqual(['settings', 'credentials', 'payload-keys'])
  })
})

// The rolling deploy's switch from SQL once the API lists the areas (#5299 D9).
describe('CrudAreas', () => {
  const areasApi =
    (answer: () => Response): typeof fetch =>
    async () =>
      answer()
  const listing = (areas: string[]) => () => new Response(JSON.stringify({ areas }), { headers: { 'x-scadbuddy-crud': '1' } })

  it('waits, logging once, until the API lists every area, then holds it', async () => {
    const logs: string[] = []
    let listed: string[] = ['events']
    const areas = new CrudAreas(BASE, { fetch: areasApi(() => listing(listed)()), wanted: SECRET_AREAS, log: (m) => logs.push(m) })
    expect(await areas.refresh()).toBe(false)
    expect(await areas.refresh()).toBe(false)
    expect(logs).toEqual([
      "CRUD areas: waiting for the backend's agent areas (missing settings, credentials, payload-keys); reading over SQL until it lists them",
    ])
    listed = ['settings']
    expect(await areas.refresh()).toBe(false)
    expect([areas.has('settings'), areas.has('credentials'), areas.complete]).toEqual([true, false, false])
    listed = ['settings', 'credentials', 'payload-keys']
    expect(await areas.refresh()).toBe(true)
    expect(logs.at(-1)).toBe("CRUD areas: the agent reads settings, credentials, payload-keys through the backend's CRUD routes")
    // Listed once, for good: an older pod answering later does not take it back.
    listed = []
    await areas.refresh()
    expect(areas.has('credentials')).toBe(true)
  })

  it("names the route's refused credential apart from a proxy's 401 or an older pod's 404", async () => {
    const logs: string[] = []
    let answer = (): Response => new Response('', { status: 404 })
    const areas = new CrudAreas(BASE, { fetch: areasApi(() => answer()), wanted: SECRET_AREAS, log: (m) => logs.push(m) })
    await areas.refresh()
    expect(logs[0]).toContain('no CRUD route answered its areas (HTTP 404)')
    answer = () => new Response('', { status: 401 })
    await areas.refresh()
    expect(logs[1]).toContain('no CRUD route answered its areas (HTTP 401)')
    answer = () => new Response('', { status: 401, headers: { 'x-scadbuddy-crud': '1' } })
    await areas.refresh()
    await areas.refresh()
    expect(logs).toHaveLength(3)
    expect(logs[2]).toContain("the backend refused the agent's service credential (HTTP 401)")
  })

  it('reads again with back-off from the start until they are listed', async () => {
    let calls = 0
    const areas = new CrudAreas(BASE, {
      fetch: areasApi(() => listing(++calls >= 3 ? [...SECRET_AREAS] : [])()),
      wanted: SECRET_AREAS,
      log: () => {},
      firstMs: 1,
      maxMs: 4,
    })
    areas.start()
    for (let i = 0; i < 200 && !areas.complete; i++) await new Promise((r) => setTimeout(r, 5))
    expect(areas.complete).toBe(true)
    const after = calls
    await new Promise((r) => setTimeout(r, 30))
    expect(calls).toBe(after)
    areas.stop()
  })
})

describe('CrudSettings and SettingsStore over the routes', () => {
  it('reads and writes with no SQL, auditing each write here, and tells the API which caches to wake', async () => {
    const { api, fetch } = setup()
    const audited: AuditEntry[] = []
    const store = new SettingsStore(NO_SQL, { record: (e: AuditEntry) => Promise.resolve(void audited.push(e)) }, {
      crud: new CrudSettings(BASE, { fetch }),
      use: () => true,
    })
    expect(await store.get('model')).toBeUndefined()
    await store.set('model', 'claude-opus')
    await store.set('warm_idle_seconds', 90)
    await store.set('approval_expiry_seconds', 60)
    expect(await store.get('model')).toBe('claude-opus')
    expect(api.seen.filter((r) => r.method === 'POST').map((r) => (r.body as { notify: unknown }).notify)).toEqual([
      { warm_evict: true, runtime: false },
      { warm_evict: false, runtime: true },
      { warm_evict: false, runtime: false },
    ])
    expect(audited.map((a) => [a.kind, a.action, a.outcome])).toEqual([
      ['settings', 'model', 'ok'],
      ['settings', 'warm_idle_seconds', 'ok'],
      ['settings', 'approval_expiry_seconds', 'ok'],
    ])
    // The gate's timers through the same store.
    expect((await gateSettings(store)).approvalExpiryS).toBe(60)
  })

  it('a compare-and-set sends what its check read, and reads again when another writer moved it', async () => {
    const { api, fetch } = setup()
    const store = new SettingsStore(NO_SQL, undefined, { crud: new CrudSettings(BASE, { fetch }), use: () => true })
    api.settings.set('mcp_auth_mode', 'bearer')
    let raced = false
    api.beforeWrite = () => {
      if (!raced) api.settings.set('mcp_auth_mode', 'bearer-changed')
      raced = true
    }
    const checks: unknown[] = []
    const written = await store.setMany(
      { mcp_auth_mode: 'disabled', mcp_anonymous_cap: 'read' },
      async (current) => {
        checks.push(await current.get('mcp_auth_mode'))
        await current.get('mcp_anonymous_cap')
        return true
      },
    )
    expect(written).toBe(true)
    expect(checks).toEqual(['bearer', 'bearer-changed'])
    const writes = api.seen.filter((r) => r.method === 'POST').map((r) => (r.body as { expect: unknown }).expect)
    expect(writes).toEqual([
      [
        { key: 'mcp_auth_mode', present: true, value: 'bearer' },
        { key: 'mcp_anonymous_cap', present: false },
      ],
      [
        { key: 'mcp_auth_mode', present: true, value: 'bearer-changed' },
        { key: 'mcp_anonymous_cap', present: false },
      ],
    ])
    expect(api.settings.get('mcp_auth_mode')).toBe('disabled')
    // Each try read the keys in one GET, before its write.
    expect(api.seen.map((r) => `${r.method} ${r.path}`)).toEqual([
      'GET /settings?key=mcp_auth_mode&key=mcp_anonymous_cap',
      'POST /settings',
      'GET /settings?key=mcp_auth_mode&key=mcp_anonymous_cap',
      'POST /settings',
    ])
    // A check that says no writes nothing.
    expect(await store.setMany({ mcp_auth_mode: 'bearer' }, () => Promise.resolve(false))).toBe(false)
    expect(api.settings.get('mcp_auth_mode')).toBe('disabled')
  })

  it('reads a key the check asks beyond its writes once, then in the next try\'s batch', async () => {
    const { api, fetch } = setup()
    const crud = new CrudSettings(BASE, { fetch })
    api.settings.set('other', 1)
    let raced = false
    api.beforeWrite = () => {
      if (!raced) api.settings.set('other', 2)
      raced = true
    }
    const seen: unknown[] = []
    expect(await crud.compareAndSet({ k: 0 }, async (c) => (seen.push(await c.get('other')), true))).toBe(true)
    expect(seen).toEqual([1, 2])
    expect(api.seen.map((r) => `${r.method} ${r.path}`)).toEqual([
      'GET /settings?key=k',
      'GET /settings?key=other',
      'POST /settings',
      'GET /settings?key=k&key=other',
      'POST /settings',
    ])
    expect((api.seen.at(-1)?.body as { expect: unknown }).expect).toEqual([{ key: 'other', present: true, value: 2 }])
  })

  it('reads at most SETTINGS_READ_MAX keys a GET', async () => {
    const { api, fetch } = setup()
    const keys = Array.from({ length: SETTINGS_READ_MAX + 1 }, (_, i) => `k${i}`)
    api.settings.set('k50', 'last')
    const found = await new CrudSettings(BASE, { fetch }).getMany(keys)
    expect([...found]).toEqual([['k50', 'last']])
    expect(api.seen.map((r) => new URLSearchParams(r.path.split('?')[1]).getAll('key').length)).toEqual([SETTINGS_READ_MAX, 1])
  })

  it('gives up after its tries when every write meets another writer', async () => {
    const { api, fetch } = setup()
    const crud = new CrudSettings(BASE, { fetch })
    let n = 0
    api.beforeWrite = () => api.settings.set('k', ++n)
    await expect(crud.compareAndSet({ k: 0 }, async (c) => (await c.get('k'), true))).rejects.toThrow('kept changing')
    expect(api.seen.filter((r) => r.method === 'POST')).toHaveLength(SETTINGS_CAS_TRIES)
  })

  it('stays on SQL until the API lists the area', async () => {
    const { api, fetch } = setup()
    let listed = false
    const rows: string[] = []
    const sql = ((strings: TemplateStringsArray) => {
      rows.push(strings.join('?'))
      return Promise.resolve([{ value: 'from-sql' }])
    }) as unknown as Sql
    const store = new SettingsStore(sql, undefined, { crud: new CrudSettings(BASE, { fetch }), use: () => listed })
    expect(await store.get('model')).toBe('from-sql')
    listed = true
    api.settings.set('model', 'from-api')
    expect(await store.get('model')).toBe('from-api')
    expect(rows).toHaveLength(1)
  })
})

describe('CrudCredentials', () => {
  it('saves, lists and reveals through the API, never needing a KEK here', async () => {
    const { api, fetch } = setup()
    const repo = new CrudCredentials(BASE, { fetch })
    const created = await repo.create({ kind: 'gateway', base_url: 'https://gw.example.com/v1/', secret: 'gw-token-0123456789', name: '  Gateway  ' }, undefined)
    // The URL and name as this process normalises them: the AAD binds the URL as spelled.
    expect(api.seen.at(-1)?.body).toEqual({ kind: 'gateway', base_url: 'https://gw.example.com/v1', secret: 'gw-token-0123456789', name: 'Gateway' })
    expect(created).toMatchObject({ kind: 'gateway', name: 'Gateway', kekId: API_KEK_ID, legacyFormat: false, last4: '6789' })
    expect(JSON.stringify(created)).not.toContain('gw-token')
    expect(await repo.opener()).toEqual({ ok: true, kek: { id: API_KEK_ID } })
    const [row] = await repo.revealAll(undefined)
    expect(row?.credential).toEqual({ kind: 'gateway', baseUrl: 'https://gw.example.com/v1', secret: 'gw-token-0123456789' })
    expect(await repo.reveal(undefined, created.id)).toEqual({ kind: 'gateway', baseUrl: 'https://gw.example.com/v1', secret: 'gw-token-0123456789' })
    expect(api.audited).toEqual(['reveal.turn', 'reveal.test'])
    expect((await repo.get())?.id).toBe(created.id)
    expect(await repo.get('nope')).toBeUndefined()
  })

  it("maps the API's refusals to the store's own errors", async () => {
    const { api, fetch } = setup()
    const repo = new CrudCredentials(BASE, { fetch })
    const error = await repo.put({ kind: 'anthropic_api_key', secret: 'x' }, undefined, 'missing').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(CredentialError)
    expect((error as CredentialError).status).toBe(404)
    // A name or URL this process refuses never reaches the API.
    await expect(repo.create({ kind: 'anthropic_api_key', secret: 's', name: 'a‮b' }, undefined)).rejects.toThrow(CredentialError)
    await expect(repo.create({ kind: 'gateway', base_url: 'ftp://x', secret: 's' }, undefined)).rejects.toThrow(CredentialError)
    const created = await repo.create({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-zzzzzzzzzzzz' }, undefined)
    ;(api.credentials[0] as Stored).kek_id = 'elsewhere'
    await expect(repo.reveal(undefined, created.id)).rejects.toThrow(SealError)
    api.hasKek = false
    const noKek = await repo.create({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-yyyyyyyyyyyy' }, undefined).catch((e: unknown) => e)
    expect((noKek as CredentialError).status).toBe(503)
    expect(((await repo.reveal(undefined).catch((e: unknown) => e)) as CredentialError).status).toBe(503)
  })

  it('records health, reorders, renames, resets and deletes', async () => {
    const { fetch } = setup()
    const repo = new CrudCredentials(BASE, { fetch })
    const a = await repo.create({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-aaaaaaaaaaaa' }, undefined)
    const b = await repo.create({ kind: 'claude_oauth_token', secret: 'sk-ant-oat01-bbbbbbbbbbbb' }, undefined)
    const until = new Date('2099-01-01T00:00:00Z')
    expect(await repo.record(a.id, 0, { kind: 'cooling_down', until, reason: '429' })).toEqual({ before: 'active', after: 'cooling_down', recovered: false })
    expect(await repo.record(a.id, 9, { kind: 'disabled', reason: '401' })).toBeUndefined()
    expect((await repo.reorder([b.id, a.id])).map((c) => c.id)).toEqual([b.id, a.id])
    expect((await repo.rename(a.id, 'Home'))?.name).toBe('Home')
    expect((await repo.reset(a.id))?.status).toBe('active')
    expect(await repo.delete(a.id)).toBe(true)
    expect((await repo.list()).map((c) => c.id)).toEqual([b.id])
  })
})

describe('the credential consumers with no KEK in this process', () => {
  it("a turn's credentials come from the API's reveal", async () => {
    const { api, fetch } = setup()
    const repo = new CrudCredentials(BASE, { fetch })
    await repo.create({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-turnturnturn' }, undefined)
    const pool = new CredentialPool({ repo, kek: NO_KEK, log: () => {} })
    const sessionId = randomUUID()
    const turnId = randomUUID()
    const [first] = await pool.candidates({ sessionId, turnId })
    expect(first?.credential).toEqual({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-turnturnturn' })
    expect(api.audited).toEqual(['reveal.turn'])
    // The reveal names the turn it is for, which the API's audit row keeps.
    const reveal = api.seen.filter((r) => r.path === '/credentials/reveal').at(-1)?.body as Record<string, unknown>
    expect([reveal.session_id, reveal.turn_id]).toEqual([sessionId, turnId])
    // The redaction of tool output reads them too, audited as such.
    expect(await toolSecrets({ kek: undefined, credentials: repo }).fresh()).toContain('sk-ant-api03-turnturnturn')
    expect(api.audited).toEqual(['reveal.turn', 'reveal.redact'])
    // Every tool call's redaction shares one reveal for SAVED_SECRETS_CACHE_MS.
    const redaction = toolSecrets({ kek: undefined, credentials: repo })
    await redaction.secrets()
    expect(await redaction.secrets()).toContain('sk-ant-api03-turnturnturn')
    expect(api.audited).toEqual(['reveal.turn', 'reveal.redact', 'reveal.redact'])
    // Without a KEK on the API either, none is usable, and the pool says why.
    api.hasKek = false
    await expect(new CredentialPool({ repo: new CrudCredentials(BASE, { fetch }), kek: NO_KEK }).candidates()).rejects.toThrow(
      'no key-encryption key: the API has no key-encryption key',
    )
  })

  it("/status says the API did not answer, not the database, when the API's key read fails", async () => {
    const { fetch } = setup()
    const failing = new (class extends CrudCredentials {
      override opener(): Promise<never> {
        return Promise.reject(new Error('the backend could not be reached'))
      }
    })(BASE, { fetch })
    const status = await aiStatus({ database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) }, credentials: failing, kek: NO_KEK }, true)
    expect(status.ai).toBe('unavailable (the API did not answer for its key-encryption key)')
  })

  it('refuses a base URL with a bare ? or #, as the API does', async () => {
    const { fetch } = setup()
    const repo = new CrudCredentials(BASE, { fetch })
    for (const base_url of ['https://gw.example/?', 'https://gw.example/#']) {
      await expect(repo.create({ kind: 'gateway', base_url, secret: 'gw-token-0123456789' }, undefined)).rejects.toThrow('must not have a query or fragment')
    }
  })

  it('the settings routes save, list and test a credential, and /status is enabled', async () => {
    const { fetch } = setup()
    const credentials: CredentialRepo = new CrudCredentials(BASE, { fetch })
    const tested: unknown[] = []
    const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
    const app = createApp({
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: NO_KEK,
      credentials,
      testConnection: (credential) => {
        tested.push(credential)
        return Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' })
      },
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      resolveHost: () => Promise.resolve(['203.0.113.10']),
      testCooldownMs: 0,
      audit: { record: () => Promise.resolve() } as unknown as AuditRepo,
      sessions: {} as SessionManager,
      upgradeWebSocket: (() => () => Promise.resolve()) as unknown as UpgradeWebSocket,
    })
    const created = await app.request('/api/v1/ai/credentials/entries', {
      method: 'POST',
      headers: UI,
      body: JSON.stringify({ kind: 'anthropic_api_key', secret: 'sk-ant-api03-routeroute01' }),
    })
    expect(created.status).toBe(201)
    const entry = (await created.json()) as CredentialEntryView
    expect(entry.usable).toBe(true)
    const list = (await (await app.request('/api/v1/ai/credentials/entries', { headers: UI })).json()) as CredentialListView
    expect([list.can_save, list.usable_now, list.credentials.length]).toEqual([true, true, 1])
    const test = await app.request(`/api/v1/ai/credentials/entries/${entry.id}/test`, { method: 'POST', headers: UI })
    expect(test.status).toBe(200)
    expect(tested).toEqual([{ kind: 'anthropic_api_key', secret: 'sk-ant-api03-routeroute01' }])
    const status = await aiStatus({ database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) }, credentials, kek: NO_KEK }, true)
    expect(status.ai).toBe('enabled')
  })

  it('until the API lists the area, the SQL store and this process own KEK answer', async () => {
    const { fetch } = setup()
    let listed = false
    const sqlRepo = { opener: undefined, list: () => Promise.resolve([]) } as unknown as CredentialRepo
    const gated = gatedCredentials(sqlRepo, new CrudCredentials(BASE, { fetch }), () => listed, NO_KEK)
    expect(await gated.opener?.()).toEqual(NO_KEK)
    listed = true
    expect(await gated.opener?.()).toEqual({ ok: true, kek: { id: API_KEK_ID } })
  })
})

describe('CrudPayloadKeys', () => {
  const session = () => `session-${randomUUID()}`

  it("keeps a released key 30 s, asks again after, and never keeps a browser call's", async () => {
    const { api, fetch } = setup()
    let now = 1_000_000
    const realNow = Date.now
    Date.now = () => now
    try {
      const keys = new CrudPayloadKeys(BASE, { fetch })
      const subject = session()
      const made = await keys.keyFor(subject, true)
      expect(made).toHaveLength(32)
      expect(await keys.keyFor(subject, false)).toEqual(made)
      expect(api.audited).toEqual([`payload_key ${subject}`])
      now += 31_000
      expect(await keys.keyFor(subject, false)).toEqual(made)
      expect(api.audited).toHaveLength(2)
      keys.forget(subject)
      await keys.keyFor(subject, false)
      expect(api.audited).toHaveLength(3)
      const bridge = `bridge-${randomUUID()}`
      await keys.keyFor(bridge, true)
      await keys.keyFor(bridge, false)
      expect(api.audited.filter((a) => a.includes('bridge-'))).toHaveLength(2)
    } finally {
      Date.now = realNow
    }
  })

  it('a forgotten subject is SubjectForgotten, and a standing session is asked for as such', async () => {
    const { api, fetch } = setup()
    const keys = new CrudPayloadKeys(BASE, { fetch })
    const subject = session()
    api.forgotten.add(subject)
    await expect(keys.keyFor(subject, true)).rejects.toThrow(SubjectForgotten)
    const standing = session()
    await expect(keys.keyFor(standing, true, { onlyForStandingSession: true })).rejects.toThrow(SubjectForgotten)
    expect(api.seen.at(-1)?.body).toEqual({ create: true, only_for_standing_session: true })
    api.standing.add(standing)
    expect(await keys.keyFor(standing, true, { onlyForStandingSession: true })).toHaveLength(32)
  })

  it('with no KEK here and no route yet, a key is unavailable, never made here', async () => {
    const { api, fetch } = setup()
    let listed = false
    const keys = gatedPayloadKeys(undefined, new CrudPayloadKeys(BASE, { fetch }), () => listed)
    await expect(keys.keyFor(session(), true)).rejects.toThrow(PayloadKeyUnavailable)
    expect(api.seen).toEqual([])
    listed = true
    expect(await keys.keyFor(session(), true)).toHaveLength(32)
  })
})

import { createHash, randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { crudUpsertActivityKinds } from '../src/activity/kinds.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { crudCall, CrudPathError, gated, segment, STORE_AREAS, TABS_PER_CALL, UUID_SEGMENT } from '../src/api/crudStores.js'
import { CrudTokenStore, hashToken, TOKEN_PREFIX } from '../src/auth/tokens.js'
import { CrudPairingStore, hashCode, PairingError } from '../src/bridge/pairings.js'
import { CrudSessionTabStore } from '../src/bridge/sessionTabs.js'
import { CrudTabImageStore } from '../src/bridge/tabImages.js'
import { CrudTabOwnerStore } from '../src/bridge/tabOwners.js'
import { BrowserOrigins, CrudSessionOrigins } from '../src/browser/store.js'
import { ChannelStore, CrudChannelRows } from '../src/notify/channels.js'
import { CrudPushStore, SubscriptionTakenError } from '../src/notify/pushStore.js'
import type { SessionImage } from '../src/sessions/blobs.js'

// The agent's small stores through the API's CRUD routes (#5299 step 5), against a fake API
// that keeps them in memory as backend api/crud_agent_stores.py does (the backend's
// tests/api/test_crud_agent_stores.py covers the routes, their locks and expiries, on
// Postgres). None of these stores takes a KEK: they work in a process that holds none.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f'

type Seen = { method: string; path: string; body: unknown; bytes?: Buffer; contentType?: string }

/** The API's side, in memory: what each route answers, with no SQL. */
class FakeApi {
  seen: Seen[] = []
  kinds: { key: string; label: string }[] = []
  origins = new Map<string, 'once' | 'session'>()
  tabs = new Map<string, string>()
  owners = new Map<string, { queue: string; claimed_at: string }>()
  images = new Map<string, { type: string; bytes: Buffer }>()
  pairings = new Map<string, { label: string; code_hash: string; status: string; tab_id?: string; kind: string; id: string; attempts: number }>()
  tokens = new Map<string, { id: string; name: string; tier: string; token_hash: string; revoked: boolean; last_used_at: string | null }>()
  /** When set, the next pairing request is refused as the cap would. */
  capReached = false

  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const contentType = new Headers(init?.headers).get('content-type') ?? undefined
    const raw = init?.body
    const bytes = raw instanceof Uint8Array ? Buffer.from(raw) : undefined
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    const path = url.pathname.slice(PREFIX.length)
    this.seen.push({ method, path: path + url.search, body, ...(bytes ? { bytes } : {}), ...(contentType ? { contentType } : {}) })
    return this.#route(method, path, url.searchParams, body ?? {}, bytes, contentType)
  }

  #json(value: unknown, status = 200): Response {
    return new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } })
  }

  #route(method: string, path: string, q: URLSearchParams, b: Record<string, unknown>, bytes: Buffer | undefined, type: string | undefined): Response {
    const ok = this.#json({ ok: true })
    let m: RegExpMatchArray | null
    if (method === 'POST' && path === '/activity-kinds') {
      this.kinds = (b.kinds as { key: string; label: string }[]).map((k) => ({ key: k.key, label: k.label }))
      return ok
    }
    if (method === 'GET' && path === '/browser-origins') {
      return this.#json({ origins: [...this.origins].map(([origin, scope]) => ({ origin, scope })) })
    }
    if (method === 'POST' && path === '/browser-origins/approve') {
      const before = this.origins.get(b.origin as string)
      this.origins.set(b.origin as string, before === 'session' ? 'session' : (b.scope as 'once' | 'session'))
      return this.#json({ done: true })
    }
    if (method === 'POST' && path === '/browser-origins/revoke') return this.#json({ done: this.origins.delete(b.origin as string) })
    if (method === 'GET' && path === '/browser-origins/approved') {
      const at = '2026-11-15T07:35:00.123456Z'
      return this.#json({ approved: [...this.origins].map(([origin, scope]) => ({ session_id: SESSION, title: 'Chat', origin, scope, approved_at: at })) })
    }
    if (path === '/session-tabs') {
      if (method === 'GET') return this.#json({ tab_id: this.tabs.get(q.get('session_id')!) ?? null })
      const row = this.tabs.get(b.session_id as string)
      const write = !b.conditional || row === undefined || (b.seen !== undefined && row === b.seen)
      if (write) this.tabs.set(b.session_id as string, b.tab_id as string)
      return this.#json({ written: write })
    }
    if (method === 'POST' && path === '/tab-owners/claim') {
      this.owners.set(b.tab_id as string, { queue: b.queue as string, claimed_at: '2026-11-15 07:35:00.123456+00' })
      return ok
    }
    if (method === 'POST' && path === '/tab-owners/release') {
      if (this.owners.get(b.tab_id as string)?.queue === b.queue) this.owners.delete(b.tab_id as string)
      return ok
    }
    if (method === 'POST' && (path === '/tab-owners/refresh' || path === '/tab-owners/release-all')) return ok
    if (method === 'GET' && path === '/tab-owners') return this.#json({ owner: this.owners.get(q.get('tab_id')!) ?? null })
    if ((m = path.match(/^\/tab-images\/([^/]+)$/)) && method === 'PUT') {
      const name = decodeURIComponent(m[1]!)
      if (createHash('sha256').update(bytes!).digest('hex') !== name.split('.')[0]) return this.#json({ detail: "an image's name is its bytes' sha256" }, 422)
      this.images.set(name, { type: type!, bytes: bytes! })
      return new Response(null, { status: 204, headers: { 'x-scadbuddy-crud': '1' } })
    }
    if ((m = path.match(/^\/tab-images\/([^/]+)\/take$/)) && method === 'POST') {
      const name = decodeURIComponent(m[1]!)
      const held = this.images.get(name)
      this.images.delete(name)
      if (!held) return new Response(null, { status: 204, headers: { 'x-scadbuddy-crud': '1' } })
      return new Response(new Uint8Array(held.bytes), { status: 200, headers: { 'content-type': held.type, 'x-scadbuddy-crud': '1' } })
    }
    if (method === 'POST' && path === '/pairings') {
      if (this.capReached) return this.#json({ detail: 'too many pairing requests are waiting for the user', reason: 'pairing-refused' }, 409)
      const id = randomUUID()
      this.pairings.set(id, { label: b.fallback_label as string, code_hash: b.code_hash as string, status: 'pending', kind: b.principal_kind as string, id: b.principal_id as string, attempts: 0 })
      return this.#json({ id, label: b.fallback_label, expires_at: '2026-11-15T07:40:00Z' }, 201)
    }
    if ((m = path.match(/^\/pairings\/([^/]+)\/accept$/))) {
      const row = this.pairings.get(m[1]!)
      if (!row || row.status !== 'pending') return this.#json({ result: 'gone' })
      if (row.code_hash !== b.code_hash) return this.#json({ result: 'wrong_code', attempts_left: 5 - ++row.attempts })
      Object.assign(row, { status: 'paired', tab_id: b.tab_id })
      return this.#json({ result: 'ok', pairing: { id: m[1], label: row.label, expires_at: '2026-11-15T15:35:00Z' }, principal_kind: row.kind, principal_id: row.id })
    }
    if (method === 'GET' && path === '/pairings/paired-tab') {
      const found = [...this.pairings].find(([, r]) => r.status === 'paired' && r.kind === q.get('principal_kind') && r.id === q.get('principal_id'))
      return this.#json({ pairing: found ? { id: found[0], label: found[1].label, expires_at: '2026-11-15T15:35:00Z', tab_id: found[1].tab_id } : null })
    }
    if (method === 'GET' && path === '/pairings/pending') {
      return this.#json({ pairings: [...this.pairings].filter(([, r]) => r.status === 'pending').map(([id, r]) => ({ id, label: r.label, expires_at: '2026-11-15T07:40:00Z' })) })
    }
    if (method === 'POST' && path === '/pairings/paired-with') {
      const tabs = b.tab_ids as string[]
      const rows = [...this.pairings].filter(([, r]) => r.status === 'paired' && tabs.includes(r.tab_id!))
      return this.#json({ pairings: rows.map(([id, r]) => ({ id, label: r.label, expires_at: '2026-11-15T15:35:00Z', tab_id: r.tab_id })) })
    }
    if ((m = path.match(/^\/pairings\/([^/]+)\/(deny|end)$/))) {
      const row = this.pairings.get(m[1]!)
      const done = row !== undefined && (m[2] === 'deny' ? row.status === 'pending' : row.status === 'paired' && row.tab_id === b.tab_id)
      if (done) row!.status = m[2] === 'deny' ? 'denied' : 'ended'
      return this.#json({ done })
    }
    if (method === 'POST' && path === '/mcp-tokens') {
      const id = randomUUID()
      this.tokens.set(id, { id, name: b.name as string, tier: b.tier as string, token_hash: b.token_hash as string, revoked: false, last_used_at: null })
      return this.#json({ token: this.#tokenRow(id) }, 201)
    }
    if (method === 'POST' && path === '/mcp-tokens/verify') {
      const row = [...this.tokens.values()].find((t) => t.token_hash === b.token_hash && !t.revoked)
      if (row) row.last_used_at = '2026-11-15T07:35:00.123456Z'
      return this.#json({ token: row ? { id: row.id, tier: row.tier } : null })
    }
    if (method === 'GET' && path === '/mcp-tokens') return this.#json({ tokens: [...this.tokens.keys()].map((id) => this.#tokenRow(id)) })
    if ((m = path.match(/^\/mcp-tokens\/([^/]+)\/revoke$/))) {
      const row = this.tokens.get(m[1]!)
      const done = row !== undefined && !row.revoked
      if (row) row.revoked = true
      return this.#json({ done })
    }
    if ((m = path.match(/^\/mcp-tokens\/([^/]+)\/live$/))) {
      const row = this.tokens.get(m[1]!)
      const live = row && !row.revoked
      return this.#json({ tier: live ? row.tier : null, approval_grant: false })
    }
    return this.#json({ detail: `no route ${method} ${path}` }, 404)
  }

  #tokenRow(id: string): unknown {
    const t = this.tokens.get(id)!
    return { id, name: t.name, tier: t.tier, created_at: '2026-11-15T07:00:00Z', expires_at: null, revoked_at: t.revoked ? '2026-11-15T07:36:00Z' : null, last_used_at: t.last_used_at, approval_grant: false }
  }
}

const target = (api: FakeApi) => ({ baseUrl: BASE, fetch: api.fetch })

/** The notify area's routes, in memory: the API seals; here the "sealed" values are kept as they came. */
class FakeNotifyApi {
  seen: Seen[] = []
  vapid: { public_key: string; private_key: string } | undefined
  subs = new Map<string, { id: string; owner: { kind: string; id: string; label: string }; p256dh: string; auth: string }>()
  sent = new Set<string>()
  channels = new Map<string, { id: string; kind: string; name: string; url: string; target: string | null; enabled: boolean; token: string | null; last_error: string | null }>()

  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input))
    const method = init?.method ?? 'GET'
    const b = typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : {}
    const path = url.pathname.slice(PREFIX.length)
    this.seen.push({ method, path, body: b })
    const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status, headers: { 'x-scadbuddy-crud': '1' } })
    let m: RegExpMatchArray | null
    if (path === '/push/vapid') {
      if (method === 'POST') this.vapid ??= b as { public_key: string; private_key: string }
      return json({ vapid: this.vapid ?? null })
    }
    if (path === '/push/subscriptions' && method === 'POST') {
      const held = this.subs.get(b.endpoint as string)
      const owner = b.owner as { kind: string; id: string; label: string }
      if (held && held.owner.id !== owner.id) return json({ detail: 'this push endpoint is subscribed for someone else', reason: 'subscription-taken' }, 409)
      const id = held?.id ?? randomUUID()
      this.subs.set(b.endpoint as string, { id, owner, p256dh: b.p256dh as string, auth: b.auth as string })
      return json({ id })
    }
    if (path === '/push/subscriptions') return json({ subscriptions: [...this.subs].map(([endpoint, s]) => ({ ...s, endpoint })) })
    if (path === '/push/subscriptions/remove') return json({ done: this.subs.delete(b.endpoint as string) })
    if (path === '/push/sent') {
      const key = `${b.request_id}|${b.target}`
      const claimed = !this.sent.has(key)
      this.sent.add(key)
      return json({ claimed })
    }
    const view = (c: NonNullable<ReturnType<typeof this.channels.get>>) => ({
      id: c.id,
      kind: c.kind,
      name: c.name,
      url: c.url,
      target: c.target,
      enabled: c.enabled,
      has_token: c.token !== null,
      token_last4: c.token ? c.token.slice(-4) : '',
      last_error: c.last_error,
      last_sent_at: null,
    })
    if (path === '/notify-channels' && method === 'GET') return json({ channels: [...this.channels.values()].map(view) })
    if (path === '/notify-channels' && method === 'POST') {
      const id = randomUUID()
      this.channels.set(id, { id, kind: b.kind as string, name: b.name as string, url: b.url as string, target: (b.target as string) ?? null, enabled: b.enabled as boolean, token: (b.token as string) ?? null, last_error: null })
      return json({ id }, 201)
    }
    if (path === '/notify-channels/open-enabled') {
      return json({ channels: [...this.channels.values()].filter((c) => c.enabled).map((c) => ({ id: c.id, kind: c.kind, url: c.url, target: c.target, token: c.token })) })
    }
    if ((m = path.match(/^\/notify-channels\/([^/]+)$/))) {
      const c = this.channels.get(m[1]!)
      return json({ channel: c ? view(c) : null })
    }
    if ((m = path.match(/^\/notify-channels\/([^/]+)\/update$/))) {
      const c = this.channels.get(m[1]!)
      if (!c) return json({ done: false })
      if (b.keep_token && (c.kind !== b.kind || c.url !== b.url)) return json({ detail: 'the channel changed since it was read', reason: 'channel-moved' }, 409)
      Object.assign(c, { kind: b.kind, name: b.name, url: b.url, target: b.target, enabled: b.enabled, token: b.keep_token ? c.token : ((b.token as string) ?? null) })
      return json({ done: true })
    }
    if ((m = path.match(/^\/notify-channels\/([^/]+)\/open$/))) {
      const c = this.channels.get(m[1]!)
      return json({ channel: c ? { id: c.id, kind: c.kind, url: c.url, target: c.target, token: c.token } : null })
    }
    if ((m = path.match(/^\/notify-channels\/([^/]+)\/record$/))) {
      this.channels.get(m[1]!)!.last_error = (b.error as string | null) ?? null
      return json({ done: true })
    }
    if ((m = path.match(/^\/notify-channels\/([^/]+)\/delete$/))) return json({ done: this.channels.delete(m[1]!) })
    return json({ detail: `no route ${method} ${path}` }, 404)
  }
}

describe('the small stores through CRUD routes (#5299 step 5)', () => {
  it('lists every small store area the backend serves', () => {
    expect([...STORE_AREAS]).toEqual(['activity-kinds', 'browser-origins', 'bridge-tabs', 'pairings', 'mcp-tokens', 'notify'])
  })

  it('upserts the activity kinds in one call', async () => {
    const api = new FakeApi()
    await crudUpsertActivityKinds(target(api), [{ key: 'agent.a', label: 'A', facets: [] }])
    expect(api.kinds).toEqual([{ key: 'agent.a', label: 'A' }])
    expect(api.seen.map((s) => `${s.method} ${s.path}`)).toEqual(['POST /activity-kinds'])
  })

  it("keeps a session's approved origins on the API once the area is listed", async () => {
    const api = new FakeApi()
    let listed = false
    // The SQL side is never reached while the area is listed: a Sql that throws shows it.
    const noSql = (() => {
      throw new Error('SQL used')
    }) as never
    const origins = new BrowserOrigins(noSql, undefined, { crud: new CrudSessionOrigins(target(api)), use: () => listed })
    await expect(origins.approved(SESSION)).rejects.toThrow('SQL used')
    listed = true
    expect(await origins.approve(SESSION, 'https://example.com', 'session')).toBe(true)
    expect(await origins.approve(SESSION, 'https://example.com', 'once')).toBe(true)
    expect(await origins.approved(SESSION)).toEqual(new Map([['https://example.com', 'session']]))
    const [row] = await origins.listApproved()
    expect(row).toEqual({ session_id: SESSION, title: 'Chat', origin: 'https://example.com', scope: 'session', approved_at: '2026-11-15T07:35:00.123Z' })
    expect(await origins.revoke(SESSION, 'https://example.com')).toBe(true)
    // A session id that is not one is answered here, as the SQL store answers it.
    expect(await origins.approved('not-a-uuid')).toEqual(new Map())
  })

  it('pairs a session with a tab only while the row still names the tab it saw', async () => {
    const api = new FakeApi()
    const tabs = new CrudSessionTabStore(target(api))
    expect(await tabs.get(SESSION)).toBeUndefined()
    expect(await tabs.setIf(SESSION, 'tab-a', undefined)).toBe(true)
    expect(await tabs.setIf(SESSION, 'tab-b', 'tab-x')).toBe(false)
    expect(await tabs.setIf(SESSION, 'tab-b', 'tab-a')).toBe(true)
    await tabs.set(SESSION, 'tab-c')
    expect(await tabs.get(SESSION)).toBe('tab-c')
    expect(api.seen.at(-2)?.body).toEqual({ session_id: SESSION, tab_id: 'tab-c' })
  })

  it("claims, reads and releases a tab's owner, the claim time as Postgres wrote it", async () => {
    const api = new FakeApi()
    const owners = new CrudTabOwnerStore(target(api))
    await owners.claim('t1', 'agent-tools.a')
    expect(await owners.ownerOf('t1')).toEqual({ queue: 'agent-tools.a', claimedAt: '2026-11-15 07:35:00.123456+00' })
    await owners.refresh([], 'agent-tools.a')
    await owners.release('t1', 'agent-tools.a', '2026-11-15 07:35:00.123456+00')
    expect(api.seen.find((s) => s.path === '/tab-owners/release')?.body).toEqual({
      tab_id: 't1',
      queue: 'agent-tools.a',
      claimed_before: '2026-11-15 07:35:00.123456+00',
    })
    expect(await owners.ownerOf('t1')).toBeUndefined()
    // An empty refresh sends nothing.
    expect(api.seen.filter((s) => s.path === '/tab-owners/refresh')).toHaveLength(0)
  })

  it('refuses an id or a name that is not one before it builds the path: ../ never reaches the API', async () => {
    const api = new FakeApi()
    const bad = '../order-costs'
    expect(await new CrudPairingStore(target(api)).deny(bad)).toBe(false)
    expect(await new CrudPairingStore(target(api)).accept(bad, 'ABCD-EFGH', 't1')).toEqual({ ok: false, reason: 'gone' })
    expect(await new CrudTokenStore(target(api)).revoke(bad)).toBe(false)
    expect(await new CrudChannelRows(target(api)).delete(bad)).toBe(false)
    expect(await new CrudChannelRows(target(api)).view(`${randomUUID()}/../x`)).toBeUndefined()
    // The last guard, under every store's own check: a failed rule is never sent.
    expect(() => segment(bad, UUID_SEGMENT, "an id")).toThrow(CrudPathError)
    const images = new CrudTabImageStore(target(api))
    await expect(images.put({ name: `../${'a'.repeat(64)}.png`, mediaType: 'image/png', bytes: Buffer.from('x') } as never)).rejects.toThrow(CrudPathError)
    expect(await images.take('../x.png')).toBeUndefined()
    expect(api.seen).toEqual([])
  })

  it('sends a long list of tabs in runs the route takes', async () => {
    const api = new FakeApi()
    const tabIds = Array.from({ length: TABS_PER_CALL * 2 + 1 }, (_, i) => `t${i}`)
    await new CrudTabOwnerStore(target(api)).refresh(tabIds, 'agent-tools.a')
    const refreshes = api.seen.filter((s) => s.path === '/tab-owners/refresh').map((s) => ((s.body as { tab_ids: string[] }).tab_ids).length)
    expect(refreshes).toEqual([TABS_PER_CALL, TABS_PER_CALL, 1])
    await new CrudPairingStore(target(api)).pairedWith(tabIds)
    const reads = api.seen.filter((s) => s.path === '/pairings/paired-with').map((s) => ((s.body as { tab_ids: string[] }).tab_ids).length)
    expect(reads).toEqual([TABS_PER_CALL, TABS_PER_CALL, 1])
  })

  it("sends a tab's image as bytes, never JSON, and takes it once", async () => {
    const api = new FakeApi()
    const images = new CrudTabImageStore(target(api))
    const bytes = Buffer.from('\x89PNG\r\n\x1a\n-pixels', 'binary')
    const image: SessionImage = { name: `${createHash('sha256').update(bytes).digest('hex')}.png`, mediaType: 'image/png', bytes }
    await images.put(image)
    const put = api.seen[0]!
    expect([put.method, put.contentType, put.body]).toEqual(['PUT', 'image/png', undefined])
    expect(put.bytes?.equals(bytes)).toBe(true)
    const taken = await images.take(image.name)
    expect(taken?.mediaType).toBe('image/png')
    expect(taken?.bytes.equals(bytes)).toBe(true)
    expect(await images.take(image.name)).toBeUndefined()
    await expect(images.put({ ...image, bytes: Buffer.from('other') })).rejects.toThrow(/sha256/)
  })

  it('pairs with the code hashed: the API never sees the code', async () => {
    const api = new FakeApi()
    const pairings = new CrudPairingStore(target(api))
    const agent = { kind: 'bearer', id: 'token:t1', tiers: ['read'] } as const
    const request = await pairings.request(agent)
    expect(JSON.stringify(api.seen)).not.toContain(request.code)
    expect((api.seen[0]!.body as { code_hash: string }).code_hash).toBe(hashCode(request.code))
    expect((await pairings.pending()).map((p) => p.id)).toEqual([request.id])

    expect(await pairings.accept(request.id, 'WRONG-CODE', 'tab-1')).toEqual({ ok: false, reason: 'wrong_code', attemptsLeft: 4 })
    const accepted = await pairings.accept(request.id, request.code.toLowerCase().replace('-', ' '), 'tab-1')
    expect(accepted).toMatchObject({ ok: true, principal: { kind: 'bearer', id: 'token:t1' } })
    expect(JSON.stringify(api.seen)).not.toContain(request.code)
    expect((await pairings.pairedTab(agent))?.tabId).toBe('tab-1')
    expect([...(await pairings.pairedWith(['tab-1', 'tab-2'])).keys()]).toEqual(['tab-1'])
    expect(await pairings.end(request.id, 'tab-2')).toBe(false)
    expect(await pairings.end(request.id, 'tab-1')).toBe(true)
    expect(await pairings.accept('not-a-uuid', 'x', 't')).toEqual({ ok: false, reason: 'gone' })
  })

  it("turns the API's cap refusal into the tool's PairingError, and refuses the browser user", async () => {
    const api = new FakeApi()
    api.capReached = true
    const pairings = new CrudPairingStore(target(api))
    const refused = pairings.request({ kind: 'anonymous', id: 'anon', tiers: ['read'] })
    await expect(refused).rejects.toBeInstanceOf(PairingError)
    await expect(refused).rejects.toThrow('too many pairing requests are waiting for the user')
    await expect(pairings.request({ kind: 'browser', id: 'browser', tiers: ['read'] })).rejects.toBeInstanceOf(PairingError)
  })

  it('mints and verifies MCP tokens by their hashes only', async () => {
    const api = new FakeApi()
    const tokens = new CrudTokenStore(target(api))
    const { token, record } = await tokens.mint({ name: 'laptop', tier: 'write' })
    expect(token.startsWith(TOKEN_PREFIX)).toBe(true)
    expect((api.seen[0]!.body as { token_hash: string }).token_hash).toBe(hashToken(token))
    expect(await tokens.verify(token)).toEqual({ id: `token:${record.id}`, kind: 'bearer', tiers: ['read', 'write'] })
    // The plaintext never reached the API, in a mint or a verify.
    expect(JSON.stringify(api.seen)).not.toContain(token)
    const [listed] = await tokens.list()
    expect(listed?.lastUsedAt).toEqual(new Date('2026-11-15T07:35:00.123Z'))
    expect(await tokens.liveTier(record.id)).toBe('write')
    expect(await tokens.revoke(record.id)).toBe(true)
    expect(await tokens.verify(token)).toBeNull()
    expect(await tokens.liveTier(record.id)).toBeNull()
    expect(await tokens.approvalGrant('not-a-uuid')).toBe(false)
    await expect(tokens.mint({ name: 'x', tier: 'read', approvalGrant: true })).rejects.toThrow(/outward/)
  })

  it('keeps Web Push records on the API with no KEK in this process', async () => {
    const api = new FakeNotifyApi()
    const push = new CrudPushStore({ baseUrl: BASE, fetch: api.fetch })
    const first = await push.vapidKeys()
    expect(first.publicKey).toHaveLength(65)
    // Made here once, stored there: the next read is the stored pair.
    expect((await push.vapidKeys()).privateKey.equals(first.privateKey)).toBe(true)
    expect(api.seen.filter((s) => s.method === 'POST' && s.path === '/push/vapid')).toHaveLength(1)

    const owner = { kind: 'browser', id: 'browser', label: 'You' } as const
    const keys = { p256dh: Buffer.concat([Buffer.from([4]), Buffer.alloc(64, 7)]), auth: Buffer.alloc(16, 9) }
    const id = await push.subscribe(owner, 'https://push.example/1', keys)
    expect(await push.subscribe(owner, 'https://push.example/1', keys)).toBe(id)
    await expect(push.subscribe({ kind: 'bearer', id: 'token:x', label: 'x' }, 'https://push.example/1', keys)).rejects.toBeInstanceOf(SubscriptionTakenError)
    const [sub] = await push.subscriptions()
    expect(sub?.keys.p256dh.equals(keys.p256dh) && sub.keys.auth.equals(keys.auth)).toBe(true)
    expect(await push.claim('question:1', `push:${id}`)).toBe(true)
    expect(await push.claim('question:1', `push:${id}`)).toBe(false)
    expect(await push.unsubscribe('https://push.example/1')).toBe(true)
  })

  it('keeps notification channels on the API with no KEK in this process, once it serves area notify', async () => {
    const api = new FakeNotifyApi()
    let listed = false
    const noSql = (() => {
      throw new Error('SQL used')
    }) as never
    const resolve = () => Promise.resolve(['203.0.113.5'])
    const channels = new ChannelStore(noSql, undefined, resolve, { crud: new CrudChannelRows({ baseUrl: BASE, fetch: api.fetch }), use: () => listed })
    // No KEK and no area yet: the routes answer "unavailable".
    expect(channels.available()).toBe(false)
    listed = true
    expect(channels.available()).toBe(true)

    const id = await channels.create({ kind: 'home_assistant', name: 'HA', url: 'https://ha.example/', target: 'mobile_app_x', token: 'long-lived-token-1234' })
    const [view] = await channels.list()
    expect(view).toMatchObject({ id, url: 'https://ha.example', has_token: true, token_last4: '1234' })
    // A rename keeps the token on the API: it is never sent back.
    await channels.update(id, { name: 'Home' })
    const rename = api.seen.find((s) => s.path.endsWith('/update'))!
    expect(rename.body).toEqual({ kind: 'home_assistant', name: 'Home', url: 'https://ha.example', target: 'mobile_app_x', enabled: true, keep_token: true })
    expect((await channels.open(id))?.token).toBe('long-lived-token-1234')
    // Moving it needs a new token, as over SQL.
    await expect(channels.update(id, { url: 'https://other.example' })).rejects.toThrow(/long-lived access token/)
    expect((await channels.enabled()).map((c) => c.id)).toEqual([id])
    await channels.record(id, 'HTTP 500')
    expect((await channels.list())[0]?.last_error).toBe('HTTP 500')
    expect(await channels.delete(id)).toBe(true)
  })

  it('picks the CRUD store per call once the area is listed', async () => {
    let listed = false
    const sql = { name: () => 'sql' }
    const crud = { name: () => 'crud' }
    const store = gated(sql, crud, () => listed)
    expect(store.name()).toBe('sql')
    listed = true
    expect(store.name()).toBe('crud')
  })

  it("counts only the route's own answer: a proxy's is refused, and an abort stops the wait", async () => {
    const proxy: typeof fetch = async () => new Response('{"tab_id":"x"}', { status: 200 })
    await expect(crudCall(proxy, BASE, 'a read', '/session-tabs')).rejects.toBeInstanceOf(CrudReadError)
    const never: typeof fetch = (_input, init) =>
      new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(init.signal!.reason)))
    const stop = new AbortController()
    const call = new CrudSessionTabStore({ baseUrl: BASE, fetch: never }).get(SESSION, stop.signal)
    stop.abort(new Error('the call gave up'))
    await expect(call).rejects.toThrow('the call gave up')
  })
})

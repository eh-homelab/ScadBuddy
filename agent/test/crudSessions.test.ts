import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { gated } from '../src/api/crudStores.js'
import { CrudAreas, SESSION_AREAS } from '../src/api/crudAreas.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { CrudSessionRows, PgSessionRows, type SessionRows } from '../src/sessions/rows.js'
import { CrudResourceRows, PgResourceRows, type ResourceRows, SessionResources } from '../src/sessions/touched.js'

// A session's row and what its calls touched through the API's CRUD routes (#5299 step 9, part
// a), against a fake API that answers as the backend's api/crud_agent_sessions.py does (the
// backend's tests/crud/test_crud_agent_sessions.py covers the routes and their SQL). The stores
// keep their interfaces: each method goes to its route once the area is listed, and to its own
// SQL until then.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const YOU = { kind: 'browser', id: 'browser', label: 'You' } as const
const TOKEN = { kind: 'bearer', id: 'token:42', label: 'CI' } as const

const WIRE_ROW = {
  id: SESSION,
  origin: 'chat',
  owner_kind: 'bearer',
  owner_id: 'token:42',
  owner_label: 'CI',
  creator_kind: 'bearer',
  creator_id: 'token:42',
  offer_kind: 'bearer',
  offer_id: 'token:7',
  offer_label: 'Other',
  offer_until: '2026-11-15T09:00:00.000Z',
  status: 'idle',
  title: 'A chat',
  tags: ['t'],
  scope: { model: 'm' },
  parent_id: null,
  runtime: 'unified',
  max_turns: 20,
  budget_usd: 5,
  cost_usd: 1,
  pool_cost_usd: 1.5,
  turns: 2,
  turn_active: false,
  created_at: '2026-11-15T07:00:00.123Z',
  updated_at: '2026-11-15T07:30:00.000Z',
  archived_at: null,
}
const WIRE_TURN_ROW = {
  ...WIRE_ROW,
  unpriced_cost_usd: 0.25,
  agent_model: null,
  agent_effort: null,
  agent_type: null,
  background_tools: true,
  turn_flow: false,
  turn_tiers: { tiers: ['read'] },
  durable_turn_id: null,
  claimed_at: '2026-11-15T07:30:00.123456Z',
}

type Seen = { method: string; path: string; body: unknown }

/** The API's side: each request recorded, each answered from `answers` by `METHOD path` (query included). */
class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, { status: number; body: unknown }>()
  notCrud = false

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = `${url.pathname.slice(PREFIX.length)}${url.search}`
    const text = request.method === 'POST' ? await request.text() : ''
    this.seen.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined })
    if (this.notCrud) return new Response('not found', { status: 404 })
    const key = `${request.method} ${path}`
    const answer = this.answers.get(key) ?? { status: 404, body: { detail: `no answer for ${key}` } }
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown, status = 200): this {
    this.answers.set(key, { status, body })
    return this
  }
}

/** A Sql that fails the test if a store runs any SQL on it. */
const noSql = new Proxy(() => {}, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

describe('the sessions areas', () => {
  it('are among the areas the agent waits for', async () => {
    expect(SESSION_AREAS).toEqual(['sessions', 'session-resources', 'transcripts', 'session-events', 'session-order-costs'])
    const api = new FakeApi().answer('GET /areas', { areas: ['settings', 'sessions'] })
    const areas = new CrudAreas(BASE, { fetch: api.fetch, wanted: SESSION_AREAS, log: () => {} })
    expect(await areas.refresh()).toBe(false)
    expect(areas.has('sessions')).toBe(true)
    expect(areas.has('session-resources')).toBe(false)
  })

  it('runs its own SQL until the area is listed, then only the routes', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}`, { session: WIRE_TURN_ROW })
    const listed = { on: false }
    const rows = gated<SessionRows>(new PgSessionRows(noSql), new CrudSessionRows(BASE, { fetch: api.fetch }), () => listed.on)
    await expect(rows.get(SESSION)).rejects.toThrow('ran SQL')
    expect(api.seen).toEqual([])
    listed.on = true
    expect((await rows.get(SESSION))?.id).toBe(SESSION)
  })
})

describe("a session's row through the CRUD routes", () => {
  const rows = (api: FakeApi) => new CrudSessionRows(BASE, { fetch: api.fetch })

  it('reads the row whole, its timestamps as dates', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}`, { session: WIRE_TURN_ROW })
    expect(await rows(api).get(SESSION)).toEqual({
      ...WIRE_TURN_ROW,
      offer_until: new Date('2026-11-15T09:00:00.000Z'),
      created_at: new Date('2026-11-15T07:00:00.123Z'),
      updated_at: new Date('2026-11-15T07:30:00.000Z'),
      archived_at: null,
    })
  })

  it('reads none for a session that is gone, and asks nothing for an id that is no UUID', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}`, { session: null })
    expect(await rows(api).get(SESSION)).toBeUndefined()
    for (const bad of ['not-a-uuid', '../areas', `${SESSION}/../../areas`, '', `${SESSION}?x=1`]) {
      expect(await rows(api).get(bad)).toBeUndefined()
    }
    expect(api.seen.map((s) => s.path)).toEqual([`/sessions/${SESSION}`])
  })

  it('names an uppercase id as Postgres does, lowercase', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}`, { session: WIRE_TURN_ROW })
    expect((await rows(api).get(SESSION.toUpperCase()))?.id).toBe(SESSION)
  })

  it('never writes to an id that is no UUID', async () => {
    const api = new FakeApi()
    const store = rows(api)
    await expect(store.offer('../areas', TOKEN, YOU, 1000)).rejects.toThrow('is not a session id')
    await expect(store.transfer('../x', TOKEN, YOU, false)).rejects.toThrow('is not a session id')
    await expect(store.raiseBudget('x/../y', YOU, 1, 100)).rejects.toThrow('is not a session id')
    await expect(store.discard('..')).rejects.toThrow('is not a session id')
    expect(api.seen).toEqual([])
  })

  it("lists a principal's sessions, every one for the browser user", async () => {
    const api = new FakeApi().answer('POST /sessions/list', { sessions: [WIRE_ROW] })
    const store = rows(api)
    const [row] = await store.list(TOKEN, { search: 'a', accept: ['.stl'], withFile: true, resource: { type: 'model', id: 'gear' }, limit: 900 })
    expect(row?.created_at).toEqual(new Date('2026-11-15T07:00:00.123Z'))
    await store.list(YOU, { archived: 'only', status: 'idle', origin: 'mcp' })
    expect(api.seen.map((s) => s.body)).toEqual([
      {
        principal: { kind: 'bearer', id: 'token:42' },
        archived: 'exclude',
        resource: { type: 'model', id: 'gear' },
        search: 'a',
        accept: ['.stl'],
        with_file: true,
        limit: 500,
      },
      { principal: null, status: 'idle', origin: 'mcp', archived: 'only', accept: [], with_file: false, limit: 100 },
    ])
  })

  it('counts the sessions an owner started lately', async () => {
    const api = new FakeApi().answer('GET /session-starts?owner_kind=bearer&owner_id=token%3A42&window_ms=60000', { count: 3 })
    expect(await rows(api).startedRecently(TOKEN, 60_000)).toBe(3)
  })

  it('inserts a row, and tells the owner limit and a forgotten parent from a failure', async () => {
    const api = new FakeApi().answer('POST /sessions', { session: WIRE_TURN_ROW })
    const store = rows(api)
    const row = {
      id: SESSION,
      origin: 'chat' as const,
      owner: TOKEN,
      title: 't',
      tags: [],
      scope: {},
      parentId: '11111111-1111-4111-8111-111111111111',
      runtime: 'unified' as const,
      maxTurns: 20,
      budgetUsd: 5,
      budgetOf: '11111111-1111-4111-8111-111111111111',
      forkOf: { parentId: '11111111-1111-4111-8111-111111111111', cut: 4 },
      backgroundTools: false as const,
      limit: { max: 10, windowMs: 60_000 },
    }
    expect(await store.insert(row)).toBeUndefined()
    expect(api.seen[0]?.body).toEqual({
      id: SESSION,
      origin: 'chat',
      owner: TOKEN,
      title: 't',
      tags: [],
      scope: {},
      parent_id: '11111111-1111-4111-8111-111111111111',
      runtime: 'unified',
      max_turns: 20,
      budget_usd: 5,
      budget_of: '11111111-1111-4111-8111-111111111111',
      fork_of: { parent_id: '11111111-1111-4111-8111-111111111111', cut: 4 },
      background_tools: false,
      limit: { max: 10, window_ms: 60_000 },
    })
    api.answer('POST /sessions', { detail: 'too many', reason: 'rate_limited' }, 429)
    expect(await store.insert(row)).toBe('rate_limited')
    api.answer('POST /sessions', { detail: 'forgotten', reason: 'forgotten' }, 404)
    expect(await store.insert(row)).toBe('forgotten')
    api.answer('POST /sessions', { detail: 'gone', reason: 'parent_gone' }, 404)
    await expect(store.insert(row)).rejects.toBeInstanceOf(CrudReadError)
    api.notCrud = true
    await expect(store.insert(row)).rejects.toThrow('no CRUD route answered')
  })

  it('sends every string well-formed: a lone surrogate goes as U+FFFD, as postgres.js wrote it', async () => {
    const api = new FakeApi().answer('POST /sessions', { session: WIRE_TURN_ROW }).answer('POST /sessions/list', { sessions: [] })
    const store = rows(api)
    await store.insert({
      id: SESSION,
      origin: 'chat',
      owner: TOKEN,
      title: 'café \ud800 🙂',
      tags: ['t\udc00'],
      scope: {},
      parentId: null,
      runtime: 'unified',
      maxTurns: 20,
      budgetUsd: 5,
      limit: { max: 10, windowMs: 60_000 },
    })
    await store.list(TOKEN, { search: 'a\ud800' })
    const [insert, list] = api.seen.map((s) => s.body as Record<string, unknown>)
    expect(insert?.title).toBe('café \ufffd 🙂')
    expect(insert?.tags).toEqual(['t\ufffd'])
    expect(list?.search).toBe('a\ufffd')
  })

  it('hands off only while the row is as it was read', async () => {
    const api = new FakeApi()
      .answer(`POST /sessions/${SESSION}/offer`, { changed: true })
      .answer(`POST /sessions/${SESSION}/offer/cancel`, { changed: false })
      .answer(`POST /sessions/${SESSION}/owner`, { changed: true })
    const store = rows(api)
    expect(await store.offer(SESSION, TOKEN, YOU, 1000)).toBe(true)
    expect(await store.cancelOffer(SESSION, YOU)).toBe(false)
    expect(await store.transfer(SESSION, TOKEN, YOU, true)).toBe(true)
    expect(api.seen.map((s) => s.body)).toEqual([
      { owner: { kind: 'bearer', id: 'token:42' }, to: YOU, ttl_ms: 1000 },
      { to: { kind: 'browser', id: 'browser' } },
      { owner: { kind: 'bearer', id: 'token:42' }, to: YOU, accepting: true },
    ])
  })

  it('raises the budget, discards a failed fork and sets a chat agent', async () => {
    const api = new FakeApi()
      .answer(`POST /sessions/${SESSION}/budget`, { budget_usd: 7.5 })
      .answer(`POST /sessions/${SESSION}/discard`, { discarded: true })
      .answer(`POST /sessions/${SESSION}/agent`, { ok: true })
    const store = rows(api)
    expect(await store.raiseBudget(SESSION, YOU, 2.5, 100)).toBe(7.5)
    api.answer(`POST /sessions/${SESSION}/budget`, { budget_usd: null })
    expect(await store.raiseBudget(SESSION, YOU, 2.5, 100)).toBeUndefined()
    expect(await store.discard(SESSION)).toBe(true)
    await store.setAgent(SESSION, { model: 'm', effort: null, type: null })
    expect(api.seen.map((s) => s.body)).toEqual([
      { by: { kind: 'browser', id: 'browser' }, add_usd: 2.5, max_usd: 100 },
      { by: { kind: 'browser', id: 'browser' }, add_usd: 2.5, max_usd: 100 },
      {},
      { model: 'm', effort: null, type: null },
    ])
  })
})

describe('what a session touched through the CRUD routes', () => {
  const touch = {
    tool: 'render_model',
    resource_type: 'output' as const,
    resource_id: 'out-1',
    action: 'created' as const,
    model_slug: 'gear',
    before_id: null,
    after_id: null,
  }

  it("records a call's rows, refused for a session that is gone", async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/resources`, { recorded: true })
    const store = new CrudResourceRows(BASE, { fetch: api.fetch })
    await store.insert(SESSION, [{ ...touch, resource_id: `bad\ud800` }])
    expect(api.seen[0]?.body).toEqual({ rows: [{ ...touch, resource_id: 'bad�' }] })
    api.answer(`POST /sessions/${SESSION}/resources`, { recorded: false })
    await expect(store.insert(SESSION, [touch])).rejects.toThrow('is gone')
    await expect(store.insert('../areas', [touch])).rejects.toThrow('is not a session id')
    expect(api.seen).toHaveLength(2)
  })

  it('lists them, none for an id that is no UUID', async () => {
    const api = new FakeApi().answer(`GET /sessions/${SESSION}/resources`, { resources: [{ ...touch, at: '2026-11-15T07:00:00.123Z' }] })
    const store = new CrudResourceRows(BASE, { fetch: api.fetch })
    expect(await store.list(SESSION)).toEqual([
      { type: 'output', id: 'out-1', action: 'created', model: 'gear', before: null, after: null, tool: 'render_model', at: '2026-11-15T07:00:00.123Z' },
    ])
    expect(await store.list('../x')).toEqual([])
    expect(api.seen).toHaveLength(1)
  })

  it('goes through the routes once the area is listed, and a failure never reaches the call', async () => {
    const api = new FakeApi()
    const listed = { on: true }
    const errors: unknown[] = []
    const sink = new SessionResources(
      gated<ResourceRows>(new PgResourceRows(noSql), new CrudResourceRows(BASE, { fetch: api.fetch }), () => listed.on),
      (err) => errors.push(err),
    )
    await sink.record({
      sessionId: SESSION,
      tool: { name: 'delete_model', risk: 'write' },
      input: { slug: 'gear' },
      result: { content: [{ type: 'text', text: '{}' }] },
    })
    expect(api.seen.map((s) => `${s.method} ${s.path}`)).toEqual([`POST /sessions/${SESSION}/resources`])
    expect(errors).toHaveLength(1)
  })
})

import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { gated } from '../src/api/crudStores.js'
import { childReasoning } from '../src/flows/queue.js'
import { durableRequestId } from '../src/gate/ids.js'
import { SessionEventPublisher } from '../src/sessions/busEvents.js'
import { CrudPublisher, PgPublisher, type Publisher } from '../src/sessions/publish.js'
import { CrudSessionReads, PgSessionReads, type SessionReads } from '../src/sessions/sessionReads.js'
import { RowSessionOwners } from '../src/temporal/toolActivities.js'
import type { TurnRow } from '../src/sessions/rows.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// The agent's NOTIFYs and a session's last event reads through the API's CRUD routes (#6501),
// against a fake API that answers as the backend's api/crud_agent_publish.py does (the
// backend's tests/crud/test_crud_agent_publish.py covers the routes and the NOTIFYs). A session
// id goes into a path only as a lowercase UUID, encoded; an id that is no UUID sends nothing.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'

type Seen = { method: string; path: string; body: unknown }

class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, { status: number; body: unknown }>()

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = `${url.pathname.slice(PREFIX.length)}${url.search}`
    const text = request.method === 'POST' ? await request.text() : ''
    this.seen.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined })
    const key = `${request.method} ${path}`
    const answer = this.answers.get(key) ?? { status: 404, body: { detail: `no answer for ${key}` } }
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown, status = 200): this {
    this.answers.set(key, { status, body })
    return this
  }
}

const noSql = new Proxy(() => {}, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

const target = (api: FakeApi) => ({ baseUrl: BASE, fetch: api.fetch })

describe('the publishes over the CRUD routes', () => {
  it('sends a session event as the bus event the SQL NOTIFYed', async () => {
    const api = new FakeApi().answer('POST /publish/session-event', { sent: true })
    const publisher = new SessionEventPublisher(gated<Publisher>(new PgPublisher(noSql), new CrudPublisher(target(api)), () => true), {
      replica: 'r1',
    })
    publisher.onAppend(SESSION, [{ type: 'session.status', sessionId: SESSION, status: 'idle' } as ServerEvent], 7)
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(api.seen).toEqual([
      {
        method: 'POST',
        path: '/publish/session-event',
        body: { id: expect.any(String), at: expect.any(String), kind: 'session.done', session_id: SESSION, seq: 7, status: 'idle', replica: 'r1' },
      },
    ])
    publisher.close()
  })

  it('sends a warm eviction for a session or every one, and nothing for any other target', async () => {
    const api = new FakeApi().answer('POST /publish/warm-evict', { sent: true })
    const publish = new CrudPublisher(target(api))
    await publish.warmEvict(SESSION.toUpperCase())
    await publish.warmEvict('*')
    await publish.warmEvict('../areas')
    await publish.warmEvict('not-a-session')
    await publish.sessionEvent({ id: 'x', at: 'now', kind: 'session.message', session_id: '../areas', seq: 1, replica: 'r' })
    expect(api.seen.map((s) => s.body)).toEqual([{ target: SESSION }, { target: '*' }])
  })

  it('throws what the API refuses, which the publisher hands to onError', async () => {
    const api = new FakeApi().answer('POST /publish/warm-evict', { detail: 'no' }, 422)
    await expect(new CrudPublisher(target(api)).warmEvict('*')).rejects.toThrow(/HTTP 422/)
  })
})

describe('the session reads over the CRUD routes', () => {
  const reads = (api: FakeApi) => gated<SessionReads>(new PgSessionReads(noSql), new CrudSessionReads(target(api)), () => true)

  it('reads the open calls, after a turn or all of them', async () => {
    const calls = [{ id: 'toolu_1', name: 'read' }]
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/open-calls`, { calls })
      .answer(`GET /sessions/${SESSION}/open-calls?turn_id=t%2F1`, { calls: [] })
    expect(await reads(api).openCalls(SESSION)).toEqual(calls)
    expect(await reads(api).openCalls(SESSION.toUpperCase(), 't/1')).toEqual([])
    // A turn id past the API's bound is no logged turn: every open call.
    expect(await reads(api).openCalls(SESSION, 't'.repeat(501))).toEqual(calls)
  })

  it('reads a child reasoning, and none for a call not logged', async () => {
    const tool = 'toolu_2'
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/reasoning?tool_use_id=${tool}&scan=2000`, {
        logged: true,
        deltas: [
          { message: 'm1', delta: 'One.' },
          { message: 'm2', delta: 'Two.' },
        ],
      })
      .answer(`GET /sessions/${SESSION}/reasoning?tool_use_id=toolu_x&scan=2000`, { logged: false, deltas: [] })
    expect(await childReasoning(reads(api), durableRequestId(SESSION, 'run-1', tool))).toBe('One.\n\nTwo.')
    expect(await childReasoning(reads(api), durableRequestId(SESSION, 'run-1', 'toolu_x'))).toBeUndefined()
  })

  it('sends no path for an id that is no UUID, and throws as the SQL cast did', async () => {
    const api = new FakeApi()
    await expect(reads(api).openCalls('../areas')).rejects.toThrow(/not a session id/)
    await expect(reads(api).reasoningDeltas('../areas', 't', 10)).rejects.toThrow(/not a session id/)
    expect(await reads(api).reasoningDeltas(SESSION, 'x'.repeat(501), 10)).toBeUndefined()
    expect(api.seen).toEqual([])
  })
})

describe("a session's owner from its row", () => {
  it('answers the owner with only the sender’s own tiers, and throws for an id that is no UUID', async () => {
    const row = {
      owner_kind: 'bearer',
      owner_id: 'token:1',
      owner_label: 'MCP token:1',
      turn_tiers: { by: { kind: 'bearer', id: 'token:1' }, tiers: ['read', 'nonsense', 'write'] },
      durable_turn_id: 'turn-1',
    } as unknown as TurnRow
    const owners = new RowSessionOwners({ get: (id) => Promise.resolve(id === SESSION ? row : undefined) })
    expect(await owners.ownerOf(SESSION)).toEqual({ kind: 'bearer', id: 'token:1', label: 'MCP token:1', turnTiers: ['read', 'write'], turnId: 'turn-1' })
    expect(await owners.ownerOf('7f1d2c3b-4a5e-4f60-8a9b-0c1d2e3f4a5b')).toBeUndefined()
    await expect(owners.ownerOf('../areas')).rejects.toThrow(/not a session id/)
    // A handoff since leaves the new owner its default.
    const handed = new RowSessionOwners({ get: () => Promise.resolve({ ...row, owner_id: 'token:2' } as TurnRow) })
    expect((await handed.ownerOf(SESSION))?.turnTiers).toBeNull()
  })
})

import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { gated } from '../src/api/crudStores.js'
import { CrudEventRows } from '../src/sessions/crudEvents.js'
import { type EventRows, EventLog, ForkParentGone, PgEventRows } from '../src/sessions/eventLog.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// A session's event log through the API's CRUD routes (#5299 step 9, part b), against a fake API
// that answers as the backend's api/crud_agent_events.py does (the backend's
// tests/crud/test_crud_agent_events.py covers the routes, the fence and the chain). An event
// travels as its JSON text, an inherited one is addressed to the session here, a session id goes
// into a path only as a lowercase UUID, and the log keeps its interface over either side.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const PARENT = '7f1d2c3b-4a5e-4f60-8a9b-0c1d2e3f4a5b'

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

const rows = (api: FakeApi) => new CrudEventRows(BASE, { fetch: api.fetch })
const delta = (sessionId: string, delta: string) => ({ type: 'assistant.text.delta', sessionId, messageId: 'm', delta }) as ServerEvent

describe('an event log over the CRUD routes', () => {
  it('appends the events as their JSON text, with the fence, and answers their seqs', async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/events`, { seqs: [4, 5] })
    const events = [delta(SESSION, 'a 🙂'), delta(SESSION, 'b')]
    expect(await rows(api).append(SESSION, events, { attempt: 'run:1', turnId: 't' })).toEqual([4, 5])
    expect(api.seen[0]?.body).toEqual({ events: events.map((e) => JSON.stringify(e)), fence: { attempt: 'run:1', turn_id: 't' } })
    await rows(api).append(SESSION, events)
    expect((api.seen[1]?.body as Record<string, unknown>).fence).toBeUndefined()
  })

  it('tells a refused fence and a session with no row apart', async () => {
    const refused = new FakeApi().answer(`POST /sessions/${SESSION}/events`, { seqs: null })
    expect(await rows(refused).append(SESSION, [delta(SESSION, 'a')], { attempt: 'run:0', turnId: 't' })).toBeUndefined()
    const gone = new FakeApi().answer(`POST /sessions/${SESSION}/events`, { detail: 'no row', reason: 'session_gone' }, 404)
    await expect(rows(gone).append(SESSION, [delta(SESSION, 'a')])).rejects.toThrow(`session ${SESSION} does not exist`)
  })

  it('reads the log after a seq, an inherited event addressed to the session', async () => {
    const parentTurn = { type: 'user.turn', sessionId: PARENT, turnId: 't', text: 'hi' }
    const api = new FakeApi().answer(`GET /sessions/${SESSION}/events?after=0&limit=500`, {
      events: [
        { seq: 1, event: JSON.stringify({ type: 'session.started', sessionId: SESSION }), inherited: false },
        { seq: 2, event: JSON.stringify(parentTurn), inherited: true },
      ],
    })
    expect(await rows(api).read(SESSION, 0, 500)).toEqual([
      { seq: 1, event: { type: 'session.started', sessionId: SESSION } },
      { seq: 2, event: { ...parentTurn, sessionId: SESSION } },
    ])
  })

  it('answers the other reads from their routes', async () => {
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/events/last-seq`, { seq: 7 })
      .answer(`GET /sessions/${SESSION}/events/user-turn?turn_id=t%2F..`, { text: 'make a box' })
      .answer(`GET /sessions/${SESSION}/events/turn?turn_id=t`, { here: true, after: 3, events: [JSON.stringify(delta(SESSION, 'a'))] })
      .answer(`GET /sessions/${SESSION}/events/turn?turn_id=t&after=3`, { here: false, after: null, events: [] })
    const store = rows(api)
    expect(await store.lastSeq(SESSION.toUpperCase())).toBe(7)
    expect(await store.userTurnText(SESSION, 't/..')).toBe('make a box')
    expect(await store.turnEvents(SESSION, 't', undefined)).toEqual({ here: true, after: '3', events: [delta(SESSION, 'a')] })
    expect(await store.turnEvents(SESSION, 't', '3')).toEqual({ here: false, after: undefined, events: [] })
  })

  it("writes a fork's log in one request, and tells a parent forgotten since", async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/fork-log`, { seqs: [1, 9] })
    const started = { type: 'session.started', sessionId: SESSION } as unknown as ServerEvent
    const idle = { type: 'session.status', sessionId: SESSION, status: 'idle' } as ServerEvent
    expect(await rows(api).forkLog(SESSION, PARENT.toUpperCase(), 8, [started], [idle])).toEqual([1, 9])
    expect(api.seen[0]?.body).toEqual({ parent_id: PARENT, cut: 8, started: [JSON.stringify(started)], then: [JSON.stringify(idle)] })
    const gone = new FakeApi().answer(`POST /sessions/${SESSION}/fork-log`, { detail: 'forgotten', reason: 'parent_gone' }, 404)
    await expect(rows(gone).forkLog(SESSION, PARENT, 0, [started], [])).rejects.toBeInstanceOf(ForkParentGone)
  })

  it('reads nothing for an id that is no UUID, and never writes to one', async () => {
    const api = new FakeApi()
    const store = rows(api)
    for (const bad of ['not-a-uuid', '../areas', `${SESSION}/../../areas`, '', `${SESSION}?x=1`]) {
      expect(await store.read(bad, 0, 10)).toEqual([])
      expect(await store.lastSeq(bad)).toBe(0)
      expect(await store.userTurnText(bad, 't')).toBeUndefined()
      expect(await store.turnEvents(bad, 't', undefined)).toEqual({ here: false, after: undefined, events: [] })
      await expect(store.append(bad, [delta(SESSION, 'a')])).rejects.toThrow('is not a session id')
      await expect(store.forkLog(bad, PARENT, 0, [], [])).rejects.toThrow('is not a session id')
    }
    expect(api.seen).toEqual([])
  })

  it('keeps the log on its SQL until the area is listed, then on the routes, and wakes on a fenced write', async () => {
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/events/last-seq`, { seq: 2 })
      .answer(`POST /sessions/${SESSION}/events`, { seqs: [3] })
    const listed = { on: false }
    const appended: number[] = []
    const log = new EventLog(noSql, {
      rows: gated<EventRows>(new PgEventRows(noSql), rows(api), () => listed.on),
      onAppend: (_id, _events, last) => appended.push(last),
    })
    await expect(log.lastSeq(SESSION)).rejects.toThrow('ran SQL')
    listed.on = true
    expect(await log.lastSeq(SESSION)).toBe(2)
    expect(await log.appendFenced(SESSION, [delta(SESSION, 'a')], { attempt: 'run:1', turnId: 't' })).toBe(true)
    expect(appended).toEqual([3])
  })
})

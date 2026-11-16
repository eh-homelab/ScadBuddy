import { randomUUID } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { CrudAreas, TURN_AREAS } from '../src/api/crudAreas.js'
import { CrudReadError } from '../src/api/crudRead.js'
import { gated } from '../src/api/crudStores.js'
import { type DurableTurn, DurableTurns } from '../src/sessions/durable.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { SessionRecord } from '../src/sessions/manager.js'
import { event } from '../src/sessions/protocol.js'
import { CrudTurnRows, type TurnRows } from '../src/sessions/turnRows.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { SESSION_BEGIN_TURN_ACTIVITY, SESSION_FINISH_ACTIVITY } from '../src/temporal/session/names.js'

// A durable turn's row writes through the API's CRUD routes (#6449), against a fake API that
// answers as the backend's api/crud_agent_turns.py does (the backend's
// tests/crud/test_crud_agent_turns.py covers the routes and their fences;
// test/turnRowsParity.pg.test.ts the port against the SQL). A session id goes into a path only as
// a lowercase UUID, encoded; a turn id is a UUID or never sent; the events travel as their JSON
// text and come back as the logged batch the caller hands its followers.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const TURN = '7f1d2c3b-4a5e-4f60-8a9b-0c1d2e3f4a5b'
const BROWSER = { kind: 'browser' as const, id: 'browser' }

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

const rows = (api: FakeApi) => new CrudTurnRows(BASE, { fetch: api.fetch })
const status = (s: 'idle' | 'running') => event({ type: 'session.status', sessionId: SESSION, status: s })

describe('a turn\'s row writes over the CRUD routes', () => {
  it('claims a send, with the claim in the body and the session in the path', async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/turn/send-claim`, { claimed: { prior_status: 'failed', runtime: 'unified' } })
    const claimed = await rows(api).sendClaim(SESSION.toUpperCase(), { author: BROWSER, turnId: TURN, tiers: ['read'], bare: true })
    expect(claimed).toEqual({ prior_status: 'failed', runtime: 'unified' })
    expect(api.seen[0]?.body).toEqual({ author: BROWSER, turn_id: TURN, tiers: ['read'], bare: true })
    api.answer(`POST /sessions/${SESSION}/turn/send-claim`, { claimed: null })
    expect(await rows(api).sendClaim(SESSION, { author: BROWSER, turnId: TURN, tiers: null, bare: false })).toBeUndefined()
  })

  it('sends no call for a path id or a turn id that is no UUID', async () => {
    const api = new FakeApi()
    const crud = rows(api)
    for (const bad of ['../x', `${SESSION}/../../stores`, '..', 'not-a-uuid', '']) {
      await expect(crud.sendClaim(bad, { author: BROWSER, turnId: TURN, tiers: null, bare: false })).rejects.toThrow(/not a session id/)
      await expect(crud.state(bad)).rejects.toThrow(/not a session id/)
      await expect(crud.endLost(bad, '1', [status('idle')])).rejects.toThrow(/not a session id/)
      await expect(crud.placement(SESSION, bad, 'run:1', 'k')).rejects.toThrow(/not a turn id/)
      await expect(crud.finish(SESSION, bad, [status('idle')])).rejects.toThrow(/not a turn id/)
    }
    expect(api.seen).toEqual([])
  })

  it('reads the turn, the queue and the stale page with their queries', async () => {
    const turn = { status: 'running', durable_turn_id: TURN, turn_attempt: 'run:1', interrupt_requested: false, owner_kind: 'browser', runtime: 'unified', event_seq: '4', cost_usd: 0.5, settled: true }
    const queued = event({ type: 'session.queue', sessionId: SESSION, version: 2, queue: [] })
    const api = new FakeApi()
      .answer(`GET /sessions/${SESSION}/turn?grace_ms=120000`, { turn })
      .answer(`GET /sessions/${SESSION}/turn/queue`, { event: JSON.stringify(queued) })
      .answer(`GET /turns/stale?after=${SESSION}&grace_ms=0&limit=10`, { rows: [{ id: SESSION, event_seq: '4' }] })
    expect(await rows(api).state(SESSION, 120_000)).toEqual(turn)
    expect(await rows(api).lastQueue(SESSION)).toEqual(queued)
    expect(await rows(api).stale(SESSION, 0, 10)).toEqual([{ id: SESSION, event_seq: '4' }])
  })

  it('ends a turn with its unworded tail and its head, and parses what was logged', async () => {
    const result = event({ type: 'session.result', sessionId: SESSION, costUsd: 0.25, turns: 1, budgetUsd: 1 })
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/turn/finish`, {
      ended: { events: [JSON.stringify(result), JSON.stringify(status('idle'))], seqs: [5, 6] },
      next: { outcome: 'started', logged: { events: [JSON.stringify(status('running'))], seqs: [7] } },
    })
    const committed = vi.fn()
    const events = { committed } as unknown as EventLog
    const finish = sessionFinishActivities(undefined as never, events, undefined, undefined, rows(api))[SESSION_FINISH_ACTIVITY]
    const next = {
      turn_id: TURN,
      text: 'next',
      words: 'next',
      author: { ...BROWSER, label: 'You' },
      images: [],
      system_append: null,
      tiers: null,
      bare: false,
      queued_at: '2026-11-16T00:00:00Z',
    }
    const answer = await finish({ sessionId: SESSION, turnId: TURN, outcome: 'done', next } as never)
    expect(answer).toEqual({ written: true, next: 'started' })
    const sent = api.seen[0]?.body as { turn_id: string; tail: string[]; next: { turn_id: string; started: string[]; bare: boolean; tiers: null } }
    expect(sent.turn_id).toBe(TURN)
    // Unworded: the end words the result with the row's spend under its lock.
    expect(JSON.parse(sent.tail[0]!)).toMatchObject({ type: 'session.result', costUsd: 0, budgetUsd: 0 })
    expect(sent.next.started.map((t) => (JSON.parse(t) as { type: string }).type)).toEqual(['user.turn', 'session.status'])
    expect(committed.mock.calls).toEqual([
      [SESSION, [result, status('idle')], [5, 6]],
      [SESSION, [status('running')], [7]],
    ])
  })

  it('hears a refused head as dropped, and a begun one\'s events once', async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/turn/begin`, { outcome: 'busy', logged: null })
    const committed = vi.fn()
    const begin = sessionFinishActivities(undefined as never, { committed } as unknown as EventLog, undefined, undefined, rows(api))[SESSION_BEGIN_TURN_ACTIVITY]
    const next = { turn_id: TURN, text: 't', words: 't', author: { ...BROWSER, label: 'You' }, images: [], system_append: null, queued_at: '' }
    expect(await begin({ sessionId: SESSION, next } as never)).toBe('busy')
    expect(committed).not.toHaveBeenCalled()
  })

  it('sends a spend with its key, and a turn id lowercase', async () => {
    const api = new FakeApi()
      .answer(`POST /sessions/${SESSION}/turn/spend`, { written: true })
      .answer(`POST /sessions/${SESSION}/turn/placement`, { mark: 3 })
    expect(await rows(api).spend(SESSION, { key: 'tok:0', costUsd: 0.25, unpricedUsd: 0.25, turns: 0 })).toBe(true)
    expect(api.seen[0]?.body).toEqual({ key: 'tok:0', cost_usd: 0.25, unpriced_usd: 0.25, turns: 0 })
    expect(await rows(api).placement(SESSION, TURN.toUpperCase(), 'run:1', TURN)).toBe(3)
    expect(api.seen[1]?.body).toMatchObject({ turn_id: TURN })
  })

  it('gives back a send\'s claim whose answer was lost once the row shows it, and only then', async () => {
    const session = { id: SESSION } as SessionRecord
    const turn = { turnId: TURN.toUpperCase(), prompt: 'hi', text: 'hi', author: BROWSER, images: [], systemAppend: '' } as unknown as DurableTurn
    const run = async (err: Error, holder: string | null) => {
      const giveBack = vi.fn(async () => undefined)
      const state = vi.fn(async () => ({ durable_turn_id: holder }))
      const turns = { sendClaim: vi.fn(async () => Promise.reject(err)), state, giveBack } as unknown as TurnRows
      const durable = new DurableTurns({ client: {} as never, sql: {} as never, events: {} as EventLog, turns })
      await expect(durable.send(session, turn)).rejects.toBe(err)
      return { giveBack, state }
    }
    // No answer: the claim may have committed. The row runs this turn, so it is given back.
    const lost = await run(new CrudReadError('the claim: fetch failed'), TURN)
    expect(lost.giveBack.mock.calls).toEqual([[SESSION, 'idle']])
    // The row runs another turn: nothing is given back.
    expect((await run(new CrudReadError('the claim: fetch failed'), randomUUID())).giveBack).not.toHaveBeenCalled()
    // The route answered (its transaction rolled back): the row is not read.
    const refused = await run(new CrudReadError('the claim: 503', 503, true), TURN)
    expect(refused.state).not.toHaveBeenCalled()
    expect(refused.giveBack).not.toHaveBeenCalled()
  })

  it('a refusal of the route throws, never reads as a fence that held', async () => {
    const api = new FakeApi().answer(`POST /sessions/${SESSION}/turn/placement`, { detail: 'down' }, 503)
    await expect(rows(api).placement(SESSION, TURN, 'run:1', TURN)).rejects.toThrow(/503/)
    // A non-CRUD answer (an old API's 404) is no answer either.
    const old: typeof fetch = async () => new Response('{}', { status: 404 })
    await expect(new CrudTurnRows(BASE, { fetch: old }).finish(SESSION, TURN, [status('idle')])).rejects.toThrow()
  })

  it('waits for area session-turns', async () => {
    expect(TURN_AREAS).toEqual(['session-turns'])
    const api = new FakeApi().answer('GET /areas', { areas: ['sessions'] })
    const areas = new CrudAreas(BASE, { fetch: api.fetch, wanted: TURN_AREAS, log: () => {} })
    expect(await areas.refresh()).toBe(false)
    expect(areas.has('session-turns')).toBe(false)
  })

  it('stays on the SQL until the API lists the area', async () => {
    const sql = { interrupt: vi.fn(async () => true) } as unknown as TurnRows
    const crud = { interrupt: vi.fn(async () => false) } as unknown as TurnRows
    let listed = false
    const turns = gated<TurnRows>(sql, crud, () => listed)
    expect(await turns.interrupt(SESSION)).toBe(true)
    listed = true
    expect(await turns.interrupt(SESSION)).toBe(false)
  })
})

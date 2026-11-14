import { describe, expect, it } from 'vitest'
import { CrudAreas, CrudReadError, crudRead, READ_AREAS } from '../src/api/crudAreas.js'
import { CrudEventLogReads } from '../src/events/eventLogReads.js'
import { CrudFlowRunReads, type FlowRunReads, gatedFlowRunReads, STILL_WAITING_BATCH } from '../src/flows/backendReads.js'

// The agent's reads of the backend's tables through its CRUD routes (#5299 D7): the clients
// against a fake API, and the rolling deploy's switch from SQL once the API lists the areas.

const BASE = 'http://api:8080/'
const AGENT = 'http://api:8080/api/v1/internal/crud/agent'

type Route = (url: URL, request: Request) => { status?: number; body?: unknown; crud?: boolean } | undefined

/** A fake API: each request is answered by the first route that answers it, else a non-CRUD 404. */
function api(routes: Route[], seen: Request[] = []): typeof fetch {
  return async (input, init) => {
    const request = new Request(input, init)
    seen.push(request.clone())
    const url = new URL(request.url)
    for (const route of routes) {
      const answer = route(url, request)
      if (answer === undefined) continue
      return new Response(answer.body === undefined ? null : JSON.stringify(answer.body), {
        status: answer.status ?? 200,
        headers: answer.crud === false ? {} : { 'x-scadbuddy-crud': '1' },
      })
    }
    return new Response('not found', { status: 404 })
  }
}

const at = (path: string, body: unknown): Route => (url) => (url.pathname === `/api/v1/internal/crud/agent${path}` ? { body } : undefined)

describe('crudRead', () => {
  it("returns only a CRUD route's own answer", async () => {
    expect(await crudRead(api([at('/x', { ok: 1 })]), BASE, 'x', '/x')).toEqual({ ok: 1 })
    // An older API without the route (no CRUD header), and the route's own refusal.
    await expect(crudRead(api([]), BASE, 'the x', '/x')).rejects.toThrow('no CRUD route answered the x (HTTP 404)')
    const refused = api([() => ({ status: 422, body: { detail: 'bad' } })])
    const error = await crudRead(refused, BASE, 'the x', '/x').catch((e: unknown) => e)
    expect(error).toBeInstanceOf(CrudReadError)
    expect((error as CrudReadError).message).toBe('the backend refused the x (HTTP 422: bad)')
    expect((error as CrudReadError).status).toBe(422)
    // A 200 with no CRUD header is a proxy's, never the route's.
    await expect(crudRead(api([() => ({ body: {}, crud: false })]), BASE, 'x', '/x')).rejects.toThrow('no CRUD route answered')
  })

  it('names a backend that is not up, never its URL', async () => {
    const down: typeof fetch = async () => {
      throw new TypeError('fetch failed', { cause: Object.assign(new Error('refused'), { code: 'ECONNREFUSED' }) })
    }
    const error = (await crudRead(down, BASE, 'the x', '/x').catch((e: unknown) => e)) as Error
    expect(error.message).toBe('the backend is not up yet (the x: ECONNREFUSED)')
    expect(error.message).not.toContain('api:8080')
  })
})

describe('CrudAreas', () => {
  it('waits, logging once, until the API lists every area, then holds it', async () => {
    const logs: string[] = []
    let listed: string[] = ['order-costs']
    const areas = new CrudAreas(BASE, { fetch: api([() => ({ body: { areas: listed } })]), wanted: READ_AREAS, log: (m) => logs.push(m) })
    expect(await areas.refresh()).toBe(false)
    expect(await areas.refresh()).toBe(false)
    expect(areas.has('events')).toBe(false)
    expect(logs).toEqual(["CRUD areas: waiting for the backend's agent areas (missing events, flows); reading over SQL until it lists them"])
    listed = ['order-costs', 'events']
    expect(await areas.refresh()).toBe(false)
    expect([areas.has('events'), areas.has('flows'), areas.complete]).toEqual([true, false, false])
    listed = ['events', 'flows']
    expect(await areas.refresh()).toBe(true)
    expect(areas.complete).toBe(true)
    expect(logs.at(-1)).toBe("CRUD areas: the agent reads events, flows through the backend's CRUD routes")
    // Listed once, for good: an older pod answering later does not take it back.
    listed = []
    await areas.refresh()
    expect(areas.has('flows')).toBe(true)
  })

  it('treats an API without the areas route as one that lists none', async () => {
    const logs: string[] = []
    const areas = new CrudAreas(BASE, { fetch: api([]), wanted: ['flows'], log: (m) => logs.push(m) })
    expect(await areas.refresh()).toBe(false)
    expect(logs[0]).toContain('no CRUD route answered its areas (HTTP 404)')
  })

  it("names a refused credential, apart from waiting for an older pod (#6234 review)", async () => {
    const logs: string[] = []
    let status = 404
    const fetchAreas: typeof fetch = async () => new Response('', { status, headers: status === 404 ? {} : { 'x-scadbuddy-crud': '1' } })
    const areas = new CrudAreas(BASE, { fetch: fetchAreas, wanted: READ_AREAS, log: (m) => logs.push(m) })
    await areas.refresh()
    status = 401
    await areas.refresh()
    await areas.refresh()
    expect(logs).toHaveLength(2)
    expect(logs[1]).toContain("the backend refused the agent's service credential (HTTP 401)")
  })

  it('reads again with back-off from the start until they are listed', async () => {
    let calls = 0
    const fetchAreas = api([
      () => {
        calls += 1
        return { body: { areas: calls >= 3 ? ['events', 'flows'] : [] } }
      },
    ])
    const areas = new CrudAreas(BASE, { fetch: fetchAreas, wanted: READ_AREAS, log: () => {}, firstMs: 1, maxMs: 4 })
    areas.start()
    for (let i = 0; i < 200 && !areas.complete; i++) await new Promise((r) => setTimeout(r, 5))
    expect(areas.complete).toBe(true)
    const after = calls
    await new Promise((r) => setTimeout(r, 30))
    expect(calls).toBe(after)
    areas.stop()
  })
})

describe('CrudFlowRunReads', () => {
  const STEP = { seq: 1, fn: 'queue_print', call_id: 'c1', status: 'running', session_id: null }
  const WAITING = { call_id: 'c1', kind: 'approval', fn: 'queue_print', prompt: null, since: '2026-11-14T10:00:00Z', hop: null, hop_until: null, reason: null, proposed: false, hops: [] }
  const RUN = { id: 'r/1', name: 'n', status: 'waiting', workflow_run_id: 'e1', starter_session: 's1', waiting_on: [WAITING], steps: [STEP] }

  it("asks for a run's starter, and none for no run", async () => {
    const seen: Request[] = []
    const starter = { open: true, unrecorded: false, started_by: { kind: 'agent', principal: 'browser', session: null } }
    const reads = new CrudFlowRunReads(BASE, { fetch: api([at('/flow-runs/r%2F1/starter', { run: starter }), at('/flow-runs/r2/starter', { run: null })], seen) })
    expect(await reads.starter('r/1', 'e 1')).toEqual(starter)
    expect(seen[0]!.url).toBe(`${AGENT}/flow-runs/r%2F1/starter?execution=e+1`)
    expect(await reads.starter('r2', 'e1')).toBeUndefined()
  })

  it("maps a decision's checks", async () => {
    const seen: Request[] = []
    const reads = new CrudFlowRunReads(BASE, {
      fetch: api([at('/flow-decisions', { approved: true, approved_unresolved: false, by_person: true, lapsed: false })], seen),
    })
    expect(await reads.decisions('flow:a:b:c/d', 'h1')).toEqual({ approved: true, approvedUnresolved: false, byPerson: true, lapsed: false })
    const url = new URL(seen[0]!.url)
    expect([url.searchParams.get('request_id'), url.searchParams.get('input_hash')]).toEqual(['flow:a:b:c/d', 'h1'])
  })

  it('asks whether a grant is in force, by query', async () => {
    const seen: Request[] = []
    const reads = new CrudFlowRunReads(BASE, { fetch: api([at('/flow-grants/in-force', { in_force: true })], seen) })
    expect(await reads.grantInForce('g/1')).toBe(true)
    expect(new URL(seen[0]!.url).searchParams.get('grant_id')).toBe('g/1')
  })

  it("reads a run's queue with its times as dates", async () => {
    const reads = new CrudFlowRunReads(BASE, {
      fetch: api([
        at('/flow-runs/r%2F1/queue', { run: { ...RUN, requested_grant: { id: 'g1', created_at: '2026-11-14T10:00:00.123456Z' } } }),
        at('/flow-runs/none/queue', { run: null }),
        at('/flow-runs/waiting', { runs: [{ ...RUN, updated_at: '2026-11-14T11:00:00+00:00' }] }),
        at('/flow-runs/r%2F1/steps', { steps: [STEP] }),
      ]),
    })
    const queue = await reads.queue('r/1')
    expect(queue?.requested_grant?.created_at.toISOString()).toBe('2026-11-14T10:00:00.123Z')
    expect(queue?.waiting_on).toEqual([WAITING])
    expect(queue?.starter_session).toBe('s1')
    expect(await reads.queue('none')).toBeUndefined()
    const [waiting] = await reads.waitingRuns()
    expect(waiting?.updated_at.toISOString()).toBe('2026-11-14T11:00:00.000Z')
    expect(await reads.steps('r/1')).toEqual([STEP])
  })

  it('pages the parent-hop approvals with the cursor it was given', async () => {
    const seen: Request[] = []
    const next = { after_updated_at: '2026-11-14T10:00:00.123456Z', after_id: 'r1' }
    const reads = new CrudFlowRunReads(BASE, { fetch: api([at('/flow-runs/parent-hop-approvals', { runs: [], next: null })], seen) })
    expect(await reads.parentHopApprovals(null, 200)).toEqual({ runs: [], next: null })
    await reads.parentHopApprovals(next, 5)
    expect(Object.fromEntries(new URL(seen[0]!.url).searchParams)).toEqual({ limit: '200' })
    expect(Object.fromEntries(new URL(seen[1]!.url).searchParams)).toEqual({ limit: '5', ...next })
  })

  it('asks which calls still wait at hop parent in batches', async () => {
    const seen: Request[] = []
    const ids = Array.from({ length: STILL_WAITING_BATCH + 3 }, (_, i) => `flow:r:e:c${i}`)
    const reads = new CrudFlowRunReads(BASE, {
      fetch: api(
        [
          (url, request) => {
            if (url.pathname !== '/api/v1/internal/crud/agent/flow-runs/parent-hop/waiting' || request.method !== 'POST') return undefined
            return { body: { waiting: ['flow:r:e:c0', `flow:r:e:c${STILL_WAITING_BATCH + 1}`] } }
          },
        ],
        seen,
      ),
    })
    expect(await reads.stillAtParentHop(ids)).toEqual(new Set(['flow:r:e:c0', `flow:r:e:c${STILL_WAITING_BATCH + 1}`]))
    expect(seen).toHaveLength(2)
    expect(((await seen[0]!.json()) as { request_ids: string[] }).request_ids).toHaveLength(STILL_WAITING_BATCH)
    expect(await seen[1]!.json()).toEqual({ request_ids: ids.slice(STILL_WAITING_BATCH) })
    expect(await reads.stillAtParentHop([])).toEqual(new Set())
    expect(seen).toHaveLength(2)
  })

  it("passes a route's failure on, so the caller's error path runs", async () => {
    const reads = new CrudFlowRunReads(BASE, { fetch: api([() => ({ status: 503, body: { detail: 'database-unavailable' } })]) })
    await expect(reads.starter('r1', 'e1')).rejects.toThrow("the backend refused a flow run's starter (HTTP 503: database-unavailable)")
    await expect(reads.waitingRuns()).rejects.toBeInstanceOf(CrudReadError)
  })
})

describe('gatedFlowRunReads', () => {
  it('reads over SQL until the area is listed, then through the routes', async () => {
    const said = (who: string): FlowRunReads =>
      ({
        starter: async () => ({ open: true, unrecorded: false, started_by: { kind: who } }),
        decisions: async () => ({ approved: who === 'crud', approvedUnresolved: false, byPerson: false, lapsed: false }),
        grantInForce: async () => who === 'crud',
        queue: async () => undefined,
        waitingRuns: async () => [],
        steps: async () => [{ seq: 1, fn: who, call_id: 'c', status: 'running' }],
        parentHopApprovals: async () => ({ runs: [], next: null }),
        stillAtParentHop: async () => new Set([who]),
      }) satisfies FlowRunReads
    let listed = false
    const reads = gatedFlowRunReads(said('sql'), said('crud'), () => listed)
    expect((await reads.starter('r', 'e'))?.started_by.kind).toBe('sql')
    expect(await reads.grantInForce('g')).toBe(false)
    listed = true
    expect((await reads.starter('r', 'e'))?.started_by.kind).toBe('crud')
    expect(await reads.grantInForce('g')).toBe(true)
    expect((await reads.decisions('q', 'h')).approved).toBe(true)
    expect((await reads.steps('r'))[0]?.fn).toBe('crud')
    expect(await reads.stillAtParentHop(['x'])).toEqual(new Set(['crud']))
  })
})

describe('CrudEventLogReads', () => {
  it("reads the log's head and its gap, newest first, with the database clock", async () => {
    const seen: Request[] = []
    const reads = new CrudEventLogReads(BASE, {
      fetch: api(
        [
          at('/events/head', { seq: '9007199254740993', at: '2026-11-14T10:00:00Z' }),
          at('/events', {
            events: [
              { seq: '12', event_id: 'b', payload: { id: 'b' }, logged_at: '2026-11-14T10:00:02Z' },
              { seq: '11', event_id: 'a', payload: { id: 'a' }, logged_at: '2026-11-14T10:00:01Z' },
            ],
            read_at: '2026-11-14T10:00:03Z',
          }),
        ],
        seen,
      ),
    })
    // A bigint past 2^53 is kept exact.
    expect(await reads.head()).toEqual({ seq: 9007199254740993n, at: new Date('2026-11-14T10:00:00Z') })
    const rows = await reads.gap(10n, 1001)
    expect(rows.map((r) => [r.seq, r.event_id, r.payload])).toEqual([
      ['12', 'b', { id: 'b' }],
      ['11', 'a', { id: 'a' }],
    ])
    expect(rows[1]?.logged_at.toISOString()).toBe('2026-11-14T10:00:01.000Z')
    expect(rows.every((r) => r.read_at.toISOString() === '2026-11-14T10:00:03.000Z')).toBe(true)
    expect(Object.fromEntries(new URL(seen[1]!.url).searchParams)).toEqual({ after: '10', limit: '1001' })
  })
})

import { describe, expect, it } from 'vitest'
import { CrudReadError, crudRead } from '../src/api/crudRead.js'
import { CrudFlowRunReads, STILL_WAITING_BATCH } from '../src/flows/backendReads.js'

// The agent's reads of the backend's tables through its CRUD routes (#5299 D7): the clients
// against a fake API.

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

import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudPending, IDS_MAX } from '../src/gate/crudPending.js'
import { REQUEST_ID_MAX } from '../src/gate/crudStore.js'
import { cancelOrphanedEntries, PendingInputSweep } from '../src/gate/sweep.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import { PgApprovalRecords } from '../src/temporal/toolActivities.js'

// The gate's pending input through the API's CRUD routes (#5299 step 10, area `pending-input`),
// against a fake API that answers as backend api/crud_agent_pending.py does (its tests,
// tests/crud/test_crud_agent_pending.py, cover the SQL, the turn fence's lock and the sweep's
// writes on Postgres). Ids go in the body or the query, never the path; an id no row can hold is
// never sent; the sweep's cancels reach the event log; and an approval counts only from the API's
// answer for this call's hash.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SID = '11111111-1111-4111-8111-111111111111'
const RID = `durable:${SID}:run-1:toolu_1`
const HASH = 'a'.repeat(64)
const TRACE = '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01'

type Seen = { method: string; path: string; body: Record<string, unknown> | undefined }

class FakeApi {
  seen: Seen[] = []
  answers: unknown[] = []

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const raw = init?.body
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    this.seen.push({ method: init?.method ?? 'GET', path: url.pathname.slice(PREFIX.length) + url.search, body })
    const answer = this.answers.shift()
    if (answer instanceof Response) return Promise.resolve(answer)
    return Promise.resolve(new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
  }
}

/** SQL that must never be reached. */
const noSql = new Proxy(() => undefined, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

function pending(): { api: FakeApi; crud: CrudPending } {
  const api = new FakeApi()
  return { api, crud: new CrudPending({ baseUrl: BASE, fetch: api.fetch }) }
}

const ROW = {
  request_id: RID,
  kind: 'approval',
  session_id: SID,
  tool: 'print_output',
  summary: 'box',
  input_hash: HASH,
  prompt: '',
  requested_by: { kind: 'browser', id: 'browser', label: 'You' },
  responders: ['browser', 'grant'],
  attention: null,
  flow_run_id: 'run-9',
  hop: 'person',
  outside_reason: 'not in the grant',
  created_at: '2026-11-15T10:00:00.123456Z',
  expires_at: '2026-11-15T10:10:00Z',
  expiring: true,
}

describe('the pending input through the CRUD routes', () => {
  it('reads the entries a filter names as the SQL did', async () => {
    const { api, crud } = pending()
    api.answers.push({ entries: [ROW] })
    const entries = await crud.entries({ ownedBy: { kind: 'bearer', id: 'agent-1' }, allApprovals: true })
    expect(api.seen[0]).toEqual({
      method: 'POST',
      path: '/pending-input/entries',
      body: { session_id: null, owned_by: { kind: 'bearer', id: 'agent-1' }, all_approvals: true, own_answers: false },
    })
    expect(entries).toEqual([
      {
        id: RID,
        kind: 'approval',
        session_id: SID,
        tool: 'print_output',
        summary: 'box',
        input_hash: HASH,
        prompt: '',
        requested_by: { kind: 'browser', id: 'browser', label: 'You' },
        responders: ['browser', 'grant'],
        created_at: '2026-11-15T10:00:00.123Z',
        expires_at: '2026-11-15T10:10:00.000Z',
        expiring: true,
        run: { id: 'run-9', hop: 'person', reason: 'not in the grant' },
      },
    ])
  })

  it('sends ids in the query, encoded, and never one no row can hold', async () => {
    const { api, crud } = pending()
    api.answers.push({ recorded: null }, { trace: null }, { session: null }, { responder: null })
    expect(await crud.recorded('../../areas?x=1#y')).toBeUndefined()
    expect(await crud.parkTrace('../../areas')).toBeUndefined()
    expect(await crud.session('../x')).toBeUndefined()
    expect(await crud.approval('../../areas', HASH)).toBeNull()
    expect(api.seen.map((s) => s.path)).toEqual([
      '/pending-input/recorded?request_id=..%2F..%2Fareas%3Fx%3D1%23y',
      '/pending-input/park-trace?request_id=..%2F..%2Fareas',
      '/pending-input/session?session_id=..%2Fx',
      `/pending-input/approval?request_id=..%2F..%2Fareas&input_hash=${HASH}`,
    ])
    const long = 'd'.repeat(REQUEST_ID_MAX + 1)
    expect(await crud.recorded(long)).toBeUndefined()
    expect(await crud.response(long)).toBeUndefined()
    expect(await crud.parkTrace(long)).toBeUndefined()
    await crud.markDecision(long, TRACE)
    expect(await crud.markPark(long, TRACE, { sessionId: SID, attempt: 'a', turnId: SID })).toBe(false)
    expect(await crud.approval(long, HASH)).toBeNull()
    expect(await crud.approval(RID, '')).toBeNull()
    expect(await crud.waitsForTab('../x')).toBe(false)
    expect(await crud.inRunOf([long])).toEqual(new Set())
    expect(api.seen).toHaveLength(4)
  })

  it('reads many ids in batches the API takes', async () => {
    const { api, crud } = pending()
    const ids = Array.from({ length: IDS_MAX + 5 }, (_, i) => `r${i}`)
    api.answers.push({ request_ids: ['r0'] }, { request_ids: [`r${IDS_MAX + 1}`] })
    expect(await crud.decidedOf(ids)).toEqual(new Set(['r0', `r${IDS_MAX + 1}`]))
    expect(api.seen.map((s) => (s.body?.request_ids as string[]).length)).toEqual([IDS_MAX, 5])
    expect(await crud.inRunOf([])).toEqual(new Set())
    expect(api.seen).toHaveLength(2)
  })

  it("marks a parked call's span with the turn's fence, and answers what the API found", async () => {
    const { api, crud } = pending()
    api.answers.push({ marked: false })
    const fence = { sessionId: SID, attempt: 'run-1/2', turnId: '22222222-2222-4222-8222-222222222222' }
    expect(await crud.markPark(RID, TRACE, fence)).toBe(false)
    expect(api.seen[0]).toEqual({
      method: 'POST',
      path: '/pending-input/trace',
      body: { request_id: RID, traceparent: TRACE, session_id: SID, attempt: 'run-1/2', turn_id: fence.turnId },
    })
  })

  it("reads a held call's response with the decision's span", async () => {
    const { api, crud } = pending()
    const recorded = { kind: 'answer', outcome: 'answered', response: { answers: ['red'] }, response_hash: 'h', responder: { kind: 'browser' }, reason: null, decision_traceparent: TRACE }
    api.answers.push({ recorded }, { recorded })
    expect(await crud.response(RID)).toEqual({ outcome: 'answered', response: { answers: ['red'] }, reason: null, responder: { kind: 'browser' }, decision_traceparent: TRACE })
    expect(await crud.recorded(RID)).toEqual({ kind: 'answer', outcome: 'answered', response: { answers: ['red'] }, responseHash: 'h', responder: { kind: 'browser' }, reason: null })
  })

  it("sweeps through the routes and hands the API's batches to the event log", async () => {
    const { api, crud } = pending()
    const told: unknown[][] = []
    const events = { committed: (...args: unknown[]) => told.push(args) } as unknown as EventLog
    const batch = { session_id: SID, events: [{ v: 1, type: 'input.resolved' }], seqs: [7] }
    api.answers.push(
      { runs: [{ workflow_id: 'w-open', workflow_run_id: 'r1' }, { workflow_id: 'w-closed', workflow_run_id: 'r2' }] },
      { done: true },
      { removed: 1, logged: [batch] },
    )
    const sweep = new PendingInputSweep({
      sql: noSql,
      events,
      describe: (workflowId) => Promise.resolve(workflowId === 'w-open' ? 'open' : 'closed'),
      routes: { crud, use: () => true },
      minAgeS: 5,
      recheckS: 9,
    })
    expect(await sweep.sweep()).toBe(1)
    expect(api.seen.map((s) => [s.path, s.body])).toEqual([
      ['/pending-input/sweep/runs?min_age_s=5&recheck_s=9', undefined],
      ['/pending-input/sweep/checked', { workflow_id: 'w-open', run_id: 'r1' }],
      ['/pending-input/orphans/cancel', { run: { workflow_id: 'w-closed', run_id: 'r2' }, reason: "the session's workflow run has ended" }],
    ])
    expect(told).toEqual([[SID, batch.events, [7]]])
  })

  it("cancels a stopped session's entries with its reason, and nothing on the SQL", async () => {
    const { api, crud } = pending()
    api.answers.push({ removed: 0, logged: [] })
    const events = { committed: () => undefined } as unknown as EventLog
    expect(await cancelOrphanedEntries({ sql: noSql, events, routes: { crud, use: () => true } }, { sessionId: SID }, 'stopped')).toBe(0)
    expect(api.seen[0]?.body).toEqual({ session_id: SID, reason: 'stopped' })
  })

  it("counts an approval only from the API's answer for this call", async () => {
    const { api, crud } = pending()
    const grants = { grantInForce: (id: string) => Promise.resolve(id === 'g-live') }
    const records = new PgApprovalRecords(noSql, grants, { crud, use: () => true })
    const call = { inputHash: HASH } as Parameters<PgApprovalRecords['approved']>[1]
    api.answers.push(
      { responder: null },
      { responder: { kind: 'browser', id: 'browser', label: 'You' } },
      { responder: { kind: 'browser', id: 'browser', label: 'You' } },
      { responder: { kind: 'run_grant', id: 'g-gone' } },
      { responder: { kind: 'run_grant', id: 'g-gone' } },
    )
    expect(await records.approved(RID, call)).toBe(false)
    expect(await records.approved(RID, call)).toBe(true)
    expect(await records.byPerson(RID, call)).toBe(true)
    expect(await records.approved(RID, call)).toBe(false)
    expect(await records.lapsed(RID, call)).toBe(true)
    // Without the call's hash nothing is asked, and nothing is approved.
    expect(await records.approved(RID)).toBe(false)
    expect(api.seen.every((s) => s.path === `/pending-input/approval?request_id=${encodeURIComponent(RID)}&input_hash=${HASH}`)).toBe(true)
    expect(api.seen).toHaveLength(5)
  })

  it('throws on an answer that is not a CRUD route', async () => {
    const { api, crud } = pending()
    api.answers.push(new Response('{}', { status: 404 }))
    await expect(crud.waitsForTab(SID)).rejects.toThrow(/no CRUD route answered/)
  })
})

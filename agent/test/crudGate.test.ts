import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudGate, REQUEST_ID_MAX } from '../src/gate/crudStore.js'
import { decideInput, denyInput, type GateDb, grantInput, openInput, type OpenInput, readInput, resolveInput } from '../src/gate/store.js'
import { RespondRefusal } from '../src/gate/validate.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import type { ServerEvent } from '../src/sessions/protocol.js'

// The tool-call gate's writes through the API's CRUD routes (#5299 step 10, area `gate`), against
// a fake API that answers as backend api/crud_agent_gate.py does (its tests,
// tests/crud/test_crud_agent_gate.py, cover the SQL, the locks, the validator and the refusals on
// Postgres; test/gateBackend.pg.test.ts checks the API writes what the agent's SQL writes). The
// store takes no SQL once the area is listed; what the API appended reaches the event log.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SID = '11111111-1111-4111-8111-111111111111'
const RID = `durable:${SID}:run-1:toolu_1`
const ME = { kind: 'browser' as const, id: 'browser', label: 'You' }
const STATUS: ServerEvent = { v: 1, type: 'session.status', sessionId: SID, status: 'waiting_approval' } as ServerEvent

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

/** A GateDb whose SQL must never be reached, and whose event log records what it is told. */
function db(api: FakeApi, use = true): { db: GateDb; told: unknown[][] } {
  const told: unknown[][] = []
  const sql = new Proxy(() => undefined, {
    apply: () => {
      throw new Error('the store ran SQL')
    },
    get: () => {
      throw new Error('the store ran SQL')
    },
  }) as unknown as Sql
  const events = { committed: (...args: unknown[]) => told.push(args) } as unknown as EventLog
  return { db: { sql, events, crud: new CrudGate({ baseUrl: BASE, fetch: api.fetch }), useCrud: () => use }, told }
}

const open: OpenInput = {
  requestId: RID,
  sessionId: SID,
  workflowId: `session-${SID}`,
  workflowRunId: 'run-1',
  kind: 'approval',
  tool: 'print_output',
  toolUseId: 'toolu_1',
  summary: 'box',
  inputHash: 'a'.repeat(64),
  prompt: '',
  requestedBy: { ...ME, extra: 'not sent' } as typeof ME,
  responders: ['browser', 'grant'],
  expiresAt: '2026-11-15T12:00:00.000Z',
}

describe('the gate through the CRUD routes', () => {
  it('opens on the API and tells the event log what it appended', async () => {
    const api = new FakeApi()
    const { db: gate, told } = db(api)
    api.answers.push({ opened: true, logged: [{ session_id: SID, events: [STATUS], seqs: [3] }] })
    expect(await openInput(gate, open)).toBe(true)
    expect(api.seen).toEqual([
      {
        method: 'POST',
        path: '/gate/open',
        body: {
          request_id: RID,
          session_id: SID,
          workflow_id: `session-${SID}`,
          workflow_run_id: 'run-1',
          kind: 'approval',
          tool: 'print_output',
          tool_use_id: 'toolu_1',
          summary: 'box',
          input_hash: 'a'.repeat(64),
          prompt: '',
          requested_by: ME,
          responders: ['browser', 'grant'],
          expires_at: '2026-11-15T12:00:00.000Z',
          questions: null,
          attention: null,
          flow_run_id: null,
          hop: null,
          hop_until: null,
          parent_session: null,
          parent_principal: null,
          outside_reason: null,
          gate: null,
        },
      },
    ])
    expect(told).toEqual([[SID, [STATUS], [3]]])
  })

  it('resolves with the raw response hashed, and tells nothing when nothing was written', async () => {
    const api = new FakeApi()
    const { db: gate, told } = db(api)
    api.answers.push({ resolved: false, logged: [] })
    const raw = { kind: 'answer', choice: 'Red' }
    expect(await resolveInput(gate, { requestId: RID, outcome: 'answered', responder: ME, response: { answers: ['Red'] }, rawResponse: raw })).toBe(false)
    const body = api.seen[0]!.body!
    expect(body).toMatchObject({ request_id: RID, outcome: 'answered', responder: ME, response: { answers: ['Red'] }, reason: null })
    expect(body.response_hash).toMatch(/^[0-9a-f]{64}$/)
    expect(told).toEqual([])
  })

  it('denies, reads and grants through their routes', async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    api.answers.push({ denied: true }, { record: { outcome: 'denied', response: null, responder: ME, reason: 'no' } }, { granted: false })
    const deny = { requestId: RID, sessionId: SID, runId: 'flow-1', tool: 'print_output', summary: 'box', inputHash: 'b'.repeat(64), reason: 'out', requestedBy: ME }
    expect(await denyInput(gate, deny)).toBe(true)
    expect(await readInput(gate, { requestId: RID })).toEqual({ outcome: 'denied', response: null, responder: ME, reason: 'no' })
    const grant = { requestId: RID, sessionId: SID, tool: 'print_output', summary: 'box', inputHash: 'b'.repeat(64), grantId: 'g1', requestedBy: ME }
    expect(await grantInput(gate, grant)).toBe(false)
    expect(api.seen.map((s) => `${s.method} ${s.path}`)).toEqual([
      'POST /gate/deny',
      `GET /gate/response?request_id=${encodeURIComponent(RID)}`,
      'POST /gate/grant',
    ])
  })

  it('keeps a request id that could reach another route in the query, encoded', async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    api.answers.push({ record: null })
    expect(await readInput(gate, { requestId: '../../areas?x=1#y' })).toBeNull()
    expect(api.seen[0]!.path).toBe('/gate/response?request_id=..%2F..%2Fareas%3Fx%3D1%23y')
  })

  it('answers an id longer than the API takes as never asked, with no call', async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    const long = `durable:${SID}:run-1:${'t'.repeat(REQUEST_ID_MAX)}`
    expect(await readInput(gate, { requestId: long })).toBeNull()
    expect(await resolveInput(gate, { requestId: long, outcome: 'cancelled', responder: ME })).toBe(false)
    const grant = { requestId: long, sessionId: SID, tool: 't', summary: '', inputHash: 'b'.repeat(64), grantId: 'g1', requestedBy: ME }
    expect(await grantInput(gate, grant)).toBe(false)
    await expect(decideInput(gate, { requestId: long, response: {}, responder: ME, role: 'browser' })).rejects.toMatchObject({ code: 'stale' })
    expect(api.seen).toEqual([])
  })

  it("grants through the API even before it lists the area, and runs the rest over SQL until then", async () => {
    const api = new FakeApi()
    const { db: gate } = db(api, false)
    api.answers.push({ granted: true })
    const grant = { requestId: RID, sessionId: SID, tool: 't', summary: '', inputHash: 'b'.repeat(64), grantId: 'g1', requestedBy: ME }
    expect(await grantInput(gate, grant)).toBe(true)
    await expect(readInput(gate, { requestId: RID })).rejects.toThrow('the store ran SQL')
  })

  it("decides on the API, asking Temporal only once the validator took it", async () => {
    const api = new FakeApi()
    const { db: gate, told } = db(api)
    const live: string[] = []
    api.answers.push(
      { refused: null, workflow: false, live_check: { workflow_id: `session-${SID}`, run_id: 'run-1' }, decided: null, logged: [] },
      { refused: null, workflow: false, live_check: null, decided: { kind: 'approval', outcome: 'approved' }, logged: [{ session_id: SID, events: [STATUS], seqs: [9] }] },
    )
    const decided = await decideInput(gate, {
      requestId: RID,
      response: { kind: 'approval', decision: 'approve' },
      responder: ME,
      role: 'browser',
      live: (workflowId, runId) => (live.push(`${workflowId}/${runId}`), Promise.resolve(true)),
    })
    expect(decided).toEqual({ kind: 'approval', outcome: 'approved' })
    expect(live).toEqual([`session-${SID}/run-1`])
    expect(api.seen.map((s) => [s.body!.check_live, s.body!.live_run])).toEqual([
      [true, null],
      [true, 'run-1'],
    ])
    expect(told).toEqual([[SID, [STATUS], [9]]])
  })

  it("is stale when the row's run has ended, and writes nothing", async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    api.answers.push({ refused: null, workflow: false, live_check: { workflow_id: 'w', run_id: 'r' }, decided: null, logged: [] })
    const call = decideInput(gate, { requestId: RID, response: {}, responder: ME, role: 'browser', live: () => Promise.resolve(false) })
    await expect(call).rejects.toMatchObject({ code: 'stale', message: expect.stringContaining('has ended') as unknown })
    expect(api.seen).toHaveLength(1)
  })

  it("throws the validator's refusal, and answers `workflow` for an entry the workflow holds", async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    api.answers.push(
      { refused: { code: 'self', message: 'never your own' }, workflow: false, live_check: null, decided: null, logged: [] },
      { refused: null, workflow: true, live_check: null, decided: null, logged: [] },
      { refused: { code: 'nonsense', message: '?' }, workflow: false, live_check: null, decided: null, logged: [] },
    )
    const args = { requestId: RID, response: {}, responder: ME, role: 'grant' as const }
    const refused = await decideInput(gate, args).catch((e: unknown) => e)
    expect(refused).toBeInstanceOf(RespondRefusal)
    expect(refused).toMatchObject({ code: 'self', message: 'never your own' })
    expect(await decideInput(gate, args)).toBe('workflow')
    await expect(decideInput(gate, args)).rejects.toThrow('unknown code')
  })

  it("fails a call the API did not answer as the route", async () => {
    const api = new FakeApi()
    const { db: gate } = db(api)
    api.answers.push(new Response('Not Found', { status: 404 }))
    await expect(openInput(gate, open)).rejects.toThrow('no CRUD route answered')
  })
})

import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { CrudApprovals } from '../src/approvals/crudApprovals.js'
import { ApprovalError, ApprovalService } from '../src/approvals/service.js'
import type { EventLog } from '../src/sessions/eventLog.js'

// The classic approvals through the API's CRUD routes (#5299 step 10, area `approvals`), against
// a fake API that answers as backend api/crud_agent_approvals.py does (its tests,
// tests/crud/test_crud_agent_approvals.py, cover the SQL, the once-only decision, its audit row,
// the prepares' bounds and their lock on Postgres). The service takes no SQL once the area is
// listed; who may decide stays its own check, made before any write is asked for; a decision's
// events reach the event log; and a decision that loses is no decision.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SID = '11111111-1111-4111-8111-111111111111'
const AID = '22222222-2222-4222-8222-222222222222'
const ME = { kind: 'browser' as const, id: 'browser', label: 'You' }
const AGENT = { kind: 'bearer' as const, id: 'agent-1', label: 'Agent one' }

type Seen = { path: string; body: Record<string, unknown> | undefined }

class FakeApi {
  seen: Seen[] = []
  answers: unknown[] = []

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const raw = init?.body
    this.seen.push({ path: url.pathname.slice(PREFIX.length) + url.search, body: typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined })
    const answer = this.answers.shift()
    return Promise.resolve(new Response(JSON.stringify(answer), { status: 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }))
  }
}

const noSql = new Proxy(() => undefined, {
  apply: () => {
    throw new Error('ran SQL')
  },
  get: () => {
    throw new Error('ran SQL')
  },
}) as unknown as Sql

function service(api: FakeApi, told: unknown[][] = []): ApprovalService {
  const events = { committed: (...args: unknown[]) => told.push(args) } as unknown as EventLog
  return new ApprovalService({
    sql: noSql,
    events,
    settings: { get: <T>() => Promise.resolve(300 as T) },
    routes: { crud: new CrudApprovals({ baseUrl: BASE, fetch: api.fetch }), use: () => true },
  })
}

function row(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: AID,
    session_id: SID,
    turn_id: null,
    tool_use_id: 'toolu_1',
    tool: 'print_output',
    input_summary: '{"output":"box"}',
    input_hash: 'a'.repeat(64),
    tier: 'outward',
    requested_by_kind: 'bearer',
    requested_by_id: 'agent-1',
    requested_by_label: 'Agent one',
    requested_tiers: null,
    created_at: '2026-11-15T10:00:00Z',
    expires_at: '2026-11-15T10:10:00Z',
    decision: null,
    decided_by_kind: null,
    decided_by_id: null,
    decided_by_label: null,
    decided_at: null,
    reason: null,
    usable_until: null,
    resume_turn_id: null,
    consumed_at: null,
    revoked_at: null,
    traceparent: null,
    decision_traceparent: null,
    due: false,
    ...extra,
  }
}

const SESSION = { owner_kind: 'browser', owner_id: 'browser', owner_label: 'You', creator_kind: 'browser', creator_id: 'browser', offer_kind: null, offer_id: null }

describe('the approvals through the CRUD routes', () => {
  it("decides on the API, with the decision's span, and hands its events to the log", async () => {
    const api = new FakeApi()
    const told: unknown[][] = []
    const batch = { session_id: SID, events: [{ v: 1, type: 'approval.resolved' }], seqs: [4] }
    const decided = row({ decision: 'approved', decided_by_kind: 'browser', decided_by_id: 'browser', decided_by_label: 'You', decided_at: '2026-11-15T10:01:00Z' })
    api.answers.push({ approval: row() }, { session: SESSION }, { approval: row() }, { approval: decided, logged: [batch] })
    const settled = await service(api, told).decide(ME, AID, true)
    expect(settled.decision).toBe('approved')
    expect(api.seen.map((s) => s.path)).toEqual([`/approvals/row?id=${AID}`, `/approvals/session?session_id=${SID}`, `/approvals/row?id=${AID}`, '/approvals/settle'])
    expect(api.seen[3]?.body).toMatchObject({ id: AID, decision: 'approved', by: ME, expiry_s: 300, reason: null })
    expect(told).toEqual([[SID, batch.events, [4]]])
  })

  it('refuses a decision the SQL path refused before any write is asked for', async () => {
    const api = new FakeApi()
    // Another agent with no approval grant may not decide.
    api.answers.push({ approval: row() }, { session: { ...SESSION, owner_kind: 'bearer', owner_id: 'agent-2', creator_kind: 'bearer', creator_id: 'agent-2' } })
    await expect(service(api).decide({ kind: 'bearer', id: 'agent-2', label: 'Agent two' }, AID, true)).rejects.toThrow(ApprovalError)
    expect(api.seen.map((s) => s.path)).not.toContain('/approvals/settle')
  })

  it('answers a decision that lost as a conflict', async () => {
    const api = new FakeApi()
    const denied = row({ decision: 'denied', decided_at: '2026-11-15T10:01:00Z' })
    api.answers.push({ approval: row() }, { session: SESSION }, { approval: row() }, { approval: null, logged: [] }, { approval: denied })
    await expect(service(api).decide(ME, AID, true)).rejects.toMatchObject({ code: 'conflict' })
  })

  it('never sends a malformed id', async () => {
    const api = new FakeApi()
    await expect(service(api).get('../../areas', ME)).rejects.toMatchObject({ code: 'not_found' })
    expect(await service(api).consumePrepared('../x', AGENT, 'a'.repeat(64))).toBeUndefined()
    expect(api.seen).toEqual([])
  })

  it("prepares under the API's bounds, giving each possible eviction a span's context", async () => {
    const api = new FakeApi()
    const own = [row({ id: '33333333-3333-4333-8333-333333333333', session_id: null }), row({ id: '44444444-4444-4444-8444-444444444444', session_id: null })]
    api.answers.push({ approvals: own }, { approval: row({ session_id: null }), evicted: [own[0]] })
    const made = await service(api).createPrepared({ toolUseId: 'toolu_1', tool: 'print_output', input: { output: 'box' }, tier: 'outward', requestedBy: AGENT }, { perPrincipal: 2, total: 50, evictReason: 'too many' })
    expect(made?.id).toBe(AID)
    const sent = api.seen[1]?.body as { approval: Record<string, unknown>; per_principal: number; evict_traceparents: string[] }
    expect(api.seen[1]?.path).toBe('/approvals/create-prepared')
    expect(sent.per_principal).toBe(2)
    expect(sent.approval).toMatchObject({ session_id: null, turn_id: null, tool: 'print_output', expiry_s: 300, requested_by: AGENT })
    expect(sent.evict_traceparents.length).toBeLessThanOrEqual(1)
  })

  it('uses a prepared action once through the API', async () => {
    const api = new FakeApi()
    api.answers.push({ approval: row({ session_id: null, consumed_at: '2026-11-15T10:02:00Z' }) }, { approval: null })
    const svc = service(api)
    expect((await svc.consumePrepared(AID, AGENT, 'a'.repeat(64)))?.consumedAt).toBe('2026-11-15T10:02:00.000Z')
    expect(await svc.consumePrepared(AID, AGENT, 'a'.repeat(64))).toBeUndefined()
    expect(api.seen[0]?.body).toEqual({ id: AID, by: { kind: 'bearer', id: 'agent-1' }, input_hash: 'a'.repeat(64) })
  })
})

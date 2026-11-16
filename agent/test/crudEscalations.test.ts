import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import type { FlowRunReads } from '../src/flows/backendReads.js'
import { CrudEscalations } from '../src/flows/crudEscalations.js'
import { Escalations, EscalationError } from '../src/flows/escalations.js'
import { FlowQueue } from '../src/flows/queue.js'
import { SessionError } from '../src/sessions/manager.js'

// A flow run's calls at the starting session's hop through the API's CRUD routes (#5299 step 10,
// area `escalations`), against a fake API that answers as backend api/crud_agent_escalations.py
// does (its tests, tests/crud/test_crud_agent_escalations.py, cover the SQL on Postgres). Who may
// answer is still checked here first; every write names the parent session; a delivery's claim
// is handed back when its turn does not start, and skipped to the person when the session cannot
// be asked; ids go in the body or the query, never the path.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const PARENT = '11111111-1111-4111-8111-111111111111'
const CHILD = '22222222-2222-4222-8222-222222222222'
const RID = `durable:${CHILD}:run-1:toolu_1`
const HOP_UNTIL = '2026-11-15T10:05:00.123456+00:00'

type Seen = { method: string; path: string; body: Record<string, unknown> | undefined }

class FakeApi {
  seen: Seen[] = []
  routes = new Map<string, (seen: Seen) => { status?: number; body: unknown }>()

  readonly fetch: typeof fetch = (input, init) => {
    const url = new URL(String(input))
    const raw = init?.body
    const body = typeof raw === 'string' ? (JSON.parse(raw) as Record<string, unknown>) : undefined
    const seen = { method: init?.method ?? 'GET', path: url.pathname.slice(PREFIX.length) + url.search, body }
    this.seen.push(seen)
    const route = this.routes.get(`${seen.method} ${url.pathname.slice(PREFIX.length)}`)
    if (!route) throw new Error(`no route for ${seen.method} ${seen.path}`)
    const answer = route(seen)
    return Promise.resolve(
      new Response(JSON.stringify(answer.body), { status: answer.status ?? 200, headers: { 'content-type': 'application/json', 'x-scadbuddy-crud': '1' } }),
    )
  }

  paths(): string[] {
    return this.seen.map((s) => `${s.method} ${s.path}`)
  }
}

/** SQL for what stays the sessions' (step 9): the parent's owner. Anything else on these tables is a test failure. */
const sessionsOnly = ((strings: TemplateStringsArray) => {
  const text = strings.join('?')
  if (text.includes('FROM ai_sessions')) return Promise.resolve([{ owner_kind: 'browser', owner_id: 'browser', owner_label: 'You' }])
  if (text.includes('ai_session_events')) return Promise.resolve([])
  throw new Error(`ran SQL: ${text}`)
}) as unknown as Sql

const reads = {
  steps: () => Promise.resolve([]),
  parentHopApprovals: () => Promise.resolve({ runs: [], next: null }),
  stillAtParentHop: () => Promise.resolve(new Set<string>()),
  queue: () => Promise.resolve({ id: 'flow-1', workflow_run_id: 'w', name: 'f', status: 'running', starter_session: PARENT, steps: [], waiting_on: [], requested_grant: null }),
} as unknown as FlowRunReads

const BROWSER = { id: 'browser', kind: 'browser' as const, tiers: ['read', 'write', 'outward'] as const }

function escalations(send?: () => Promise<void>, denied = true) {
  const api = new FakeApi()
  const responded: unknown[] = []
  const routes = { crud: new CrudEscalations({ baseUrl: BASE, fetch: api.fetch }), use: () => true }
  const service = new Escalations({
    sql: sessionsOnly,
    reads,
    routes,
    durable: () => ({ respond: (_s, args) => (responded.push(args), Promise.resolve({ outcome: denied ? 'denied' : 'not_found' }) as never) }),
    ...(send ? { send } : {}),
    currentTiers: () => Promise.resolve(undefined),
  })
  return { api, service, responded, routes }
}

const ENTRY = { session_id: CHILD, hop: 'parent', due: false, claimed: false, parent_session: PARENT, parent_principal: 'browser' }
const caller = { principal: BROWSER as never, session: PARENT }

describe('the escalations through the CRUD routes', () => {
  it('passes a call up naming the parent session, after its own check', async () => {
    const { api, service } = escalations()
    api.routes.set('GET /escalations/entry', () => ({ body: { entry: ENTRY } }))
    api.routes.set('POST /escalations/pass-up', () => ({ body: { moved: true } }))
    expect(await service.respond(caller, RID, 'pass_up', { reason: 'yours' })).toEqual({ request_id: RID, outcome: 'passed_up' })
    expect(api.seen[0]!.path).toBe(`/escalations/entry?request_id=${encodeURIComponent(RID)}`)
    expect(api.seen[1]!.body).toEqual({ request_id: RID, session: PARENT, reason: 'yours' })
  })

  it('refuses another session before any write', async () => {
    const { api, service } = escalations()
    api.routes.set('GET /escalations/entry', () => ({ body: { entry: ENTRY } }))
    await expect(service.respond({ ...caller, session: CHILD }, RID, 'pass_up', {})).rejects.toMatchObject({ status: 403 })
    expect(api.paths()).toEqual([`GET /escalations/entry?request_id=${encodeURIComponent(RID)}`])
  })

  it("claims a deny, keeps the claim once it lands, and puts the deadline back when it does not", async () => {
    for (const denied of [true, false]) {
      const { api, service, responded } = escalations(undefined, denied)
      api.routes.set('GET /escalations/entry', () => ({ body: { entry: ENTRY } }))
      api.routes.set('POST /escalations/claim-deny', () => ({ body: { hop_until: HOP_UNTIL } }))
      api.routes.set('POST /escalations/release-deny', () => ({ body: { done: true } }))
      const out = await service.respond(caller, RID, 'deny', {})
      expect(out.outcome).toBe(denied ? 'denied' : 'with_person')
      expect(responded).toHaveLength(1)
      const released = api.seen.filter((s) => s.path === '/escalations/release-deny')
      expect(released.map((s) => s.body)).toEqual(denied ? [] : [{ request_id: RID, session: PARENT, hop_until: new Date(HOP_UNTIL).toISOString() }])
    }
  })

  it('is a conflict when the claim is not had', async () => {
    const { api, service, responded } = escalations()
    api.routes.set('GET /escalations/entry', () => ({ body: { entry: ENTRY } }))
    api.routes.set('POST /escalations/claim-deny', () => ({ body: { hop_until: null } }))
    await expect(service.respond(caller, RID, 'deny', {})).rejects.toBeInstanceOf(EscalationError)
    expect(responded).toEqual([])
  })

  it('moves the due entries through the API', async () => {
    const { api, service } = escalations()
    api.routes.set('POST /escalations/move-due', () => ({ body: { moved: 3 } }))
    expect(await service.moveDue()).toBe(3)
  })
})

describe('a delivery through the CRUD routes', () => {
  const WAITING = {
    request_id: RID,
    flow_run_id: 'flow-1',
    tool: 'print_output',
    summary: 'box',
    outside_reason: 'not in the grant',
    hop_until: HOP_UNTIL,
    past_grace: true,
    session_id: CHILD,
    parent_session: PARENT,
    parent_principal: 'browser',
  }

  function delivering(send: () => Promise<void>) {
    const made = escalations(send)
    made.api.routes.set('POST /escalations/deliveries/page', () => ({ body: { ids: [] } }))
    made.api.routes.set('POST /escalations/waiting', () => ({ body: { calls: [WAITING] } }))
    made.api.routes.set('POST /escalations/claim', () => ({ body: { claimed: [RID] } }))
    made.api.routes.set('POST /escalations/unclaim', () => ({ body: { done: true } }))
    made.api.routes.set('POST /escalations/skip', () => ({ body: { done: true } }))
    return made
  }

  const CALLS = { session: PARENT, child: [RID], own: [] }

  it('claims the calls, then starts the turn', async () => {
    let sent = 0
    const { api, service } = delivering(async () => void sent++)
    expect(await service.deliver()).toBe(1)
    expect(sent).toBe(1)
    expect(api.seen.find((s) => s.path === '/escalations/claim')!.body).toEqual(CALLS)
    expect(api.paths().some((p) => p.includes('unclaim') || p.includes('skip'))).toBe(false)
  })

  it('hands the claim back when the session is busy, and skips it when it cannot be asked', async () => {
    for (const [code, path] of [
      ['busy', '/escalations/unclaim'],
      ['budget_exhausted', '/escalations/skip'],
    ] as const) {
      const { api, service } = delivering(() => Promise.reject(new SessionError(code, 'no')))
      expect(await service.deliver()).toBe(0)
      expect(api.seen.filter((s) => s.path === path).map((s) => s.body)).toEqual([CALLS])
    }
  })

  it('hands the claim back and reports a failure that is no SessionError', async () => {
    const { api, service } = delivering(() => Promise.reject(new Error('boom')))
    await expect(service.deliver()).rejects.toThrow('boom')
    expect(api.paths()).toContain('POST /escalations/unclaim')
  })

  it("logs why the turn did not start when the hand back fails too, and reports the hand back's failure", async () => {
    const { api, service } = delivering(() => Promise.reject(new SessionError('busy', 'busy')))
    api.routes.set('POST /escalations/unclaim', () => ({ status: 500, body: {} }))
    const logged: string[] = []
    const error = console.error
    console.error = (...args: unknown[]) => void logged.push(args.map(String).join(' '))
    try {
      await expect(service.deliver()).rejects.toThrow()
    } finally {
      console.error = error
    }
    expect(logged.some((l) => l.includes('did not start (busy)') && l.includes('not handed back'))).toBe(true)
  })

  it('sends at most IDS_MAX ids a list, and hands back the earlier batches when a later claim fails', async () => {
    const { api, routes } = escalations()
    const own = Array.from({ length: 1001 }, (_, i) => `durable:${PARENT}:run-1:toolu_${i}`)
    const bodies: { child: string[]; own: string[] }[] = []
    api.routes.set('POST /escalations/claim', (seen) => {
      const body = seen.body as { child: string[]; own: string[] }
      bodies.push(body)
      return { body: { claimed: [...body.child, ...body.own] } }
    })
    expect(await routes.crud.claim({ session: PARENT, child: [RID], own })).toHaveLength(1002)
    expect(bodies.map((b) => [b.child.length, b.own.length])).toEqual([
      [1, 1000],
      [0, 1],
    ])
    let calls = 0
    api.routes.set('POST /escalations/claim', (seen) => {
      calls += 1
      if (calls === 2) return { status: 500, body: {} }
      const body = seen.body as { child: string[]; own: string[] }
      return { body: { claimed: [...body.child, ...body.own] } }
    })
    const back: string[] = []
    api.routes.set('POST /escalations/unclaim', (seen) => {
      back.push(...(seen.body as { own: string[] }).own)
      return { body: {} }
    })
    await expect(routes.crud.claim({ session: PARENT, child: [], own })).rejects.toThrow()
    expect(back).toEqual(own.slice(0, 1000))
    for (const route of ['unclaim', 'skip'] as const) {
      api.seen = []
      api.routes.set(`POST /escalations/${route}`, () => ({ body: {} }))
      await routes.crud[route]({ session: PARENT, child: [], own })
      expect(api.seen.map((s) => (s.body as { own: string[] }).own.length)).toEqual([1000, 1])
    }
  })
})

describe("a run's queue through the CRUD routes", () => {
  it("reads the run's child entries, a deny's claim (infinity) with no deadline", async () => {
    const { api, routes } = escalations()
    const row = {
      request_id: RID,
      kind: 'approval',
      session_id: CHILD,
      tool: 'print_output',
      summary: 'box',
      input_hash: null,
      hop: 'parent',
      hop_until: 'infinity',
      outside_reason: null,
      hops: [],
      created_at: '2026-11-15T10:00:00Z',
      expires_at: '2026-11-15T11:00:00Z',
      expiring: false,
    }
    api.routes.set('GET /escalations/run-entries', () => ({ body: { entries: [row, { ...row, request_id: `${RID}2`, hop_until: HOP_UNTIL }] } }))
    const queue = await new FlowQueue(sessionsOnly, reads, routes).forRun('flow-1')
    const children = queue!.entries.filter((e) => e.source === 'child')
    expect(children.map((e) => (e as { hop_until: string | null }).hop_until)).toEqual([null, new Date(HOP_UNTIL).toISOString()])
    expect(api.seen[0]!.path).toBe('/escalations/run-entries?flow_run_id=flow-1')
  })
})

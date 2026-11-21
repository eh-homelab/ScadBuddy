import { describe, expect, it } from 'vitest'
import { CrudOrderCostRows, OrderCosts, orderCostLease, type AssistantCostReport } from '../src/sessions/orderCosts.js'

// A session's order costs and its spend's report through the API's CRUD routes (#5299 step 9,
// part c), against a fake API that answers as the backend's api/crud_agent_order_costs.py and
// lease routes do (the backend's tests/crud/test_crud_agent_order_costs.py covers the routes, the
// lease checks and the SQL pod's lock). The report lock is a lease row: every write of a report
// names its token, and the lease is released however the report ends.

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'
const SESSION = '0b0e7c55-6a3e-4a37-9d3c-4b1a1f0e2a10'
const LEASE = `/leases/${encodeURIComponent(orderCostLease(SESSION))}`

type Seen = { method: string; path: string; body: unknown }

class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, { status: number; body: unknown }>()

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = url.pathname.slice(PREFIX.length)
    const text = request.method === 'GET' ? '' : await request.text()
    this.seen.push({ method: request.method, path: `${path}${url.search}`, body: text ? JSON.parse(text) : undefined })
    const answer = this.answers.get(`${request.method} ${path}`) ?? { status: 200, body: { done: true } }
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown, status = 200): this {
    this.answers.set(key, { status, body })
    return this
  }
}

const rows = (api: FakeApi) => new CrudOrderCostRows({ baseUrl: BASE, fetch: api.fetch })

describe('order costs over the CRUD routes', () => {
  it("records a revision's row under its session", async () => {
    const api = new FakeApi()
    await rows(api).insert(SESSION.toUpperCase(), 'o1', 'i1')
    expect(api.seen).toEqual([{ method: 'POST', path: `/sessions/${SESSION}/order-costs`, body: { order_id: 'o1', order_item_id: 'i1' } }])
    await expect(rows(api).insert('../areas', 'o', 'i')).rejects.toThrow('is not a session id')
  })

  it('reports under the lease, each write naming its token, and releases it', async () => {
    const api = new FakeApi()
      .answer(`POST ${LEASE}`, { expires_at: '2026-11-15T22:00:00Z' })
      .answer(`POST /sessions/${SESSION}/order-costs/report`, {
        report: {
          pending: [
            { order_id: 'o1', order_item_id: 'i1', attempted_usd: null },
            { order_id: 'o2', order_item_id: 'i2', attempted_usd: 0.25 },
          ],
          cost: 1.25,
          carried: 0.25,
          principal: 'token:42',
          over: false,
        },
      })
    const posted: AssistantCostReport[] = []
    const costs = new OrderCosts({ rows: rows(api), post: async (r) => (posted.push(r), r.orderId === 'o1' ? 'recorded' : 'deferred') })
    await costs.report(SESSION)
    expect(posted.map((r) => [r.orderId, r.costUsd])).toEqual([
      ['o1', '1.00000000'],
      ['o2', '0.25000000'],
    ])
    const token = (api.seen[0]?.body as { token: string }).token
    expect(api.seen.map((s) => [s.method, s.path.split('?')[0], (s.body as { outcome?: string } | undefined)?.outcome])).toEqual([
      ['POST', LEASE, undefined],
      ['POST', `/sessions/${SESSION}/order-costs/report`, undefined],
      ['POST', `/sessions/${SESSION}/order-costs/attempt`, undefined],
      ['POST', `/sessions/${SESSION}/order-costs/settle`, 'reported'],
      ['POST', `/sessions/${SESSION}/order-costs/settle`, 'unsent'],
      ['DELETE', LEASE, undefined],
    ])
    for (const s of api.seen.slice(1, -1)) expect((s.body as { token: string }).token).toBe(token)
    expect(api.seen[2]?.body).toEqual({ token, order_id: 'o1', items: ['i1'], amount_usd: 1 })
    expect(api.seen.at(-1)?.path).toBe(`${LEASE}?token=${token}`)
  })

  it('sends nothing while another holder keeps the lease, and never throws', async () => {
    const api = new FakeApi().answer(`POST ${LEASE}`, { detail: 'busy', reason: 'busy' }, 409)
    const posted: unknown[] = []
    await new OrderCosts({ rows: rows(api), post: async (r) => (posted.push(r), 'recorded') }).report(SESSION)
    expect(posted).toEqual([])
    expect(api.seen.map((s) => s.method)).toEqual(['POST'])
  })

  it('releases the lease when a write is refused as lost, and reports the error', async () => {
    const api = new FakeApi()
      .answer(`POST ${LEASE}`, { expires_at: '2026-11-15T22:00:00Z' })
      .answer(`POST /sessions/${SESSION}/order-costs/report`, { detail: 'lost', reason: 'lease-lost' }, 409)
    const errors: unknown[] = []
    await new OrderCosts({ rows: rows(api), post: async () => 'recorded', onError: (e) => errors.push(e) }).report(SESSION)
    expect(errors).toHaveLength(1)
    expect(api.seen.at(-1)?.method).toBe('DELETE')
  })

  it('takes no lease for an id that is no UUID', async () => {
    const api = new FakeApi()
    await new OrderCosts({ rows: rows(api), post: async () => 'recorded' }).report('../areas')
    expect(api.seen).toEqual([])
  })

  it('asks the sweep for its sessions, then reports each', async () => {
    const api = new FakeApi().answer('POST /order-costs/sweep', { sessions: [SESSION] }).answer(`POST ${LEASE}`, { expires_at: '2026-11-15T22:00:00Z' })
    api.answer(`POST /sessions/${SESSION}/order-costs/report`, { report: null })
    await new OrderCosts({ rows: rows(api), post: async () => 'recorded', sweepLimit: 3 }).sweep()
    expect(api.seen[0]).toEqual({ method: 'POST', path: '/order-costs/sweep', body: { limit: 3 } })
    expect(api.seen.map((s) => s.method)).toEqual(['POST', 'POST', 'POST', 'DELETE'])
  })
})

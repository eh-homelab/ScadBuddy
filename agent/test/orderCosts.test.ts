import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import type { TouchedCall } from '../src/sessions/touched.js'
import { crudCostPoster, REVISION_TOOLS, type AssistantCostReport } from '../src/sessions/orderCosts.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runToolWithOutcome } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// The assistant-cost report's client (#5330): the CRUD route's own 4xx is final, anything
// else (a 5xx, a proxy's answer, an older API without the route, no answer) is sent again.

const REPORT: AssistantCostReport = { orderId: 'o/1', orderItemId: 'i1', sessionId: 's1', principal: 'person:owner', costUsd: '0.50000000' }

const ENTRY = '{"entry": {"id": "e1"}, "written": true}'

function answering(status: number, crud: boolean, seen: Request[] = [], body = ENTRY): typeof fetch {
  return async (input, init) => {
    seen.push(new Request(input, init))
    return new Response(body, { status, headers: crud ? { 'x-scadbuddy-crud': '1' } : {} })
  }
}

describe('crudCostPoster', () => {
  it('posts the share to the agent CRUD route', async () => {
    const seen: Request[] = []
    expect(await crudCostPoster('http://api:8080/', answering(201, true, seen))(REPORT, 1_000)).toBe('recorded')
    expect(seen[0]!.url).toBe('http://api:8080/api/v1/internal/crud/agent/orders/o%2F1/assistant-cost')
    expect(seen[0]!.method).toBe('POST')
    expect(await seen[0]!.json()).toEqual({ session_id: 's1', principal: 'person:owner', order_item_id: 'i1', cost_usd: '0.50000000' })
    expect(await crudCostPoster('http://api', answering(200, true))(REPORT, 1_000)).toBe('recorded')
  })

  it('defers a share the backend found under one minor unit', async () => {
    const tooSmall = answering(200, true, [], '{"entry": null, "written": false}')
    expect(await crudCostPoster('http://api', tooSmall)(REPORT, 1_000)).toBe('deferred')
  })

  it("tells the route's refusal from a failure to try again", async () => {
    expect(await crudCostPoster('http://api', answering(404, true))(REPORT, 1_000)).toBe('refused')
    expect(await crudCostPoster('http://api', answering(409, true))(REPORT, 1_000)).toBe('refused')
    expect(await crudCostPoster('http://api', answering(422, true))(REPORT, 1_000)).toBe('refused')
    // No credential yet, a dead one, another caller's or a busy API: sent again later.
    for (const status of [400, 401, 403, 429]) {
      expect(await crudCostPoster('http://api', answering(status, true))(REPORT, 1_000)).toBe('failed')
    }
    expect(await crudCostPoster('http://api', answering(404, false))(REPORT, 1_000)).toBe('failed')
    expect(await crudCostPoster('http://api', answering(503, true))(REPORT, 1_000)).toBe('failed')
    const down: typeof fetch = async () => {
      throw new TypeError('fetch failed')
    }
    expect(await crudCostPoster('http://api', down)(REPORT, 1_000)).toBe('failed')
  })
})

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('the revision tools as OrderCosts sees them', () => {
  it.each([...REVISION_TOOLS])('%s reports a backend refusal as a failed call', async (name) => {
    server.use(
      http.post(`${BACKEND}/api/v1/orders/:order/items/:item/revisions*`, () =>
        HttpResponse.json({ type: 'https://scadbuddy.dev/problems/orders/item_locked', title: 'locked', status: 409 }, { status: 409 }),
      ),
    )
    const seen: TouchedCall[] = []
    const tool = ALL_TOOLS.find((t) => t.name === name)!
    const run = await runToolWithOutcome(tool, { order_id: 'o1', item_id: 'i1', output_id: 'out1' }, {
      ...services({ touched: { record: async (call) => void seen.push(call) } }),
      principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
      session: '0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f',
      gate: 'workflow',
      progress: async () => {},
      signal: new AbortController().signal,
    })
    expect(run.outcome).toBe('error')
    expect(seen.map((c) => [c.tool.name, c.ok])).toEqual([[name, false]])
  })
})

import { describe, expect, it } from 'vitest'
import { crudCostPoster, type AssistantCostReport } from '../src/sessions/orderCosts.js'

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
    expect(await crudCostPoster('http://api', answering(404, false))(REPORT, 1_000)).toBe('failed')
    expect(await crudCostPoster('http://api', answering(503, true))(REPORT, 1_000)).toBe('failed')
    const down: typeof fetch = async () => {
      throw new TypeError('fetch failed')
    }
    expect(await crudCostPoster('http://api', down)(REPORT, 1_000)).toBe('failed')
  })
})

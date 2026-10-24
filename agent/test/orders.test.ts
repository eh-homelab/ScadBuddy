import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { NOT_A_TOOL } from '../src/tools/coverage.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { ToolError } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// Orders phase 1 (#4249, spec 2026-10-18 §7.3, plan Ruling 14): one tool per route group,
// tiered as the spec reads: a record the shop keeps is `write`, a step that commits the shop
// to the customer (accepting, a price, a made quantity, a shipment, closing, cancelling) is
// `outward`. No body ever names an author or a status.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const ORDER = '0e5a3c1e-1111-4222-8333-944455556666'
const ITEM = '7b1c2d3e-4f50-4617-8829-aabbccddeeff'
const CUSTOMER = 'c0ffee00-1111-4222-8333-944455556666'

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    pollIntervalMs: 1,
  }
}

describe('order tools (#4249)', () => {
  it('tiers each as plan Ruling 14 does', () => {
    const names = [
      'customer_list',
      'customer_get',
      'customer_upsert',
      'order_list',
      'order_get',
      'order_create',
      'order_update',
      'order_item_add',
      'order_item_update',
      'order_item_quote',
      'order_item_accept_quote',
      'order_accept',
      'order_item_fulfil',
      'order_ship',
      'order_close',
      'order_cancel',
      'order_reopen',
    ]
    expect(Object.fromEntries(names.map((n) => [n, tool(n).risk]))).toEqual({
      customer_list: 'read',
      customer_get: 'read',
      customer_upsert: 'write',
      order_list: 'read',
      order_get: 'read',
      order_create: 'write',
      order_update: 'write',
      order_item_add: 'write',
      order_item_update: 'write',
      order_item_quote: 'write',
      order_item_accept_quote: 'outward',
      order_accept: 'outward',
      order_item_fulfil: 'outward',
      order_ship: 'outward',
      order_close: 'outward',
      order_cancel: 'outward',
      order_reopen: 'outward',
    })
  })

  it('leaves the audit line to the page', () => {
    const operation = 'GET /api/v1/orders/{order_id}/events'
    expect(ALL_TOOLS.flatMap((t) => t.routes)).not.toContain(operation)
    expect(NOT_A_TOOL.map((n) => n.operation)).toContain(operation)
  })

  it('creates a customer with no author of its own, and edits one by id', async () => {
    const sent: { method: string; body: unknown }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/customers`, async ({ request }) => {
        sent.push({ method: 'POST', body: await request.json() })
        return HttpResponse.json({ id: CUSTOMER, display_name: 'Ada' }, { status: 201 })
      }),
      http.patch(`${BACKEND}/api/v1/customers/${CUSTOMER}`, async ({ request }) => {
        sent.push({ method: 'PATCH', body: await request.json() })
        return HttpResponse.json({ id: CUSTOMER, display_name: 'Ada King' })
      }),
      http.post(`${BACKEND}/api/v1/customers/${CUSTOMER}/archive`, () => {
        sent.push({ method: 'ARCHIVE', body: null })
        return HttpResponse.json({ id: CUSTOMER, archived_at: '2026-10-20T00:00:00Z' })
      }),
    )
    const made = await tool('customer_upsert').execute(
      { display_name: 'Ada', contacts: [{ kind: 'email', value: 'ada@example.test', primary: true }] },
      ctx(),
    )
    expect(firstText(made)).toEqual({ id: CUSTOMER, display_name: 'Ada' })
    await tool('customer_upsert').execute({ customer_id: CUSTOMER, display_name: 'Ada King', archive: true }, ctx())
    expect(sent).toEqual([
      { method: 'POST', body: { display_name: 'Ada', contacts: [{ kind: 'email', value: 'ada@example.test', primary: true }] } },
      { method: 'PATCH', body: { display_name: 'Ada King' } },
      { method: 'ARCHIVE', body: null },
    ])
    await expect(tool('customer_upsert').execute({ archive: true }, ctx())).rejects.toThrow('customer_id')
  })

  it('passes the list filters and the cursor through to the backend', async () => {
    let url = ''
    server.use(
      http.get(`${BACKEND}/api/v1/orders`, ({ request }) => {
        url = request.url
        return HttpResponse.json({ items: [{ id: ORDER, number: 'ORD-2026-1' }], next_cursor: 'abc' })
      }),
    )
    const result = await tool('order_list').execute({ status: 'quoted', customer_id: CUSTOMER, cursor: 'xyz', limit: 5 }, ctx())
    expect(firstText(result)).toEqual({ items: [{ id: ORDER, number: 'ORD-2026-1' }], next_cursor: 'abc' })
    const query = new URL(url).searchParams
    expect(Object.fromEntries(query)).toEqual({ status: 'quoted', customer_id: CUSTOMER, cursor: 'xyz', limit: '5' })
  })

  it('edits, holds and records a payment through their own routes', async () => {
    const calls: string[] = []
    server.use(
      http.patch(`${BACKEND}/api/v1/orders/${ORDER}`, async ({ request }) => {
        calls.push(`PATCH ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ORDER, status: 'draft' })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/hold`, () => {
        calls.push('HOLD')
        return HttpResponse.json({ id: ORDER, on_hold: true })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/payments`, async ({ request }) => {
        calls.push(`PAY ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: 'p1', amount_minor: 500 }, { status: 201 })
      }),
    )
    const result = await tool('order_update').execute(
      { order_id: ORDER, notes: 'rush', hold: true, payment: { amount_minor: 500, method: 'cash' } },
      ctx(),
    )
    expect(calls).toEqual(['PATCH {"notes":"rush"}', 'HOLD', 'PAY {"amount_minor":500,"method":"cash","notes":""}'])
    expect(firstText(result)).toEqual({ order: { id: ORDER, on_hold: true }, payment: { id: 'p1', amount_minor: 500 } })
    await expect(tool('order_update').execute({ order_id: ORDER }, ctx())).rejects.toThrow('nothing to change')
  })

  it('adds an item from an output or by hand, edits one and deletes a draft', async () => {
    const sent: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items`, async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json({ id: ITEM, status: 'draft' }, { status: 201 })
      }),
      http.patch(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}`, async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json({ id: ITEM, status: 'draft', quantity: 3 })
      }),
      http.delete(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}`, () => new HttpResponse(null, { status: 204 })),
    )
    await tool('order_item_add').execute({ order_id: ORDER, output_id: 'out1', quantity: 2 }, ctx())
    await tool('order_item_add').execute(
      { order_id: ORDER, slug: 'demo', commit: 'a'.repeat(40), inputs: { params: { width: 7 } } },
      ctx(),
    )
    await tool('order_item_update').execute({ order_id: ORDER, item_id: ITEM, quantity: 3 }, ctx())
    const gone = await tool('order_item_update').execute({ order_id: ORDER, item_id: ITEM, delete: true }, ctx())
    expect(sent).toEqual([
      { output_id: 'out1', quantity: 2, notes: '' },
      { slug: 'demo', commit: 'a'.repeat(40), inputs: { params: { width: 7 } }, quantity: 1, notes: '' },
      { quantity: 3 },
    ])
    expect(firstText(gone)).toEqual({ deleted: ITEM })
  })

  it('cancels the order or one item, each through its own route', async () => {
    const calls: string[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/cancel`, async ({ request }) => {
        calls.push(`ORDER ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: ORDER, status: 'cancelled' })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/cancel`, () => {
        calls.push('ITEM')
        return HttpResponse.json({ id: ITEM, status: 'cancelled' })
      }),
    )
    await tool('order_cancel').execute({ order_id: ORDER, reason: 'changed mind' }, ctx())
    await tool('order_cancel').execute({ order_id: ORDER, item_id: ITEM }, ctx())
    expect(calls).toEqual(['ORDER {"reason":"changed mind"}', 'ITEM'])
    expect(tool('order_cancel').summarize({ order_id: ORDER, item_id: ITEM })).toContain(`item ${ITEM}`)
  })

  it('reopens the order or one item, and unarchives a customer', async () => {
    const calls: string[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/reopen`, () => {
        calls.push('ORDER')
        return HttpResponse.json({ id: ORDER, status: 'shipped' })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/reopen`, () => {
        calls.push('ITEM')
        return HttpResponse.json({ id: ITEM, status: 'approved' })
      }),
      http.post(`${BACKEND}/api/v1/customers/${CUSTOMER}/unarchive`, () => {
        calls.push('CUSTOMER')
        return HttpResponse.json({ id: CUSTOMER, archived_at: null })
      }),
    )
    await tool('order_reopen').execute({ order_id: ORDER }, ctx())
    await tool('order_reopen').execute({ order_id: ORDER, item_id: ITEM }, ctx())
    await tool('customer_upsert').execute({ customer_id: CUSTOMER, unarchive: true }, ctx())
    expect(calls).toEqual(['ORDER', 'ITEM', 'CUSTOMER'])
    expect(tool('order_reopen').summarize({ order_id: ORDER, item_id: ITEM })).toContain(`item ${ITEM}`)
    await expect(tool('customer_upsert').execute({ unarchive: true }, ctx())).rejects.toThrow('customer_id')
    await expect(
      tool('customer_upsert').execute({ customer_id: CUSTOMER, archive: true, unarchive: true }, ctx()),
    ).rejects.toThrow('both')
  })

  it('records, edits and accepts an attempt, and ships', async () => {
    const calls: string[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/fulfilments`, async ({ request }) => {
        calls.push(`RECORD ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: 'f1', attempt: 1 }, { status: 201 })
      }),
      http.patch(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/fulfilments/f1`, async ({ request }) => {
        calls.push(`EDIT ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: 'f1', attempt: 1 })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/fulfilments/f1/accept`, () => {
        calls.push('ACCEPT')
        return HttpResponse.json({ id: 'f1', accepted_at: 'now' })
      }),
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/shipments`, async ({ request }) => {
        calls.push(`SHIP ${JSON.stringify(await request.json())}`)
        return HttpResponse.json({ id: 's1' }, { status: 201 })
      }),
    )
    const fulfil = tool('order_item_fulfil')
    await fulfil.execute({ order_id: ORDER, item_id: ITEM, attempt: { quantity: 2, outcome: 'succeeded' } }, ctx())
    await fulfil.execute({ order_id: ORDER, item_id: ITEM, fulfilment_id: 'f1', attempt: { quantity: 1, outcome: 'partial' } }, ctx())
    await fulfil.execute({ order_id: ORDER, item_id: ITEM, fulfilment_id: 'f1', accept: true }, ctx())
    await expect(fulfil.execute({ order_id: ORDER, item_id: ITEM }, ctx())).rejects.toThrow(ToolError)
    await tool('order_ship').execute({ order_id: ORDER, carrier: 'Post', items: [{ order_item_id: ITEM, quantity: 2 }] }, ctx())
    expect(calls).toEqual([
      'RECORD {"quantity":2,"outcome":"succeeded","notes":""}',
      'EDIT {"quantity":1,"outcome":"partial","notes":""}',
      'ACCEPT',
      `SHIP {"carrier":"Post","items":[{"order_item_id":"${ITEM}","quantity":2}]}`,
    ])
  })

  it("passes a lifecycle refusal on with the backend's problem", async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/close`, () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/orders/order_not_closable',
            title: 'Conflict',
            status: 409,
            detail: 'order ORD-2026-1 cannot close: 1 item(s) still open, 0 fulfilled item(s) not shipped',
            items: [ITEM],
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const refused = tool('order_close').execute({ order_id: ORDER }, ctx())
    await expect(refused).rejects.toMatchObject({ status: 409 })
    await expect(refused).rejects.toThrow('close order')
  })
})

// Orders phase 2 (#4250, spec 2026-10-18 §7.3, plan Ruling 11): the price book, the quote in
// both modes and the rough estimate.
describe('pricing tools (#4250)', () => {
  const OPERATION = '5d4c3b2a-1111-4222-8333-944455556666'
  const FILAMENT = 'f11a0000-1111-4222-8333-944455556666'
  const QUOTE = { id: 'q1', source: 'slice', unit_price_minor: 234, lines: [{ kind: 'filament' }, { kind: 'filament' }] }
  const SLICE = {
    printer_id: 1,
    filament_plan: { slots: [{ slot_id: 1, spool_id: 9 }, { slot_id: 2, spool_id: 5 }] },
    choices: { nozzles: [{ size: '0.4' }] },
    filaments: { '0': FILAMENT, '1': FILAMENT },
  }

  it('tiers the book, the quote and the estimate as Ruling 11 does', () => {
    expect(['price_book_get', 'price_book_set', 'order_estimate', 'order_item_quote'].map((n) => tool(n).risk)).toEqual([
      'read',
      'write',
      'read',
      'write',
    ])
  })

  it('quotes from a slice: sends a key, follows the 202 to the quote', async () => {
    const keys: (string | null)[] = []
    let body: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/quote`, async ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        body = await request.json()
        return HttpResponse.json({ id: OPERATION, status: 'running' }, { status: 202 })
      }),
      http.get(`${BACKEND}/api/v1/operations/${OPERATION}`, () =>
        HttpResponse.json({ id: OPERATION, status: 'succeeded', result: QUOTE }),
      ),
    )
    const answer = await tool('order_item_quote').execute({ order_id: ORDER, item_id: ITEM, slice: SLICE }, ctx())
    expect(firstText(answer)).toEqual(QUOTE)
    expect(keys).toHaveLength(1)
    expect(keys[0]).toMatch(/^[0-9a-f]{32}$/)
    expect(body).toEqual({
      ...SLICE,
      filament_plan: { ...SLICE.filament_plan, force_colour_match: false },
      choices: { nozzles: [{ size: '0.4', flow: 'standard' }] },
    })
  })

  it('answers a slice quote still running past the follow window', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/quote`, () =>
        HttpResponse.json({ id: OPERATION, status: 'running' }, { status: 202 }),
      ),
      http.get(`${BACKEND}/api/v1/operations/${OPERATION}`, () => HttpResponse.json({ id: OPERATION, status: 'running' })),
    )
    const answer = await tool('order_item_quote').execute(
      { order_id: ORDER, item_id: ITEM, slice: SLICE },
      { ...ctx(), commandFollowMs: 0 },
    )
    expect(firstText(answer)).toMatchObject({ status: 'running', operation_id: OPERATION })
  })

  it('records a typed price in manual mode, and takes exactly one mode', async () => {
    let body: unknown
    server.use(
      http.post(`${BACKEND}/api/v1/orders/${ORDER}/items/${ITEM}/quotes`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ id: 'q2', source: 'manual' }, { status: 201 })
      }),
    )
    const answer = await tool('order_item_quote').execute(
      { order_id: ORDER, item_id: ITEM, manual: { unit_price_minor: 1500, label: 'Typed' } },
      ctx(),
    )
    expect(firstText(answer)).toEqual({ id: 'q2', source: 'manual' })
    expect(body).toEqual({ unit_price_minor: 1500, label: 'Typed' })
    await expect(tool('order_item_quote').execute({ order_id: ORDER, item_id: ITEM }, ctx())).rejects.toThrow(ToolError)
    await expect(
      tool('order_item_quote').execute(
        { order_id: ORDER, item_id: ITEM, manual: { unit_price_minor: 1 }, slice: SLICE },
        ctx(),
      ),
    ).rejects.toThrow('exactly one')
  })

  it('reads the book, and sets the policy over what it was', async () => {
    const policy = { machine_rate_per_hour: '1.5', setup_fee: '0', labour_per_item: '2', markup_percent: '0', rounding: 'to_minor_unit' }
    const book = { currency: 'USD', minor_exponent: 2, policy, rough_estimate_factor: '1.15', filaments: [] }
    let put: unknown
    server.use(
      http.get(`${BACKEND}/api/v1/orders/price-book`, () => HttpResponse.json(book)),
      http.put(`${BACKEND}/api/v1/orders/price-book`, async ({ request }) => {
        put = await request.json()
        return HttpResponse.json(book)
      }),
    )
    expect(firstText(await tool('price_book_get').execute({}, ctx()))).toEqual(book)
    await tool('price_book_set').execute({ policy: { markup_percent: '15' } }, ctx())
    expect(put).toEqual({ ...policy, markup_percent: '15' })
  })

  it('patches, adds and archives a filament', async () => {
    const sent: { method: string; body: unknown }[] = []
    server.use(
      http.patch(`${BACKEND}/api/v1/orders/price-book/filaments/${FILAMENT}`, async ({ request }) => {
        sent.push({ method: 'PATCH', body: await request.json() })
        return HttpResponse.json({ id: FILAMENT, cost_per_kg: '30' })
      }),
      http.post(`${BACKEND}/api/v1/orders/price-book/filaments`, async ({ request }) => {
        sent.push({ method: 'POST', body: await request.json() })
        return HttpResponse.json({ id: 'new' }, { status: 201 })
      }),
      http.post(`${BACKEND}/api/v1/orders/price-book/filaments/${FILAMENT}/archive`, () => {
        sent.push({ method: 'ARCHIVE', body: null })
        return HttpResponse.json({ id: FILAMENT, archived_at: '2026-10-22T00:00:00Z' })
      }),
    )
    const patched = await tool('price_book_set').execute({ filament: { filament_id: FILAMENT, cost_per_kg: '30' } }, ctx())
    expect(firstText(patched)).toEqual({ filament: { id: FILAMENT, cost_per_kg: '30' } })
    await tool('price_book_set').execute({ filament: { material: 'PLA', colour: '#FFFFFF', cost_per_kg: '20' } }, ctx())
    await tool('price_book_set').execute({ filament: { filament_id: FILAMENT, archive: true } }, ctx())
    expect(sent).toEqual([
      { method: 'PATCH', body: { cost_per_kg: '30' } },
      { method: 'POST', body: { material: 'PLA', colour: '#FFFFFF', cost_per_kg: '20' } },
      { method: 'ARCHIVE', body: null },
    ])
    await expect(tool('price_book_set').execute({}, ctx())).rejects.toThrow(ToolError)
  })

  it('reads the rough estimate', async () => {
    let url = ''
    const estimate = { estimate: true, grams: '24.24', filament_minor: 62, currency: 'USD', minor_exponent: 2, factor: '1.15', parts: [], unknown_parts: 0 }
    server.use(
      http.get(`${BACKEND}/api/v1/orders/estimate`, ({ request }) => {
        url = request.url
        return HttpResponse.json(estimate)
      }),
    )
    const answer = await tool('order_estimate').execute({ output_id: 'a'.repeat(32), filament_id: FILAMENT }, ctx())
    expect(firstText(answer)).toEqual(estimate)
    expect(new URL(url).searchParams.get('filament_id')).toBe(FILAMENT)
  })
})

import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import { DEFAULT_ORDERS_QUERY } from '../../../lib/ordersQuery'
import { MAZE_SLUG } from '../../fixtures'
import { resetMockState } from '../../handlers'
import { CUSTOMER_ADA, ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, OUTPUT_REAGAN, PROBLEMS } from './state'

// The mock answers the lifecycle's refusals as the backend does (orders/lifecycle.py,
// api/orders.py): 409 problems typed `orders/<slug>` naming the ids in the way, so the
// pages' error paths are testable against the same shapes.

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)

describe('orders mock (#4249)', () => {
  beforeEach(() => resetMockState())

  it('lists newest first, pages by cursor, filters, and refuses an unknown parameter', async () => {
    const first = await api.orders(DEFAULT_ORDERS_QUERY, { limit: 2 })
    expect(first.items.map((o) => o.number)).toEqual(['ORD-2026-3', 'ORD-2026-2'])
    expect(first.next_cursor).not.toBeNull()
    const second = await api.orders(DEFAULT_ORDERS_QUERY, { limit: 2, cursor: first.next_cursor })
    expect(second.items.map((o) => o.number)).toEqual(['ORD-2026-1'])
    expect(second.next_cursor).toBeNull()
    const mine = await api.orders({ ...DEFAULT_ORDERS_QUERY, customer: CUSTOMER_ADA, status: 'accepted' })
    expect(mine.items.map((o) => o.id)).toEqual([ORDER_ACCEPTED])
    const byName = await api.orders({ ...DEFAULT_ORDERS_QUERY, q: 'bob' })
    expect(byName.items.map((o) => o.number)).toEqual(['ORD-2026-1'])
    const bad = await failure(api.orders(DEFAULT_ORDERS_QUERY, { cursor: 'nonsense' }))
    expect(bad?.problem.type).toMatch(/bad-cursor$/)
    const response = await fetch('/api/v1/orders?customer=x')
    expect(response.status).toBe(400)
    expect(((await response.json()) as { type: string }).type).toMatch(/unknown-parameter$/)
  })

  it('accepts only an order whose open items are quoted, naming the rest', async () => {
    const refused = await failure(api.acceptOrder(ORDER_DRAFT))
    expect(refused?.status).toBe(409)
    expect(refused?.problem.type).toBe(`${PROBLEMS}items_not_quoted`)
    expect((refused?.problem as { items?: string[] }).items).toEqual([ITEM_DRAFT])
    const quote = await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 1200 })
    expect(quote.total_minor).toBe(2400)
    expect((await api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id)).status).toBe('approved')
    expect((await api.order(ORDER_DRAFT)).order.status).toBe('quoted')
    const accepted = await api.acceptOrder(ORDER_DRAFT)
    expect(accepted.status).toBe('accepted')
    expect(accepted.shipping_address?.recipient).toBe('Ada Lovelace')
    expect((await api.order(ORDER_DRAFT)).totals.subtotal_minor).toBe(2400)
  })

  it('refuses a close while an item is open, then closes once shipped', async () => {
    const refused = await failure(api.closeOrder(ORDER_ACCEPTED))
    expect(refused?.problem.type).toBe(`${PROBLEMS}order_not_closable`)
    expect((refused?.problem as { items?: string[] }).items).toEqual([ITEM_APPROVED])
    const attempt = await api.recordFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 2, outcome: 'succeeded' })
    expect((await api.order(ORDER_ACCEPTED)).order.status).toBe('in_production')
    await api.acceptFulfilment(ORDER_ACCEPTED, ITEM_APPROVED, attempt.id)
    expect((await api.order(ORDER_ACCEPTED)).order.status).toBe('ready')
    await api.addShipment(ORDER_ACCEPTED, { carrier: 'Post', items: [{ order_item_id: ITEM_APPROVED, quantity: 2 }] })
    expect((await api.order(ORDER_ACCEPTED)).order.status).toBe('shipped')
    expect((await api.closeOrder(ORDER_ACCEPTED)).status).toBe('closed')
    const events = await api.orderEvents(ORDER_ACCEPTED)
    expect(events.at(-1)?.kind).toBe('order.status')
  })

  it('refuses what advances an order on hold, not a release, a cancel or an edit', async () => {
    await api.holdOrder(ORDER_DRAFT)
    const quote = await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 1 })
    const refused = await failure(api.acceptOrderQuote(ORDER_DRAFT, ITEM_DRAFT, quote.id))
    expect(refused?.problem.type).toBe(`${PROBLEMS}order_on_hold`)
    expect((await api.updateOrder(ORDER_DRAFT, { notes: 'waiting' })).notes).toBe('waiting')
    expect((await api.releaseOrder(ORDER_DRAFT)).on_hold).toBe(false)
    expect((await api.cancelOrder(ORDER_DRAFT, 'changed mind')).status).toBe('cancelled')
    expect((await api.order(ORDER_DRAFT)).items[0]?.status).toBe('cancelled')
  })

  it('deletes a draft item, refuses to delete a quoted one, and locks the reference after accept', async () => {
    const added = await api.addOrderItem(ORDER_DRAFT, { output_id: OUTPUT_REAGAN, quantity: 1 })
    expect(added.reference?.slug).toBe('name-keychain')
    expect(added.colours.length).toBeGreaterThan(0)
    await api.deleteOrderItem(ORDER_DRAFT, added.id)
    await api.quoteOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 5 })
    const refused = await failure(api.deleteOrderItem(ORDER_DRAFT, ITEM_DRAFT))
    expect(refused?.problem.type).toBe(`${PROBLEMS}item_not_draft`)
    const locked = await failure(api.updateOrderItem(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 3 }))
    expect(locked?.problem.type).toBe(`${PROBLEMS}item_locked`)
    const bad = await failure(api.addOrderItem(ORDER_DRAFT, { slug: 'nothing', commit: 'a'.repeat(40), inputs: {}, quantity: 1 }))
    expect(bad?.status).toBe(422)
    expect((bad?.problem as { errors?: { loc: string[] }[] }).errors?.[0]?.loc).toEqual(['body', 'slug'])
  })

  it("adds an item by hand at a template's own revision with default inputs, and refuses HEAD (#4251)", async () => {
    const model = await api.getModel(MAZE_SLUG)
    const commit = model.version ?? ''
    const added = await api.addOrderItem(ORDER_DRAFT, { slug: MAZE_SLUG, commit, inputs: { params: {}, v: 0 }, quantity: 2 })
    expect(added.reference).toMatchObject({ slug: MAZE_SLUG, commit, inputs: { params: {}, v: 0 }, template_name: model.name })
    expect(added.title).toBe(model.name)
    expect(added.colours).toEqual([])
    expect(added.output_id).toBeNull()
    const head = await failure(api.addOrderItem(ORDER_DRAFT, { slug: MAZE_SLUG, commit: 'HEAD', inputs: {}, quantity: 1 }))
    expect(head?.status).toBe(422)
    expect((head?.problem as { errors?: { loc: string[] }[] }).errors?.[0]?.loc).toEqual(['body', 'commit'])
  })

  it('keeps customers by id on an edit and archives them', async () => {
    const ada = await api.customer(CUSTOMER_ADA)
    const email = ada.contacts[0]!
    const edited = await api.updateCustomer(CUSTOMER_ADA, {
      contacts: [{ id: email.id, kind: 'email', value: 'ada@lovelace.test', primary: true }],
    })
    expect(edited.contacts.map((c) => [c.id === email.id, c.value])).toEqual([[true, 'ada@lovelace.test']])
    expect(edited.addresses).toHaveLength(1)
    const twice = await failure(
      api.createCustomer({
        display_name: 'Twice',
        contacts: [
          { kind: 'email', value: 'a@x.test', primary: true },
          { kind: 'email', value: 'b@x.test', primary: true },
        ],
      }),
    )
    expect(twice?.status).toBe(422)
    await api.archiveCustomer(CUSTOMER_ADA)
    expect((await api.customers()).map((c) => c.display_name)).toEqual(['Bob Builder'])
    expect((await api.customers({ includeArchived: true })).length).toBe(2)
  })
})

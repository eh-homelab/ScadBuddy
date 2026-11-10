import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import { DEFAULT_ORDERS_QUERY } from '../../../lib/ordersQuery'
import { MAZE_SLUG } from '../../fixtures'
import { resetMockState } from '../../handlers'
import { MAX_SCAN_PAGES, SCAN_PAGE_SIZE } from './orders'
import { CUSTOMER_ADA, ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, OUTPUT_REAGAN, PROBLEMS, seedCart, state } from './state'

// The mock answers the lifecycle's refusals as the backend does (orders/lifecycle.py,
// api/orders.py): 409 problems typed `orders/<slug>` naming the ids in the way, so the
// pages' error paths are testable against the same shapes.

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)

describe('orders mock (#4249)', () => {
  beforeEach(() => resetMockState())

  it('adds a batch of items all or none, naming the refused one (#4777)', async () => {
    const before = state.items.length
    const added = await api.addOrderItems(ORDER_DRAFT, [
      { output_id: 'c'.repeat(32), quantity: 2 },
      { kind: 'custom', title: 'Box', quantity: 1, unit_price_minor: 100 },
    ])
    expect(added.map((i) => [i.kind, i.quantity])).toEqual([
      ['model', 2],
      ['custom', 1],
    ])
    expect(state.items).toHaveLength(before + 2)
    const refused = await failure(
      api.addOrderItems(ORDER_DRAFT, [
        { output_id: 'c'.repeat(32), quantity: 1 },
        { output_id: 'f'.repeat(32), quantity: 1 },
      ]),
    )
    expect(refused?.problem.status).toBe(422)
    expect(refused?.problem['item_index']).toBe(1)
    // As `api/orders.py` `_in_item`: the field's place is inside the item.
    expect((refused?.problem['errors'] as { loc: unknown[] }[]).map((e) => e.loc.slice(0, 3))).toEqual([['body', 'items', 1]])
    expect(state.items).toHaveLength(before + 2)
    expect((await failure(api.addOrderItems(ORDER_DRAFT, [])))?.problem.status).toBe(422)
    // The plain batch is refused on an accepted order; its own route takes it.
    expect((await failure(api.addOrderItems(ORDER_ACCEPTED, [{ output_id: OUTPUT_REAGAN, quantity: 1 }])))?.problem.status).toBe(409)
    const after = await api.addOrderItemsAfterAcceptance(ORDER_ACCEPTED, [{ output_id: OUTPUT_REAGAN, quantity: 1 }])
    expect(after).toHaveLength(1)
  })

  it('lists every model\'s outputs newest first with their filters (#4777)', async () => {
    const all = await api.listRecentOutputs({})
    expect(all.items.map((o) => o.name)).toEqual(['Reagan', 'Nova', 'Workshop'])
    expect(all.total).toBe(3)
    expect((await api.listRecentOutputs({ ordered: 'yes' })).items.map((o) => o.id)).toEqual([OUTPUT_REAGAN])
    expect((await api.listRecentOutputs({ q: 'NOVA' })).items.map((o) => o.name)).toEqual(['Nova'])
    expect((await api.listRecentOutputs({ from: '2026-09-21T00:00:00Z' })).items.map((o) => o.name)).toEqual(['Reagan'])
    expect((await api.listRecentOutputs({ to: '2026-09-19T00:00:00Z' })).items.map((o) => o.name)).toEqual(['Workshop'])
    expect((await api.listRecentOutputs({ slug: [MAZE_SLUG] })).items).toEqual([])
    const page = await api.listRecentOutputs({}, { limit: 2 })
    expect(page.items).toHaveLength(2)
    const next = await api.listRecentOutputs({}, { limit: 2, after: page.items[1]!.id })
    expect(next.items.map((o) => o.name)).toEqual(['Workshop'])
  })

  it('adds a library file item and refuses any other source as the backend does (#5544)', async () => {
    const added = await api.addOrderItem(ORDER_DRAFT, { library_file_id: 120, quantity: 2 })
    expect(added).toMatchObject({
      kind: 'library_file',
      title: 'drawer-label.3mf',
      reference: null,
      output_id: null,
      status: 'draft',
      library_file: { id: 120, filename: 'drawer-label.3mf', library: 'bambuddy' },
    })
    for (const body of [
      { library_file_id: 120, quantity: 1, output_id: OUTPUT_REAGAN },
      { library_file_id: 120, quantity: 1, slug: MAZE_SLUG },
      { library_file_id: 120, quantity: 1, kind: 'custom' as const, title: 'x', unit_price_minor: 1 },
      { library_file_id: 120, quantity: 1, kind: 'model' as const },
      { library_file_id: 120, quantity: 1, accept: false },
      { library_file_id: 0, quantity: 1 },
      { kind: 'library_file' as const, quantity: 1 },
    ]) {
      expect((await failure(api.addOrderItem(ORDER_DRAFT, body)))?.status, JSON.stringify(body)).toBe(422)
    }
    const missing = await failure(api.addOrderItem(ORDER_DRAFT, { library_file_id: 999_999, quantity: 1 }))
    expect(missing?.status).toBe(422)
    // An STL prints; the dialog's search lists unsliced 3MFs only, but the route takes any printable file.
    expect((await api.addOrderItem(ORDER_DRAFT, { library_file_id: 46, quantity: 1 })).kind).toBe('library_file')
    const late = await failure(api.addOrderItem(ORDER_ACCEPTED, { library_file_id: 120, quantity: 1 }))
    expect(late?.problem.type).toBe(`${PROBLEMS}item_locked`)
    expect((await api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, { library_file_id: 120, quantity: 1 })).status).toBe('draft')
  })

  it('prints a library file item as printing.py does, and refuses one whose file is not in the library (#5544)', async () => {
    const item = state.items.find((i) => i.id === ITEM_APPROVED)!
    Object.assign(item, {
      kind: 'library_file',
      reference: null,
      output_id: null,
      colours: [],
      library_file: { id: 120, filename: 'drawer-label.3mf', library: 'bambuddy' },
    })
    const body = {
      printer_id: 1,
      filament_plan: { force_colour_match: false },
      choices: { nozzles: [{ size: '0.4' as const, flow: 'standard' as const }], tier: 'standard' as const, bed_type: 'Textured PEI Plate' },
    }
    const attempt = await api.printItem(ORDER_ACCEPTED, ITEM_APPROVED, body)
    expect(attempt).toMatchObject({ order_item_id: ITEM_APPROVED, purpose: 'production', copies: 2 })
    expect(item.status).toBe('queued')
    // The same item again, its file no longer in the library.
    state.fulfilments = state.fulfilments.filter((f) => f.order_item_id !== ITEM_APPROVED)
    item.status = 'approved'
    item.library_file = { id: 999_999, filename: 'deleted.3mf', library: 'bambuddy' }
    const missing = await failure(api.printItem(ORDER_ACCEPTED, ITEM_APPROVED, body))
    expect(missing?.problem.type).toBe(`${PROBLEMS}library_file_gone`)
    // A custom item is still refused as one.
    Object.assign(item, { kind: 'custom', library_file: null })
    expect((await failure(api.printItem(ORDER_ACCEPTED, ITEM_APPROVED, body)))?.problem.type).toBe(`${PROBLEMS}item_custom`)
  })

  it('adds an item after acceptance as the backend does (#5349)', async () => {
    const custom = { kind: 'custom' as const, title: 'Tag 49', quantity: 2, unit_price_minor: 300 }
    const plain = await failure(api.addOrderItem(ORDER_ACCEPTED, custom))
    expect(plain?.problem.type).toBe(`${PROBLEMS}item_locked`)
    expect(plain?.problem.detail).toMatch(/items\/after-acceptance/)
    const early = await failure(api.addOrderItemAfterAcceptance(ORDER_DRAFT, custom))
    expect(early?.problem.type).toBe(`${PROBLEMS}order_not_accepted`)
    const accepted = await failure(api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, { ...custom, accept: true }))
    expect(accepted?.status).toBe(422)
    const added = await api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, custom)
    // Its price is recorded, not accepted: pending in the totals, the accepted prices kept.
    expect(added.status).toBe('quoted')
    const detail = await api.order(ORDER_ACCEPTED)
    expect(detail.order.status).toBe('accepted')
    expect(detail.totals.pending_minor).toBe(600)
    const events = await api.orderEvents(ORDER_ACCEPTED)
    expect(events.filter((e) => e.kind === 'item.added_after_acceptance').map((e) => e.detail)).toEqual([
      { item_id: added.id, title: 'Tag 49', quantity: 2, order_status: 'accepted', item_kind: 'custom' },
    ])
    // The new item's quantity still changes; the accepted item's stays pinned.
    await api.updateOrderItem(ORDER_ACCEPTED, added.id, { quantity: 3, accept: false })
    const locked = await failure(api.updateOrderItem(ORDER_ACCEPTED, ITEM_APPROVED, { quantity: 3 }))
    expect(locked?.problem.type).toBe(`${PROBLEMS}item_locked`)
    await api.holdOrder(ORDER_ACCEPTED)
    const held = await failure(api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, custom))
    expect(held?.problem.type).toBe(`${PROBLEMS}order_on_hold`)
  })

  it('checks charges after acceptance with Pydantic\'s lax int and code-point length (#5568)', async () => {
    const post = { kind: 'shipping' as const, label: 'Post', amount_minor: 500 }
    // As Pydantic does: a label of 200 code points (emoji are two UTF-16 units each) and an
    // integer string pass; 201 code points do not.
    const long = await failure(api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, [{ ...post, label: '\u{1F4E6}'.repeat(201) }]))
    expect(long?.status).toBe(422)
    await api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, [{ ...post, label: '\u{1F4E6}'.repeat(200), amount_minor: '500' as unknown as number }])
    expect((await api.order(ORDER_ACCEPTED)).charges.map((c) => c.amount_minor)).toEqual([500])
    // The rest of the lax int (#5573): booleans, `_` separators and a zero fraction pass;
    // a fraction or an exponent does not.
    const as = (v: unknown) => [{ ...post, amount_minor: v as number }]
    for (const [sent, stored] of [[true, 1], [false, 0], ['1_000', 1000], ['5.0', 5], [' +4 ', 4], ['5.00', 5], ['1_000.0', 1000]] as const) {
      await api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, as(sent))
      expect((await api.order(ORDER_ACCEPTED)).charges.map((c) => c.amount_minor)).toEqual([stored])
    }
    for (const sent of ['5.5', '5.', '1e3']) {
      expect((await failure(api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, as(sent))))?.status).toBe(422)
    }
  })

  it('changes the charges after acceptance as the backend does (#5351)', async () => {
    const post = { kind: 'shipping' as const, label: 'Post', amount_minor: 500 }
    const cap = { kind: 'discount' as const, label: 'Agreed cap', amount_minor: -300 }
    const plain = await failure(api.updateOrder(ORDER_ACCEPTED, { charges: [post, cap] }))
    expect(plain?.problem.type).toBe(`${PROBLEMS}order_not_editable`)
    expect(plain?.problem.detail).toMatch(/PUT \/orders\/.+\/charges\/after-acceptance/)
    const early = await failure(api.replaceChargesAfterAcceptance(ORDER_DRAFT, [post]))
    expect(early?.problem.type).toBe(`${PROBLEMS}order_not_accepted`)
    // Each charge is checked as the backend's `ChargeInput` is (#5563).
    for (const bad of [{ ...post, kind: 'gift' }, { ...post, label: '' }, { ...post, amount_minor: 1.5 }, { ...post, note: 'x' }]) {
      const refused = await failure(api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, [post, bad as typeof post]))
      expect(refused?.status).toBe(422)
      expect(refused?.problem.detail).toMatch(/^charges\.1: /)
    }
    const changed = await api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, [post, cap])
    expect(changed.status).toBe('accepted')
    const detail = await api.order(ORDER_ACCEPTED)
    expect(detail.charges.map((c) => [c.kind, c.amount_minor])).toEqual([
      ['shipping', 500],
      ['discount', -300],
    ])
    expect(detail.totals.charges_minor).toBe(200)
    const events = await api.orderEvents(ORDER_ACCEPTED)
    expect(events.filter((e) => e.kind === 'charges.replaced_after_acceptance').map((e) => e.detail)).toEqual([
      { order_status: 'accepted', before: [post], after: [post, cap] },
    ])
    await api.holdOrder(ORDER_ACCEPTED)
    const held = await failure(api.replaceChargesAfterAcceptance(ORDER_ACCEPTED, []))
    expect(held?.problem.type).toBe(`${PROBLEMS}order_on_hold`)
  })

  it('takes an item added after acceptance back, alone or with its order (#5349)', async () => {
    const added = await api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, { kind: 'request', title: 'Tag 49', quantity: 1 })
    await api.cancelOrderItem(ORDER_ACCEPTED, added.id)
    expect((await api.reopenOrderItem(ORDER_ACCEPTED, added.id)).status).toBe('draft')
    await api.cancelOrder(ORDER_ACCEPTED)
    const reopened = await api.reopenOrder(ORDER_ACCEPTED)
    expect(reopened.status).toBe('accepted')
    const detail = await api.order(ORDER_ACCEPTED)
    expect(detail.items.find((i) => i.id === added.id)?.status).toBe('draft')
    expect(detail.items.find((i) => i.id === ITEM_APPROVED)?.status).toBe('approved')
  })

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

  it('caps the margin scan as the backend does: an empty page can still have a cursor (#5519)', async () => {
    const states = 'margin_state=ok&margin_state=thin&margin_state=loss&margin_state=unknown'
    const before = (await (await fetch(`/api/v1/orders?${states}`)).json()) as { items: { id: string }[] }
    expect(before.items.length).toBeGreaterThan(0)
    // A full scan of newer orders with no items, so no margin state, ahead of the seeds.
    const template = state.orders.find((o) => o.id === ORDER_DRAFT)
    if (!template) throw new Error('no seed order')
    for (let n = 0; n < MAX_SCAN_PAGES * SCAN_PAGE_SIZE; n++) {
      const stamp = new Date(Date.UTC(2026, 10, 1, 0, 0, n)).toISOString()
      state.orders.push({ ...template, id: `filler-${String(n).padStart(4, '0')}`, number: `ORD-F-${n}`, created_at: stamp })
    }
    const first = (await (await fetch(`/api/v1/orders?${states}`)).json()) as { items: { id: string }[]; next_cursor: string | null }
    expect(first.items).toEqual([])
    expect(first.next_cursor).not.toBeNull()
    const next = (await (await fetch(`/api/v1/orders?${states}&cursor=${first.next_cursor}`)).json()) as { items: { id: string }[]; next_cursor: string | null }
    expect(next.items.map((o) => o.id)).toEqual(before.items.map((o) => o.id))
    expect(next.next_cursor).toBeNull()
  })

  it('at the cap a short page carries the cursor of the last row read, not of its last match (#5520)', async () => {
    const states = 'margin_state=ok&margin_state=thin&margin_state=loss&margin_state=unknown'
    const before = (await (await fetch(`/api/v1/orders?${states}`)).json()) as { items: { id: string }[] }
    expect(before.items.length).toBeGreaterThan(1)
    const template = state.orders.find((o) => o.id === ORDER_DRAFT)
    if (!template) throw new Error('no seed order')
    for (let n = 0; n < MAX_SCAN_PAGES * SCAN_PAGE_SIZE; n++) {
      const stamp = new Date(Date.UTC(2026, 10, 1, 0, 0, n)).toISOString()
      state.orders.push({ ...template, id: `filler-${String(n).padStart(4, '0')}`, number: `ORD-F-${n}`, created_at: stamp })
    }
    // One match, newer than every filler: the scan finds it, then reads fillers to the cap.
    const match = state.orders.find((o) => o.id === before.items[0]?.id)
    if (!match) throw new Error('no matching order')
    match.created_at = new Date(Date.UTC(2026, 11, 1)).toISOString()
    const first = (await (await fetch(`/api/v1/orders?${states}`)).json()) as { items: { id: string }[]; next_cursor: string | null }
    expect(first.items.map((o) => o.id)).toEqual([match.id])
    expect(first.next_cursor).not.toBeNull()
    const next = (await (await fetch(`/api/v1/orders?${states}&cursor=${first.next_cursor}`)).json()) as { items: { id: string }[]; next_cursor: string | null }
    // The cursor is past the fillers read, so the match is not repeated and the rest follow.
    expect(next.items.map((o) => o.id)).toEqual(before.items.slice(1).map((o) => o.id))
    expect(next.next_cursor).toBeNull()
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
    // A draft item with logged time stays on the record (#5326).
    const logged = await api.addOrderItem(ORDER_DRAFT, { output_id: OUTPUT_REAGAN, quantity: 1 })
    await api.addTimeEntry(ORDER_DRAFT, { order_item_id: logged.id, kind: 'design', minutes: 5 })
    const kept = await failure(api.deleteOrderItem(ORDER_DRAFT, logged.id))
    expect(kept?.problem.type).toBe(`${PROBLEMS}item_has_entries`)
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

// #4955 — the items card's routes, as `api/orders.py` answers them: a duplicate, and a bulk fulfil.
describe('orders mock · duplicate and bulk fulfil (#4955)', () => {
  beforeEach(() => resetMockState())

  it('duplicates an item with its design and no quote, and refuses past quoted without after_acceptance', async () => {
    const original = state.items.find((i) => i.id === ITEM_DRAFT)!
    const copy = await api.duplicateOrderItem(ORDER_DRAFT, ITEM_DRAFT)
    expect(copy.id).not.toBe(ITEM_DRAFT)
    expect([copy.kind, copy.title, copy.quantity, copy.reference, copy.status]).toEqual([original.kind, original.title, original.quantity, original.reference, 'draft'])
    expect(state.quotes.filter((q) => q.order_item_id === copy.id)).toEqual([])
    expect(state.events.at(-1)).toMatchObject({ kind: 'item.duplicated', detail: { item_id: copy.id, from_item_id: ITEM_DRAFT } })
    const locked = await failure(api.duplicateOrderItem(ORDER_ACCEPTED, ITEM_APPROVED))
    expect(locked?.problem.type).toBe(`${PROBLEMS}item_locked`)
    const after = await api.duplicateOrderItem(ORDER_ACCEPTED, ITEM_APPROVED, { after_acceptance: true })
    expect(after.status).toBe('draft')
    expect((await failure(api.duplicateOrderItem(ORDER_DRAFT, 'f'.repeat(32))))?.problem.status).toBe(404)
  })

  it("keeps a custom item's price on its copy, accepted only before acceptance", async () => {
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Box', quantity: 2, unit_price_minor: 300 })
    const copy = await api.duplicateOrderItem(ORDER_DRAFT, custom.id)
    expect(state.quotes.filter((q) => q.order_item_id === copy.id).map((q) => [q.unit_price_minor, q.accepted])).toEqual([[300, true]])
  })

  it('fulfils several items in one go, and writes nothing when one is refused', async () => {
    const order = state.orders.find((o) => o.id === ORDER_ACCEPTED)!
    const second = await api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, { kind: 'custom', title: 'Box', quantity: 3, unit_price_minor: 300 })
    // Not approved yet (its price waits for the customer): nothing is written.
    const before = state.fulfilments.length
    const refused = await failure(
      api.fulfilOrderItems(ORDER_ACCEPTED, [
        { item_id: ITEM_APPROVED, quantity: 2 },
        { item_id: second.id, quantity: 1 },
      ]),
    )
    expect(refused?.problem.type).toBe(`${PROBLEMS}item_not_approved`)
    expect(refused?.problem['items']).toEqual([second.id])
    expect(state.fulfilments).toHaveLength(before)
    const made = await api.fulfilOrderItems(ORDER_ACCEPTED, [{ item_id: ITEM_APPROVED, quantity: 2 }])
    expect(made.map((f) => [f.order_item_id, f.quantity, f.outcome, f.accepted_at !== null])).toEqual([[ITEM_APPROVED, 2, 'succeeded', true]])
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('fulfilled')
    expect(order.status).not.toBe('accepted')
    expect((await failure(api.fulfilOrderItems(ORDER_ACCEPTED, [])))?.problem.status).toBe(422)
    const twice = await failure(api.fulfilOrderItems(ORDER_ACCEPTED, [{ item_id: second.id, quantity: 1 }, { item_id: second.id, quantity: 1 }]))
    expect(twice?.problem.status).toBe(422)
  })

  it("refuses both on a visitor's cart (#5282)", async () => {
    const cart = seedCart()
    const item = await api.addOrderItem(cart.id, { kind: 'request', title: 'Tag', quantity: 1 })
    expect((await failure(api.duplicateOrderItem(cart.id, item.id)))?.problem.type).toBe(`${PROBLEMS}order_not_submitted`)
    expect((await failure(api.fulfilOrderItems(cart.id, [{ item_id: item.id, quantity: 1 }])))?.problem.type).toBe(`${PROBLEMS}order_not_submitted`)
  })
})

describe('orders mock · storefront prices (#5283)', () => {
  beforeEach(() => resetMockState())

  it('refuses the shop\'s accept of any reprice of a storefront custom item, a quantity one too (#5283)', async () => {
    const order = state.orders.find((o) => o.id === ORDER_DRAFT)!
    order.source = 'storefront'
    const item = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Box', quantity: 1, unit_price_minor: 100 })
    expect(item.status).toBe('quoted')
    for (const body of [{ quantity: 2, accept: true }, { unit_price_minor: 200, accept: true }]) {
      expect((await failure(api.updateOrderItem(ORDER_DRAFT, item.id, body)))?.problem.type).toBe(`${PROBLEMS}customer_accepts`)
    }
    expect(state.items.find((i) => i.id === item.id)?.quantity).toBe(1)
    // No reprice, nothing to accept: the edit goes through.
    expect((await api.updateOrderItem(ORDER_DRAFT, item.id, { title: 'Crate', accept: true })).title).toBe('Crate')
    // Unset, the price is left for the customer.
    expect((await api.updateOrderItem(ORDER_DRAFT, item.id, { quantity: 3 })).status).toBe('quoted')
  })
})

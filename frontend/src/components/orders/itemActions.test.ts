import { describe, expect, it } from 'vitest'
import type { Order, OrderItemDetail, Quote, Shipment } from '../../api/types'
import { itemActions, itemEdits, itemSource, LOCKED_TEXT, NO_SLICE_SOURCE, REVISED_TEXT } from './itemActions'

// #4955, order operations spec 2026-11-07 §4.1: the row's ⋯ menu shows an action only when
// it is allowed now, and a refused one says why in words.

const order = (fields: Partial<Order> = {}) => ({ status: 'draft', on_hold: false, ...fields }) as Order
const quote = (fields: Partial<Quote> = {}) => ({ id: 'q1', accepted: false, quantity: 1, created_at: '2026-11-07T10:00:00Z', ...fields }) as Quote
const item = (fields: Partial<OrderItemDetail> = {}) =>
  ({
    id: 'i1',
    kind: 'model',
    status: 'draft',
    quantity: 1,
    quotes: [],
    fulfilments: [],
    reference: { slug: 'cap', commit: 'a'.repeat(40), inputs: {} },
    output_id: 'o'.repeat(32),
    library_file: null,
    origin_archive_ids: [],
    revised_after_acceptance: false,
    ...fields,
  }) as unknown as OrderItemDetail

const keys = (o: Order, i: OrderItemDetail, shipments: Shipment[] = [], unfulfilAllowed = true) =>
  itemActions(o, i, { shipments, unfulfilAllowed }).map((a) => a.key)

describe('itemActions (#4955)', () => {
  it('offers a draft item details, a slice quote and its delete', () => {
    expect(keys(order(), item())).toEqual(['open', 'quote', 'duplicate', 'delete'])
  })

  it("refuses a slice quote, with the drawer's reason, for an item with nothing rendered to slice", () => {
    const blocked = (i: OrderItemDetail) => itemActions(order(), i, { shipments: [], unfulfilAllowed: true }).find((a) => a.key === 'quote')?.blocked
    expect(blocked(item({ kind: 'request', reference: null, output_id: null }))).toBe(NO_SLICE_SOURCE)
    expect(blocked(item({ output_id: null }))).toBe(NO_SLICE_SOURCE)
    expect(blocked(item())).toBeNull()
    expect(blocked(item({ kind: 'library_file', reference: null, output_id: null, library_file: { id: 1, filename: 'a.3mf', library: 'native' } }))).toBeNull()
  })

  it('offers no slice quote for a custom item', () => {
    expect(keys(order(), item({ kind: 'custom' }))).toEqual(['open', 'duplicate', 'delete'])
  })

  it('cancels a quoted item rather than deleting it', () => {
    expect(keys(order({ status: 'quoted' }), item({ status: 'quoted', quotes: [quote()] }))).toEqual(['open', 'quote', 'duplicate', 'cancel'])
  })

  it('offers print and fulfil on an accepted order, and no quote of a priced item', () => {
    const priced = item({ status: 'approved', quotes: [quote({ accepted: true })] })
    expect(keys(order({ status: 'accepted' }), priced)).toEqual(['open', 'print', 'fulfil', 'duplicate', 'cancel'])
  })

  it('says why print and fulfil are refused while the order is on hold', () => {
    const priced = item({ status: 'approved', quotes: [quote({ accepted: true })] })
    const actions = itemActions(order({ status: 'accepted', on_hold: true }), priced, { shipments: [], unfulfilAllowed: true })
    const blocked = Object.fromEntries(actions.map((a) => [a.key, a.blocked]))
    expect(blocked.print).toMatch(/on hold/)
    expect(blocked.fulfil).toMatch(/on hold/)
    expect(blocked.open).toBeNull()
  })

  it('offers Unfulfil on a fulfilled item, refused when the setting is off', () => {
    const done = item({ status: 'fulfilled', quotes: [quote({ accepted: true })] })
    expect(keys(order({ status: 'ready' }), done)).toEqual(['open', 'unfulfil', 'duplicate'])
    const off = itemActions(order({ status: 'ready' }), done, { shipments: [], unfulfilAllowed: false })
    expect(off.find((a) => a.key === 'unfulfil')?.blocked).toMatch(/Settings/)
  })

  it('reopens a cancelled item, and says why a shipped order takes back no unpriced one', () => {
    expect(keys(order({ status: 'quoted' }), item({ status: 'cancelled' }))).toEqual(['open', 'reopen', 'duplicate'])
    const shipped = itemActions(order({ status: 'shipped' }), item({ status: 'cancelled' }), { shipments: [], unfulfilAllowed: true })
    expect(shipped.find((a) => a.key === 'reopen')?.blocked).toMatch(/no accepted price/)
  })

  it('duplicates on an open or accepted order, refused on hold, and not once shipped', () => {
    const priced = item({ status: 'approved', quotes: [quote({ accepted: true })] })
    const held = itemActions(order({ status: 'in_production', on_hold: true }), priced, { shipments: [], unfulfilAllowed: true })
    expect(held.find((a) => a.key === 'duplicate')?.blocked).toMatch(/on hold/)
    expect(keys(order({ status: 'shipped' }), item({ status: 'fulfilled', quotes: [quote({ accepted: true })] }))).not.toContain('duplicate')
  })

  it("offers only details on a visitor's cart (#5282)", () => {
    expect(keys(order({ source: 'storefront', submitted_at: null }), item())).toEqual(['open'])
  })

  it('offers only details on an ended order', () => {
    expect(keys(order({ status: 'closed' }), item({ status: 'fulfilled' }))).toEqual(['open'])
    expect(keys(order({ status: 'cancelled' }), item({ status: 'cancelled' }))).toEqual(['open'])
  })
})

describe('itemSource (#4955)', () => {
  it('names where the item came from', () => {
    expect(itemSource(item({ output_id: null }))).toBe('model')
    expect(itemSource(item())).toBe('output')
    expect(itemSource(item({ kind: 'library_file', reference: null, output_id: null, library_file: { id: 1, filename: 'a.3mf', library: 'native' } }))).toBe('file')
    expect(itemSource(item({ kind: 'library_file', reference: null, output_id: null, origin_archive_ids: [71] }))).toBe('past print')
    expect(itemSource(item({ kind: 'custom', reference: null, output_id: null }))).toBe('custom')
    expect(itemSource(item({ kind: 'request', reference: null, output_id: null }))).toBe('request')
  })
})

// #4955 step 2, spec 2026-11-07 §4.1: inline edit. Quantity and price follow the backend's
// `item_open` and `requote_open`; past them they are read-only with the reason shown. The
// title stays editable while the item is open.
describe('itemEdits (#4955)', () => {
  const accepted = [quote({ accepted: true })]

  it('edits everything on a draft or quoted order', () => {
    for (const status of ['draft', 'quoted'] as const) {
      expect(itemEdits(order({ status }), item())).toEqual({ title: true, quantity: true, price: true, locked: null })
    }
  })

  it('locks the quantity and price of an accepted item, with the reason, and keeps the title', () => {
    expect(itemEdits(order({ status: 'accepted' }), item({ status: 'approved', quotes: accepted }))).toEqual({
      title: true,
      quantity: false,
      price: false,
      locked: `${LOCKED_TEXT} To change what they get, revise the design or add an item.`,
    })
  })

  it('names only the ways out the backend takes now', () => {
    const accepted = [quote({ accepted: true })]
    const priced = (fields: Partial<OrderItemDetail> = {}) => item({ status: 'approved', quotes: accepted, ...fields })
    const locked = (o: Order, i: OrderItemDetail) => itemEdits(o, i).locked
    // A model item on an accepted order: revise it, or add an item.
    expect(locked(order({ status: 'accepted' }), priced())).toBe(`${LOCKED_TEXT} To change what they get, revise the design or add an item.`)
    // A custom or library file item takes no revision (`NO_REVISIONS`).
    expect(locked(order({ status: 'accepted' }), priced({ kind: 'custom', reference: null, output_id: null }))).toBe(`${LOCKED_TEXT} To change what they get, add an item.`)
    // A shipped order takes neither, nor does an order on hold.
    expect(locked(order({ status: 'shipped' }), priced())).toBe(LOCKED_TEXT)
    expect(locked(order({ status: 'accepted', on_hold: true }), priced())).toBe(LOCKED_TEXT)
  })

  it('edits an item added after acceptance until its price is accepted (#5349)', () => {
    expect(itemEdits(order({ status: 'in_production' }), item({ status: 'quoted', quotes: [quote()] }))).toEqual({
      title: true,
      quantity: true,
      price: true,
      locked: null,
    })
  })

  it('re-quotes an item revised after acceptance, its quantity pinned (#5350)', () => {
    const revised = item({ status: 'draft', quotes: accepted, revised_after_acceptance: true })
    expect(itemEdits(order({ status: 'accepted' }), revised)).toEqual({ title: true, quantity: false, price: true, locked: REVISED_TEXT })
  })

  it("never re-quotes a custom item's own price past acceptance", () => {
    const custom = item({ kind: 'custom', status: 'approved', quotes: accepted, revised_after_acceptance: true })
    expect(itemEdits(order({ status: 'accepted' }), custom).price).toBe(false)
  })

  it('edits nothing on a settled item, an ended order or a visitor cart', () => {
    const none = { title: false, quantity: false, price: false, locked: null }
    expect(itemEdits(order(), item({ status: 'fulfilled' }))).toEqual(none)
    expect(itemEdits(order(), item({ status: 'cancelled' }))).toEqual(none)
    expect(itemEdits(order({ status: 'closed' }), item({ status: 'approved', quotes: accepted }))).toEqual(none)
    expect(itemEdits(order({ source: 'storefront', submitted_at: null }), item())).toEqual(none)
  })
})

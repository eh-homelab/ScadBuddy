import { describe, expect, it } from 'vitest'
import type { Order, OrderItemDetail, Quote, Shipment } from '../../api/types'
import { itemActions, itemSource } from './itemActions'

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
    expect(keys(order(), item())).toEqual(['open', 'quote', 'delete'])
  })

  it('offers no slice quote for a custom item', () => {
    expect(keys(order(), item({ kind: 'custom' }))).toEqual(['open', 'delete'])
  })

  it('cancels a quoted item rather than deleting it', () => {
    expect(keys(order({ status: 'quoted' }), item({ status: 'quoted', quotes: [quote()] }))).toEqual(['open', 'quote', 'cancel'])
  })

  it('offers print and fulfil on an accepted order, and no quote of a priced item', () => {
    const priced = item({ status: 'approved', quotes: [quote({ accepted: true })] })
    expect(keys(order({ status: 'accepted' }), priced)).toEqual(['open', 'print', 'fulfil', 'cancel'])
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
    expect(keys(order({ status: 'ready' }), done)).toEqual(['open', 'unfulfil'])
    const off = itemActions(order({ status: 'ready' }), done, { shipments: [], unfulfilAllowed: false })
    expect(off.find((a) => a.key === 'unfulfil')?.blocked).toMatch(/Settings/)
  })

  it('reopens a cancelled item, and says why a shipped order takes back no unpriced one', () => {
    expect(keys(order({ status: 'quoted' }), item({ status: 'cancelled' }))).toEqual(['open', 'reopen'])
    const shipped = itemActions(order({ status: 'shipped' }), item({ status: 'cancelled' }), { shipments: [], unfulfilAllowed: true })
    expect(shipped.find((a) => a.key === 'reopen')?.blocked).toMatch(/no accepted price/)
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

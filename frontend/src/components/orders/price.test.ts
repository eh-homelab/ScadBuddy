import { describe, expect, it } from 'vitest'
import type { OrderItemDetail, Quote } from '../../api/types'
import { byCreated, itemPrice, parseUnitPrice, unitPrice } from './price'

type QuoteFields = Pick<Quote, 'id' | 'created_at' | 'unit_price_minor' | 'total_minor' | 'accepted'>

/** A whole `Quote` from the fields these tests vary, so the fixture keeps in step with the type. */
function quote({ id, created_at, unit_price_minor, total_minor, accepted }: QuoteFields): Quote {
  return {
    id,
    order_item_id: 'i-1',
    created_at,
    source: 'manual',
    accepted,
    sliced_sha256: null,
    sliced_blob_key: null,
    plates: [],
    lines: [{ kind: 'manual', label: 'Quoted by hand', quantity: 1, unit_minor: unit_price_minor, amount_minor: unit_price_minor }],
    unit_cost_minor: unit_price_minor,
    unit_price_minor,
    quantity: unit_price_minor ? Math.round(total_minor / unit_price_minor) : 0,
    total_minor,
    created_by: { kind: 'browser', principal: null, session: null },
  }
}

function priced(quantity: number, quotes: QuoteFields[]): Pick<OrderItemDetail, 'quantity' | 'quotes'> {
  return { quantity, quotes: quotes.map(quote) }
}

describe('unitPrice', () => {
  it("is the accepted quote's, beside its pinned total", () => {
    const item = priced(3, [
      { id: 'a', created_at: '2026-10-24T22:00:00Z', unit_price_minor: 500, total_minor: 1000, accepted: true },
      { id: 'b', created_at: '2026-10-24T23:00:00Z', unit_price_minor: 700, total_minor: 2100, accepted: false },
    ])
    expect(unitPrice(item)).toEqual({ minor: 500, accepted: true })
    expect(itemPrice(item)).toEqual({ amount: 1000, accepted: true })
  })

  it("is the newest quote's by instant before one is accepted", () => {
    const item = priced(2, [
      { id: 'later', created_at: '2026-10-24T23:00:00.123Z', unit_price_minor: 725, total_minor: 1450, accepted: false },
      { id: 'whole', created_at: '2026-10-24T23:00:00Z', unit_price_minor: 800, total_minor: 1600, accepted: false },
    ])
    expect(unitPrice(item)).toEqual({ minor: 725, accepted: false })
    expect(itemPrice(item)).toEqual({ amount: 1450, accepted: false })
  })

  it('is null with no quote', () => {
    expect(unitPrice(priced(1, []))).toBeNull()
  })
})

describe('parseUnitPrice', () => {
  it('reads minor units at the exponent, never through a float', () => {
    expect(parseUnitPrice('12.5', 2)).toBe(1250)
    expect(parseUnitPrice(' 0.10 ', 2)).toBe(10)
    expect(parseUnitPrice('1.234', 3)).toBe(1234)
    expect(parseUnitPrice('7', 0)).toBe(7)
  })

  it('refuses a negative amount, a finer one, and text', () => {
    expect(parseUnitPrice('-1', 2)).toBeNull()
    expect(parseUnitPrice('12.345', 2)).toBeNull()
    expect(parseUnitPrice('1.5', 0)).toBeNull()
    expect(parseUnitPrice('', 2)).toBeNull()
    expect(parseUnitPrice('abc', 2)).toBeNull()
  })
})

describe('byCreated', () => {
  it('orders by instant, not text: a whole second sorts before its own fraction', () => {
    const whole = { id: 'b', created_at: '2026-10-24T23:00:00Z' }
    const later = { id: 'a', created_at: '2026-10-24T23:00:00.123Z' }
    expect([later, whole].sort(byCreated).map((q) => q.id)).toEqual(['b', 'a'])
  })

  it('breaks a tie on the millisecond by the microseconds, as the backend does (#4725)', () => {
    const first = { id: 'z', created_at: '2026-10-24T23:00:00.123100Z' }
    const second = { id: 'a', created_at: '2026-10-24T23:00:00.123900Z' }
    expect([second, first].sort(byCreated).map((q) => q.id)).toEqual(['z', 'a'])
    // A whole millisecond and its own microseconds: .123 is .123000.
    expect([{ id: 'b', created_at: '2026-10-24T23:00:00.123001Z' }, { id: 'c', created_at: '2026-10-24T23:00:00.123Z' }].sort(byCreated).map((q) => q.id)).toEqual(['c', 'b'])
  })

  it('breaks a tie by id', () => {
    const at = '2026-10-24T23:00:00Z'
    expect([{ id: 'q2', created_at: at }, { id: 'q1', created_at: at }].sort(byCreated).map((q) => q.id)).toEqual(['q1', 'q2'])
  })
})

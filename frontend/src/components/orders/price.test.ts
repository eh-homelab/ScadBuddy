import { describe, expect, it } from 'vitest'
import type { OrderItemDetail } from '../../api/types'
import { byCreated, itemPrice, parseUnitPrice, unitPrice } from './price'

function priced(quantity: number, quotes: { id: string; created_at: string; unit_price_minor: number; total_minor: number; accepted: boolean }[]) {
  return { quantity, quotes } as unknown as OrderItemDetail
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

  it('breaks a tie by id', () => {
    const at = '2026-10-24T23:00:00Z'
    expect([{ id: 'q2', created_at: at }, { id: 'q1', created_at: at }].sort(byCreated).map((q) => q.id)).toEqual(['q1', 'q2'])
  })
})

import { describe, expect, it } from 'vitest'
import { orderNumberMatcher } from './orderNumber'

describe('orderNumberMatcher', () => {
  it('finds the order number as a whole token inside longer text', () => {
    const matcher = orderNumberMatcher('ORD-2026-3')
    expect(matcher.test('ORD-2026-3')).toBe(true)
    expect(matcher.test('ORD-2026-3 · Draft')).toBe(true)
    expect(matcher.test('(ORD-2026-3)')).toBe(true)
  })

  it('never matches a longer number or token', () => {
    const matcher = orderNumberMatcher('ORD-2026-3')
    expect(matcher.test('ORD-2026-30')).toBe(false)
    expect(matcher.test('ORD-2026-3-a')).toBe(false)
    expect(matcher.test('XORD-2026-3')).toBe(false)
    expect(matcher.test('a-ORD-2026-3')).toBe(false)
  })

  it('refuses anything that is not an order number', () => {
    expect(() => orderNumberMatcher('ORD-2026-.')).toThrow('Not an order number')
    expect(() => orderNumberMatcher('(draft)')).toThrow('Not an order number')
    expect(() => orderNumberMatcher('')).toThrow('Not an order number')
  })
})

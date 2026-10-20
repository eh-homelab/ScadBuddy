import { describe, expect, it } from 'vitest'
import { formatMoney, fractionDigits, toMajor, toMinor } from './money'

describe('money (#4249)', () => {
  it('reads the minor unit from the currency', () => {
    expect(fractionDigits('USD')).toBe(2)
    expect(fractionDigits('JPY')).toBe(0)
    expect(fractionDigits('XXX_NOT_A_CODE')).toBe(2)
  })
  it('round-trips typed amounts through minor units', () => {
    expect(toMinor('12.34', 'USD')).toBe(1234)
    expect(toMinor('-2', 'USD')).toBe(-200)
    expect(toMinor('1200', 'JPY')).toBe(1200)
    expect(toMinor('twelve', 'USD')).toBeNull()
    expect(toMinor('', 'USD')).toBeNull()
    expect(toMajor(1234, 'USD')).toBe('12.34')
  })
  it('formats with the currency', () => {
    expect(formatMoney(1234, 'USD')).toContain('12.34')
    expect(formatMoney(1200, 'JPY')).toMatch(/1,?200/)
  })
})

import { describe, expect, it } from 'vitest'
import { formatMoney, toMajor, toMinor } from './money'

// #4250 Ruling 5: the exponent is the backend's (ISO 4217), passed in; `Intl` is asked for
// the formatting only, never for the digits.
describe('money (#4249, #4250)', () => {
  it('round-trips typed amounts through minor units at the exponent given', () => {
    expect(toMinor('12.34', 2)).toBe(1234)
    expect(toMinor('-2', 2)).toBe(-200)
    expect(toMinor('1200', 0)).toBe(1200)
    expect(toMinor('twelve', 2)).toBeNull()
    expect(toMinor('', 2)).toBeNull()
    expect(toMajor(1234, 2)).toBe('12.34')
    expect(toMajor(1200, 0)).toBe('1200')
  })
  it('refuses a finer amount than the minor unit rather than rounding it', () => {
    expect(toMinor('12.345', 2)).toBeNull()
    expect(toMinor('12.5', 0)).toBeNull()
    expect(toMinor('12.3', 2)).toBe(1230)
  })
  it('formats and parses IQD in thousandths, whatever Intl would say', () => {
    // CLDR gives IQD no decimals; ISO 4217 gives it three.
    expect(formatMoney(12345, 'IQD', 3)).toContain('12.345')
    expect(toMinor('12.345', 3)).toBe(12345)
    expect(toMajor(12345, 3)).toBe('12.345')
  })
  it('formats JPY in whole yen and USD in cents', () => {
    expect(formatMoney(1200, 'JPY', 0)).toMatch(/1,?200/)
    expect(formatMoney(1200, 'JPY', 0)).not.toContain('.')
    expect(formatMoney(1234, 'USD', 2)).toContain('12.34')
  })
  it('falls back to the code for one Intl refuses', () => {
    // A code of the wrong shape is a RangeError in `Intl.NumberFormat`.
    expect(formatMoney(1234, 'US', 2)).toBe('12.34 US')
  })
})

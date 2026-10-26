import { describe, expect, it } from 'vitest'
import { MAX_QUANTITY, parseQuantity } from './quantity'

describe('parseQuantity (#4727)', () => {
  it('takes a whole number from 1 to the bound', () => {
    expect(parseQuantity('1')).toBe(1)
    expect(parseQuantity(' 12 ')).toBe(12)
    expect(parseQuantity(String(MAX_QUANTITY))).toBe(MAX_QUANTITY)
  })

  it('refuses what Number reads as an integer but is not typed as one', () => {
    for (const text of ['1e9', '2.0', '0x10', '+3', '', '0', '-1', String(MAX_QUANTITY + 1)]) expect(parseQuantity(text), text).toBeNull()
  })
})

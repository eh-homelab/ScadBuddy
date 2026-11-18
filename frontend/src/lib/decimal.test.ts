import { describe, expect, it } from 'vitest'
import { addDecimals, cmpDecimals, divDecimal, formatCost, mulDecimal, parseDecimal, subDecimals } from './decimal'

describe('parseDecimal', () => {
  it('accepts a whole number, a dot and a comma, and says the dot form', () => {
    expect(parseDecimal('12')).toBe('12')
    expect(parseDecimal('12.5')).toBe('12.5')
    expect(parseDecimal('12,5')).toBe('12.5')
    expect(parseDecimal(' 7.25 ')).toBe('7.25')
  })

  it('refuses everything else', () => {
    for (const text of ['1e3', '', '-1', '1.', '.5', '1,2,3', 'abc', '1 000']) expect(parseDecimal(text), text).toBeNull()
  })
})

describe('exact arithmetic', () => {
  it('adds and subtracts on the longest scale', () => {
    expect(addDecimals('0.1', '0.2')).toBe('0.3')
    expect(subDecimals('117.04', '117')).toBe('0.04')
    expect(subDecimals('1', '1.5')).toBe('-0.5')
    expect(subDecimals('-1.25', '2')).toBe('-3.25')
    expect(subDecimals('0.1', '0.3')).toBe('-0.2')
  })

  it('multiplies by a whole number', () => {
    expect(mulDecimal('19.99', 3)).toBe('59.97')
  })

  it('compares by value, not by text', () => {
    expect(cmpDecimals('10.0', '10')).toBe(0)
    expect(cmpDecimals('9.99', '10')).toBe(-1)
    expect(cmpDecimals('10.01', '10')).toBe(1)
  })

  it('divides half up', () => {
    expect(divDecimal('1', '3', 4)).toBe('0.3333')
    expect(divDecimal('2', '3', 4)).toBe('0.6667')
    expect(divDecimal('0.00005', '1', 4)).toBe('0.0001')
  })

  it('divides a negative half away from zero, and throws on zero', () => {
    expect(divDecimal('-1', '3', 4)).toBe('-0.3333')
    expect(divDecimal('1', '-3', 4)).toBe('-0.3333')
    expect(divDecimal('-0.00005', '1', 4)).toBe('-0.0001')
    expect(() => divDecimal('1', '0', 4)).toThrow(RangeError)
    expect(() => divDecimal('1', '0.00', 4)).toThrow(RangeError)
  })

  it('refuses a factor that is not a whole number', () => {
    expect(() => mulDecimal('1.5', 0.5)).toThrow(RangeError)
    expect(mulDecimal('-1.5', 2)).toBe('-3.0')
  })
})

describe('formatCost', () => {
  it('keeps the digits it is given', () => {
    expect(formatCost('21.4', 'EUR', 2)).toContain('21.40')
    expect(formatCost('21.4567', 'EUR', 2)).toContain('21.4567')
  })

  it('shows no decimals at exponent 0 unless the string has them', () => {
    expect(formatCost('1500', 'JPY', 0)).toMatch(/1,500(?![.,]\d)/)
    expect(formatCost('1500.25', 'JPY', 0)).toContain('1,500.25')
  })
})

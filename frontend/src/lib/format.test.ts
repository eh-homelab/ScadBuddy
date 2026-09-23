import { describe, expect, it } from 'vitest'
import { bbox } from '../mocks/fixtures'
import { formatBbox, formatValue, inkOn, normalizeHex, timeAgo } from './format'

describe('formatBbox', () => {
  it('prints the size, one decimal, in millimetres', () => {
    expect(formatBbox(bbox(95.7, 34.6, 6.8))).toBe('95.7 × 34.6 × 6.8 mm')
    expect(formatBbox(bbox(42, 42, 21))).toBe('42.0 × 42.0 × 21.0 mm')
  })
})

describe('formatValue', () => {
  it('renders booleans as on and off', () => {
    expect(formatValue(true)).toBe('on')
    expect(formatValue(false)).toBe('off')
  })

  it('passes numbers and strings through', () => {
    expect(formatValue(5.2)).toBe('5.2')
    expect(formatValue('Reagan')).toBe('Reagan')
  })
})

describe('normalizeHex', () => {
  it('expands, uppercases and adds the hash', () => {
    expect(normalizeHex('abc')).toBe('#AABBCC')
    expect(normalizeHex('#1b6ca8')).toBe('#1B6CA8')
  })
})

describe('inkOn', () => {
  it('picks dark ink on a light swatch and light ink on a dark one', () => {
    expect(inkOn('#FFFFFF')).toBe('#12161d')
    expect(inkOn('#1B6CA8')).toBe('#f2f5fa')
  })
})

describe('timeAgo', () => {
  const now = Date.parse('2026-09-22T12:00:00Z')

  it('counts back in the largest sensible unit', () => {
    expect(timeAgo('2026-09-22T11:00:00Z', now)).toBe('1 hour ago')
    expect(timeAgo('2026-09-20T12:00:00Z', now)).toBe('2 days ago')
    expect(timeAgo('2026-09-22T11:59:30Z', now)).toBe('30 seconds ago')
  })
})

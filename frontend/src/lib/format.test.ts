import { describe, expect, it } from 'vitest'
import { bbox } from '../mocks/fixtures'
import { formatBbox, formatDuration, formatValue, inkOn, normalizeHex, timeAgo } from './format'

describe('formatBbox', () => {
  it('prints the size, one decimal, in millimetres', () => {
    expect(formatBbox(bbox(95.7, 34.6, 6.8))).toBe('95.7 × 34.6 × 6.8 mm')
    expect(formatBbox(bbox(42, 42, 21))).toBe('42.0 × 42.0 × 21.0 mm')
  })

  it('converts to inches, two decimals, when that is the display unit', () => {
    expect(formatBbox(bbox(95.7, 34.6, 6.8), 'in')).toBe('3.77 × 1.36 × 0.27 in')
    expect(formatBbox(bbox(25.4, 50.8, 254), 'in')).toBe('1.00 × 2.00 × 10.00 in')
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

  it('drops alpha the way OpenSCAD does, shorthand included', () => {
    expect(normalizeHex('#f14c')).toBe('#FF1144')
    expect(normalizeHex('#1b6ca880')).toBe('#1B6CA8')
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

describe('formatDuration', () => {
  it('reads as hours and minutes, or minutes, or seconds', () => {
    expect(formatDuration(6437)).toBe('1h 47m')
    expect(formatDuration(2590)).toBe('43m')
    expect(formatDuration(42)).toBe('42s')
    expect(formatDuration(7200)).toBe('2h 0m')
  })
})

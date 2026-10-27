import { describe, expect, it } from 'vitest'
import { bbox } from '../mocks/fixtures'
import {
  dueAt,
  dueDay,
  formatBbox,
  formatBytes,
  formatDueDate,
  formatDuration,
  formatElapsed,
  formatValue,
  inkOn,
  normalizeHex,
  queuePlace,
  timeAgo,
} from './format'

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

describe('formatBytes', () => {
  it('uses decimal units up to terabytes', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(48213)).toBe('48 kB')
    expect(formatBytes(2091667)).toBe('2.1 MB')
    expect(formatBytes(1846000000)).toBe('1.8 GB')
    expect(formatBytes(3e12)).toBe('3.0 TB')
  })
})

describe('a waiting render (#1638)', () => {
  it('says where it stands in the queue', () => {
    expect(queuePlace(1)).toBe('next in line')
    expect(queuePlace(4)).toBe('3 ahead')
  })

  it('counts its time to the second, never below zero', () => {
    expect(formatElapsed(7.9)).toBe('0:07')
    expect(formatElapsed(252)).toBe('4:12')
    expect(formatElapsed(-3)).toBe('0:00')
  })
})

describe('due dates (#4728)', () => {
  it('reads the stored UTC midnight as that day in every time zone', () => {
    const stored = dueAt('2026-10-20')
    expect(stored).toBe('2026-10-20T00:00:00Z')
    expect(dueDay(stored)).toBe('2026-10-20')
    expect(dueDay('2026-10-20T00:00:00+00:00')).toBe('2026-10-20')
    expect(dueDay('not a date')).toBe('')
    // The local calendar day, written as the locale writes it.
    expect(formatDueDate(stored)).toBe(new Date(2026, 9, 20).toLocaleDateString())
  })
})

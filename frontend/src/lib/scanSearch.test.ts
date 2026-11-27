import { describe, expect, it } from 'vitest'
import { isScadBuddyCode, scanSearchPath, scanSearchQuery, searchesSpools } from './scanSearch'

// #8372: a scanned code that names no ScadBuddy thing searches the spools.

describe('scanSearchQuery', () => {
  it('makes 8 to 14 digits a gtin: search', () => {
    expect(scanSearchQuery('6975373321234')).toBe('gtin:6975373321234')
    expect(scanSearchQuery(' 12345678 ')).toBe('gtin:12345678')
  })

  it('keeps any other text as the trimmed text', () => {
    expect(scanSearchQuery('1234567')).toBe('1234567')
    expect(scanSearchQuery('ABC 12')).toBe('ABC 12')
    expect(scanSearchQuery(' ABC 12 ')).toBe('ABC 12')
  })
})

describe('scanSearchPath', () => {
  it('is the Spools tab with the encoded query and from=scan', () => {
    expect(scanSearchPath('a b&c')).toBe('/filament/spools?q=a%20b%26c&from=scan')
  })
})

describe('isScadBuddyCode', () => {
  it('is a letter, an optional dash and digits', () => {
    for (const text of ['S-142', 'l4', 'K-3']) expect(isScadBuddyCode(text), text).toBe(true)
    for (const text of ['6975373321234', 'https://x']) expect(isScadBuddyCode(text), text).toBe(false)
  })
})

describe('searchesSpools', () => {
  it('is false for a text with the ScadBuddy code shape or a ScadBuddy QR text (D11)', () => {
    for (const text of ['L-99', 's142', 'https://h/x/L-99', 'https://h/x/S-5?x=1', 'scadbuddy:spool:0b6c1f2e-1d2a-4c3b-9e8f-7a6b5c4d3e2f', 'SCADBUDDY:spool:x']) expect(searchesSpools(text), text).toBe(false)
  })

  it('is true for any other text', () => {
    for (const text of ['6975373321234', 'https://example.com/p', 'ABC 12']) expect(searchesSpools(text), text).toBe(true)
  })
})

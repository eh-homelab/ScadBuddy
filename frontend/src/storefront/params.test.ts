import { describe, expect, it } from 'vitest'
import { KEYCHAIN } from '../mocks/features/storefrontPublic'
import { keepOffered, parseNumber } from './params'

describe('parseNumber', () => {
  it.each([
    ['12', 12],
    ['-3.5', -3.5],
    [' 0.25 ', 0.25],
    ['.5', 0.5],
    ['7.', 7],
  ])('reads %s as a decimal', (text, value) => {
    expect(parseNumber(text)).toBe(value)
  })

  it.each(['0x10', '1e3', 'Infinity', '1,5', '--1', '+', '-', '1.2.3', ''])('refuses %s', (text) => {
    expect(parseNumber(text)).toBeNaN()
  })
})

describe('keepOffered', () => {
  it('keeps the values the latest listing still offers', () => {
    const latest = {
      ...KEYCHAIN,
      params: {
        name: { kind: 'text' as const, label: 'Name', group: '', colour: false, max_length: 3 },
        base_colour: KEYCHAIN.params['base_colour']!,
      },
    }
    expect(keepOffered(latest, { name: 'Ada', size: 50, base_colour: '#E8532F' })).toEqual({ name: 'Ada', base_colour: '#E8532F' })
    expect(keepOffered(latest, { name: 'Adalind' })).toEqual({})
  })
})

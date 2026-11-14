import { describe, expect, it } from 'vitest'
import { KEYCHAIN } from '../mocks/features/storefrontPublic'
import { keepOffered, parseNumber, shownLabel, valueText } from './params'

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

describe('valueText', () => {
  it('says yes or no for a switch, and shows any other value as it is', () => {
    expect(valueText(true)).toBe('Yes')
    expect(valueText(false)).toBe('No')
    expect(valueText(12.5)).toBe('12.5')
    expect(valueText('Ada')).toBe('Ada')
  })
})

// The table of backend/tests/orders/storefront/test_inputs.py
// `test_a_public_label_leaves_out_the_extruder_note`: the preview and the public page agree.
describe('shownLabel (#6137)', () => {
  it.each([
    ['Letter colour (extruder 2)', 'Letter colour'],
    ['Base and border colour (Extruder 1)', 'Base and border colour'],
    ['Colour (extruder 1) of the rim', 'Colour of the rim'],
    ['Width (mm)', 'Width (mm)'],
    ['(extruder 1)', '(extruder 1)'],
    ['Colour (ext. 1)', 'Colour (ext. 1)'],
    ['Colour (AMS slot 2)', 'Colour (AMS slot 2)'],
    ['Rim(extruder 2)colour', 'Rim colour'],
    ['Colour (extruder \uff12)', 'Colour (extruder \uff12)'],
  ])('shows %j as %j', (caption, shown) => {
    expect(shownLabel('colour', null, caption)).toBe(shown)
  })

  it('takes the listing’s own label, else the name', () => {
    expect(shownLabel('colour', 'Letters', 'Letter colour (extruder 2)')).toBe('Letters')
    expect(shownLabel('colour', null, null)).toBe('colour')
  })
})

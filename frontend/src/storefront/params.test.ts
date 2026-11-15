import { describe, expect, it } from 'vitest'
import { CHARM, KEYCHAIN } from '../mocks/features/storefrontPublic'
import { startColour, filamentFinish, keepOffered, paramFilaments, parseNumber, shownLabel, swatchBackground, valueText } from './params'

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

describe('colour choices (#6152)', () => {
  const blue = KEYCHAIN.colours[0]!
  const orange = KEYCHAIN.colours[1]!

  it('never offers a support material', () => {
    const pva = { id: 'f-pva', colour: '#1B6CA8', colour_name: 'Natural', material: 'PVA' }
    const param = KEYCHAIN.params.base_colour!
    expect(paramFilaments(param, [...KEYCHAIN.colours, pva]).map((f) => f.id)).toEqual([blue.id, orange.id])
  })
})

describe('filament names and finishes (#6152)', () => {
  const f = (id: string, colour: string, colour_name: string | null, extra: { material?: string; subtype?: string | null; brand?: string | null } = {}) => ({
    id,
    colour,
    colour_name,
    material: extra.material ?? 'PLA',
    subtype: extra.subtype ?? null,
    brand: extra.brand ?? null,
  })

  it('draws translucent, silk and multi-colour filaments apart from a solid one', () => {
    expect(filamentFinish(f('a', '#FFFFFF', 'Insignia Clear', { material: 'PETG' }))).toBe('translucent')
    expect(filamentFinish(f('b', '#C0C0C0', 'Silver', { subtype: 'Silk' }))).toBe('silk')
    expect(filamentFinish(f('c', '#FFFFFF', 'Fluorescent Rainbow'))).toBe('multi')
    expect(filamentFinish(f('d', '#000000', 'Velvet Eclipse', { subtype: 'Tri Color' }))).toBe('multi')
    expect(filamentFinish(f('e', '#FFFFFF', 'Jade White'))).toBe('solid')
    expect(swatchBackground(f('e', '#FFFFFF', 'Jade White'))).toBe('#FFFFFF')
    expect(swatchBackground(f('c', '#FFFFFF', 'Fluorescent Rainbow'))).not.toBe('#FFFFFF')
  })
})

describe('start colours (#6144)', () => {
  it('starts a colour at what an unpicked render takes, never at its first choice', () => {
    const body = CHARM.params.body_colour!
    expect(startColour({ ...body, default: '#E8532F' })).toBe('#E8532F')
    // No default the page may show (its filament is gone): no start colour.
    expect(startColour({ ...body, default: null })).toBeUndefined()
  })
})

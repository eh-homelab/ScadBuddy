import { describe, expect, it } from 'vitest'
import { CHARM, KEYCHAIN } from '../mocks/features/storefrontPublic'
import { oneFilamentPerColour, settleMaterial, startMaterial, filamentFinish, filamentLabels, keepOffered, keepPicks, nearestFilament, paramFilaments, parseNumber, shownLabel, swatchBackground, valueText } from './params'

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

describe('picks (#6152)', () => {
  const blue = KEYCHAIN.colours[0]!
  const orange = KEYCHAIN.colours[1]!

  it('finds the nearest colour by RGB distance', () => {
    expect(nearestFilament('#E0602A', KEYCHAIN.colours)).toBe(orange)
    expect(nearestFilament('#102080', KEYCHAIN.colours)).toBe(blue)
    expect(nearestFilament('#102080', [])).toBeUndefined()
  })

  it('keeps only a pick of an offered colour parameter whose colour is the value', () => {
    const values = { base_colour: '#e8532f', name: 'Ada' }
    expect(keepPicks(KEYCHAIN, values, { base_colour: orange.id })).toEqual({ base_colour: orange.id })
    expect(keepPicks(KEYCHAIN, values, { base_colour: blue.id, name: orange.id, gone: blue.id })).toEqual({})
  })

  it('never offers a support material', () => {
    const pva = { id: 'f-pva', colour: '#1B6CA8', colour_name: 'Natural', material: 'PVA' }
    const param = KEYCHAIN.params.base_colour!
    expect(paramFilaments(param, [...KEYCHAIN.colours, pva]).map((f) => f.id)).toEqual([blue.id, orange.id])
    expect(paramFilaments(param, KEYCHAIN.colours, 'PETG')).toEqual([])
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

  it('names the finish, then the brand, then the hex to tell filaments apart', () => {
    const labels = filamentLabels([
      f('a', '#0047BB', 'Blue', { subtype: 'Matte' }),
      f('b', '#1B6CA8', 'Blue', { subtype: 'Silk' }),
      f('c', '#0000FF', 'Blue', { brand: 'Inland' }),
      f('d', '#0011FF', 'Blue', { brand: 'Bambu' }),
      f('e', '#000000', 'Black'),
      f('g', '#111111', 'Black'),
      f('h', '#FFFFFF', 'Translucent Clear', { subtype: 'Translucent', material: 'PETG' }),
    ])
    expect([...labels.values()]).toEqual([
      'Blue · Matte',
      'Blue · Silk',
      'Blue (Inland)',
      'Blue (Bambu)',
      'Black #000000',
      'Black #111111',
      'Translucent Clear',
    ])
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

describe('one material (#6152)', () => {
  it('starts at the material that has every start colour', () => {
    expect(startMaterial(CHARM)).toBe('PLA')
    const tealFirst = {
      ...CHARM,
      params: Object.fromEntries(
        Object.entries(CHARM.params).map(([name, param]) => [name, param.kind === 'choice' ? { ...param, choices: ['#1C8C8C', ...param.choices] } : param]),
      ),
    }
    expect(startMaterial(tealFirst)).toBe('PETG')
  })

  it('sets every colour, set or not, to a filament of the material', () => {
    const settled = settleMaterial(CHARM, { body_colour: '#E8532F' }, { body_colour: 'f-orange' }, 'PETG')
    expect(settled).toEqual({
      values: { body_colour: '#C0502A', rim_colour: '#1C8C8C' },
      picks: { body_colour: 'f-petg-rust', rim_colour: 'f-petg-teal' },
      moved: true,
    })
    // A pick already of the material stays, and the unset rim says nothing moved.
    expect(settleMaterial(CHARM, { body_colour: '#C0502A' }, { body_colour: 'f-petg-rust' }, 'PETG').moved).toBe(false)
    // Any other listing is left as it is.
    expect(settleMaterial({ ...CHARM, one_material: false }, {}, {}, 'PETG')).toEqual({ values: {}, picks: {}, moved: false })
  })
})

describe('oneFilamentPerColour (#6152)', () => {
  const cheap = { id: 'f-cheap', colour: '#C0392B', colour_name: 'Cheap red', material: 'PLA' }
  const dear = { id: 'f-dear', colour: '#c0392b', colour_name: 'Dear red', material: 'PLA' }
  const choice = { kind: 'choice' as const, group: '', colour: true, choices: ['#C0392B', '#1B6CA8'] }
  const listing = {
    ...CHARM,
    one_material: false,
    params: { body_colour: { ...choice, label: 'Body colour' }, rim_colour: { ...choice, label: 'Rim colour' } },
    colours: [cheap, dear, CHARM.colours[0]!],
  }

  it('moves an earlier pick of the colour to the filament just picked, and names it', () => {
    expect(oneFilamentPerColour(listing, { body_colour: 'f-cheap', rim_colour: 'f-dear' }, 'rim_colour')).toEqual({
      picks: { body_colour: 'f-dear', rim_colour: 'f-dear' },
      moved: ['body_colour'],
    })
  })

  it('leaves picks of other colours, and of one filament, as they are', () => {
    const picks = { body_colour: 'f-cheap', rim_colour: CHARM.colours[0]!.id }
    expect(oneFilamentPerColour(listing, picks, 'rim_colour')).toEqual({ picks, moved: [] })
    expect(oneFilamentPerColour(listing, { body_colour: 'f-dear', rim_colour: 'f-dear' })).toEqual({
      picks: { body_colour: 'f-dear', rim_colour: 'f-dear' },
      moved: [],
    })
  })

  it('gives each colour one filament when a material settles (#6152 review)', () => {
    const one = { ...listing, one_material: true }
    // The body keeps its pick; the rim, unset, starts at the same red, so it takes that filament.
    const settled = settleMaterial(one, { body_colour: '#c0392b' }, { body_colour: 'f-dear' }, 'PLA')
    expect(settled.picks).toEqual({ body_colour: 'f-dear', rim_colour: 'f-dear' })
  })
})

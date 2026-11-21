import { describe, expect, it } from 'vitest'
import type { Filament, Listing } from './api/client'
import { swatchStyle } from '../lib/filamentSwatch'
import { clashText, colourPlan, colourSwatches } from './colours'

const BLUE = '#1B6CA8'
const ORANGE = '#E8532F'
const TEAL = '#1C8C8C'
const RUST = '#C0502A'
const BLACK = '#101010'

const f = (id: string, colour: string, colour_name: string | null, material: string, extra: Partial<Filament> = {}): Filament => ({ id, colour, colour_name, material, ...extra })

const CHOICES = [BLUE, ORANGE, TEAL, RUST, BLACK]
const part = (label: string, start: string) => ({ kind: 'choice' as const, label, group: '', colour: true, choices: CHOICES, default: start })

/** #6258: blue and orange only in PLA, teal and rust only in PETG, black in both. */
const LISTING: Listing = {
  id: 'l',
  title: 'Charm',
  description: '',
  params: { body_colour: part('Body colour', BLUE), rim_colour: part('Rim colour', BLUE), loop_colour: part('Loop colour', BLACK) },
  colours: [
    f('b', BLUE, 'Ocean blue', 'PLA'),
    f('o', ORANGE, 'Signal orange', 'PLA'),
    f('t', TEAL, 'Teal', 'PETG'),
    f('r', RUST, 'Rust', 'PETG'),
    f('k1', BLACK, 'Black', 'PLA'),
    f('k2', BLACK, 'Black', 'PETG'),
    f('s', '#FFFFFF', 'Natural', 'PVA'),
  ],
  media: [],
  // The shop's order: PETG first. The defaults (blue) fit PLA only, so PLA wins until a pick.
  materials: ['PETG', 'PLA'],
  updated_at: '2026-11-15T00:00:00Z',
  available: true,
}

describe('colour swatches (#6258)', () => {
  it('makes one swatch per colour across materials, named by the colour, never a support', () => {
    const swatches = colourSwatches(LISTING.params.body_colour!, LISTING.colours)
    expect(swatches.map((swatch) => swatch.name)).toEqual(['Ocean blue', 'Signal orange', 'Teal', 'Rust', 'Black'])
  })

  it('never names a material, even inside a finish', () => {
    const filaments = [f('a', '#000000', 'Black', 'PLA', { subtype: 'PLA Matte' }), f('b', '#111111', 'Black PETG', 'PETG', { subtype: 'PETG HF' })]
    const param = { kind: 'choice' as const, label: 'Body', group: '', colour: true, choices: ['#000000', '#111111'] }
    expect(colourSwatches(param, filaments).map((swatch) => swatch.name)).toEqual(['Black · Matte', 'Black · HF'])
  })

  it('names the finish, then the brand, then the hex to tell colours apart (#6152)', () => {
    const filaments = [
      f('a', '#0047BB', 'Blue', 'PLA', { subtype: 'Matte' }),
      f('b', '#1B6CA8', 'Blue', 'PLA', { subtype: 'Silk' }),
      f('c', '#0000FF', 'Blue', 'PLA', { brand: 'Inland' }),
      f('d', '#0011FF', 'Blue', 'PLA', { brand: 'Bambu' }),
      f('e', '#000000', 'Black', 'PLA'),
      f('g', '#111111', 'Black', 'PETG'),
      f('h', '#FFFFFF', 'Translucent Clear', 'PETG', { subtype: 'Translucent' }),
    ]
    const param = { kind: 'choice' as const, label: 'Body', group: '', colour: true, choices: filaments.map((each) => each.colour) }
    expect(colourSwatches(param, filaments).map((swatch) => swatch.name)).toEqual([
      'Blue · Matte',
      'Blue · Silk',
      'Blue (Inland)',
      'Blue (Bambu)',
      'Black #000000',
      'Black #111111',
      'Translucent Clear',
    ])
  })
})

describe('swatches drawn from the filament (#6215)', () => {
  const body = (filaments: readonly Filament[]) => ({ kind: 'choice' as const, label: 'Body', group: '', colour: true, choices: [...new Set(filaments.map((each) => each.colour))] })
  const swatches = (filaments: readonly Filament[], materials?: readonly string[]) => colourSwatches(body(filaments), filaments, materials)

  it('draws a sparkle, wood or gradient filament with its effect', () => {
    const filaments = [
      f('s', '#202020', 'Night', 'PLA', { effect: 'sparkle' }),
      f('w', '#8B5A2B', 'Oak', 'PLA', { effect: 'wood' }),
      f('g', '#FF0000', 'Sunset', 'PLA', { effect: 'gradient', extra_colours: ['#FF0000', '#FFCC00'] }),
    ]
    const drawn = swatches(filaments)
    for (const [index, filament] of filaments.entries()) expect(drawn[index]!.style).toEqual(swatchStyle({ ...filament, alpha: 255 }))
    expect(drawn[0]!.style.backgroundImage).toContain('radial-gradient')
    expect(drawn[1]!.style.backgroundImage).toContain('repeating-linear-gradient(90deg')
  })

  it('draws the stops of a multi-colour filament', () => {
    const [swatch] = swatches([f('m', '#FF0000', 'Rainbow', 'PLA', { effect: 'multicolor', extra_colours: ['#FF0000', '#00FF00', '#0000FF'] })])
    expect(swatch!.style.backgroundImage).toMatch(/conic-gradient\(from 0deg, #FF0000 .*#00FF00 .*#0000FF /)
  })

  it('tells a translucent filament from its opaque twin, by swatch and by label', () => {
    const [clear] = swatches([f('c', '#FFFFFF', null, 'PETG', { alpha: 128 })])
    const [solid] = swatches([f('o', '#FFFFFF', null, 'PETG', { alpha: 255 })])
    expect(clear!.name).toBe('#FFFFFF80')
    expect(solid!.name).toBe('#FFFFFF')
    expect(clear!.style.backgroundImage).toContain('#FFFFFF80')
    expect(clear!.style).not.toEqual(solid!.style)
    // A null alpha reads as opaque, never as alpha 00.
    const [unset] = swatches([f('n', '#FFFFFF', null, 'PETG', { alpha: null })])
    expect(unset!.name).toBe('#FFFFFF')
    expect(unset!.style).toEqual(solid!.style)
  })

  it('names the finish from the effect when it has one, else from the subtype', () => {
    const filaments = [
      f('a', '#C0C0C0', 'Silver', 'PLA', { effect: 'silk', subtype: 'Basic' }),
      f('b', '#FFFFFF', 'White', 'PETG', { effect: 'translucent', alpha: 100 }),
      f('c', '#000000', 'Black', 'PLA', { subtype: 'Matte' }),
      f('d', '#FF00FF', 'Pink', 'PLA', { effect: 'dual-color', extra_colours: ['#FF00FF', '#00FFFF'] }),
    ]
    expect(swatches(filaments).map((swatch) => swatch.name)).toEqual(['Silver · Silk', 'White · Translucent', 'Black · Matte', 'Pink · Dual-color'])
  })

  it('takes the filament of the first allowed material when two share a colour', () => {
    const filaments = [f('k1', BLACK, 'Black', 'PLA', { effect: 'sparkle' }), f('k2', BLACK, 'Black', 'PETG', { effect: 'matte' })]
    const [petg] = swatches(filaments, ['PETG', 'PLA'])
    expect(petg!.filament.id).toBe('k2')
    expect(petg!.name).toBe('Black · Matte')
    expect(petg!.style).toEqual(swatchStyle({ colour: BLACK, alpha: 255, effect: 'matte' }))
    // No list (a back-office preview of a draft): the colours' own order.
    expect(swatches(filaments)[0]!.filament.id).toBe('k1')
  })

  it('never names a material, from an effect, a subtype or a colour name', () => {
    const filaments = [
      f('a', '#C0C0C0', 'PLA Silk Silver', 'PLA', { effect: 'silk', subtype: 'PLA Silk' }),
      f('b', '#FFFFFF', 'PETG Clear', 'PETG', { effect: 'translucent', alpha: 90, subtype: 'PETG Translucent' }),
    ]
    for (const swatch of swatches(filaments, ['PLA', 'PETG'])) expect(swatch.name).not.toMatch(/PLA|PETG/)
  })
})

describe('clash text (#6258)', () => {
  it('names the clashing parts by their labels, and leaves out a part it does not know', () => {
    expect(clashText(LISTING, ['body_colour', 'rim_colour'])).toBe(
      "The body colour and the rim colour can't go together. Each print is made from one kind of plastic, and a few colours only come in one kind. Change one of them.",
    )
    expect(clashText(LISTING, ['colours.0'])).toBe(
      "Some colours can't go together. Each print is made from one kind of plastic, and a few colours only come in one kind. Change one of them.",
    )
  })
})

describe('colour plan (#6258)', () => {
  it('shows the defaults while the visitor sets no colour', () => {
    expect(colourPlan(LISTING, {})).toEqual({ material: 'pla', shown: { body_colour: BLUE, rim_colour: BLUE, loop_colour: BLACK } })
  })

  it('moves an untouched part to its closest PETG colour once a PETG-only colour is picked, and keeps a default PETG offers', () => {
    const plan = colourPlan(LISTING, { body_colour: RUST })
    expect(plan.material).toBe('petg')
    expect(plan.shown).toEqual({ body_colour: RUST, rim_colour: TEAL, loop_colour: BLACK })
  })

  it('still moves untouched parts in the back office’s preview, which sends no materials', () => {
    expect(colourPlan({ ...LISTING, materials: [] }, { body_colour: RUST }).shown.rim_colour).toBe(TEAL)
  })

  it('never moves a colour the visitor set', () => {
    // Orange is PLA only and teal PETG only: no material fits. The shop answers 422
    // `listing_colours_clash`; the page keeps both picks and the untouched default.
    expect(colourPlan(LISTING, { body_colour: ORANGE, rim_colour: TEAL })).toEqual({
      material: null,
      shown: { body_colour: ORANGE, rim_colour: TEAL, loop_colour: BLACK },
    })
  })

  it('weighs a filament’s alpha: a clear default goes to a clear colour, not to an opaque one of near its RGB', () => {
    const choices = [TEAL, '#FFFFFF', '#F8F8F8', '#E0E0E0']
    const listing: Listing = {
      ...LISTING,
      params: { body_colour: { ...part('Body colour', TEAL), choices }, rim_colour: { ...part('Rim colour', '#FFFFFF'), choices } },
      colours: [
        f('t', TEAL, 'Teal', 'PETG'),
        f('c', '#FFFFFF', 'Clear', 'PETG', { alpha: 0 }),
        f('w', '#F8F8F8', 'Snow', 'PLA'),
        f('i', '#E0E0E0', 'Frost', 'PLA', { alpha: 0 }),
      ],
    }
    // Snow is PLA only: the print is PLA, and the clear rim goes to the clear Frost.
    expect(colourPlan(listing, { body_colour: '#F8F8F8' }).shown.rim_colour).toBe('#E0E0E0')
  })
})

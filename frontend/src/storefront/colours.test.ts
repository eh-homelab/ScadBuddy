import { describe, expect, it } from 'vitest'
import type { Filament, Listing } from './api/client'
import { clashText, colourPlan, colourSwatches } from './colours'

const BLUE = '#1B6CA8'
const ORANGE = '#E8532F'
const TEAL = '#1C8C8C'
const RUST = '#C0502A'
const BLACK = '#101010'

const f = (id: string, colour: string, colour_name: string, material: string, extra: Partial<Filament> = {}): Filament => ({ id, colour, colour_name, material, ...extra })

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

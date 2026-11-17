import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, MediaView } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { colourProblems, defaultFits, defaultsFor, emptyDraft, fittedDefault, fixColour, isSupport, listingInput, listingRefusal, nearestFilaments, previewListing, savedKey, sellMedia, titleColours, withoutWords, type ListingDraft } from './listingDraft'

function filament(id: string, colour: string, colour_name: string): Filament {
  return {
    id,
    material: 'PLA',
    subtype: null,
    brand: null,
    colour_name,
    colour,
    alpha: 255,
    extra_colours: [],
    effect: null,
    colour_source: 'manual',
    spool_linked: false,
    cost_per_kg: '20',
    density_g_cm3: '1.24',
    density_profile: null,
    archived_at: null,
    created_at: '2026-11-01T00:00:00Z',
    updated_at: '2026-11-01T00:00:00Z',
  }
}

const BLUE = filament('blue', '#1b6ca8', 'Ocean')
const NAVY = filament('navy', '#1F2A44', 'Navy')
const CORAL = filament('coral', '#E8532F', 'Coral')
const YELLOW = filament('yellow', '#FFC20E', 'Sunflower Yellow')

function media(id: string, kind: 'image' | 'video', content_type: string, missing = false): MediaView {
  return { id, file: `${id}.bin`, kind, caption: '', poster: null, missing, readonly: false, content_type, size: 1, source_commit: null, source_revision_id: null }
}

describe('defaultsFor (#6131): the smart defaults', () => {
  it('offers free text and every colour, and locks the geometry at the values given', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [YELLOW, BLUE, CORAL], { text_size: 20, keyring_hole: false })
    expect(params).toEqual({
      name: { kind: 'text', max_length: 20 },
      body_color: { kind: 'choice', choices: [] },
      text_color: { kind: 'choice', choices: [] },
    })
    expect(fixed).toEqual({
      font: 'Liberation Sans:style=Bold',
      text_size: 20,
      text_depth: 1.6,
      thickness: 5.2,
      padding: 6,
      corner_radius: 4,
      keyring_hole: false,
      hole_side: 'left',
    })
    // Every priced filament of the design's material, those the colours name first.
    expect(colours).toEqual(['blue', 'coral', 'yellow'])
  })

  it('offers a colour the person changed too: locking one is the person’s choice', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [BLUE, NAVY, CORAL], { body_color: '#1f2a44' })
    expect(params['body_color']).toEqual({ kind: 'choice', choices: [] })
    expect(fixed['body_color']).toBeUndefined()
    expect(colours).toEqual(['navy', 'coral', 'blue'])
  })

  it('offers only the design’s material, and only filaments with a density', () => {
    const petg = { ...filament('petg', '#FFFFFF', 'White'), material: 'PETG' }
    const unpriced = { ...filament('unpriced', '#00FF00', 'Green'), density_g_cm3: null }
    expect(defaultsFor(keychainSchema, [petg, BLUE, unpriced]).colours).toEqual(['blue'])
  })

  it('locks a colour at its value only when no filament can be offered', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [])
    expect(params['body_color']).toBeUndefined()
    expect(fixed['body_color']).toBe('#1B6CA8')
    expect(colours).toEqual([])
  })

  it('agrees with the shared vectors', () => {
    const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../../../test-fixtures/storefront/sell-draft-vectors.json`, 'utf8')) as {
      name: string
      schema: CustomizerSchema
      filaments: Filament[]
      values: Record<string, string | number | boolean>
      expected: { params: unknown; fixed: unknown; colours: string[] }
      /** The params the save sends for those defaults (`listingInput`), where the vector pins them. */
      input?: unknown
    }[]
    expect(vectors.length).toBeGreaterThan(0)
    for (const vector of vectors) {
      const defaults = defaultsFor(vector.schema, vector.filaments, vector.values)
      expect(defaults, vector.name).toEqual(vector.expected)
      if (vector.input !== undefined) {
        const draft = { ...emptyDraft('m', 'c'.repeat(40)), ...defaults }
        expect(listingInput(draft, vector.schema, vector.filaments).params, vector.name).toEqual(vector.input)
      }
    }
  })
})

describe('the value an offered parameter starts at (#6144)', () => {
  const param = (name: string) => keychainSchema.parameters!.find((p) => p.name === name)!

  it('starts a colour whose template default no colour on offer has at the first colour on offer', () => {
    const { params } = defaultsFor(keychainSchema, [YELLOW, BLUE])
    expect(params['body_color']).toEqual({ kind: 'choice', choices: [] })
    expect(params['text_color']).toEqual({ kind: 'choice', choices: [], default: '#1b6ca8' })
  })

  it('starts it at the colour the person was looking at, when one on offer has it', () => {
    const { params } = defaultsFor(keychainSchema, [YELLOW, BLUE], { text_color: '#ffc20e' })
    expect(params['text_color']).toEqual({ kind: 'choice', choices: [], default: '#ffc20e' })
    // The template's default on offer stays the start: no listing default.
    expect(defaultsFor(keychainSchema, [YELLOW, BLUE, CORAL], { text_color: '#ffc20e' }).params['text_color']).toEqual({ kind: 'choice', choices: [] })
  })

  it('moves a number to the nearest value on the step when the range leaves the template’s out', () => {
    const size = param('text_size') // 6 to 28, step 0.5, starts at 14
    expect(fittedDefault(size, { kind: 'number', min: 16.5, max: 28, step: 1 }, [])).toEqual({ kind: 'number', min: 16.5, max: 28, step: 1, default: 16.5 })
    expect(fittedDefault(size, { kind: 'number', min: 6, max: 12, step: 0.5 }, [])).toEqual({ kind: 'number', min: 6, max: 12, step: 0.5, default: 12 })
    // 14 is on a step of 4 from 6, not on one of 5: the nearest step below it, or the value given when on one.
    expect(fittedDefault(size, { kind: 'number', min: 6, max: 28, step: 4 }, [])).toEqual({ kind: 'number', min: 6, max: 28, step: 4 })
    expect(fittedDefault(size, { kind: 'number', min: 6, max: 28, step: 5 }, [])).toEqual({ kind: 'number', min: 6, max: 28, step: 5, default: 11 })
    expect(fittedDefault(size, { kind: 'number', min: 6, max: 28, step: 5 }, [], 16)).toEqual({ kind: 'number', min: 6, max: 28, step: 5, default: 16 })
  })

  it('keeps the listing’s own default while it fits, and drops one that no longer does for the template’s', () => {
    const size = param('text_size')
    expect(fittedDefault(size, { kind: 'number', min: 10, max: 20, step: 0.5, default: 18 }, [])).toEqual({ kind: 'number', min: 10, max: 20, step: 0.5, default: 18 })
    expect(fittedDefault(size, { kind: 'number', min: 10, max: 16, step: 0.5, default: 18 }, [])).toEqual({ kind: 'number', min: 10, max: 16, step: 0.5 })
  })

  it('takes the locked value when it fits and the template’s does not', () => {
    const side = param('hole_side')
    const choices = (side.options ?? []).map((option) => option.value).filter((value) => value !== side.initial)
    expect(fittedDefault(side, { kind: 'choice', choices }, [], choices[1])).toEqual({ kind: 'choice', choices, default: choices[1] })
    expect(fittedDefault(side, { kind: 'choice', choices }, [], side.initial)).toEqual({ kind: 'choice', choices, default: choices[0] })
  })

  it('checks a default as publish does', () => {
    const size = param('text_size')
    expect(defaultFits(size, { kind: 'number', min: 6, max: 28, step: 0.5 }, 14.5, [])).toBe(true)
    expect(defaultFits(size, { kind: 'number', min: 6, max: 28, step: 0.5 }, 14.25, [])).toBe(false)
    expect(defaultFits(param('name'), { kind: 'text', max_length: 3 }, 'Reagan', [])).toBe(false)
    expect(defaultFits(param('body_color'), { kind: 'choice', choices: [] }, '#1B6CA8', ['#1b6ca8'])).toBe(true)
  })
})

describe('listingRefusal (#6144, #6152): a refusal about one parameter', () => {
  const type = (slug: string) => `https://scadbuddy.dev/problems/storefront/${slug}`

  it('names the parameter of a default, colour or support refusal', () => {
    expect(listingRefusal({ type: type('listing_default_invalid'), title: 't', detail: 'd', name: 'text_size' })).toEqual({ kind: 'param', code: 'default_invalid', name: 'text_size', detail: 'd' })
    expect(listingRefusal({ type: type('listing_colour_unknown'), title: 't', name: 'body_color' })).toEqual({ kind: 'param', code: 'colour_unknown', name: 'body_color', detail: 't' })
    expect(listingRefusal({ type: type('listing_support_offered'), title: 't', detail: 'd', name: 'support_colour' })).toMatchObject({ kind: 'param', code: 'support_offered' })
  })

  it('reads a colour refusal that names no parameter as a filament gone from the price book', () => {
    expect(listingRefusal({ type: type('listing_colour_unknown'), title: 't', detail: 'd' })).toEqual({ kind: 'filament_unknown', detail: 'd' })
  })
})

describe('colourProblems (#6131)', () => {
  function draft(over: Partial<ListingDraft>): ListingDraft {
    return { ...emptyDraft('name-keychain', 'c'.repeat(40)), title: 'Tag', ...over }
  }

  it('names each colour that is not a listing filament’s, offered or locked', () => {
    // The smart defaults start an offered colour at one on offer (#6144); a person who unticks it makes it a problem again.
    expect(colourProblems(draft({ ...defaultsFor(keychainSchema, [CORAL]) }), keychainSchema, [CORAL])).toEqual([])
    const problems = colourProblems(draft({ params: { body_color: { kind: 'choice', choices: [] } }, fixed: { text_color: '#E8532F' }, colours: ['coral'] }), keychainSchema, [CORAL])
    expect(problems).toEqual([{ name: 'body_color', label: 'Plate', colour: '#1B6CA8' }])
    expect(colourProblems(draft({ fixed: { body_color: '#1B6CA8', text_color: '#E8532F' }, colours: ['coral'] }), keychainSchema, [CORAL])).toEqual([{ name: 'body_color', label: 'Plate', colour: '#1B6CA8' }])
  })

  it('names an offered colour whose default is not a listing filament’s', () => {
    const problems = colourProblems(draft({ params: { text_color: { kind: 'choice', choices: [] } }, colours: ['blue'] }), keychainSchema, [BLUE, CORAL])
    expect(problems.map((p) => p.name)).toEqual(['text_color'])
  })

  it('checks an offered colour’s listing default, not the template’s (#6144)', () => {
    // The template's #E8532F has no listing filament, but the listing starts the colour at blue.
    const offered = draft({ params: { text_color: { kind: 'choice', choices: [], default: '#1B6CA8' } }, fixed: { body_color: '#1b6ca8' }, colours: ['blue'] })
    expect(colourProblems(offered, keychainSchema, [BLUE, CORAL])).toEqual([])
    const unlisted = draft({ params: { text_color: { kind: 'choice', choices: [], default: '#E8532F' } }, fixed: { body_color: '#1b6ca8' }, colours: ['blue'] })
    expect(colourProblems(unlisted, keychainSchema, [BLUE, CORAL])).toEqual([{ name: 'text_color', label: 'Text', colour: '#E8532F' }])
  })

  it('finds nothing once every colour has its filament', () => {
    expect(colourProblems(draft({ ...defaultsFor(keychainSchema, [BLUE, CORAL]) }), keychainSchema, [BLUE, CORAL])).toEqual([])
  })

  it('fixes a colour with the filament chosen: the filament is added and the colour locked at it', () => {
    const start = draft({ ...defaultsFor(keychainSchema, [CORAL]) })
    const fixed = fixColour(start, 'body_color', NAVY)
    expect(fixed.colours).toEqual(['coral', 'navy'])
    expect(fixed.fixed['body_color']).toBe('#1F2A44')
    expect(fixed.params['body_color']).toBeUndefined()
    expect(colourProblems(fixed, keychainSchema, [CORAL, NAVY])).toEqual([])
  })

  it('lists the filaments nearest the colour first', () => {
    expect(nearestFilaments('#1B6CA8', [YELLOW, CORAL, NAVY, BLUE]).map((f) => f.id)).toEqual(['blue', 'navy', 'coral', 'yellow'])
  })
})

describe('a colour the customer chooses stays out of the title (#6131)', () => {
  const offered: ListingDraft = { ...emptyDraft('name-keychain', 'c'.repeat(40)), title: 'Owl Pencil Cup – Navy', params: { body_color: { kind: 'choice', choices: [] } }, colours: ['navy', 'coral'] }

  it('names the colours on offer that the title holds, whole words and case aside', () => {
    expect(titleColours(offered, keychainSchema, [NAVY, CORAL])).toEqual(['Navy'])
    expect(titleColours({ ...offered, title: 'Navyish owl in coral' }, keychainSchema, [NAVY, CORAL])).toEqual(['Coral'])
  })

  it('leaves a locked colour in the title: that product is sold in it', () => {
    const locked: ListingDraft = { ...offered, params: {}, fixed: { body_color: '#1F2A44' } }
    expect(titleColours(locked, keychainSchema, [NAVY, CORAL])).toEqual([])
  })

  it('takes the colours out with the separators they leave', () => {
    expect(withoutWords('Owl Pencil Cup – Navy', ['Navy'])).toBe('Owl Pencil Cup')
    expect(withoutWords('Navy Owl (Coral)', ['Navy', 'Coral'])).toBe('Owl')
    expect(withoutWords('Owl, navy, large', ['Navy'])).toBe('Owl, large')
    // #6140 review: a slash, a colon, a bar or a middle dot is a separator too.
    expect(withoutWords('Owl Cup / Navy', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Owl Cup: Navy', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Navy | Owl Cup', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Owl Cup · Navy · Large', ['Navy'])).toBe('Owl Cup · Large')
    // One that joins two words stays.
    expect(withoutWords('Owl Cup 1/2 Navy', ['Navy'])).toBe('Owl Cup 1/2')
  })
})

describe('support settings stay internal (#6131, the owner 2026-11-13)', () => {
  const schema: CustomizerSchema = {
    ...keychainSchema,
    groups: ['Colours', 'Supports'],
    parameters: [
      { name: 'body', type: 'color', group: 'Colours', caption: 'Body', initial: '#1b6ca8' },
      { name: 'supports', type: 'boolean', group: 'Supports', caption: 'Use supports', initial: true },
      { name: 'support_colour', type: 'color', group: 'Supports', caption: 'Support material', initial: '#E8532F' },
    ],
  }
  /** A support material: not the design's PLA, so its colour is the support setting's alone. */
  const SUPPORT = { ...CORAL, id: 'coral-pva', material: 'PVA' }

  it('saves no support setting offered, and no support material’s colour on offer', () => {
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: 'Tag',
      params: { body: { kind: 'choice', choices: [] }, supports: { kind: 'choice', choices: [true, false] } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id, SUPPORT.id],
    }
    const body = listingInput(draft, schema, [BLUE, SUPPORT])
    expect(body.params).toEqual({ body: { kind: 'choice', choices: ['#1b6ca8'] } })
    expect(body.fixed).toEqual({ support_colour: '#E8532F', supports: true })
    // The support colour's filament stays on the listing: publish checks a locked colour
    // against the listing's filaments (`checked_fixed`, `listing_colour_unknown`).
    expect(body.colours).toEqual([BLUE.id, SUPPORT.id])
  })

  it('leaves a support material’s colour in the title: no customer can choose it (#6140 review)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: `Tag ${SUPPORT.colour_name} ${BLUE.colour_name}`,
      params: { body: { kind: 'choice', choices: [] } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id, SUPPORT.id],
    }
    expect(titleColours(draft, schema, [BLUE, SUPPORT])).toEqual([BLUE.colour_name])
  })

  it('keeps a filament of the design’s material a choice when a support setting uses its colour too (#6140 review)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: 'Tag',
      params: { body: { kind: 'choice', choices: [] } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id, CORAL.id],
    }
    expect(listingInput(draft, schema, [BLUE, CORAL]).params).toEqual({ body: { kind: 'choice', choices: ['#1b6ca8', '#E8532F'] } })
  })

  it('reads the listing’s own label too, as publish does (#6152, backend `_sets_support`)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: 'Tag',
      params: { body: { kind: 'choice', choices: [], label: 'Interface plate colour' } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id],
    }
    expect(isSupport(schema.parameters![0]!, 'Interface plate colour')).toBe(true)
    const body = listingInput(draft, schema, [BLUE])
    expect(body.params).toEqual({})
    expect(body.fixed).toEqual({ support_colour: '#E8532F', body: '#1b6ca8' })
  })

  it('shows no support material in the preview, as the public listing does (#6152)', () => {
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: 'Tag',
      params: { body: { kind: 'choice', choices: [] } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id, SUPPORT.id],
    }
    expect(previewListing(draft, schema, [BLUE, SUPPORT], [], () => '').colours.map((f) => f.id)).toEqual([BLUE.id])
  })

  it('offers no colour of a support material ticked by hand (#6152)', () => {
    const natural = { ...filament('pva-natural', '#F4F1E8', 'Natural'), material: 'PVA' }
    const draft: ListingDraft = {
      ...emptyDraft('tag', 'c'.repeat(40)),
      title: 'Tag',
      params: { body: { kind: 'choice', choices: [] } },
      fixed: { support_colour: '#E8532F' },
      colours: [BLUE.id, natural.id],
    }
    expect(listingInput(draft, schema, [BLUE, natural]).params).toEqual({ body: { kind: 'choice', choices: ['#1b6ca8'] } })
  })
})

describe('sellMedia (#6131)', () => {
  it('takes the images a customer can see, never a video, a missing file or another type', () => {
    const items = [media('a', 'image', 'image/png'), media('b', 'video', 'video/mp4'), media('c', 'image', 'image/png', true), media('d', 'image', 'application/octet-stream')]
    expect(sellMedia(items)).toEqual(['a'])
  })
})

describe('savedKey (#6290): a null field is an absent one, and nothing else is', () => {
  const body = (params: Record<string, unknown>, fixed: Record<string, unknown> = {}) =>
    savedKey({ slug: 'name-keychain', commit: 'c'.repeat(40), title: 'Tag', params, fixed } as Parameters<typeof savedKey>[0])

  it('reads a bound’s unset label, default and step as null or absent alike', () => {
    expect(body({ size: { kind: 'number', min: 6, max: 28, label: null, default: null, step: null } })).toBe(body({ size: { kind: 'number', min: 6, max: 28 } }))
  })

  it('still sees a default that went from a value to unset, and a fixed value taken away', () => {
    expect(body({ size: { kind: 'number', min: 6, max: 28, default: 14 } })).not.toBe(body({ size: { kind: 'number', min: 6, max: 28, default: null } }))
    expect(body({}, { thickness: 5.2 })).not.toBe(body({}, {}))
    expect(body({}, { thickness: 0 })).not.toBe(body({}, {}))
  })
})

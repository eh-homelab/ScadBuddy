import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import type { CustomizerSchema, Filament, MediaView } from '../../api/types'
import { keychainSchema } from '../../mocks/fixtures'
import { colourProblems, defaultsFor, emptyDraft, fixColour, nearestFilaments, sellMedia, titleColours, withoutWords, type ListingDraft } from './listingDraft'

function filament(id: string, colour: string, colour_name: string): Filament {
  return {
    id,
    material: 'PLA',
    subtype: null,
    brand: null,
    colour_name,
    colour,
    cost_per_kg: '20',
    density_g_cm3: null,
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
  it('offers free text and colours, and locks the geometry at the values given', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [BLUE, CORAL, YELLOW], { text_size: 20, keyring_hole: false })
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
    // Each colour's filament, matched case-insensitively, in the schema's order.
    expect(colours).toEqual(['blue', 'coral'])
  })

  it('locks a colour the person changed from the default, so the product sells as seen', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [BLUE, NAVY, CORAL], { body_color: '#1f2a44' })
    expect(params['body_color']).toBeUndefined()
    expect(fixed['body_color']).toBe('#1f2a44')
    expect(params['text_color']).toEqual({ kind: 'choice', choices: [] })
    expect(colours).toEqual(['navy', 'coral'])
  })

  it('locks a colour no filament has, at its value', () => {
    const { params, fixed, colours } = defaultsFor(keychainSchema, [YELLOW])
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
    }[]
    expect(vectors.length).toBeGreaterThan(0)
    for (const vector of vectors) {
      expect(defaultsFor(vector.schema, vector.filaments, vector.values), vector.name).toEqual(vector.expected)
    }
  })
})

describe('colourProblems (#6131)', () => {
  function draft(over: Partial<ListingDraft>): ListingDraft {
    return { ...emptyDraft('name-keychain', 'c'.repeat(40)), title: 'Tag', ...over }
  }

  it('names each colour that is not a listing filament’s, offered or locked', () => {
    const problems = colourProblems(draft({ ...defaultsFor(keychainSchema, [CORAL]) }), keychainSchema, [CORAL])
    expect(problems).toEqual([{ name: 'body_color', label: 'Plate', colour: '#1B6CA8' }])
  })

  it('names an offered colour whose default is not a listing filament’s', () => {
    const problems = colourProblems(draft({ params: { text_color: { kind: 'choice', choices: [] } }, colours: ['blue'] }), keychainSchema, [BLUE, CORAL])
    expect(problems.map((p) => p.name)).toEqual(['text_color'])
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
  })
})

describe('sellMedia (#6131)', () => {
  it('takes the images a customer can see, never a video, a missing file or another type', () => {
    const items = [media('a', 'image', 'image/png'), media('b', 'video', 'video/mp4'), media('c', 'image', 'image/png', true), media('d', 'image', 'application/octet-stream')]
    expect(sellMedia(items)).toEqual(['a'])
  })
})

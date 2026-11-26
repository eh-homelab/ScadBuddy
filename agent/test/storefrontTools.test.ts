import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import type { components } from '../src/api/schema.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { colourProblems, defaultsFor, draftMaterials, fileTitleColours, isSupportMaterial, soldDesign, titleColours, withColourChoices, withoutWords } from '../src/tools/storefrontDraft.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6131 (design 2026-11-13 §6) — the assistant's storefront tools: a draft (`write`) built
// with the Storefront page's smart defaults, and a publish (`outward`, so a person approves).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

type Schema = components['schemas']['RevisionSchema']
type Filament = components['schemas']['Filament']

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    pollIntervalMs: 1,
  }
}

interface Vector {
  name: string
  schema: Schema
  filaments: Filament[]
  values: Record<string, string | number | boolean>
  expected: { params: unknown; fixed: unknown; colours: string[] }
  /** The params a save sends for those defaults, where the vector pins them. */
  input?: unknown
}

/**
 * #6710 — a library-file case: the file as the library's routes answer it, and the body a new
 * listing of it saves, but `materials`, which each side sets its own way.
 */
interface FileVector {
  name: string
  file: {
    id: number
    library: 'bambuddy' | 'native'
    filename: string
    slots: components['schemas']['SlotNeed'][]
    plates: components['schemas']['OutputPlate'][]
  }
  filaments: Filament[]
  expected: Record<string, unknown>
  /** Titles and the colour names in each that a customer can pick (`titleColours` of the new draft). */
  titles: { title: string; colours: string[] }[]
}

/**
 * #5701 — a "Sell on storefront" case of a render's whole inputs (`source`): the smart defaults
 * of its values and, for a pipeline template, its own inputs and their version (`soldDesign`).
 */
interface SourceVector {
  name: string
  schema: Schema
  filaments: Filament[]
  source: Record<string, unknown>
  expected: Record<string, unknown>
}

const shared = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/storefront/sell-draft-vectors.json`, 'utf8')) as (Vector | FileVector | SourceVector)[]
const vectors = shared.filter((vector): vector is Vector => !('file' in vector) && !('source' in vector))
const fileVectors = shared.filter((vector): vector is FileVector => 'file' in vector)
const sourceVectors = shared.filter((vector): vector is SourceVector => 'source' in vector)
const owl = vectors[0] as Vector

const OUTPUT = 'a'.repeat(32)
const COMMIT = 'c'.repeat(40)
const LISTING = 'l1'
/** The owl's colours on offer: its priced PLA filaments, the one its body is first. */
const ALL_COLOURS = ['#1F2A44', '#000000', '#ffffff']

/** The owl's output, model, schema and price book; `onPost` gets the listing a draft posts. */
function serveOwl(onPost: (body: unknown) => void): void {
  server.use(
    http.get(`${BACKEND}/api/v1/outputs/${OUTPUT}`, () =>
      HttpResponse.json({ id: OUTPUT, slug: 'owl', model_version: COMMIT, name: 'Hoot owl – Black', job_id: 'j1', params: owl.values }),
    ),
    http.get(`${BACKEND}/api/v1/jobs/j1`, () =>
      HttpResponse.json({ id: 'j1', slug: 'owl', status: 'done', started_at: '2026-11-13T10:00:00Z', finished_at: '2026-11-13T10:02:00Z' }),
    ),
    http.get(`${BACKEND}/api/v1/models/owl`, () =>
      HttpResponse.json({
        slug: 'owl',
        name: 'Owl',
        media: [
          { id: 'cover', file: 'cover.png', kind: 'image', caption: '', missing: false, content_type: 'image/png' },
          { id: 'clip', file: 'clip.mp4', kind: 'video', caption: '', missing: false, content_type: 'video/mp4' },
        ],
      }),
    ),
    http.get(`${BACKEND}/api/v1/models/owl/versions`, () => HttpResponse.json([{ commit: COMMIT, short: 'ccccccc', current: true }])),
    http.get(`${BACKEND}/api/v1/models/owl/versions/${COMMIT}/schema`, () => HttpResponse.json(owl.schema)),
    http.get(`${BACKEND}/api/v1/orders/price-book`, () => HttpResponse.json({ filaments: owl.filaments })),
    http.post(`${BACKEND}/api/v1/storefront/listings`, async ({ request }) => {
      const body = (await request.json()) as { title: string; params: Record<string, unknown> }
      onPost(body)
      return HttpResponse.json({ id: LISTING, ...body, published: false }, { status: 201 })
    }),
  )
}

describe('storefront tools (#6131)', () => {
  it('drafts at write and publishes at outward', () => {
    expect(tool('storefront_listing_draft').risk).toBe('write')
    expect(tool('storefront_listing_publish').risk).toBe('outward')
  })

  it('keeps the smart defaults the page keeps (the shared vectors)', () => {
    for (const vector of vectors) {
      const defaults = defaultsFor(vector.schema, vector.filaments, vector.values)
      expect(defaults, vector.name).toEqual(vector.expected)
      if (vector.input !== undefined) expect(withColourChoices(defaults, vector.schema, vector.filaments).params, vector.name).toEqual(vector.input)
    }
  })

  it('keeps a colour the customer chooses out of the title, as the page does', () => {
    const listing = defaultsFor(owl.schema, owl.filaments, owl.values)
    expect(titleColours('Owl – Black', listing, owl.schema, owl.filaments)).toEqual(['Black'])
    expect(withoutWords('Owl Pencil Cup – Navy', ['Navy'])).toBe('Owl Pencil Cup')
    // The page's separators: a slash, a colon, a bar or a middle dot too; one inside a word stays.
    expect(withoutWords('Owl, navy, large', ['Navy'])).toBe('Owl, large')
    expect(withoutWords('Owl Cup / Navy', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Owl Cup: Navy', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Navy | Owl Cup', ['Navy'])).toBe('Owl Cup')
    expect(withoutWords('Owl Cup · Navy · Large', ['Navy'])).toBe('Owl Cup · Large')
    expect(withoutWords('Owl Cup 1/2 Navy', ['Navy'])).toBe('Owl Cup 1/2')
  })

  it('never offers a support setting, nor a colour only a support setting uses', () => {
    const support = vectors.find((vector) => vector.name.startsWith('support settings')) as Vector
    const listing = defaultsFor(support.schema, support.filaments, {})
    // The PVA filament is on the listing for the locked support colour (`defaultsFor`), and an
    // old listing offers the supports switch.
    expect(listing.colours).toContain('f-pva')
    const saved = withColourChoices(
      { ...listing, params: { ...listing.params, supports: { kind: 'choice', choices: [true, false] } } },
      support.schema,
      support.filaments,
    )
    expect(saved.params).toEqual({ body: { kind: 'choice', choices: ['#ffffff'] } })
    expect(saved.fixed).toMatchObject({ supports: true, support_colour: '#F5F5DC' })
  })

  it('drafts an owl from its output, unpublished, with the colour on offer out of its title', async () => {
    let posted: unknown
    serveOwl((body) => (posted = body))
    const result = firstText(await tool('storefront_listing_draft').execute({ output_id: OUTPUT }, ctx())) as Record<string, unknown>
    expect(posted).toEqual({
      slug: 'owl',
      commit: COMMIT,
      title: 'Hoot owl',
      description: '',
      params: {
        label: { kind: 'text', max_length: 200 },
        body: { kind: 'choice', choices: ALL_COLOURS },
        eyes: { kind: 'choice', choices: ALL_COLOURS },
      },
      fixed: owl.expected.fixed,
      colours: owl.expected.colours,
      media_ids: ['cover'],
      materials: ['PLA'],
    })
    expect(result).toMatchObject({
      listing_id: LISTING,
      published: false,
      colour_problems: [],
      page: `/storefront/listings/${LISTING}`,
    })
    expect(result['notes']).toEqual(['This design took 120 s to render; the store\'s preview and price give up after about 100 s (#6120).', 'Took Black out of the title: the customer chooses the colour.'])
  })

  it('locks only what it is told to lock, a colour at its value', async () => {
    let posted: { params: Record<string, unknown>; fixed: Record<string, unknown> } | undefined
    serveOwl((body) => (posted = body as typeof posted))
    await tool('storefront_listing_draft').execute({ output_id: OUTPUT, lock: ['body'] }, ctx())
    expect(Object.keys(posted?.params ?? {})).toEqual(['label', 'eyes'])
    expect(posted?.fixed).toEqual({ ...(owl.expected.fixed as Record<string, unknown>), body: '#1f2a44' })
    await expect(tool('storefront_listing_draft').execute({ output_id: OUTPUT, lock: ['nope'] }, ctx())).rejects.toThrow('nope is not a parameter')
  })

  it('drafts from a model and its values, and refuses a value the schema does not have', async () => {
    let posted: { title: string; params: Record<string, unknown>; fixed: unknown; colours: unknown } | undefined
    serveOwl((body) => (posted = body as typeof posted))
    const result = firstText(await tool('storefront_listing_draft').execute({ slug: 'owl', values: owl.values, title: 'Owl' }, ctx())) as Record<string, unknown>
    expect(posted?.title).toBe('Owl')
    expect(posted?.params).toEqual({
      label: { kind: 'text', max_length: 200 },
      body: { kind: 'choice', choices: ALL_COLOURS },
      eyes: { kind: 'choice', choices: ALL_COLOURS },
    })
    expect(posted?.fixed).toEqual(owl.expected.fixed)
    expect(posted?.colours).toEqual(owl.expected.colours)
    // No render, so no render-time note; no colour in the title, so none taken out; no pipeline
    // (#5701), so no design to set up and no inputs.
    expect(posted).not.toHaveProperty('inputs')
    expect(result['notes']).toEqual([])
    await expect(tool('storefront_listing_draft').execute({ slug: 'owl', values: { bodyy: '#000000' } }, ctx())).rejects.toThrow('bodyy is not a parameter')
  })

  it('leaves a support-only filament’s colour in the title, as the page does', () => {
    const support = vectors.find((vector) => vector.name.startsWith('support settings')) as Vector
    const listing = defaultsFor(support.schema, support.filaments, {})
    expect(listing.colours).toContain('f-pva')
    expect(titleColours('Owl – Natural PVA supports', listing, support.schema, support.filaments)).toEqual([])
    expect(titleColours('Owl – White', listing, support.schema, support.filaments)).toEqual(['White'])
  })

  it('names exactly one source', async () => {
    await expect(tool('storefront_listing_draft').execute({}, ctx())).rejects.toThrow('exactly one')
    await expect(tool('storefront_listing_draft').execute({ output_id: OUTPUT, slug: 'owl' }, ctx())).rejects.toThrow('exactly one')
    await expect(tool('storefront_listing_draft').execute({ slug: 'owl', library_file_id: 46 }, ctx())).rejects.toThrow('exactly one')
  })

  describe('a pipeline template (#5701, spec 2026-11-17 §9)', () => {
    const house = sourceVectors[0] as SourceVector
    const DOLLHOUSE = 'd'.repeat(32)

    /** The dollhouse's output (whole inputs `inputs`), model, schema and price book; `onPost` gets the listing a draft posts. */
    function serveDollhouse(inputs: Record<string, unknown>, onPost: (body: unknown) => void, schema: Schema = house.schema): void {
      const { params, ...rest } = inputs
      server.use(
        http.get(`${BACKEND}/api/v1/outputs/${DOLLHOUSE}`, () =>
          HttpResponse.json({ id: DOLLHOUSE, slug: 'dollhouse', model_version: COMMIT, name: 'Dollhouse', job_id: 'j2', params, inputs: rest }),
        ),
        http.get(`${BACKEND}/api/v1/jobs/j2`, () => HttpResponse.json({ id: 'j2', slug: 'dollhouse', status: 'done' })),
        http.get(`${BACKEND}/api/v1/models/dollhouse`, () => HttpResponse.json({ slug: 'dollhouse', name: 'Dollhouse', media: [] })),
        http.get(`${BACKEND}/api/v1/models/dollhouse/versions`, () => HttpResponse.json([{ commit: COMMIT, short: 'ccccccc', current: true }])),
        http.get(`${BACKEND}/api/v1/models/dollhouse/versions/${COMMIT}/schema`, () => HttpResponse.json(schema)),
        http.get(`${BACKEND}/api/v1/orders/price-book`, () => HttpResponse.json({ filaments: house.filaments })),
        http.post(`${BACKEND}/api/v1/storefront/listings`, async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>
          onPost(body)
          return HttpResponse.json({ id: LISTING, ...body, published: false }, { status: 201 })
        }),
      )
    }

    it('splits a render’s whole inputs as the page does (the shared vectors)', () => {
      expect(sourceVectors.length).toBeGreaterThan(0)
      for (const vector of sourceVectors) {
        const { values, design } = soldDesign(vector.schema, vector.source)
        expect({ ...defaultsFor(vector.schema, vector.filaments, values), ...design }, vector.name).toEqual(vector.expected)
      }
    })

    it('drafts from an output with its own inputs and their version, and leaves out what the pipeline sets', async () => {
      for (const vector of sourceVectors) {
        let posted: Record<string, unknown> | undefined
        serveDollhouse(vector.source, (body) => (posted = body as Record<string, unknown>), vector.schema)
        const result = firstText(await tool('storefront_listing_draft').execute({ output_id: DOLLHOUSE }, ctx())) as Record<string, unknown>
        const { inputs, inputs_v, fixed } = vector.expected
        expect(posted, vector.name).toMatchObject({ fixed, ...(inputs !== undefined ? { inputs, inputs_v } : {}) })
        if (inputs === undefined) {
          expect(posted, vector.name).not.toHaveProperty('inputs')
          expect(posted, vector.name).not.toHaveProperty('inputs_v')
        }
        expect(Object.keys((posted?.['params'] as object | undefined) ?? {}), vector.name).not.toContain('piece')
        // An output with none of the design's own inputs says so (the vectors' empty case).
        const empty = inputs !== undefined && Object.keys(inputs as object).length === 0
        expect(result['notes'], vector.name).toEqual(
          empty
            ? [
                `The output holds none of Dollhouse's own design inputs (its pipeline's), so the listing starts with none: open the listing at /storefront/listings/${LISTING} and press "Set up the design" before you publish it.`,
              ]
            : [],
        )
      }
    })

    it('takes empty inputs at version 0 from an output that holds no inputs at all, and says so', async () => {
      let posted: Record<string, unknown> | undefined
      serveDollhouse({ params: { course: 200 } }, (body) => (posted = body as Record<string, unknown>))
      const result = firstText(await tool('storefront_listing_draft').execute({ output_id: DOLLHOUSE }, ctx())) as Record<string, unknown>
      expect(posted).toMatchObject({ inputs: {}, inputs_v: 0, fixed: { course: 200 } })
      expect(result['notes']).toEqual([
        `The output holds none of Dollhouse's own design inputs (its pipeline's), so the listing starts with none: open the listing at /storefront/listings/${LISTING} and press "Set up the design" before you publish it.`,
      ])
    })

    it('sets no inputs from a model, and says the design needs setting up in the editor', async () => {
      let posted: Record<string, unknown> | undefined
      serveDollhouse(house.source, (body) => (posted = body as Record<string, unknown>))
      const result = firstText(await tool('storefront_listing_draft').execute({ slug: 'dollhouse' }, ctx())) as Record<string, unknown>
      expect(posted).not.toHaveProperty('inputs')
      expect(posted).not.toHaveProperty('inputs_v')
      expect(result['notes']).toEqual([
        `Dollhouse has a design of its own to set up (its pipeline's inputs), which a model brings none of: open the listing at /storefront/listings/${LISTING} and press "Set up the design" before you publish it, or draft it from a saved output of that design.`,
      ])
    })

    it('says a version whose pipeline declares no inputs cannot be listed, not to set it up (#7879)', async () => {
      const faulted = { ...house.schema, pipeline: { inputs_version: null, inputs: null, fault: 'pipeline/__init__.py: no module named inputs' } } as Schema
      const said = `This version of Dollhouse cannot be listed: its pipeline declares no inputs (pipeline/__init__.py: no module named inputs), so the editor offers no "Set up the design" and publish refuses it (listing_inputs_undeclared). Choose another version in the editor, or fix the template and draft it again.`
      serveDollhouse(house.source, () => {}, faulted)
      const fromModel = firstText(await tool('storefront_listing_draft').execute({ slug: 'dollhouse' }, ctx())) as Record<string, unknown>
      expect(fromModel['notes']).toEqual([said])
      serveDollhouse({ params: { course: 200 } }, () => {}, faulted)
      const fromOutput = firstText(await tool('storefront_listing_draft').execute({ output_id: DOLLHOUSE }, ctx())) as Record<string, unknown>
      expect(fromOutput['notes']).toEqual([said])
    })

    it('refuses to lock or set a parameter the pipeline sets itself', async () => {
      serveDollhouse(house.source, () => {})
      await expect(tool('storefront_listing_draft').execute({ output_id: DOLLHOUSE, lock: ['piece'] }, ctx())).rejects.toThrow(
        'piece is set by the design itself, so a listing cannot lock it',
      )
      await expect(tool('storefront_listing_draft').execute({ slug: 'dollhouse', values: { piece: 'roof' } }, ctx())).rejects.toThrow(
        'piece is set by the design itself, so a listing cannot set it',
      )
      await expect(tool('storefront_listing_draft').execute({ slug: 'dollhouse', lock: ['piece'] }, ctx())).rejects.toThrow(
        'piece is set by the design itself, so a listing cannot lock it',
      )
      // The pipeline's own reason, even for a name its schema's parameters leave out.
      serveDollhouse(house.source, () => {}, { ...house.schema, pipeline_params: ['piece', 'storeys'] })
      await expect(tool('storefront_listing_draft').execute({ slug: 'dollhouse', values: { storeys: 2 } }, ctx())).rejects.toThrow(
        'storeys is set by the design itself, so a listing cannot set it',
      )
      await expect(tool('storefront_listing_draft').execute({ slug: 'dollhouse', lock: ['storeys'] }, ctx())).rejects.toThrow(
        'storeys is set by the design itself, so a listing cannot lock it',
      )
    })
  })

  describe('a library file (#6710 §11.3)', () => {
    /** The vector's file in the library that serves; `onPost` gets the listing a draft posts. */
    function serveFile(vector: FileVector, onPost: (body: unknown) => void, fileType = '3mf'): void {
      const { file } = vector
      server.use(
        http.get(`${BACKEND}/api/v1/settings`, () => HttpResponse.json({ library_backend: file.library })),
        http.get(`${BACKEND}/api/v1/print/library/${file.id}`, () =>
          HttpResponse.json({ id: file.id, filename: file.filename, file_type: fileType, has_thumbnail: true, is_external: false, per_print: false, print_count: 0, printable: true, sliced: false }),
        ),
        http.get(`${BACKEND}/api/v1/print/library/${file.id}/filaments`, () => HttpResponse.json({ library_file_id: file.id, source: 'inventory', track_switch: false, slots: file.slots })),
        http.get(`${BACKEND}/api/v1/print/library/${file.id}/plates`, () => HttpResponse.json(file.plates)),
        http.get(`${BACKEND}/api/v1/orders/price-book`, () => HttpResponse.json({ filaments: vector.filaments })),
        http.post(`${BACKEND}/api/v1/storefront/listings`, async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>
          onPost(body)
          return HttpResponse.json({ id: LISTING, ...body, published: false }, { status: 201 })
        }),
      )
    }

    it('drafts the body the page saves for the same file (the shared vectors)', async () => {
      expect(fileVectors.length).toBeGreaterThan(0)
      for (const vector of fileVectors) {
        let posted: Record<string, unknown> | undefined
        serveFile(vector, (body) => (posted = body as Record<string, unknown>))
        const result = firstText(await tool('storefront_listing_draft').execute({ library_file_id: vector.file.id }, ctx())) as Record<string, unknown>
        const { materials, ...body } = posted ?? {}
        expect(body, vector.name).toEqual(vector.expected)
        // The assistant allows the materials it offers, support left out, as for a design (#6302 review).
        expect(materials, vector.name).toEqual(['PLA'])
        expect(result, vector.name).toMatchObject({ listing_id: LISTING, published: false, colour_problems: [], page: `/storefront/listings/${LISTING}` })
        // The page's "Leave … out of the title" warning names the same colours this copy takes out.
        for (const { title, colours } of vector.titles) {
          expect(fileTitleColours(title, vector.expected['colours'] as string[], vector.filaments), `${vector.name}: ${title}`).toEqual(colours)
        }
      }
    })

    it('notes what publish would refuse: no filament a customer can pick, or a support part with no support filament', async () => {
      const vector = fileVectors.find((one) => one.file.slots.some((slot) => isSupportMaterial(slot.material))) as FileVector
      const support = vector.filaments.filter((filament) => isSupportMaterial(filament.material))
      const customer = vector.filaments.filter((filament) => !isSupportMaterial(filament.material))
      const draft = async (filaments: Filament[]) => {
        serveFile({ ...vector, filaments }, () => {})
        const result = firstText(await tool('storefront_listing_draft').execute({ library_file_id: vector.file.id }, ctx())) as Record<string, unknown>
        return result['notes']
      }
      expect(await draft(support)).toEqual([
        'The price book has no priced filament a customer can pick: publish refuses the listing until one is added and ticked on the page.',
      ])
      expect(await draft(customer)).toEqual([
        'The file has a support part and the price book has no priced support filament: publish refuses it (listing_file_support_unset) until one is added and ticked on the page.',
      ])
      expect(await draft(vector.filaments)).toEqual([])
    })

    it('takes a colour on offer out of a title it is given', async () => {
      const vector = fileVectors[0] as FileVector
      let posted: { title?: string } | undefined
      serveFile(vector, (body) => (posted = body as typeof posted))
      const result = firstText(await tool('storefront_listing_draft').execute({ library_file_id: vector.file.id, title: 'Pencil cup – Navy' }, ctx())) as Record<string, unknown>
      expect(posted?.title).toBe('Pencil cup')
      expect(result['notes']).toEqual(['Took Navy out of the title: the customer chooses the colour.'])
    })

    it('refuses a file a listing cannot sell, and the design-only fields', async () => {
      const vector = fileVectors[0] as FileVector
      serveFile(vector, () => {}, 'gcode.3mf')
      await expect(tool('storefront_listing_draft').execute({ library_file_id: vector.file.id }, ctx())).rejects.toThrow('not a 3MF or an STL')
      await expect(tool('storefront_listing_draft').execute({ library_file_id: vector.file.id, values: { a: 1 } }, ctx())).rejects.toThrow('values go with slug')
      await expect(tool('storefront_listing_draft').execute({ library_file_id: vector.file.id, lock: ['a'] }, ctx())).rejects.toThrow('lock goes with')
    })
  })

  it('publishes the listing named', async () => {
    let called = false
    server.use(
      http.post(`${BACKEND}/api/v1/storefront/listings/${LISTING}/publish`, () => {
        called = true
        return HttpResponse.json({ id: LISTING, title: 'Navy owl', published: true, published_at: '2026-11-13T11:00:00Z' })
      }),
    )
    const result = firstText(await tool('storefront_listing_publish').execute({ listing_id: LISTING }, ctx()))
    expect(called).toBe(true)
    expect(result).toEqual({ listing_id: LISTING, title: 'Navy owl', published: true, published_at: '2026-11-13T11:00:00Z' })
  })
})

describe('colourProblems (#6144)', () => {
  it('checks an offered colour’s listing default, not the template’s', () => {
    // The owl's body starts at white in the template; no white filament is on this listing,
    // but the listing starts the body at navy, which is.
    const listing = { params: { body: { kind: 'choice' as const, choices: [], default: '#1F2A44' }, eyes: { kind: 'choice' as const, choices: [] } }, fixed: {}, colours: ['f-navy', 'f-black'] }
    expect(colourProblems(listing, owl.schema, owl.filaments)).toEqual([])
    const unlisted = { ...listing, params: { ...listing.params, body: { kind: 'choice' as const, choices: [], default: '#FF0000' } } }
    expect(colourProblems(unlisted, owl.schema, owl.filaments)).toEqual([{ name: 'body', label: 'Body', colour: '#FF0000' }])
  })
})

describe('isSupportMaterial (#6710, #7189 and #7195 reviews)', () => {
  it('reads a support material as the backend and the page do: the shared vectors', () => {
    // `test-fixtures/storefront/support-materials.json`, which the backend's
    // `tests/orders/storefront/test_support_material.py` and the page's `listingDraft.test.ts` read too.
    const cases = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/storefront/support-materials.json`, 'utf8')) as { support: string[]; customer: string[] }
    for (const material of cases.support) expect(isSupportMaterial(material), material).toBe(true)
    for (const material of cases.customer) expect(isSupportMaterial(material), material).toBe(false)
    expect(isSupportMaterial(null)).toBe(false)
    expect(isSupportMaterial(undefined)).toBe(false)
  })
})

describe('draftMaterials (#6302 review)', () => {
  it('allows the materials the draft offers, support left out, PETG and PLA first', () => {
    const made = (id: string, material: string): Filament => ({ ...(owl.filaments[0] as Filament), id, material })
    const filaments = [made('abs', 'ABS'), made('pla', 'PLA'), made('pva', 'PVA'), made('petg', 'petg'), made('pla-2', 'pla')]
    expect(draftMaterials(['abs', 'pla', 'pva', 'petg', 'pla-2'], filaments)).toEqual(['petg', 'PLA', 'ABS'])
    expect(draftMaterials([], filaments)).toEqual([])
  })
})

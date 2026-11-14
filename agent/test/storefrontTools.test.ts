import { readFileSync } from 'node:fs'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import type { components } from '../src/api/schema.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import type { Tool, ToolContext } from '../src/tools/registry.js'
import { defaultsFor, titleColours, withColourChoices, withoutWords } from '../src/tools/storefrontDraft.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #6131 (design 2026-11-13 §6) — the assistant's storefront tools: a draft (`write`) built
// with the Storefront page's smart defaults, and a publish (`outward`, so a person approves).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

type Schema = components['schemas']['CustomizerSchema']
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

const vectors = JSON.parse(readFileSync(`${import.meta.dirname}/../../test-fixtures/storefront/sell-draft-vectors.json`, 'utf8')) as Vector[]
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
      one_material: false,
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
    // No render, so no render-time note; no colour in the title, so none taken out.
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

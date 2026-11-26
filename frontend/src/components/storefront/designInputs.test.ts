import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { Filament, Param } from '../../api/types'
import { server } from '../../mocks/server'
import { designStart, designText, fieldGroups, fieldInputs, fieldValues, inputsRefusalText, withDesign, withoutInput } from './designInputs'
import { emptyDraft, type DesignSchema, type ListingDraft, type ListingRefusal } from './listingDraft'

const COMMIT = 'c'.repeat(40)

const SHAPE = {
  type: 'object',
  properties: {
    house: {
      type: 'object',
      title: 'House',
      description: 'The box house.',
      properties: {
        cols: { type: 'integer', title: 'Modules wide', minimum: 1, maximum: 4 },
        storeys: { type: 'integer', title: 'Storeys', minimum: 1, maximum: 3 },
        flat_roof: { type: 'boolean' },
      },
      required: ['cols', 'storeys'],
      additionalProperties: false,
    },
    tags: { type: 'array', title: 'Tags', items: { type: 'string', maxLength: 5 } },
    finish: { type: 'string', enum: ['matte', 'gloss'], default: 'matte' },
  },
  required: ['house'],
  additionalProperties: false,
}

const param = (over: Partial<Param> & Pick<Param, 'name' | 'type'>): Param => ({ group: 'Shape', caption: over.name, initial: 1, ...over }) as Param

const SCHEMA = {
  title: 'Kit',
  source_sha256: 'a'.repeat(64),
  groups: [],
  parameters: [
    param({ name: 'piece', type: 'string', initial: 'wall' }),
    param({ name: 'height', type: 'slider', initial: 210, min: 180, max: 240, step: 5 }),
    param({ name: 'label', type: 'string', initial: 'Home', max_length: 40 }),
    param({ name: 'wall', type: 'color', initial: '#FFFFFF' }),
    param({ name: 'secret', type: 'number', group: 'Hidden', initial: 3 }),
  ],
  pipeline: { inputs_version: 1, inputs: SHAPE, fault: '' },
  pipeline_params: ['piece'],
} as DesignSchema

const FILAMENTS = [
  { id: 'white', material: 'PLA', colour: '#ffffff', density_g_cm3: '1.24' },
  { id: 'red', material: 'PLA', colour: '#FF0000', density_g_cm3: '1.24' },
] as Filament[]

function draft(over: Partial<ListingDraft> = {}): ListingDraft {
  return {
    ...emptyDraft('kit', COMMIT),
    params: { label: { kind: 'text', max_length: 10 }, wall: { kind: 'choice', choices: [] } },
    fixed: { height: 220 },
    colours: ['white', 'red'],
    ...over,
  }
}

function refuse(status: number, detail: string): Response {
  return HttpResponse.json({ title: 'Refused', status, detail }, { status, headers: { 'Content-Type': 'application/problem+json' } })
}

/** What the migrate route answers, from the inputs sent; every body it got. */
function migrate(answer: (inputs: Record<string, unknown>) => Response | Record<string, unknown>) {
  const seen: unknown[] = []
  server.use(
    http.post('/api/v1/models/:slug/inputs/migrate', async ({ request }) => {
      const body = (await request.json()) as { inputs: Record<string, unknown> }
      seen.push(body)
      const made = answer(body.inputs)
      return made instanceof Response ? made : HttpResponse.json({ inputs: made, from_version: 0, to_version: 1 })
    }),
  )
  return seen
}

const PARAMS = { piece: 'wall', height: 220, label: 'Home', wall: '#FFFFFF', secret: 3 }

describe('the working copy "Set up the design" opens on (#5701 §5.3)', () => {
  it('is the listing’s inputs with its locks, its starting values and the defaults as params', async () => {
    const made = await designStart(SCHEMA, draft({ inputs: { house: { cols: 3 } }, inputs_v: 1, params: { label: { kind: 'text', max_length: 10, default: 'Den' } } }))
    expect(made).toEqual({ inputs: { house: { cols: 3 }, params: { ...PARAMS, label: 'Den' }, v: 1 }, note: null })
  })

  it('is brought up to the version the listing’s commit has', async () => {
    const seen = migrate((inputs) => ({ ...inputs, house: { cols: 3, storeys: 1 }, v: 1, params: { height: 999 } }))
    const made = await designStart(SCHEMA, draft({ inputs: { house: { cols: 3 } }, inputs_v: 0 }))
    expect(seen).toEqual([{ inputs: { house: { cols: 3 }, params: PARAMS, v: 0 }, version: COMMIT }])
    // What a migration makes of `params` never reaches the listing, as at publish (§4.3).
    expect(made).toEqual({ inputs: { house: { cols: 3, storeys: 1 }, params: PARAMS, v: 1 }, note: null })
  })

  it('starts a listing with none from the template’s own design', async () => {
    migrate((inputs) => ({ ...inputs, house: { cols: 2, storeys: 1 }, v: 1 }))
    expect(await designStart(SCHEMA, draft())).toEqual({ inputs: { house: { cols: 2, storeys: 1 }, params: PARAMS, v: 1 }, note: null })
  })

  it('starts empty when the template fills nothing in', async () => {
    migrate(() => refuse(422, 'no migrate'))
    expect(await designStart(SCHEMA, draft())).toEqual({ inputs: { params: PARAMS, v: 1 }, note: null })
  })

  it('says so when the listing’s own inputs cannot be taken', async () => {
    migrate((inputs) => ('house' in inputs ? refuse(422, 'no such house') : { ...inputs, v: 1 }))
    const older = await designStart(SCHEMA, draft({ inputs: { house: {} }, inputs_v: 0 }))
    expect(older.inputs).toEqual({ params: PARAMS, v: 1 })
    expect(older.note).toContain('no such house')
    const newer = await designStart(SCHEMA, draft({ inputs: { house: {} }, inputs_v: 4 }))
    expect(newer.note).toContain('newer version')
  })

  it('rejects when the render service does not answer, so nothing is lost', async () => {
    migrate(() => refuse(500, 'down'))
    await expect(designStart(SCHEMA, draft({ inputs: { house: {} }, inputs_v: 0 }))).rejects.toThrow('down')
  })
})

describe('"Use this design" (#5701 §5.3)', () => {
  it('writes the inputs and their version, and no param of its own into them', () => {
    const made = withDesign(draft(), SCHEMA, FILAMENTS, { house: { cols: 4 }, params: PARAMS, v: 1 })
    expect(made.inputs).toEqual({ house: { cols: 4 } })
    expect(made.inputs_v).toBe(1)
  })

  it('leaves the settings untouched when the working copy changed none', () => {
    const before = draft()
    const made = withDesign(before, SCHEMA, FILAMENTS, { params: { ...PARAMS, wall: '#ffffff' }, v: 1 })
    expect(made.params).toBe(before.params)
    expect(made.fixed).toBe(before.fixed)
  })

  it('sets a locked value, and the starting value of an offered one within its bounds', () => {
    const made = withDesign(draft(), SCHEMA, FILAMENTS, { params: { ...PARAMS, height: 185, label: 'A very long name', wall: '#FF0000' }, v: 1 })
    expect(made.fixed).toEqual({ height: 185 })
    expect(made.params['wall']).toEqual({ kind: 'choice', choices: [], default: '#FF0000' })
    expect(made.params['label']).toMatchObject({ kind: 'text', max_length: 10 })
    expect(String((made.params['label'] as { default?: unknown }).default ?? '').length).toBeLessThanOrEqual(10)
    expect(made.colours).toEqual(['white', 'red'])
  })

  it('drops the listing’s own starting value where the working copy is back at the template’s', () => {
    const own = draft({ params: { label: { kind: 'text', max_length: 10, default: 'Den' } } })
    expect(withDesign(own, SCHEMA, FILAMENTS, { params: PARAMS, v: 1 }).params).toEqual({ label: { kind: 'text', max_length: 10 } })
  })

  it('ignores a parameter the pipeline sets and a hidden one', () => {
    const made = withDesign(draft(), SCHEMA, FILAMENTS, { params: { ...PARAMS, piece: 'roof', secret: 9 }, v: 1 })
    expect(made.fixed).toEqual({ height: 220 })
  })
})

describe('a design in words (#5701 §5.1)', () => {
  it('is the schema’s titles and values, and what the schema does not have', () => {
    expect(designText(SHAPE, { house: { cols: 3, storeys: 1, flat_roof: true }, tags: ['a', 'b'], old_thing: 4 })).toBe(
      'House: Modules wide 3, Storeys 1, Flat roof on. Tags: a, b. Old thing: 4',
    )
  })
})

describe('a refusal of the inputs (#5701 §5.1)', () => {
  const refusal = (name: string, reason: string | null, detail = `${name}: raw text`, code = 'invalid') =>
    ({ kind: 'inputs', code, name, reason, detail }) as Extract<ListingRefusal, { kind: 'inputs' }>

  it.each([
    ['inputs.house.storeys', 'maximum', 'Storeys: at most 3.'],
    ['inputs.house.cols', 'minimum', 'Modules wide: at least 1.'],
    ['inputs.house', 'required', 'House: not set. Set up the design.'],
    ['inputs.house.attic', 'additional', 'Attic: this version of the template does not have it.'],
    ['inputs.tags.1', 'maxLength', 'Tags, item 2: at most 5 characters.'],
    ['inputs.finish', 'enum', 'Finish: one of matte, gloss.'],
    ['inputs.house.cols', 'type', 'Modules wide: must be a whole number.'],
    ['inputs.house.cols', 'odd_check', 'Modules wide: raw text.'],
  ])('%s %s reads with its title', (name, reason, text) => {
    expect(inputsRefusalText(refusal(name, reason), SHAPE)).toBe(text)
  })

  it('never shows a path, whatever the code', () => {
    for (const [code, reason] of [
      ['unexpected', null],
      ['undeclared', null],
      ['migration_failed', null],
      ['reserved', null],
      ['invalid', 'too_large'],
      ['invalid', 'unstorable'],
      ['invalid', 'newer'],
    ] as const) {
      expect(inputsRefusalText(refusal('inputs.house', reason, 'inputs.house: no', code), SHAPE)).not.toContain('inputs.')
    }
  })

  it('takes one the version does not have off the listing', () => {
    expect(withoutInput(draft({ inputs: { house: { cols: 2, attic: 1 }, x: 1 } }), 'inputs.house.attic').inputs).toEqual({ house: { cols: 2 }, x: 1 })
  })
})

describe('the schema’s own form (#5701 §5.2)', () => {
  const groups = fieldGroups(SHAPE)

  it('draws one level of object, named by its titles', () => {
    expect(groups.map((group) => [group.title, group.fields.map((field) => [field.name, field.label, field.required])])).toEqual([
      [
        'House',
        [
          ['house.cols', 'Modules wide', true],
          ['house.storeys', 'Storeys', true],
          ['house.flat_roof', 'Flat roof', false],
        ],
      ],
      [null, [['tags', 'Tags', false]]],
      [null, [['finish', 'Finish', false]]],
    ])
  })

  it('starts at the listing’s values, then the schema’s defaults', () => {
    expect(fieldValues(groups, { house: { cols: 3 } })).toEqual({ 'house.cols': 3, finish: 'matte' })
  })

  it('makes the inputs again, and leaves out an object nothing in is set', () => {
    expect(fieldInputs(groups, { 'house.cols': 3, 'house.flat_roof': false, tags: ['a'] })).toEqual({ house: { cols: 3, flat_roof: false }, tags: ['a'] })
    const optional = fieldGroups({ ...SHAPE, required: [] })
    expect(fieldInputs(optional, { 'house.flat_roof': false })).toEqual({})
  })

  it('keeps what it cannot draw, and drops a field it draws that was left empty', () => {
    const base = { house: { cols: 2, storeys: 1, attic: { rooms: 2 } }, rooms: [{ name: 'hall' }], tags: ['x'] }
    expect(fieldInputs(groups, { 'house.cols': 3, 'house.flat_roof': false }, base)).toEqual({
      house: { cols: 3, flat_roof: false, attic: { rooms: 2 } },
      rooms: [{ name: 'hall' }],
    })
  })

  it('keeps a key the schema does not declare, which publish then names (#7794 review)', () => {
    // Not dropped in silence: an input of another version stays, and publish refuses it in the Design block.
    const base = { house: { cols: 2, storeys: 1, gone: 1 }, old: true }
    expect(fieldInputs(groups, { 'house.cols': 2, 'house.storeys': 1 }, base)).toEqual({ house: { cols: 2, storeys: 1, gone: 1 }, old: true })
  })

  it('lays a top-level field over the inputs it opened on', () => {
    const base = { house: { cols: 2, storeys: 1 }, finish: 'gloss', rooms: [{ name: 'hall' }] }
    expect(fieldInputs(groups, { 'house.cols': 2, 'house.storeys': 1, finish: 'matte' }, base)).toEqual({
      house: { cols: 2, storeys: 1 },
      finish: 'matte',
      rooms: [{ name: 'hall' }],
    })
  })
})

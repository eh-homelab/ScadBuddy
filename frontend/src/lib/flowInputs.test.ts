import { afterEach, describe, expect, it } from 'vitest'
import { defaultText, initialValues, inputFields, parseSchemaText, toInputs, undrawnFields } from './flowInputs'

const SCHEMA = {
  type: 'object',
  properties: {
    slug: { type: 'string', title: 'Template', description: 'What to render' },
    copies: { type: 'integer', minimum: 1, default: 2 },
    colour: { type: 'string', enum: ['red', 'blue'], default: 'red' },
    scale: { type: 'number' },
    dry_run: { type: 'boolean', default: true },
    plates: { type: 'array', items: { type: 'integer' }, default: [1, 2] },
  },
  required: ['slug'],
}

describe('flow inputs (#2483)', () => {
  it('reads one field per input, in order, with what the form shows', () => {
    const fields = inputFields(SCHEMA)
    expect(fields.map((f) => [f.name, f.type, f.required])).toEqual([
      ['slug', 'string', true],
      ['copies', 'integer', false],
      ['colour', 'string', false],
      ['scale', 'number', false],
      ['dry_run', 'boolean', false],
      ['plates', 'array', false],
    ])
    expect(fields[0]).toMatchObject({ label: 'Template', description: 'What to render' })
    expect(fields[2]!.enum).toEqual(['red', 'blue'])
    expect(fields[5]!.itemType).toBe('integer')
  })

  it('has no fields without a schema or properties', () => {
    expect(inputFields(null)).toEqual([])
    expect(inputFields(undefined)).toEqual([])
    expect(inputFields({ type: 'object' })).toEqual([])
  })

  it('starts the form at the defaults', () => {
    expect(initialValues(inputFields(SCHEMA))).toEqual({
      slug: '',
      copies: '2',
      colour: 'red',
      scale: '',
      dry_run: true,
      plates: '1\n2',
    })
  })

  it('turns the form into inputs, leaving out what is blank', () => {
    const fields = inputFields(SCHEMA)
    const values = { ...initialValues(fields), slug: ' box ', copies: '3', scale: '', plates: '4\n\n5\n' }
    expect(toInputs(fields, values)).toEqual({
      inputs: { slug: ' box ', copies: 3, colour: 'red', dry_run: true, plates: [4, 5] },
      errors: {},
    })
  })

  it('sends a string or array emptied over its default as empty, not the default (#2662)', () => {
    const schema = {
      type: 'object',
      properties: {
        note: { type: 'string', default: 'hello' },
        tags: { type: 'array', items: { type: 'string' }, default: ['a'] },
        copies: { type: 'integer', default: 2 },
        colour: { type: 'string', enum: ['red', 'blue'], default: 'red' },
        label: { type: 'string' },
      },
    }
    const fields = inputFields(schema)
    const values = { note: '', tags: '', copies: '', colour: '', label: '' }
    // A number has no empty value, and an enum's blank is no choice: both take the default.
    expect(toInputs(fields, values)).toEqual({ inputs: { note: '', tags: [] }, errors: {} })
  })

  it('sends a string of only spaces over its default as typed, not emptied', () => {
    const fields = inputFields({ type: 'object', properties: { note: { type: 'string', default: 'hello' } } })
    expect(toInputs(fields, { note: '  ' })).toEqual({ inputs: { note: '  ' }, errors: {} })
  })

  it('names each field that does not parse or is missing', () => {
    const fields = inputFields(SCHEMA)
    const values = { ...initialValues(fields), copies: '2.5', scale: 'big', plates: 'x' }
    expect(toInputs(fields, values).errors).toEqual({
      slug: 'Required',
      copies: 'A whole number',
      scale: 'A number',
      plates: 'Line 1: a whole number',
    })
  })

  it('parses the editor text: blank is no schema, bad JSON is a problem', () => {
    expect(parseSchemaText('  ')).toEqual({ schema: null })
    expect(parseSchemaText('{"type": "object"}')).toEqual({ schema: { type: 'object' } })
    expect(parseSchemaText('{"type": ').error).toMatch(/JSON/)
    expect(parseSchemaText('[1]').error).toMatch(/object/)
  })
})

const zone = process.env.TZ

afterEach(() => {
  if (zone === undefined) delete process.env.TZ
  else process.env.TZ = zone
})

describe("a prompt's answer (#7364)", () => {
  const prop = (schema: Record<string, unknown>, required: string[] = []) =>
    inputFields({ type: 'object', properties: { x: schema }, required })
  const at = { type: 'string', format: 'date-time' }

  it('reads the formats it draws, of a string only', () => {
    expect(prop({ type: 'string', format: 'email' })[0]!.format).toBe('email')
    expect(prop({ type: 'string', format: 'date' })[0]!.format).toBe('date')
    expect(prop(at)[0]!.format).toBe('date-time')
    expect(prop({ type: 'string', format: 'uri' })[0]).not.toHaveProperty('format')
    expect(prop({ type: 'integer', format: 'email' })[0]).not.toHaveProperty('format')
  })

  it.each([
    ['2026-03-08T01:59', '2026-03-08T01:59:00-05:00'],
    ['2026-03-08T03:00', '2026-03-08T03:00:00-04:00'],
    ['2026-07-01T09:30', '2026-07-01T09:30:00-04:00'],
    ['2026-11-24T09:30:15', '2026-11-24T09:30:15-05:00'],
    ['2026-11-24T09:30:15.250', '2026-11-24T09:30:15.250-05:00'],
    // A time the clocks skip is the instant the browser reads it as.
    ['2026-03-08T02:30', '2026-03-08T03:30:00-04:00'],
  ])('sends a date-time with the offset of its own local time: %s', (typed, sent) => {
    process.env.TZ = 'America/New_York'
    expect(toInputs(prop(at), { x: typed }).inputs).toEqual({ x: sent })
    expect(new Date(sent).getTime()).toBe(new Date(typed).getTime())
  })

  it.each([
    ['Asia/Kolkata', '2026-07-01T09:30:00+05:30'],
    ['America/St_Johns', '2026-07-01T09:30:00-02:30'],
    ['UTC', '2026-07-01T09:30:00+00:00'],
  ])('writes the offset of %s', (tz, sent) => {
    process.env.TZ = tz
    expect(toInputs(prop(at), { x: '2026-07-01T09:30' }).inputs).toEqual({ x: sent })
  })

  it('keeps the instant where the zone has an offset of seconds', () => {
    process.env.TZ = 'America/New_York'
    const { x } = toInputs(prop(at), { x: '1800-01-01T12:00' }).inputs
    expect(x).toMatch(/^1800-01-01T\d\d:\d\d:\d\d-04:56$/)
    expect(new Date(String(x)).getTime()).toBe(new Date('1800-01-01T12:00').getTime())
  })

  it('shows a date-time default as a local time, and sends it back as it came', () => {
    process.env.TZ = 'America/New_York'
    const fields = prop({ ...at, default: '2026-07-01T13:30:00.125Z' })
    const values = initialValues(fields)
    expect(values).toEqual({ x: '2026-07-01T09:30:00.125' })
    expect(toInputs(fields, values).inputs).toEqual({ x: '2026-07-01T13:30:00.125Z' })
    expect(initialValues(prop({ ...at, default: '2026-01-01t12:00:00z' }))).toEqual({ x: '2026-01-01T07:00' })
    // The second 01:30 of the night the clocks go back is not swapped for the first.
    const late = prop({ ...at, default: '2026-11-01T01:30:00-05:00' })
    expect(toInputs(late, initialValues(late)).inputs).toEqual({ x: '2026-11-01T01:30:00-05:00' })
  })

  it('says what is no date-time, and leaves a choice of date-times as it is', () => {
    expect(toInputs(prop(at), { x: 'soon' }).errors).toEqual({ x: 'A date and a time' })
    const choice = prop({ ...at, enum: ['2026-07-01T13:30:00Z'] })
    expect(toInputs(choice, { x: '2026-07-01T13:30:00Z' }).inputs).toEqual({ x: '2026-07-01T13:30:00Z' })
    expect(initialValues(prop({ ...at, enum: ['2026-07-01T13:30:00Z'], default: '2026-07-01T13:30:00Z' }))).toEqual({
      x: '2026-07-01T13:30:00Z',
    })
  })

  it('says a year of five digits is the problem, before anything is sent', () => {
    expect(toInputs(prop(at), { x: '12026-07-01T09:30' }).errors).toEqual({ x: 'The year must have four digits' })
    const day = prop({ type: 'string', format: 'date' })
    expect(toInputs(day, { x: '12026-07-01' })).toEqual({ inputs: {}, errors: { x: 'The year must have four digits' } })
    expect(toInputs(day, { x: '2026-07-01' }).inputs).toEqual({ x: '2026-07-01' })
    // Plain text is not a date.
    expect(toInputs(prop({ type: 'string' }), { x: '12026-07-01' }).inputs).toEqual({ x: '12026-07-01' })
  })

  it('says what a field the browser could not read must hold', () => {
    const fields = inputFields({
      type: 'object',
      properties: { size: { type: 'number' }, copies: { type: 'integer' }, at },
      required: ['copies'],
    })
    const blank = { size: '', copies: '', at: '' }
    expect(toInputs(fields, blank, { answer: true, unread: ['size', 'copies', 'at'] }).errors).toEqual({
      size: 'A number',
      copies: 'A whole number',
      at: 'This is not complete',
    })
  })

  it('reads a default as a person does: a date-time in local time', () => {
    process.env.TZ = 'America/New_York'
    const text = (schema: Record<string, unknown>) => defaultText(prop(schema)[0]!)
    expect(text({ ...at, default: '2026-07-01t13:30:00z' })).toBe(new Date(2026, 6, 1, 9, 30).toLocaleString())
    expect(text({ ...at, default: 'soon' })).toBe('soon')
    expect(text({ ...at, enum: ['2026-07-01T13:30:00Z'], default: '2026-07-01T13:30:00Z' })).toBe('2026-07-01T13:30:00Z')
    expect(text({ type: 'array', items: { type: 'string' }, default: ['a', 'b'] })).toBe('a, b')
    expect(text({ type: 'integer', default: 2 })).toBe('2')
    expect(text({ type: 'string' })).toBe('')
  })

  it('leaves out an optional field left empty, and never takes a default for a required one', () => {
    const fields = inputFields({
      type: 'object',
      properties: {
        note: { type: 'string', default: 'hello' },
        tags: { type: 'array', items: { type: 'string' }, default: ['a'] },
        name: { type: 'string', default: 'Jo' },
        copies: { type: 'integer', default: 2 },
        sms: { type: 'boolean' },
      },
      required: ['name', 'copies'],
    })
    const blank = { note: '', tags: '', name: '', copies: '', sms: false }
    expect(toInputs(fields, blank, { answer: true })).toEqual({
      inputs: { sms: false },
      errors: { name: 'Required', copies: 'Required' },
    })
  })

  it('names the fields it cannot draw', () => {
    const schema = {
      type: 'object',
      properties: {
        email: { type: 'string' },
        where: { type: 'object', title: 'Where', properties: {} },
        odd: { type: ['string', 'null'] },
        broken: 3,
      },
    }
    expect(inputFields(schema).map((f) => f.name)).toEqual(['email'])
    expect(undrawnFields(schema)).toEqual(['Where', 'odd', 'broken'])
    expect(undrawnFields(null)).toEqual([])
  })

  it('takes a field with a name every object has', () => {
    const fields = inputFields({
      type: 'object',
      properties: { constructor: { type: 'array', items: { type: 'integer' } } },
    })
    expect(toInputs(fields, { constructor: '1\n2' } as never).inputs).toEqual({ constructor: [1, 2] })
  })
})

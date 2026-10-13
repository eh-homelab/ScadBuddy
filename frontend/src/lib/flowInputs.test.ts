import { describe, expect, it } from 'vitest'
import { initialValues, inputFields, parseSchemaText, toInputs } from './flowInputs'

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

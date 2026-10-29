import { describe, expect, it } from 'vitest'
import { parametersOf } from './schema'

describe('parametersOf', () => {
  it('lists properties with type, required, description, enum and default', () => {
    const schema = {
      type: 'object',
      properties: {
        slug: { type: 'string', description: 'The model.' },
        limit: { type: 'integer', default: 20 },
        order: { enum: ['asc', 'desc'], default: 'asc' },
        tags: { type: ['array', 'null'] },
        free: {},
      },
      required: ['slug'],
    }
    expect(parametersOf(schema)).toEqual([
      { name: 'slug', type: 'string', required: true, description: 'The model.', enum: null, default: null },
      { name: 'limit', type: 'integer', required: false, description: null, enum: null, default: '20' },
      { name: 'order', type: 'enum', required: false, description: null, enum: ['asc', 'desc'], default: '"asc"' },
      { name: 'tags', type: 'array | null', required: false, description: null, enum: null, default: null },
      { name: 'free', type: 'any', required: false, description: null, enum: null, default: null },
    ])
  })

  it('joins anyOf types', () => {
    const schema = {
      type: 'object',
      properties: {
        id: { anyOf: [{ type: 'string' }, { type: 'integer' }] },
        when: { oneOf: [{ type: 'string' }, { type: 'null' }] },
      },
    }
    expect(parametersOf(schema).map((p) => [p.name, p.type])).toEqual([
      ['id', 'string | integer'],
      ['when', 'string | null'],
    ])
  })

  it('answers no parameters for a schema that is not an object', () => {
    expect(parametersOf(null)).toEqual([])
    expect(parametersOf('object')).toEqual([])
    expect(parametersOf([])).toEqual([])
    expect(parametersOf({ type: 'string' })).toEqual([])
    expect(parametersOf({ type: 'object', properties: [] })).toEqual([])
    expect(parametersOf({ type: 'object' })).toEqual([])
  })
})

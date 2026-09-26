import { describe, expect, it } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import { keychainSchema } from '../mocks/fixtures'
import {
  colorParamNames,
  colorsFrom,
  defaultValues,
  diffFromDefaults,
  extrudersOf,
} from './params'

describe('defaultValues', () => {
  it('takes every parameter from every group', () => {
    const values = defaultValues(keychainSchema)
    expect(Object.keys(values)).toHaveLength(11)
    expect(values['name']).toBe('Reagan')
    expect(values['keyring_hole']).toBe(true)
  })
})

describe('colour order', () => {
  it('is extruder order, in schema sequence', () => {
    expect(colorParamNames(keychainSchema)).toEqual(['body_color', 'text_color'])
  })

  it('reads the live values', () => {
    const values = { ...defaultValues(keychainSchema), body_color: '#000000' }
    expect(colorsFrom(keychainSchema, values)).toEqual(['#000000', '#E8532F'])
  })

  it('gives parameters sharing a colour the first one’s extruder, with no gap', () => {
    const schema: CustomizerSchema = {
      ...keychainSchema,
      parameters: [
        ...(keychainSchema.parameters ?? []),
        { name: 'rim_color', type: 'color', initial: '#123456', group: 'Colours' },
      ],
    }
    const values = { ...defaultValues(schema), text_color: '#000000', body_color: '#000' }
    expect(extrudersOf(schema, values)).toEqual(
      new Map([
        ['body_color', 1],
        ['text_color', 1],
        ['rim_color', 2],
      ]),
    )
  })
})

describe('diffFromDefaults', () => {
  it('is empty for the defaults', () => {
    expect(diffFromDefaults(keychainSchema, defaultValues(keychainSchema))).toEqual([])
  })

  it('reports each changed parameter with its old value', () => {
    const diff = diffFromDefaults(keychainSchema, {
      ...defaultValues(keychainSchema),
      name: 'Nova',
      text_size: 18,
    })
    expect(diff).toEqual([
      { name: 'name', caption: 'Name on the tag', value: 'Nova', initial: 'Reagan' },
      { name: 'text_size', caption: 'Text size', value: 18, initial: 14 },
    ])
  })

  it('ignores parameters the value set does not mention', () => {
    expect(diffFromDefaults(keychainSchema, {})).toEqual([])
  })
})

import { describe, expect, it } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import { colorParamNames, colorsFrom, defaultValues, diffFromDefaults } from './params'

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

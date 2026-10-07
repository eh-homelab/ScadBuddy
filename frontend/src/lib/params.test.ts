import { describe, expect, it } from 'vitest'
import type { CustomizerSchema } from '../api/types'
import { keychainSchema } from '../mocks/fixtures'
import type { Param } from '../api/types'
import {
  checkParamValue,
  colorParamNames,
  colorsFrom,
  defaultValues,
  diffFromDefaults,
  extrudersOf,
  extrudersIn,
  outOfRange,
  rangeProblem,
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

// #938 — once a render has said which colours it used, that decides the extruders.
describe('extrudersIn', () => {
  const values = defaultValues(keychainSchema)

  it("numbers each colour parameter by its colour's place in the render", () => {
    expect(extrudersIn(keychainSchema, values, ['#E8532F', '#1b6ca8'], values)).toEqual(
      new Map([
        ['body_color', 2],
        ['text_color', 1],
      ]),
    )
  })

  it('marks a colour the render never used as in no extruder (null)', () => {
    // A hard-coded colour took extruder 1; neither parameter was drawn with.
    expect(extrudersIn(keychainSchema, values, ['#3366FF'], values)).toEqual(
      new Map([
        ['body_color', null],
        ['text_color', null],
      ]),
    )
  })

  it('says nothing about a colour changed since the render, rather than "not in this render"', () => {
    // The render ran with the defaults; Text has since changed to a colour it never saw.
    const edited = { ...values, text_color: '#00FF00' }
    expect(extrudersIn(keychainSchema, edited, ['#E8532F', '#1B6CA8'], values)).toEqual(
      new Map([
        ['body_color', 2],
        ['text_color', undefined],
      ]),
    )
  })

  it('says nothing about a colour that is not hex, which the render reports resolved', () => {
    // The backend resolves CSS names; the render reports "red" as #FF0000.
    const named = { ...values, body_color: 'red' }
    expect(extrudersIn(keychainSchema, named, ['#FF0000', '#E8532F'], named)).toEqual(
      new Map([
        ['body_color', undefined],
        ['text_color', 2],
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

describe('checkParamValue (#254)', () => {
  const param = (fields: Partial<Param> & Pick<Param, 'type'>): Param => ({ name: 'p', group: '', ...fields })

  it('takes a select option in its own type, as the select widget hands it on', () => {
    const select = param({ type: 'select', options: [{ name: 'Ten', value: 10 }, { name: 'Twenty', value: 20 }] })
    expect(checkParamValue(select, '20')).toEqual({ ok: true, value: 20 })
    expect(checkParamValue(select, 30)).toEqual({ ok: false, message: '"p" is one of 10, 20.' })
  })

  it('normalises a colour the way the colour field does', () => {
    expect(checkParamValue(param({ type: 'color' }), 'f80')).toEqual({ ok: true, value: '#FF8800' })
    expect(checkParamValue(param({ type: 'color' }), 'orange').ok).toBe(false)
  })

  it('holds numbers to their type and limits', () => {
    expect(checkParamValue(param({ type: 'integer', min: 1, max: 4 }), 2.5).ok).toBe(false)
    expect(checkParamValue(param({ type: 'slider', min: 1, max: 4 }), 0).ok).toBe(false)
    expect(checkParamValue(param({ type: 'slider', min: 1, max: 4 }), 2.5)).toEqual({ ok: true, value: 2.5 })
  })

  it('reads a numeric string as the number a model meant (#948)', () => {
    // A top-level `value` in set_param can reach the tab as "30" where the same value
    // nested in set_params arrives as 30: one write must not need a retry.
    expect(checkParamValue(param({ type: 'slider', min: 5, max: 60 }), '30')).toEqual({ ok: true, value: 30 })
    expect(checkParamValue(param({ type: 'number' }), ' -2.5 ')).toEqual({ ok: true, value: -2.5 })
    expect(checkParamValue(param({ type: 'integer' }), '1e2')).toEqual({ ok: true, value: 100 })
    // Still held to the type and limits once read.
    expect(checkParamValue(param({ type: 'integer' }), '2.5').ok).toBe(false)
    expect(checkParamValue(param({ type: 'slider', min: 5, max: 60 }), '99').ok).toBe(false)
    // Anything that is not a plain decimal number is still refused.
    for (const text of ['', ' ', 'big', '3mm', '0x10', 'Infinity', 'NaN', '1,5']) {
      expect(checkParamValue(param({ type: 'number' }), text)).toEqual({ ok: false, message: '"p" takes a number.' })
    }
  })

  it('lets a file parameter take a sample or an uploaded asset, never a path', () => {
    const file = param({ type: 'file', samples: ['heart.svg'] })
    expect(checkParamValue(file, 'heart.svg').ok).toBe(true)
    expect(checkParamValue(file, 'a'.repeat(64)).ok).toBe(true)
    expect(checkParamValue(file, '').ok).toBe(true)
    expect(checkParamValue(file, '../model.scad').ok).toBe(false)
  })
})

describe('rangeProblem (#921)', () => {
  const size: Param = { group: 'Main', name: 'text_size', type: 'number', initial: 14, caption: 'Letter height', min: 8, max: 40 }

  it('names the field by its caption and gives the range', () => {
    expect(rangeProblem(size, 500)).toBe('Letter height must be between 8 and 40.')
    expect(rangeProblem(size, 2)).toBe('Letter height must be between 8 and 40.')
  })

  it('accepts the bounds themselves', () => {
    expect(rangeProblem(size, 8)).toBeNull()
    expect(rangeProblem(size, 40)).toBeNull()
  })

  it('names the one bound a half-open range has', () => {
    expect(rangeProblem({ ...size, max: null }, 2)).toBe('Letter height must be at least 8.')
    expect(rangeProblem({ ...size, min: null }, 50)).toBe('Letter height must be at most 40.')
  })

  it('falls back to the variable name, and ignores what is not a number', () => {
    expect(rangeProblem({ ...size, caption: null }, 500)).toBe('text_size must be between 8 and 40.')
    expect(rangeProblem({ ...size, type: 'string', min: null, max: null }, 'x')).toBeNull()
    expect(rangeProblem({ ...size, type: 'slider' }, 41)).not.toBeNull()
  })

  it('finds the first out-of-range value in a schema', () => {
    const values = defaultValues(keychainSchema)
    expect(outOfRange(keychainSchema, values)).toBeUndefined()
    expect(outOfRange(keychainSchema, { ...values, text_size: 500 })?.name).toBe('text_size')
  })
})

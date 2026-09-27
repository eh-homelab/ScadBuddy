import { describe, expect, it } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import { defaultValues } from './params'
import { applyPreset, presetParams } from './presets'

describe('applyPreset', () => {
  it('lays the preset over the defaults', () => {
    const { values, skipped } = applyPreset(keychainSchema, {
      id: 'x',
      name: 'Tiny',
      origin: 'mine',
      params: { text_size: 10 },
    })
    expect(values).toEqual({ ...defaultValues(keychainSchema), text_size: 10 })
    expect(skipped).toEqual([])
  })

  it('skips a value for a parameter the template no longer has', () => {
    const { values, skipped } = applyPreset(keychainSchema, {
      id: 'x',
      name: 'Old',
      origin: 'mine',
      params: { name: 'Ada', engrave_depth: 2 },
    })
    expect(values['name']).toBe('Ada')
    expect(values).not.toHaveProperty('engrave_depth')
    expect(skipped).toEqual(['engrave_depth'])
  })
})

describe('presetParams', () => {
  it('keeps only what differs from the defaults', () => {
    const values = { ...defaultValues(keychainSchema), name: 'Nova', keyring_hole: false }
    expect(presetParams(keychainSchema, values)).toEqual({ name: 'Nova', keyring_hole: false })
  })

  it('is empty at the defaults', () => {
    expect(presetParams(keychainSchema, defaultValues(keychainSchema))).toEqual({})
  })
})

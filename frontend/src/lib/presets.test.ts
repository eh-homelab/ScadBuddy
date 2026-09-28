import { describe, expect, it } from 'vitest'
import { keychainSchema } from '../mocks/fixtures'
import { defaultValues } from './params'
import {
  MAX_PRESET_TAG,
  MAX_PRESET_TAGS,
  applyPreset,
  parsePresetTags,
  presetParams,
  presetTagsProblem,
} from './presets'

describe('applyPreset', () => {
  it('lays the preset over the defaults', () => {
    const { values, skipped } = applyPreset(keychainSchema, {
      id: 'x',
      name: 'Tiny',
      origin: 'mine',
      params: { text_size: 10 },
      description: '',
      tags: [],
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
      description: '',
      tags: [],
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

describe('parsePresetTags', () => {
  it('splits on commas and cleans each tag as the server does', () => {
    expect(parsePresetTags(' gift,Gift , ,  big   tag,x')).toEqual(['gift', 'big tag', 'x'])
    expect(parsePresetTags('')).toEqual([])
  })
})

describe('presetTagsProblem', () => {
  it('names the bound a set of tags is past, or nothing', () => {
    expect(presetTagsProblem(['a', 'b'])).toBeNull()
    expect(presetTagsProblem(Array.from({ length: MAX_PRESET_TAGS + 1 }, (_, n) => `t${n}`))).toBe(
      `At most ${MAX_PRESET_TAGS} tags.`,
    )
    expect(presetTagsProblem(['t'.repeat(MAX_PRESET_TAG + 1)])).toContain(
      `longer than ${MAX_PRESET_TAG} characters`,
    )
  })
})

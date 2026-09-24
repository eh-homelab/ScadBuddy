import { describe, expect, it } from 'vitest'
import { printOptionDefaults } from '../mocks/fixtures'
import {
  effectiveScope,
  formatOption,
  isNonDefault,
  PRINT_OPTIONS,
  resolveOptions,
  type OptionLayer,
} from './printOptions'

describe('resolveOptions', () => {
  it('lets a later layer win field by field without clearing its siblings', () => {
    const resolved = resolveOptions(
      { timelapse: true, layer_inspect: true },
      { timelapse: false },
      { quantity: 2 },
    )
    expect(resolved).toEqual({ timelapse: false, layer_inspect: true, quantity: 2 })
  })

  it('treats null and undefined as "not set at this scope"', () => {
    expect(resolveOptions({ timelapse: true }, { timelapse: null }, undefined)).toEqual({
      timelapse: true,
    })
  })

  it('keeps false, which is a value and not an absence', () => {
    expect(resolveOptions({ use_ams: false })).toEqual({ use_ams: false })
  })
})

describe('effectiveScope', () => {
  const layers: OptionLayer[] = [
    { scope: 'global', options: { timelapse: true, use_ams: false } },
    { scope: 'printer', options: { timelapse: false } },
    { scope: 'model', options: {} },
    { scope: 'request', options: undefined },
  ]

  it('names the most specific layer that set the value', () => {
    expect(effectiveScope(layers, 'timelapse')).toBe('printer')
    expect(effectiveScope(layers, 'use_ams')).toBe('global')
  })

  it('is null when nothing set it, so the UI can say Bambuddy decides', () => {
    expect(effectiveScope(layers, 'quantity')).toBeNull()
  })
})

describe('isNonDefault', () => {
  it('is true only when the effective value differs from Bambuddy’s own', () => {
    expect(isNonDefault('timelapse', { timelapse: true }, printOptionDefaults)).toBe(true)
    expect(isNonDefault('timelapse', { timelapse: false }, printOptionDefaults)).toBe(false)
    expect(isNonDefault('timelapse', {}, printOptionDefaults)).toBe(false)
  })
})

describe('the option list', () => {
  it('covers every field #88 names, once each', () => {
    const names = PRINT_OPTIONS.map((spec) => spec.name)
    expect(new Set(names).size).toBe(names.length)
    expect(names).toEqual(
      expect.arrayContaining([
        'bed_levelling',
        'flow_cali',
        'vibration_cali',
        'nozzle_offset_cali',
        'layer_inspect',
        'timelapse',
        'use_ams',
        'quantity',
        'manual_start',
        'insert_at_top',
        'auto_off_after',
        'project_id',
        'preheat_override',
        'preheat_chamber_target_override',
      ]),
    )
  })

  it('has a control kind for every option and a default for all but the two nullable ones', () => {
    for (const spec of PRINT_OPTIONS) {
      expect(['boolean', 'calibration', 'preheat', 'number']).toContain(spec.kind)
    }
  })
})

describe('formatOption', () => {
  it('reads as a person would say it', () => {
    expect(formatOption('boolean', true)).toBe('On')
    expect(formatOption('boolean', false)).toBe('Off')
    expect(formatOption('calibration', 'auto')).toBe('Auto')
    expect(formatOption('number', 3)).toBe('3')
    expect(formatOption('number', null)).toBe('not set')
  })
})

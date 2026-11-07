import { describe, expect, it } from 'vitest'
import { changedCount, samePartSettings, sentPartSettings, withSetting } from './partSettings'

describe('partSettings (#5627)', () => {
  it('sends only the slots and settings that are set, in slot order', () => {
    expect(
      sentPartSettings({ '10': { wall_loops: 3 }, '2': { top_shell_layers: 8, ironing_type: null }, '1': {} }),
    ).toEqual({ '2': { top_shell_layers: 8 }, '10': { wall_loops: 3 } })
    expect(sentPartSettings({ '1': { wall_loops: null } })).toBeNull()
    expect(sentPartSettings(undefined)).toBeNull()
  })

  it('unsets a setting set back to the process value, or to nothing', () => {
    expect(withSetting({ top_shell_layers: 8 }, 'top_shell_layers', 5, 5)).toEqual({})
    expect(withSetting({}, 'wall_loops', 4, 2)).toEqual({ wall_loops: 4 })
    expect(withSetting({ wall_loops: 4 }, 'wall_loops', null, 2)).toEqual({})
    expect(changedCount({ wall_loops: 4, ironing_type: null })).toBe(1)
  })

  it('compares what two maps set, whatever their order or empty slots', () => {
    expect(
      samePartSettings({ '1': { wall_loops: 3, top_shell_layers: 8 }, '2': {} }, { '1': { top_shell_layers: 8, wall_loops: 3 } }),
    ).toBe(true)
    expect(samePartSettings({}, null)).toBe(true)
    expect(samePartSettings({ '1': { wall_loops: 3 } }, null)).toBe(false)
  })
})

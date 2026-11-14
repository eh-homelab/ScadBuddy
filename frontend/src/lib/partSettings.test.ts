import { describe, expect, it } from 'vitest'
import {
  changedCount,
  comparedBaseline,
  samePartSettings,
  sentPartSettings,
  slotBaseline,
  withSetting,
  withoutDefaults,
  withoutSeededDefaults,
} from './partSettings'

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
    // #2998: typed text is compared with the default as Bambu writes it.
    expect(withSetting({}, 'top_shell_layers', '5', 5)).toEqual({})
    expect(withSetting({}, 'skirt_loops', '2', undefined)).toEqual({ skirt_loops: '2' })
  })

  it('compares what two maps set, whatever their order or empty slots', () => {
    expect(
      samePartSettings({ '1': { wall_loops: 3, top_shell_layers: 8 }, '2': {} }, { '1': { top_shell_layers: 8, wall_loops: 3 } }),
    ).toBe(true)
    expect(samePartSettings({}, null)).toBe(true)
    expect(samePartSettings({ '1': { wall_loops: '3' } }, { '1': { wall_loops: 3 } })).toBe(true)
    expect(samePartSettings({ '1': { wall_loops: 3 } }, null)).toBe(false)
  })

  it("drops the settings equal to the slot's default: the process's, or the template's for the slot", () => {
    expect(
      withoutDefaults(
        { '1': { top_shell_layers: 5, wall_loops: 4 }, '2': { wall_loops: 2 } },
        { top_shell_layers: 5, wall_loops: 2 },
      ),
    ).toEqual({ '1': { wall_loops: 4 } })
    expect(withoutDefaults({ '1': { wall_loops: 4 } }, null)).toEqual({ '1': { wall_loops: 4 } })
    // The template sets slot 2's top shell layers to 5 over the process's 3: a 3 there is a
    // change (it prints the process's), and a 5 is none.
    const process = { top_shell_layers: 3 }
    const slots = { '2': { top_shell_layers: 5 } }
    expect(withoutDefaults({ '2': { top_shell_layers: 3 } }, process, slots)).toEqual({ '2': { top_shell_layers: 3 } })
    expect(withoutDefaults({ '2': { top_shell_layers: 5 } }, process, slots)).toEqual({})
    expect(slotBaseline('1', process, slots)).toEqual(process)
  })

  it('compares a rounded process value as no default, so choosing it is sent', () => {
    // The process's 15.5% shows as 16; 16 is still a change from what it prints.
    const process = { sparse_infill_density: 16, wall_loops: 2 }
    const inexact = ['sparse_infill_density']
    expect(comparedBaseline('1', process, null, inexact)).toEqual({ wall_loops: 2 })
    expect(withSetting({}, 'sparse_infill_density', 16, comparedBaseline('1', process, null, inexact)?.sparse_infill_density)).toEqual({
      sparse_infill_density: 16,
    })
    expect(withoutDefaults({ '1': { sparse_infill_density: 16 } }, process, null, inexact)).toEqual({
      '1': { sparse_infill_density: 16 },
    })
    // A template's own 16 on the slot is exact only when it differs from the process's.
    expect(comparedBaseline('2', process, { '2': { ...process, sparse_infill_density: 20 } }, inexact)).toEqual({
      sparse_infill_density: 20,
      wall_loops: 2,
    })
  })

  it('drops only a seeded value equal to the default, never one the user set', () => {
    const process = { top_shell_layers: 5 }
    const current = { '1': { top_shell_layers: 5 }, '2': { top_shell_layers: 5 } }
    // Slot 1's 5 was remembered; slot 2's was typed in this dialog.
    expect(withoutSeededDefaults(current, { '1': { top_shell_layers: 5 } }, process)).toEqual({
      '1': {},
      '2': { top_shell_layers: 5 },
    })
  })
})

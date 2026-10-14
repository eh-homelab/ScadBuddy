import { describe, expect, it } from 'vitest'
import type { FilamentOptions, SlicedProfile } from '../api/types'
import { slicedEstimate } from './slicedEstimate'

const profile = {
  plates: [
    { index: 1, print_seconds: 1800, weight_g: 4.5, nozzle_diameters: ['0.4'] },
    { index: 2, print_seconds: 5400, weight_g: 13.5, nozzle_diameters: ['0.4'] },
  ],
} as SlicedProfile
const filaments = { slots: [{ slot_id: 1, used_grams: 4.5 }] } as unknown as FilamentOptions

describe('slicedEstimate (#2671)', () => {
  it("is the plate's own estimate, with each slot's grams when the filaments read was of that plate", () => {
    expect(slicedEstimate(profile, 1, filaments, 1)).toMatchObject({
      print_time_seconds: 1800,
      filament_used_g: 4.5,
      slots: [{ slot_id: 1, grams: 4.5 }],
    })
  })

  it("never puts another plate's, or every plate's, grams beside this plate's total (#2691 review)", () => {
    expect(slicedEstimate(profile, 2, filaments, 1)?.slots).toEqual([])
    expect(slicedEstimate(profile, 2, filaments, 'all')).toMatchObject({ print_time_seconds: 5400, slots: [] })
  })

  it('is nothing for a plate the file holds no G-code for', () => {
    expect(slicedEstimate(profile, 3, filaments, 3)).toBeNull()
  })
})

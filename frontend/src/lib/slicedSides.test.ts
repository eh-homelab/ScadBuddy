import { describe, expect, it } from 'vitest'
import type { NozzlePlan } from '../api/types'
import { asSliced } from './slicedSides'

const plan: NozzlePlan = {
  slots: [
    { slot_id: 1, side: 'L', name: 'Inland Black', colour: '#1A1A1A', material: 'PLA', by_hand: false },
    { slot_id: 2, side: 'R', name: 'Insignia Black', colour: '#101010', material: 'PLA', by_hand: false },
    { slot_id: 3, side: 'L', name: 'Mistletoe Green', colour: '#3F8E43', material: 'PLA', by_hand: false },
  ],
  size: '0.4',
  left_flow: 'high_flow',
  right_flow: 'high_flow',
  track_switch: true,
  summary: '',
}

describe('asSliced (#2703)', () => {
  it("puts each filament on the slice's side: pixel-art-mosaic, preview-slice 37", () => {
    const sliced = asSliced(plan, {
      job_id: 37,
      status: 'completed',
      slots: [
        { slot_id: 1, grams: 60.6, side: 'R' },
        { slot_id: 2, grams: 2.6, side: 'R' },
        { slot_id: 3, grams: 5.0, side: 'L' },
      ],
    })
    expect(sliced?.slots?.map((slot) => [slot.slot_id, slot.side])).toEqual([
      [1, 'R'],
      [2, 'R'],
      [3, 'L'],
    ])
  })

  it('is null when the slice agrees, says nothing, or there is none', () => {
    const agrees = { job_id: 1, status: 'completed', slots: [{ slot_id: 1, side: 'L' as const }] }
    expect(asSliced(plan, agrees)).toBeNull()
    expect(asSliced(plan, { job_id: 1, status: 'completed', slots: [{ slot_id: 1 }] })).toBeNull()
    expect(asSliced(plan, null)).toBeNull()
    expect(asSliced(null, agrees)).toBeNull()
  })
})

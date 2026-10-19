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
      slicer: 'bambuddy',
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

  it('drops the planned summary, which names the planned sides (#2737 review)', () => {
    const sliced = asSliced(
      { ...plan, summary: 'Inland Black → left · Insignia Black → right · Mistletoe Green → left' },
      { job_id: 39, status: 'completed', slicer: 'bambuddy', slots: [{ slot_id: 1, grams: 60.6, side: 'R' }] },
    )
    expect(sliced?.summary).toBe('')
  })

  it("leaves off a slot this plate's slice does not use, whose side is only planned (#2737 review)", () => {
    // A multi-plate file: this plate's slice uses slots 1 and 2 only, and moves slot 1.
    const sliced = asSliced(plan, {
      job_id: 38,
      slicer: 'bambuddy',
      status: 'completed',
      slots: [
        { slot_id: 1, grams: 8.1, side: 'R' },
        { slot_id: 2, grams: 1.2, side: 'R' },
      ],
    })
    expect(sliced?.slots?.map((slot) => [slot.slot_id, slot.side])).toEqual([
      [1, 'R'],
      [2, 'R'],
    ])
  })

  it("leaves off a slot this plate's slice does not use when no slot moved (#3751)", () => {
    // A multi-plate file whose printed plate keeps every planned side but uses slots 1 and 3.
    const sliced = asSliced(plan, {
      job_id: 40,
      slicer: 'bambuddy',
      status: 'completed',
      slots: [
        { slot_id: 1, grams: 8.1, side: 'L' },
        { slot_id: 3, grams: 1.2, side: 'L' },
      ],
    })
    expect(sliced?.slots?.map((slot) => [slot.slot_id, slot.side])).toEqual([
      [1, 'L'],
      [3, 'L'],
    ])
  })

  it('is null when the slice agrees, says nothing, or there is none', () => {
    const agrees = {
      job_id: 1,
      status: 'completed',
      slicer: 'bambuddy' as const,
      slots: [
        { slot_id: 1, side: 'L' as const },
        { slot_id: 2, side: 'R' as const },
        { slot_id: 3, side: 'L' as const },
      ],
    }
    expect(asSliced(plan, agrees)).toBeNull()
    expect(asSliced(plan, { job_id: 1, status: 'completed', slicer: 'bambuddy', slots: [{ slot_id: 1 }] })).toBeNull()
    // Sides, but of no slot the plan has: the plan's line stays (#4391 review).
    expect(asSliced(plan, { job_id: 1, status: 'completed', slicer: 'bambuddy', slots: [{ slot_id: 9, side: 'R' }] })).toBeNull()
    expect(asSliced(plan, null)).toBeNull()
    expect(asSliced(null, agrees)).toBeNull()
  })
})

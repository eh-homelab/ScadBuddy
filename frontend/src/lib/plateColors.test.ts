import { describe, expect, it } from 'vitest'
import type { SlotNeed, SpoolOption } from '../api/types'
import { resolvedColors } from './plateColors'

const slot = (slot_id: number, colour: string | null): SlotNeed => ({ slot_id, colour, colour_matches: [] })
const spool = (spool_id: number, colour: string | null): SpoolOption => ({ spool_id, material: 'PLA', colour })

describe('resolvedColors (#1723)', () => {
  it("maps each slot's design colour to its chosen spool's", () => {
    const map = resolvedColors(
      [slot(1, '#ff0000'), slot(2, '#00FF00')],
      [spool(7, '#0000FF'), spool(8, '#ffffff')],
      [
        { slot_id: 1, spool_id: 7 },
        { slot_id: 2, spool_id: 8 },
      ],
    )
    expect([...map]).toEqual([
      ['#FF0000', '#0000FF'],
      ['#00FF00', '#FFFFFF'],
    ])
  })

  it('leaves a slot with no chosen spool, or a spool with no colour, as designed', () => {
    const map = resolvedColors([slot(1, '#FF0000'), slot(2, '#00FF00'), slot(3, null)], [spool(9, null)], [
      { slot_id: 2, spool_id: 9 },
    ])
    expect(map.size).toBe(0)
  })
})

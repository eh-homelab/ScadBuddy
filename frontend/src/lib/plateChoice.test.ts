import { describe, expect, it } from 'vitest'
import { offeredChoice, plateFields, platesSummary, singlePlate, togglePlate } from './plateChoice'

const three = [1, 2, 3]
const plates = three.map((index) => ({ index, has_thumbnail: true }))

describe('plate choice (#3652)', () => {
  it('ticks and unticks plates, and every plate ticked is all of them', () => {
    expect(togglePlate('all', 2, three)).toEqual([1, 3])
    expect(togglePlate([1, 3], 2, three)).toBe('all')
    expect(togglePlate([3], 1, three)).toEqual([1, 3])
    expect(togglePlate([1], 1, three)).toEqual([])
  })

  it('sends one plate as plate_id, several as plate_ids, every one as all_plates', () => {
    expect(plateFields('all')).toEqual({ plate_id: 1, all_plates: true })
    expect(plateFields([2])).toEqual({ plate_id: 2, all_plates: false })
    expect(plateFields([1, 3])).toEqual({ plate_id: 1, all_plates: false, plate_ids: [1, 3] })
  })

  it('keeps to the plates offered', () => {
    expect(offeredChoice('all', [])).toEqual([1])
    expect(offeredChoice('all', [1])).toEqual([1])
    expect(offeredChoice([2, 3], [1, 2])).toEqual([2])
    expect(offeredChoice([3], [1, 2])).toEqual([1])
    expect(offeredChoice([1, 2], [1, 2])).toBe('all')
    expect(offeredChoice([], three)).toEqual([])
    expect(singlePlate(offeredChoice([2], three))).toBe(2)
  })

  it('says what prints', () => {
    expect(platesSummary('all', plates)).toBe('All 3 plates')
    expect(platesSummary([1, 3], plates)).toBe('Plates 1 and 3 (2 of 3)')
    expect(platesSummary([2], plates)).toBe('Plate 2 (1 of 3)')
    expect(platesSummary([], plates)).toBe('No plate chosen')
  })
})

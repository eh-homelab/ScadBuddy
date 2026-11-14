import { describe, expect, it } from 'vitest'
import { spoolChip, spoolColour, spoolValue } from './filamentColour'

describe('spoolChip (#3358)', () => {
  it('reads a colour, keeping whether it is translucent', () => {
    expect(spoolChip({ rgba: 'F5547CFF' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: false })
    expect(spoolChip({ rgba: '#F5547C80' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: true })
    expect(spoolChip({ rgba: 'F5547C' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: false })
    // #3486 review: a leading # and surrounding space, as the removed CLEAR_RGBA took.
    expect(spoolChip({ rgba: ' #F5547C ' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: false })
  })

  it('tells a spool recorded clear from one with no readable colour', () => {
    expect(spoolChip({ rgba: '00000000' })).toEqual({ kind: 'clear' })
    expect(spoolChip({ rgba: null })).toEqual({ kind: 'unknown' })
    expect(spoolChip({ rgba: '' })).toEqual({ kind: 'unknown' })
    expect(spoolChip({ rgba: 'not-a-colour' })).toEqual({ kind: 'unknown' })
  })

  it('leaves spoolColour as it was: a colour, or none for clear and unknown alike', () => {
    expect(spoolColour({ rgba: 'F5547CFF' })).toEqual({ hex: '#F5547C', translucent: false })
    expect(spoolColour({ rgba: '00000000' })).toBeNull()
    expect(spoolColour({ rgba: null })).toBeNull()
  })
})

describe('spoolValue (owner, 2026-11-14)', () => {
  it("keeps a translucent spool's alpha, so the part draws see-through", () => {
    expect(spoolValue({ rgba: 'ffffff80' })).toBe('#FFFFFF80')
    expect(spoolValue({ rgba: ' #F5547C80 ' })).toBe('#F5547C80')
    expect(spoolValue({ rgba: 'F5547CFF' })).toBe('#F5547C')
    expect(spoolValue({ rgba: 'F5547C' })).toBe('#F5547C')
    expect(spoolValue({ rgba: '00000000' })).toBeNull()
  })
})

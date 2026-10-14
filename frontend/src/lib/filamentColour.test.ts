import { describe, expect, it } from 'vitest'
import { spoolChip, spoolColour } from './filamentColour'

describe('spoolChip (#3358)', () => {
  it('reads a colour, keeping whether it is translucent', () => {
    expect(spoolChip({ rgba: 'F5547CFF' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: false })
    expect(spoolChip({ rgba: '#F5547C80' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: true })
    expect(spoolChip({ rgba: 'F5547C' })).toEqual({ kind: 'colour', hex: '#F5547C', translucent: false })
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

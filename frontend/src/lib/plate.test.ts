import { describe, expect, it } from 'vitest'
import type { Plate, PlateFit } from '../api/types'
import { fitLabel, fitMessages } from './plate'

const H2C: Plate = {
  model: 'Bambu Lab H2C',
  name: 'H2C',
  size: [330, 320],
  height: 325,
  usable: { min_x: 25, min_y: 0, max_x: 325, max_y: 320 },
}

function fit(rest: Partial<PlateFit>): PlateFit {
  return { plate: H2C, overshoots: [], problem: null, ...rest }
}

describe('fitMessages', () => {
  it('is empty when the model fits', () => {
    expect(fitMessages(fit({}))).toEqual([])
    expect(fitLabel(fit({}))).toBeNull()
  })

  it('names the axis, the overshoot and the printer', () => {
    const over = fit({ overshoots: [{ axis: 'X', size: 312.14, limit: 300 }] })
    expect(fitMessages(over)).toEqual(['X is 12.1 mm over the H2C (312.1 of 300.0 mm)'])
    expect(fitLabel(over)).toBe('Too big on X')
  })

  it('names every axis that does not fit', () => {
    const over = fit({
      overshoots: [
        { axis: 'Y', size: 330, limit: 320 },
        { axis: 'Z', size: 400, limit: 325 },
      ],
    })
    expect(fitLabel(over)).toBe('Too big on Y, Z')
  })

  it('says the default plate rather than naming a printer', () => {
    const over = fit({
      plate: { ...H2C, model: null, name: 'Default plate' },
      overshoots: [{ axis: 'Y', size: 300, limit: 256 }],
    })
    expect(fitMessages(over)).toEqual(['Y is 44.0 mm over the default plate (300.0 of 256.0 mm)'])
  })

  it('passes on what the send would refuse a box that fits for', () => {
    const tower = fit({ problem: 'the model is 295.0 x 315.0 mm, which leaves no room for the 60 mm prime tower' })
    expect(fitMessages(tower)).toEqual([
      'the model is 295.0 x 315.0 mm, which leaves no room for the 60 mm prime tower',
    ])
    expect(fitLabel(tower)).toBe('Does not fit')
  })
})

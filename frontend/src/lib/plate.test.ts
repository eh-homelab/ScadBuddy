import { describe, expect, it } from 'vitest'
import type { Job, Plate, PlateFit } from '../api/types'
import { fitLabel, fitMessages, fitTargets, platesFitMessages, worstFit } from './plate'

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

  it('states the overshoot in the display unit', () => {
    const over = fit({ overshoots: [{ axis: 'X', size: 330.2, limit: 304.8 }] })
    expect(fitMessages(over, 'in')).toEqual(['X is 1.00 in over the H2C (13.00 of 12.00 in)'])
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

describe('multi-plate fit (#289)', () => {
  const box = (x: number, y: number, z: number): Job['bbox_mm'] => ({
    min: [0, 0, 0],
    max: [x, y, z],
    size: [x, y, z],
  })
  const done = (rest: Partial<Job>): Job =>
    ({ id: 'j', slug: 'maze', status: 'done', created_at: '', bbox_mm: box(498, 248, 8.5), ...rest }) as Job

  it('checks the one model of an ordinary render', () => {
    expect(fitTargets(done({ colors: ['#111111', '#222222'] }))).toEqual([
      { plate: null, size: [498, 248, 8.5], colours: 2 },
    ])
    expect(fitTargets(done({ plates: [] }))).toEqual([{ plate: null, size: [498, 248, 8.5], colours: 1 }])
    expect(fitTargets(undefined)).toEqual([])
    expect(fitTargets(done({ status: 'running', bbox_mm: null }))).toEqual([])
  })

  it('counts only the colours a part prints, pairing them by extruder', () => {
    // An arranged output keeps a planned slot no part uses (#428): `colors` names it,
    // `parts` does not, and a slot no part prints needs no prime tower.
    const part = (extruder: number, colour: string) => ({ name: 'wall', colour, extruder, watertight: true })
    expect(
      fitTargets(done({ colors: ['#123456', '#111111'], parts: [part(2, '#111111')] })),
    ).toEqual([{ plate: null, size: [498, 248, 8.5], colours: 1 }])
    expect(
      fitTargets(
        done({ colors: ['#111111', '#222222', '#333333'], parts: [part(1, '#111111'), part(3, '#333333')] }),
      ),
    ).toEqual([{ plate: null, size: [498, 248, 8.5], colours: 2 }])
  })

  it('checks each plate with its own box and colours, not the preview of them all', () => {
    const job = done({
      colors: ['#111111', '#222222', '#FFFFFF'],
      plates: [
        { index: 1, bbox_mm: box(244, 244, 8.5)!, colors: ['#111111', '#222222'] },
        { index: 2, bbox_mm: box(248, 248, 5.6)!, colors: ['#FFFFFF'] },
      ],
    })
    expect(fitTargets(job)).toEqual([
      { plate: 1, size: [244, 244, 8.5], colours: 2 },
      { plate: 2, size: [248, 248, 5.6], colours: 1 },
    ])
  })

  it('names the plate a problem is on, and reports the plate that does not fit', () => {
    const fits = [fit({}), fit({ overshoots: [{ axis: 'X', size: 312, limit: 300 }] })]
    const targets = [
      { plate: 1, size: [1, 1, 1], colours: 2 },
      { plate: 2, size: [1, 1, 1], colours: 1 },
    ]
    expect(platesFitMessages(fits, targets)).toEqual(['plate 2: X is 12.0 mm over the H2C (312.0 of 300.0 mm)'])
    expect(worstFit(fits)).toBe(fits[1])
    expect(worstFit([])).toBeUndefined()
  })

  it('leaves a one-plate message unprefixed', () => {
    const over = fit({ overshoots: [{ axis: 'Y', size: 330, limit: 320 }] })
    expect(platesFitMessages([over], [{ plate: null, size: [1, 1, 1], colours: 1 }])).toEqual([
      'Y is 10.0 mm over the H2C (330.0 of 320.0 mm)',
    ])
  })
})

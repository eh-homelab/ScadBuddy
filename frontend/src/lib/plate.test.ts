import { describe, expect, it } from 'vitest'
import type { BoundingBox, Plate } from '../api/types'
import { describeOvershoot, overshoots } from './plate'

const H2C: Plate = {
  model: 'Bambu Lab H2C',
  name: 'H2C',
  size: [330, 320],
  height: 325,
  usable: { min_x: 25, min_y: 0, max_x: 325, max_y: 320 },
}

function bbox(x: number, y: number, z: number): BoundingBox {
  return { min: [-x / 2, -y / 2, 0], max: [x / 2, y / 2, z], size: [x, y, z] }
}

describe('overshoots', () => {
  it('is empty when the model fits', () => {
    expect(overshoots(bbox(300, 320, 325), H2C)).toEqual([])
  })

  it('checks X and Y against where every extruder reaches, not the whole bed', () => {
    // 310 mm fits the 330 mm bed but not the 300 mm both H2C nozzles reach.
    expect(overshoots(bbox(310, 10, 5), H2C)).toEqual([{ axis: 'X', size: 310, limit: 300 }])
  })

  it('names every axis that does not fit, Z against the printable height', () => {
    expect(overshoots(bbox(10, 330, 400), H2C).map((over) => over.axis)).toEqual(['Y', 'Z'])
  })
})

describe('describeOvershoot', () => {
  it('names the axis, the overshoot and the printer', () => {
    expect(describeOvershoot({ axis: 'X', size: 312.14, limit: 300 }, H2C)).toBe(
      'X is 12.1 mm over the H2C (312.1 of 300.0 mm)',
    )
  })
})

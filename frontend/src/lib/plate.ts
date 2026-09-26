import type { BoundingBox, Plate } from '../api/types'
import { mm } from './format'

export interface Overshoot {
  axis: 'X' | 'Y' | 'Z'
  size: number
  limit: number
}

/**
 * #81 — every axis on which `bbox` does not fit `plate`. X and Y are checked against
 * where every extruder reaches rather than the bed, because that is the area the send
 * lays the model out on and refuses it for overflowing; Z against the printable height.
 */
export function overshoots(bbox: BoundingBox, plate: Plate): Overshoot[] {
  const [x, y, z] = bbox.size
  const { usable } = plate
  const limits: [Overshoot['axis'], number, number][] = [
    ['X', x, usable.max_x - usable.min_x],
    ['Y', y, usable.max_y - usable.min_y],
    ['Z', z, plate.height],
  ]
  return limits
    .filter(([, size, limit]) => size > limit)
    .map(([axis, size, limit]) => ({ axis, size, limit }))
}

export function describeOvershoot(over: Overshoot, plate: Plate): string {
  const target = plate.model ? `the ${plate.name}` : 'the default plate'
  return `${over.axis} is ${mm(over.size - over.limit)} mm over ${target} (${mm(over.size)} of ${mm(over.limit)} mm)`
}

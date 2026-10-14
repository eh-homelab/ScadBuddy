import type { BoundingBox } from '../api/types'

/**
 * #364 — the preview GLB is in OpenSCAD's coordinates (turned Y-up), not recentred, so
 * a template drawn away from the origin lands off the plate and away from its outline.
 * This is the scene translation that sits the model's box centred on the plate, on the
 * floor, which is where the outline and the camera expect it (and where the slicer
 * puts it). Scene X is model X, scene Y is model Z, scene Z is −model Y.
 */
export function sceneOffset(bbox: BoundingBox): [number, number, number] {
  const [minX, minY, minZ] = bbox.min
  const [maxX, maxY] = bbox.max
  return [-(minX + maxX) / 2, -minZ, (minY + maxY) / 2]
}

/** The size the camera frames a model by: its longest edge, never under 20 mm. */
export function framingSpan(size: BoundingBox['size']): number {
  return Math.max(size[0], size[1], size[2], 20)
}

/**
 * How far the span may move from the one framed before the camera refits. Below it the
 * view stays the viewer's, so an ordinary edit never yanks an orbit back.
 */
export const REFIT_RATIO = 1.3

/** #364 — refit on the first model, and when the size moves past `REFIT_RATIO` either way. */
export function shouldRefit(framedSpan: number | null, size: BoundingBox['size']): boolean {
  if (framedSpan === null) return true
  const ratio = framingSpan(size) / framedSpan
  return ratio > REFIT_RATIO || ratio < 1 / REFIT_RATIO
}

/** Where the camera looks, and from how far, for a model of `size` placed by `sceneOffset`. */
export function cameraFraming(size: BoundingBox['size']): {
  target: [number, number, number]
  distance: number
} {
  return { target: [0, size[2] / 2, 0], distance: framingSpan(size) * 1.9 + 40 }
}

/** The orbit's reach for an ordinary model (#364). */
export const MIN_REACH = 1200

/**
 * #2381 — how far the camera stands back for a model of `size`, and how far the orbit
 * must then reach. A multi-plate row 1.7 m long is framed from 3.3 m: an
 * orbit capped at `MIN_REACH` pulled the camera back in, and the row ran off the view. A
 * portrait view stands back as far as it is narrow, as the print dialog's does.
 */
export function cameraReach(
  size: BoundingBox['size'],
  aspect = 1,
): { target: [number, number, number]; distance: number; maxDistance: number } {
  const { target, distance } = cameraFraming(size)
  const back = distance / Math.min(1, Math.max(aspect, 0.1))
  return { target, distance: back, maxDistance: Math.max(MIN_REACH, back * 2) }
}

/**
 * #2364 — a loaded scene's box, three.js's Y-up, as the model box `sceneOffset` and the
 * framing take: for a mesh with no recorded box (a Bambuddy library file's plate, which
 * keeps the 3MF's placement on the bed), measured from the mesh itself.
 */
export function modelBox(min: [number, number, number], max: [number, number, number]): BoundingBox {
  // Scene X is model X, scene Y is model Z, scene Z is −model Y.
  return {
    min: [min[0], -max[2], min[1]],
    max: [max[0], -min[2], max[1]],
    size: [max[0] - min[0], max[2] - min[2], max[1] - min[1]],
  }
}

/**
 * #2381 — each plate's footprint on the floor of the scene, where `sceneOffset(whole)`
 * puts it: its centre (scene X, Z) and its width and depth.
 */
export function plateFootprint(
  plate: BoundingBox,
  whole: BoundingBox,
): { center: [number, number]; size: [number, number] } {
  const [dx, , dz] = sceneOffset(whole)
  return {
    center: [(plate.min[0] + plate.max[0]) / 2 + dx, -(plate.min[1] + plate.max[1]) / 2 + dz],
    size: [plate.size[0], plate.size[1]],
  }
}

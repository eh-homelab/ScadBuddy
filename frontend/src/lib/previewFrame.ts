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

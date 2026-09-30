import * as THREE from 'three'

export type Vec3 = [number, number, number]

/**
 * A camera pose for an image of the preview (#722): where it stands, what it looks at,
 * its vertical field of view in degrees, and the image's width over its height, when
 * that is not the viewer's own. The Rendered image dialog frames its own copy, so the
 * main viewer's camera never moves.
 */
export interface CameraView {
  position: Vec3
  target: Vec3
  fov: number
  aspect?: number
}

/** The main viewer's limits (`Preview.tsx`'s OrbitControls), kept by the framing too. */
export const MIN_DISTANCE = 40
export const MAX_DISTANCE = 1200
export const MAX_POLAR = Math.PI / 2 - 0.02
const MIN_POLAR = 0.01

const vec = (value: Vec3) => new THREE.Vector3(...value)
const tuple = (value: THREE.Vector3): Vec3 => [value.x, value.y, value.z]

/**
 * Turns the camera around its target, as dragging in the viewer does: a drag the
 * height of the frame is a full turn (OrbitControls' `rotateSpeed` of 1). It never
 * goes below the plate.
 */
export function orbitView(view: CameraView, dx: number, dy: number, frameHeight: number): CameraView {
  const target = vec(view.target)
  const offset = vec(view.position).sub(target)
  const spherical = new THREE.Spherical().setFromVector3(offset)
  const height = Math.max(frameHeight, 1)
  spherical.theta -= (2 * Math.PI * dx) / height
  spherical.phi = THREE.MathUtils.clamp(spherical.phi - (2 * Math.PI * dy) / height, MIN_POLAR, MAX_POLAR)
  offset.setFromSpherical(spherical)
  return { ...view, position: tuple(target.add(offset)) }
}

/**
 * Slides the camera and its target together, as a right-drag does in the viewer: the
 * point under the pointer at the target's depth follows the pointer.
 */
export function panView(view: CameraView, dx: number, dy: number, frameHeight: number): CameraView {
  const position = vec(view.position)
  const target = vec(view.target)
  const camera = new THREE.PerspectiveCamera(view.fov, view.aspect ?? 1)
  camera.position.copy(position)
  camera.lookAt(target)
  camera.updateMatrixWorld()
  const depth = position.distanceTo(target) * Math.tan(THREE.MathUtils.degToRad(view.fov / 2))
  const unit = (2 * depth) / Math.max(frameHeight, 1)
  const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0)
  const up = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 1)
  const shift = right.multiplyScalar(-dx * unit).add(up.multiplyScalar(dy * unit))
  return { ...view, position: tuple(position.add(shift)), target: tuple(target.add(shift)) }
}

/** Moves the camera toward (`factor` < 1) or away from its target, within the viewer's limits. */
export function zoomView(view: CameraView, factor: number): CameraView {
  const target = vec(view.target)
  const offset = vec(view.position).sub(target)
  const distance = THREE.MathUtils.clamp(offset.length() * factor, MIN_DISTANCE, MAX_DISTANCE)
  offset.setLength(distance)
  return { ...view, position: tuple(target.add(offset)) }
}

/** The zoom factor for a wheel turn of `deltaY` pixels: 100 px is about 10%. */
export function wheelFactor(deltaY: number): number {
  return Math.pow(0.999, -deltaY)
}

/**
 * A `width` × `height` view reshaped to `aspect`, keeping its longer edge, so the
 * view's own aspect gives back the view's own size.
 */
export function framedSize(width: number, height: number, aspect: number): { width: number; height: number } {
  const longest = Math.max(width, height)
  if (!(aspect > 0) || longest <= 0) return { width, height }
  return aspect >= 1
    ? { width: longest, height: Math.round(longest / aspect) }
    : { width: Math.round(longest * aspect), height: longest }
}

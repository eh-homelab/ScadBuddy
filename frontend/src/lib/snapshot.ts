import type * as THREE from 'three'

/** Names the viewer gives its aids, so an image of the scene can leave them out. */
export const PLATE_OBJECT = 'scadbuddy:plate'
export const BBOX_OBJECT = 'scadbuddy:bbox'

/** The longest edge an image is drawn at, whatever the GPU would allow. */
export const MAX_IMAGE_EDGE = 8192

export interface SnapshotOptions {
  /** Pixels per CSS pixel of the viewer: 2 draws the view at twice its size. */
  scale: number
  /** Draw the build plate. The bounding box outline is never drawn. */
  plate: boolean
  /** Leave the background out, for an image laid over something else. */
  transparent: boolean
}

type Renderer = Pick<
  THREE.WebGLRenderer,
  | 'domElement'
  | 'capabilities'
  | 'getPixelRatio'
  | 'setPixelRatio'
  | 'getClearAlpha'
  | 'setClearAlpha'
  | 'render'
>

/**
 * The largest scale, at most `wanted`, that keeps a `width` × `height` (CSS pixels)
 * view inside the GPU's texture limit and `MAX_IMAGE_EDGE`.
 */
export function clampScale(wanted: number, width: number, height: number, limit: number): number {
  const edge = Math.min(limit, MAX_IMAGE_EDGE)
  const longest = Math.max(width, height, 1)
  return Math.max(Math.min(wanted, edge / longest), 0.1)
}

/**
 * Draws the camera's current view once more at `scale`, without the viewer's aids, and
 * returns it as a PNG. The live canvas is resized for the one frame and put back
 * before the call returns: `toBlob` copies the bitmap when it is called, and the
 * browser paints nothing in between, so the viewer never shows the larger frame.
 */
export function captureSnapshot(
  gl: Renderer,
  scene: THREE.Scene,
  camera: THREE.Camera,
  options: SnapshotOptions,
): Promise<Blob | null> {
  const canvas = gl.domElement
  const ratio = gl.getPixelRatio()
  const scale = clampScale(
    options.scale,
    canvas.clientWidth,
    canvas.clientHeight,
    gl.capabilities.maxTextureSize,
  )

  const hidden: THREE.Object3D[] = []
  scene.traverse((object) => {
    const aid = object.name === BBOX_OBJECT || (!options.plate && object.name === PLATE_OBJECT)
    if (aid && object.visible) {
      object.visible = false
      hidden.push(object)
    }
  })
  const background = scene.background
  const clearAlpha = gl.getClearAlpha()
  if (options.transparent) {
    scene.background = null
    gl.setClearAlpha(0)
  }

  try {
    gl.setPixelRatio(scale)
    gl.render(scene, camera)
    return new Promise((resolve) => canvas.toBlob((blob) => resolve(blob), 'image/png'))
  } finally {
    for (const object of hidden) object.visible = true
    scene.background = background
    gl.setClearAlpha(clearAlpha)
    gl.setPixelRatio(ratio)
    gl.render(scene, camera)
  }
}

/** The pixel size an image of a `width` × `height` view comes out at. */
export function snapshotSize(width: number, height: number, scale: number, limit = MAX_IMAGE_EDGE) {
  const clamped = clampScale(scale, width, height, limit)
  return { width: Math.floor(width * clamped), height: Math.floor(height * clamped) }
}

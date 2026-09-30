import * as THREE from 'three'
import { describe, expect, it, vi } from 'vitest'
import { BBOX_OBJECT, captureSnapshot, clampScale, MAX_IMAGE_EDGE, PLATE_OBJECT, snapshotSize } from './snapshot'

function fakeRenderer(width = 800, height = 500) {
  const drawn: { ratio: number; plate: boolean; bbox: boolean; background: unknown; alpha: number }[] = []
  let ratio = 1.5
  let alpha = 1
  const canvas = {
    clientWidth: width,
    clientHeight: height,
    toBlob: (done: (blob: Blob | null) => void) => done(new Blob(['png'], { type: 'image/png' })),
  } as unknown as HTMLCanvasElement
  const gl = {
    domElement: canvas,
    capabilities: { maxTextureSize: 16384 },
    getPixelRatio: () => ratio,
    setPixelRatio: (value: number) => {
      ratio = value
    },
    getClearAlpha: () => alpha,
    setClearAlpha: (value: number) => {
      alpha = value
    },
    render: vi.fn((scene: THREE.Scene) => {
      drawn.push({
        ratio,
        plate: scene.getObjectByName(PLATE_OBJECT)!.visible,
        bbox: scene.getObjectByName(BBOX_OBJECT)!.visible,
        background: scene.background,
        alpha,
      })
    }),
  }
  return { gl: gl as unknown as THREE.WebGLRenderer, drawn }
}

function scene() {
  const root = new THREE.Scene()
  root.background = new THREE.Color('#0b0e13')
  const plate = new THREE.Group()
  plate.name = PLATE_OBJECT
  const bbox = new THREE.Object3D()
  bbox.name = BBOX_OBJECT
  root.add(plate, bbox)
  return root
}

describe('captureSnapshot', () => {
  it('draws one larger frame without the aids, then puts the view back', async () => {
    const { gl, drawn } = fakeRenderer()
    const root = scene()
    const background = root.background

    const blob = await captureSnapshot(gl, root, new THREE.PerspectiveCamera(), {
      scale: 3,
      plate: false,
      transparent: true,
    })

    expect(blob?.type).toBe('image/png')
    expect(drawn[0]).toEqual({ ratio: 3, plate: false, bbox: false, background: null, alpha: 0 })
    expect(drawn[1]).toEqual({ ratio: 1.5, plate: true, bbox: true, background, alpha: 1 })
  })

  it('keeps the plate and the background when asked to', async () => {
    const { gl, drawn } = fakeRenderer()
    const root = scene()
    await captureSnapshot(gl, root, new THREE.PerspectiveCamera(), {
      scale: 2,
      plate: true,
      transparent: false,
    })
    expect(drawn[0]).toMatchObject({ plate: true, bbox: false, background: root.background, alpha: 1 })
  })

  it("draws a framed view from a copy of the camera, at the framing's shape (#722)", async () => {
    const { gl } = fakeRenderer(800, 500)
    let size = { x: 800, y: 500 }
    const sizes: { x: number; y: number }[] = []
    Object.assign(gl, {
      getSize: (target: THREE.Vector2) => target.set(size.x, size.y),
      setSize: (x: number, y: number, style?: boolean) => {
        expect(style).toBe(false)
        size = { x, y }
        sizes.push(size)
      },
    })
    const cameras: THREE.Camera[] = []
    ;(gl.render as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (_scene: THREE.Scene, camera: THREE.Camera) => cameras.push(camera),
    )
    const camera = new THREE.PerspectiveCamera(35, 1.6)
    camera.position.set(210, 170, 230)

    await captureSnapshot(gl, scene(), camera, {
      scale: 2,
      plate: true,
      transparent: false,
      view: { position: [0, 50, 300], target: [0, 20, 0], fov: 30, aspect: 1 },
    })

    // Square: the view's longer edge both ways, then the viewer's own size again.
    expect(sizes).toEqual([
      { x: 800, y: 800 },
      { x: 800, y: 500 },
    ])
    const shot = cameras[0] as THREE.PerspectiveCamera
    expect(shot).not.toBe(camera)
    expect(shot.position.toArray()).toEqual([0, 50, 300])
    expect(shot.aspect).toBe(1)
    expect(shot.fov).toBe(30)
    // The viewer's camera did not move, and drew the frame that put the view back.
    expect(camera.position.toArray()).toEqual([210, 170, 230])
    expect(cameras[1]).toBe(camera)
  })

  it("keeps the viewer's own shape for a framed view without an aspect", async () => {
    const { gl, drawn } = fakeRenderer(800, 500)
    const setSize = vi.fn()
    Object.assign(gl, { getSize: vi.fn(), setSize })
    await captureSnapshot(gl, scene(), new THREE.PerspectiveCamera(35, 1.6), {
      scale: 2,
      plate: true,
      transparent: false,
      view: { position: [0, 50, 300], target: [0, 20, 0], fov: 35 },
    })
    expect(setSize).not.toHaveBeenCalled()
    expect(drawn[0]?.ratio).toBe(2)
  })
})

describe('clampScale', () => {
  it('stays inside the GPU limit and the longest edge', () => {
    expect(clampScale(4, 1000, 600, 16384)).toBe(4)
    expect(clampScale(4, 3000, 600, 16384)).toBeCloseTo(MAX_IMAGE_EDGE / 3000)
    expect(clampScale(4, 3000, 600, 4096)).toBeCloseTo(4096 / 3000)
  })

  it('sizes the image it will draw', () => {
    expect(snapshotSize(800, 500, 2)).toEqual({ width: 1600, height: 1000 })
  })
})

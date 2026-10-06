import { act, render, screen } from '@testing-library/react'
import * as THREE from 'three'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BoundingBox, Job } from '../api/types'
import { BBOX_OBJECT } from '../lib/snapshot'
import { Preview } from './Preview'

// #364 — the scene's own parts, rendered as plain elements: jsdom has no WebGL, so the
// Canvas mounts its children into the DOM, where their props can be read back.
const scene = vi.hoisted(() => ({
  camera: null as unknown as THREE.PerspectiveCamera,
  controls: null as unknown as { target: THREE.Vector3; update: () => void },
  pending: new Map<string, { promise: Promise<void>; resolve: () => void }>(),
}))

vi.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => <div data-testid="scene">{children}</div>,
  useLoader: Object.assign(
    (_loader: unknown, url: string) => {
      const pending = scene.pending.get(url)
      if (pending) throw pending.promise
      return { scene: { clone: () => ({ url }) } }
    },
    { clear: () => {} },
  ),
  useThree: (select: (state: unknown) => unknown) =>
    select({ camera: scene.camera, controls: scene.controls, invalidate: () => {} }),
}))
vi.mock('@react-three/drei', () => ({ Grid: () => null, OrbitControls: () => null }))

function box(min: [number, number, number], max: [number, number, number]): BoundingBox {
  return { min, max, size: [max[0] - min[0], max[1] - min[1], max[2] - min[2]] }
}

function job(id: string, bbox: BoundingBox, overrides: Partial<Job> = {}): Job {
  return {
    id: id.repeat(32),
    slug: 'storage-box',
    status: 'done',
    created_at: '2026-09-27T10:00:00Z',
    preview_url: `/api/v1/jobs/${id}/preview.glb`,
    bbox_mm: bbox,
    colors: ['#FF0000'],
    log_tail: [],
    ...overrides,
  }
}

function suspend(url: string) {
  let resolve = () => {}
  const promise = new Promise<void>((done) => (resolve = done))
  scene.pending.set(url, { promise, resolve })
  return () => {
    scene.pending.delete(url)
    resolve()
  }
}

const outline = () => screen.getByTestId('scene').querySelector(`[name="${BBOX_OBJECT}"]`)
const modelPosition = () =>
  screen.getByTestId('scene').querySelector('primitive')?.parentElement?.getAttribute('position')

describe('Preview scene (#364)', () => {
  beforeEach(() => {
    // The scene's three.js tags are not HTML: React says so, and it is not the point here.
    vi.spyOn(console, 'error').mockImplementation(() => {})
    scene.camera = new THREE.PerspectiveCamera()
    scene.controls = { target: new THREE.Vector3(), update: vi.fn() }
  })
  afterEach(() => {
    scene.pending.clear()
    vi.restoreAllMocks()
  })

  it('refits the camera when a preset jumps the model to a much bigger size', () => {
    const { rerender } = render(<Preview job={job('a', box([-40, -40, 0], [40, 40, 80]))} rendering={false} />)
    const first = scene.camera.position.length()
    expect(first).toBeGreaterThan(0)

    rerender(<Preview job={job('b', box([-129, -60, 0], [129, 60, 60]))} rendering={false} />)
    expect(scene.camera.position.length()).toBeGreaterThan(first * 2)
  })

  it("keeps the viewer's orbit through an ordinary small edit", () => {
    const { rerender } = render(<Preview job={job('a', box([-40, -40, 0], [40, 40, 80]))} rendering={false} />)
    // The viewer orbits somewhere else.
    scene.camera.position.set(5, 300, 5)

    rerender(<Preview job={job('b', box([-42, -40, 0], [42, 40, 82]))} rendering={false} />)
    expect(scene.camera.position.toArray()).toEqual([5, 300, 5])
  })

  it('draws a model OpenSCAD placed off the origin inside its outline, on the plate centre', () => {
    // A swatch drawn from the origin out, as `cube([80, 40, 2])` is.
    render(<Preview job={job('a', box([0, 0, 0], [80, 40, 2]))} rendering={false} />)
    // Scene X is model X, scene Z is −model Y: moved back by half of each.
    expect(modelPosition()).toBe('-40,0,20')
    expect(outline()?.getAttribute('position')).toBe('0,1,0')
  })

  it('takes the outline down with a failed render', () => {
    const { rerender } = render(<Preview job={job('a', box([-5, -5, 0], [5, 5, 5]))} rendering={false} />)
    expect(outline()).not.toBeNull()

    rerender(
      <Preview
        job={job('b', box([-5, -5, 0], [5, 5, 5]), { status: 'failed', preview_url: null, log_tail: ['ERROR: boom'] })}
        rendering={false}
      />,
    )
    expect(screen.getByTestId('render-log')).toBeInTheDocument()
    expect(outline()).toBeNull()
  })

  it('keeps the spinner until the GLB is drawn', async () => {
    const loaded = suspend('/api/v1/jobs/a/preview.glb')
    render(<Preview job={job('a', box([-5, -5, 0], [5, 5, 5]))} rendering={false} />)

    expect(screen.getByText('Rendering')).toBeInTheDocument()

    await act(async () => loaded())
    expect(screen.queryByText('Rendering')).not.toBeInTheDocument()
  })
})

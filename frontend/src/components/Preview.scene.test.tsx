import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
  orbit: {} as { maxDistance?: number },
  /** #2364 — a mesh loaded from a URL, where one is wanted; else a stand-in. */
  meshes: new Map<string, THREE.Object3D>(),
}))

vi.mock('@react-three/fiber', () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => <div data-testid="scene">{children}</div>,
  useLoader: Object.assign(
    (_loader: unknown, url: string) => {
      const pending = scene.pending.get(url)
      if (pending) throw pending.promise
      return { scene: scene.meshes.get(url) ?? { clone: () => ({ url }) } }
    },
    { clear: () => {} },
  ),
  useThree: (select: (state: unknown) => unknown) =>
    select({ camera: scene.camera, controls: scene.controls, invalidate: () => {} }),
}))
vi.mock('@react-three/drei', () => ({
  Grid: () => null,
  Html: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  OrbitControls: (props: { maxDistance?: number }) => {
    scene.orbit = props
    return null
  },
}))

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
    scene.meshes.clear()
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

  it('frames a library file’s plate placed off the bed centre, which came with no box (#2364)', () => {
    // Bambuddy's plate keeps the 3MF's placement: an 80 x 40 x 10 mm part at (128, 128).
    const placed = new THREE.Group()
    const part = new THREE.Mesh(new THREE.BoxGeometry(80, 10, 40))
    part.position.set(128, 5, 128)
    placed.add(part)
    const url = '/api/v1/print/library/1630/preview.glb?plate=1'
    scene.meshes.set(url, placed)

    render(<Preview job={job('a', box([0, 0, 0], [1, 1, 1]), { preview_url: url, bbox_mm: null })} rendering={false} />)

    // Moved onto the plate centre, as a box from the render would have put it.
    expect(modelPosition()).toBe('-128,0,-128')
    // Framed for its 80 mm, not left where the default camera looks.
    expect(scene.controls.target.y).toBeCloseTo(5)
    expect(scene.camera.position.length()).toBeCloseTo(80 * 1.9 + 40)
    // And its measured size is shown, as a rendered model's is (#2866).
    expect(screen.getByTestId('bbox-readout')).toHaveTextContent(/80.*40.*10/)
  })

  it('frames every plate of a long multi-plate row and outlines each by number (#2381)', () => {
    // coaster-set at 12 x 150 mm: six plates of two in one 1.7 m row.
    const plates = Array.from({ length: 6 }, (_, i) => ({
      index: i + 1,
      bbox_mm: box([-850 + i * 290, -152, 0], [-850 + i * 290 + 270, 152, 5]),
      colors: ['#FF0000'],
    }))
    render(
      <Preview
        job={job('a', box([-850, -152, 0], [850, 152, 5]), { plates })}
        rendering={false}
        plate={{ name: 'H2C', model: 'H2C', size: [330, 320], height: 325, usable: { min_x: 0, min_y: 0, max_x: 330, max_y: 320 } }}
      />,
    )

    const distance = scene.camera.position.length()
    expect(distance).toBeCloseTo(1700 * 1.9 + 40)
    // The orbit reaches as far as the framing: the old 1200 mm cap pulled it back in.
    expect(scene.orbit.maxDistance).toBeGreaterThanOrEqual(distance)
    expect(screen.getAllByTestId('plate-label').map((label) => label.textContent)).toEqual([
      'Plate 1',
      'Plate 2',
      'Plate 3',
      'Plate 4',
      'Plate 5',
      'Plate 6',
    ])
    // Each outline under its own plate's parts, along the row.
    const outlines = [...screen.getByTestId('scene').querySelectorAll('[name^="scadbuddy:plate:"]')]
    expect(outlines.map((group) => group.getAttribute('position'))).toEqual(
      plates.map((_, i) => `${-850 + i * 290 + 135},0.05,0`),
    )
  })

  it('shows one plate of a multi-plate render alone, then all of them again (#2731)', async () => {
    const plates = [1, 2, 3].map((index) => ({
      index,
      bbox_mm: box([-450 + (index - 1) * 310, -100, 0], [-450 + (index - 1) * 310 + 280, 100, 5]),
      colors: ['#FF0000'],
    }))
    const alone = new THREE.Group()
    alone.add(new THREE.Mesh(new THREE.BoxGeometry(280, 5, 200)))
    scene.meshes.set('/api/v1/jobs/a/preview.glb?plate=2', alone)
    const user = userEvent.setup()
    const { rerender } = render(<Preview job={job('a', box([-450, -100, 0], [450, 100, 5]), { plates })} rendering={false} />)
    const picker = screen.getByRole('group', { name: 'Plates shown' })
    expect(screen.getByRole('button', { name: 'All plates' })).toHaveAttribute('aria-pressed', 'true')
    const all = scene.camera.position.length()

    await user.click(screen.getByRole('button', { name: 'Plate 2 alone' }))

    expect(screen.getByRole('button', { name: 'Plate 2 alone' })).toHaveAttribute('aria-pressed', 'true')
    // Plate 2's own mesh (the only one with a 280 mm box), framed, with no outlines.
    expect(scene.camera.position.length()).toBeLessThan(all / 2)
    expect(screen.queryAllByTestId('plate-label')).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'All plates' }))
    expect(screen.getAllByTestId('plate-label')).toHaveLength(3)

    // A new render shows every plate again, whatever was chosen for the last one.
    await user.click(screen.getByRole('button', { name: 'Plate 2 alone' }))
    rerender(<Preview job={job('b', box([-450, -100, 0], [450, 100, 5]), { plates })} rendering={false} />)
    expect(screen.getByRole('button', { name: 'All plates' })).toHaveAttribute('aria-pressed', 'true')
    expect(picker).toBeInTheDocument()
  })

  it('offers no plate picker for a one-plate render', () => {
    render(<Preview job={job('a', box([-5, -5, 0], [5, 5, 5]))} rendering={false} />)
    expect(screen.queryByRole('group', { name: 'Plates shown' })).not.toBeInTheDocument()
  })
})

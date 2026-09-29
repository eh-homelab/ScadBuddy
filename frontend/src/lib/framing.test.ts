import { describe, expect, it } from 'vitest'
import {
  MAX_DISTANCE,
  MAX_POLAR,
  MIN_DISTANCE,
  framedSize,
  orbitView,
  panView,
  wheelFactor,
  zoomView,
  type CameraView,
  type Vec3,
} from './framing'

const VIEW: CameraView = { position: [0, 100, 200], target: [0, 20, 0], fov: 35, aspect: 1.6 }

const distance = (a: Vec3, b: Vec3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])

describe('framing (#722)', () => {
  it('orbits around the target at the same distance', () => {
    const turned = orbitView(VIEW, 100, 0, 500)
    expect(turned.target).toEqual(VIEW.target)
    expect(distance(turned.position, turned.target)).toBeCloseTo(distance(VIEW.position, VIEW.target))
    expect(turned.position[0]).not.toBeCloseTo(VIEW.position[0])
    // Sideways only: the height is kept.
    expect(turned.position[1]).toBeCloseTo(VIEW.position[1])
  })

  it('never orbits below the plate', () => {
    const low = orbitView(VIEW, 0, -5000, 500)
    const offset = low.position.map((value, index) => value - low.target[index]!)
    const polar = Math.acos(offset[1]! / Math.hypot(...offset))
    expect(polar).toBeLessThanOrEqual(MAX_POLAR + 1e-9)
  })

  it('pans the camera and the target together', () => {
    const slid = panView(VIEW, 50, 0, 500)
    const moved = slid.target.map((value, index) => value - VIEW.target[index]!)
    const cameraMoved = slid.position.map((value, index) => value - VIEW.position[index]!)
    moved.forEach((value, index) => expect(cameraMoved[index]).toBeCloseTo(value))
    // A drag to the right moves the scene right, so the camera goes left (-x here).
    expect(moved[0]).toBeLessThan(0)
    expect(distance(slid.position, slid.target)).toBeCloseTo(distance(VIEW.position, VIEW.target))
  })

  it("zooms toward the target within the viewer's limits", () => {
    const before = distance(VIEW.position, VIEW.target)
    expect(distance(zoomView(VIEW, 0.5).position, VIEW.target)).toBeCloseTo(before / 2)
    expect(distance(zoomView(VIEW, 0.001).position, VIEW.target)).toBeCloseTo(MIN_DISTANCE)
    expect(distance(zoomView(VIEW, 1000).position, VIEW.target)).toBeCloseTo(MAX_DISTANCE)
    expect(wheelFactor(100)).toBeGreaterThan(1)
    expect(wheelFactor(-100)).toBeLessThan(1)
  })

  it("reshapes a view's size to an aspect, keeping its longer edge", () => {
    expect(framedSize(800, 500, 800 / 500)).toEqual({ width: 800, height: 500 })
    expect(framedSize(800, 500, 1)).toEqual({ width: 800, height: 800 })
    expect(framedSize(800, 500, 16 / 9)).toEqual({ width: 800, height: 450 })
    expect(framedSize(800, 500, 0.5)).toEqual({ width: 400, height: 800 })
  })
})

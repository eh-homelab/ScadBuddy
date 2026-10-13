import { describe, expect, it } from 'vitest'
import type { BoundingBox } from '../api/types'
import { cameraFraming, cameraReach, MIN_REACH, modelBox, plateFootprint, sceneOffset, shouldRefit } from './previewFrame'

describe('sceneOffset (#364)', () => {
  it('moves a model drawn away from the origin onto the plate centre, on the floor', () => {
    // backpack-tag-pair's GLB: x −13.6…187.5 and OpenSCAD z down to −50.4 are both off
    // the origin the outline and the camera are centred on.
    const offset = sceneOffset({ min: [-13.6, 13.2, -2], max: [187.5, 50.4, 3], size: [201.1, 37.2, 5] })
    // Scene X is model X, scene Y is model Z (up), scene Z is −model Y.
    expect(offset[0]).toBeCloseTo(-(187.5 - 13.6) / 2)
    expect(offset[1]).toBeCloseTo(2)
    expect(offset[2]).toBeCloseTo((13.2 + 50.4) / 2)
  })

  it('leaves a model OpenSCAD already centred where it is', () => {
    const offset = sceneOffset({ min: [-40, -20, 0], max: [40, 20, 2], size: [80, 40, 2] })
    expect(offset.map((v) => v + 0)).toEqual([0, 0, 0])
  })
})

describe('shouldRefit (#364)', () => {
  it('frames the first model', () => {
    expect(shouldRefit(null, [80, 80, 80])).toBe(true)
  })

  it('refits when a preset grows the model past the framed view', () => {
    expect(shouldRefit(80, [258, 120, 60])).toBe(true)
  })

  it('refits when the model shrinks well inside the view', () => {
    expect(shouldRefit(258, [80, 80, 40])).toBe(true)
  })

  it("leaves a small edit to the viewer's own orbit", () => {
    expect(shouldRefit(80, [84, 80, 40])).toBe(false)
    expect(shouldRefit(80, [76, 70, 40])).toBe(false)
  })
})

describe('cameraFraming', () => {
  it('aims at the middle of the model, which sits centred on the plate', () => {
    const { target, distance } = cameraFraming([80, 40, 30])
    expect(target).toEqual([0, 15, 0])
    expect(distance).toBeCloseTo(80 * 1.9 + 40)
  })
})

describe('cameraReach (#2381)', () => {
  it('lets the orbit reach as far back as a long multi-plate row is framed from', () => {
    const { distance, maxDistance } = cameraReach([1700, 305, 5])
    expect(distance).toBeGreaterThan(MIN_REACH)
    expect(maxDistance).toBeGreaterThanOrEqual(distance)
  })

  it('keeps the ordinary reach for an ordinary model', () => {
    expect(cameraReach([80, 40, 20]).maxDistance).toBe(MIN_REACH)
  })

  it('stands back as far as a portrait view is narrow', () => {
    expect(cameraReach([80, 40, 20], 0.5).distance).toBeCloseTo(cameraFraming([80, 40, 20]).distance * 2)
  })

  it('stands back at most 10 times as far, however narrow the view (#2867)', () => {
    const square = cameraFraming([80, 40, 20]).distance
    expect(cameraReach([80, 40, 20], 0.1).distance).toBeCloseTo(square * 10)
    // Narrower than 0.1 is clamped there, not backed off without end.
    expect(cameraReach([80, 40, 20], 0.01).distance).toBeCloseTo(square * 10)
    expect(cameraReach([80, 40, 20], 0).distance).toBeCloseTo(square * 10)
  })
})

describe('modelBox (#2364)', () => {
  it('reads a Y-up scene box as the model box that centres it on the plate', () => {
    // A library plate's part at scene (88..168, 0..10, 108..148).
    const bbox = modelBox([88, 0, 108], [168, 10, 148])
    expect(bbox.size).toEqual([80, 40, 10])
    expect(sceneOffset(bbox).map((v) => v + 0)).toEqual([-128, 0, -128])
  })
})

describe('plateFootprint (#2381)', () => {
  it('puts a plate where the whole render is moved to', () => {
    const whole: BoundingBox = { min: [0, 0, 0], max: [600, 100, 5], size: [600, 100, 5] }
    const second: BoundingBox = { min: [320, 0, 0], max: [600, 100, 5], size: [280, 100, 5] }
    expect(plateFootprint(second, whole)).toEqual({ center: [160, 0], size: [280, 100] })
  })
})

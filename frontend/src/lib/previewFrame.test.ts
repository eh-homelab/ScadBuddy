import { describe, expect, it } from 'vitest'
import { cameraFraming, sceneOffset, shouldRefit } from './previewFrame'

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

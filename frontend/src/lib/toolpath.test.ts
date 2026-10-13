import { describe, expect, it } from 'vitest'
import { demoToolpath, encodeToolpath } from '../mocks/toolpath'
import { PRINT_MOVE, TRAVEL_MOVE, decodeToolpath, drawnCount, featureColour, speedColour, toolpathLines } from './toolpath'

describe('toolpath (#2072)', () => {
  it('decodes the header and the float32 segments after it', () => {
    const path = decodeToolpath(demoToolpath())
    expect(path.header.features).toEqual(['Outer wall', 'Sparse infill'])
    expect(path.header.segment_count).toBe(6)
    expect(Array.from(path.segments.subarray(8, 16))).toEqual([10, 10, 0.2, 30, 10, 0.2, 50, 1].map(Math.fround))
  })

  it('decodes a header whose length is not a multiple of 4 as padded', () => {
    const buffer = encodeToolpath({ features: ['x'], layers: [], max_speed: 1, truncated: true }, [[1, 2, 3, 4, 5, 6, 7, 1]])
    const path = decodeToolpath(buffer)
    expect(path.header.truncated).toBe(true)
    expect(path.segments[0]).toBe(1)
  })

  it('draws travel only in Movement, in its own colour, in a Y-up frame', () => {
    const path = decodeToolpath(demoToolpath())
    const movement = toolpathLines(path, 'movement', null)
    expect(movement.count).toBe(6)
    // G-code (x, y, z) is three.js (x, z, -y).
    expect(Array.from(movement.positions.subarray(0, 6))).toEqual([0, 0.2, -0, 10, 0.2, -10].map(Math.fround))
    expect(Array.from(movement.colours.subarray(0, 3))).toEqual(TRAVEL_MOVE.map(Math.fround))
    expect(Array.from(movement.colours.subarray(6, 9))).toEqual(PRINT_MOVE.map(Math.fround))
    expect(toolpathLines(path, 'speed', null).count).toBe(5)
  })

  it('colours Speed from slow blue to fast red against the top speed', () => {
    expect(speedColour(0, 200)).toEqual([0.16, 0.35, 0.95])
    expect(speedColour(200, 200)).toEqual([0.95, 0.2, 0.15])
    expect(speedColour(900, 200)).toEqual([0.95, 0.2, 0.15])
    const path = decodeToolpath(demoToolpath())
    const speed = toolpathLines(path, 'speed', null)
    // The infill (200 mm/s) is the fastest: red.
    expect(Array.from(speed.colours.subarray(12, 15))).toEqual([0.95, 0.2, 0.15].map(Math.fround))
  })

  it('draws Layers by feature up to the layer chosen', () => {
    const path = decodeToolpath(demoToolpath())
    expect(drawnCount(path, 0)).toBe(4)
    expect(drawnCount(path, 1)).toBe(6)
    const first = toolpathLines(path, 'layers', 0)
    expect(first.count).toBe(3)
    expect(Array.from(first.colours.subarray(12, 15))).toEqual(featureColour('Sparse infill', 1).map(Math.fround))
  })
})

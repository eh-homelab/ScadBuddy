import { describe, expect, it } from 'vitest'
import { buildGlb, hexToLinearRgb, keychainGlb } from './glb'

function readChunks(glb: Uint8Array) {
  const view = new DataView(glb.buffer, glb.byteOffset, glb.byteLength)
  const jsonLength = view.getUint32(12, true)
  const json = JSON.parse(new TextDecoder().decode(glb.subarray(20, 20 + jsonLength)))
  const binLength = view.getUint32(20 + jsonLength, true)
  return { magic: view.getUint32(0, true), total: view.getUint32(8, true), json, binLength }
}

describe('buildGlb', () => {
  it('writes a valid GLB container', () => {
    const glb = buildGlb([{ color: '#ff0000', min: [0, 0, 0], max: [10, 10, 10] }])
    const { magic, total, binLength } = readChunks(glb)

    expect(magic).toBe(0x46546c67) // 'glTF'
    expect(total).toBe(glb.byteLength)
    expect(binLength).toBeGreaterThan(0)
    expect(glb.byteLength % 4).toBe(0)
  })

  it('emits one primitive and one material per colour', () => {
    const glb = buildGlb([
      { color: '#1B6CA8', min: [-5, -5, 0], max: [5, 5, 2] },
      { color: '#E8532F', min: [-3, -2, 2], max: [3, 2, 4] },
    ])
    const { json } = readChunks(glb)

    expect(json.meshes[0].primitives).toHaveLength(2)
    expect(json.materials).toHaveLength(2)
    expect(json.materials.map((m: { name: string }) => m.name)).toEqual([
      'extruder-1',
      'extruder-2',
    ])
    expect(json.meshes[0].primitives[1].material).toBe(1)
  })

  it('records the bounding box on the position accessors', () => {
    const glb = keychainGlb(['#1B6CA8', '#E8532F'], { x: 95.7, y: 34.6, z: 6.8 })
    const { json } = readChunks(glb)

    const plate = json.accessors[0]
    expect(plate.type).toBe('VEC3')
    expect(plate.count).toBe(8)
    expect(plate.max[0] - plate.min[0]).toBeCloseTo(95.7, 5)
    expect(json.accessors[1].max[2]).toBeCloseTo(6.8, 5)
  })
})

describe('hexToLinearRgb', () => {
  it('converts sRGB to linear', () => {
    expect(hexToLinearRgb('#000000')).toEqual([0, 0, 0])
    expect(hexToLinearRgb('#ffffff')).toEqual([1, 1, 1])
    const [r] = hexToLinearRgb('#808080')
    expect(r).toBeCloseTo(0.2158, 3)
  })

  it('expands three-digit hex', () => {
    expect(hexToLinearRgb('#f00')).toEqual(hexToLinearRgb('#ff0000'))
  })
})

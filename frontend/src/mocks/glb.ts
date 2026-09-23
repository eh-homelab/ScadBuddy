/**
 * Minimal glTF 2.0 binary writer used by the mock API and by tests.
 *
 * The real backend writes one mesh primitive per colour (spec §6); this builds
 * the same shape out of axis-aligned boxes so the viewer, the bounding-box
 * overlay and the colour strip all have realistic data to render.
 */

export interface GlbPart {
  /** `#rrggbb` */
  color: string
  /** millimetres, z-up, matching OpenSCAD's coordinate system */
  min: [number, number, number]
  max: [number, number, number]
}

const BOX_INDICES = [
  0, 1, 2, 0, 2, 3, // -z
  4, 6, 5, 4, 7, 6, // +z
  0, 4, 5, 0, 5, 1, // -y
  1, 5, 6, 1, 6, 2, // +x
  2, 6, 7, 2, 7, 3, // +y
  3, 7, 4, 3, 4, 0, // -x
]

function boxPositions(min: [number, number, number], max: [number, number, number]): number[] {
  const [x0, y0, z0] = min
  const [x1, y1, z1] = max
  return [
    x0, y0, z0, x1, y0, z0, x1, y1, z0, x0, y1, z0,
    x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1,
  ]
}

function srgbToLinear(channel: number): number {
  return channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4)
}

export function hexToLinearRgb(hex: string): [number, number, number] {
  const clean = hex.replace('#', '')
  const full = clean.length === 3 ? [...clean].map((c) => c + c).join('') : clean
  const int = Number.parseInt(full.padEnd(6, '0').slice(0, 6), 16)
  return [
    srgbToLinear(((int >> 16) & 0xff) / 255),
    srgbToLinear(((int >> 8) & 0xff) / 255),
    srgbToLinear((int & 0xff) / 255),
  ]
}

function align4(n: number): number {
  return (4 - (n % 4)) % 4
}

/** Builds a GLB whose primitives are one box per part, each with its own material. */
export function buildGlb(parts: GlbPart[]): Uint8Array {
  const positionBlocks = parts.map((p) => new Float32Array(boxPositions(p.min, p.max)))
  const indexBlock = new Uint16Array(BOX_INDICES)

  const bufferViews: Record<string, number>[] = []
  const accessors: Record<string, unknown>[] = []
  const chunks: Uint8Array[] = []
  let offset = 0

  for (const positions of positionBlocks) {
    const bytes = new Uint8Array(positions.buffer.slice(0))
    bufferViews.push({ buffer: 0, byteOffset: offset, byteLength: bytes.byteLength, target: 34962 })
    chunks.push(bytes)
    offset += bytes.byteLength
  }

  // Every part reuses the same index block; only positions differ.
  const indexBytes = new Uint8Array(indexBlock.buffer.slice(0))
  const indexViewIndex = bufferViews.length
  bufferViews.push({
    buffer: 0,
    byteOffset: offset,
    byteLength: indexBytes.byteLength,
    target: 34963,
  })
  chunks.push(indexBytes)
  offset += indexBytes.byteLength
  const indexPad = align4(offset)
  if (indexPad) {
    chunks.push(new Uint8Array(indexPad))
    offset += indexPad
  }

  parts.forEach((part, i) => {
    accessors.push({
      bufferView: i,
      componentType: 5126, // FLOAT
      count: 8,
      type: 'VEC3',
      min: part.min,
      max: part.max,
    })
  })
  const indexAccessorIndex = accessors.length
  accessors.push({
    bufferView: indexViewIndex,
    componentType: 5123, // UNSIGNED_SHORT
    count: BOX_INDICES.length,
    type: 'SCALAR',
  })

  const gltf = {
    asset: { version: '2.0', generator: 'scadbuddy-mock' },
    scene: 0,
    scenes: [{ nodes: [0] }],
    nodes: [{ name: 'model', mesh: 0 }],
    meshes: [
      {
        name: 'model',
        primitives: parts.map((_, i) => ({
          attributes: { POSITION: i },
          indices: indexAccessorIndex,
          material: i,
        })),
      },
    ],
    materials: parts.map((part, i) => {
      const [r, g, b] = hexToLinearRgb(part.color)
      return {
        name: `extruder-${i + 1}`,
        pbrMetallicRoughness: {
          baseColorFactor: [r, g, b, 1],
          metallicFactor: 0.05,
          roughnessFactor: 0.7,
        },
      }
    }),
    accessors,
    bufferViews,
    buffers: [{ byteLength: offset }],
  }

  const jsonBytes = new TextEncoder().encode(JSON.stringify(gltf))
  const jsonPad = align4(jsonBytes.byteLength)
  const jsonLength = jsonBytes.byteLength + jsonPad

  const binBytes = new Uint8Array(offset)
  let cursor = 0
  for (const chunk of chunks) {
    binBytes.set(chunk, cursor)
    cursor += chunk.byteLength
  }

  const total = 12 + 8 + jsonLength + 8 + offset
  const out = new Uint8Array(total)
  const view = new DataView(out.buffer)

  view.setUint32(0, 0x46546c67, true) // 'glTF'
  view.setUint32(4, 2, true)
  view.setUint32(8, total, true)

  view.setUint32(12, jsonLength, true)
  view.setUint32(16, 0x4e4f534a, true) // 'JSON'
  out.set(jsonBytes, 20)
  out.fill(0x20, 20 + jsonBytes.byteLength, 20 + jsonLength)

  const binHeader = 20 + jsonLength
  view.setUint32(binHeader, offset, true)
  view.setUint32(binHeader + 4, 0x004e4942, true) // 'BIN\0'
  out.set(binBytes, binHeader + 8)

  return out
}

/** The two-colour keychain the mock catalogue ships: a plate plus raised text. */
export function keychainGlb(colors: string[], bbox: { x: number; y: number; z: number }): Uint8Array {
  const [body = '#1b6ca8', text = '#e8532f'] = colors
  const halfX = bbox.x / 2
  const halfY = bbox.y / 2
  const plateHeight = Math.min(3, bbox.z * 0.5)
  return buildGlb([
    { color: body, min: [-halfX, -halfY, 0], max: [halfX, halfY, plateHeight] },
    {
      color: text,
      min: [-halfX * 0.78, -halfY * 0.45, plateHeight],
      max: [halfX * 0.86, halfY * 0.45, bbox.z],
    },
  ])
}

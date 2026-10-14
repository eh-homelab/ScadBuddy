import type { ToolpathHeader } from '../lib/toolpath'

/**
 * #2072 — a toolpath encoded as the backend answers one: a little-endian `uint32` header
 * length, the JSON header padded to 4 bytes, then eight `float32` per segment.
 */
export function encodeToolpath(header: Omit<ToolpathHeader, 'segment_count' | 'version'>, segments: number[][]): ArrayBuffer {
  let json = JSON.stringify({ version: 1, segment_count: segments.length, ...header })
  json += ' '.repeat((4 - (json.length % 4)) % 4)
  const head = new TextEncoder().encode(json)
  const buffer = new ArrayBuffer(4 + head.length + segments.length * 32)
  new DataView(buffer).setUint32(0, head.length, true)
  new Uint8Array(buffer, 4, head.length).set(head)
  new Float32Array(buffer, 4 + head.length).set(segments.flat())
  return buffer
}

/** Two layers of a 20 mm square: a travel, the walls, and the infill faster. */
export function demoToolpath(): ArrayBuffer {
  return encodeToolpath(
    {
      features: ['Outer wall', 'Sparse infill'],
      layers: [
        { z: 0.2, first_segment: 0 },
        { z: 0.4, first_segment: 4 },
      ],
      max_speed: 200,
      truncated: false,
    },
    [
      [0, 0, 0.2, 10, 10, 0.2, 500, 0],
      [10, 10, 0.2, 30, 10, 0.2, 50, 1],
      [30, 10, 0.2, 30, 30, 0.2, 50, 1],
      [30, 30, 0.2, 10, 10, 0.2, 200, 2],
      [10, 10, 0.4, 30, 10, 0.4, 50, 1],
      [30, 10, 0.4, 30, 30, 0.4, 100, 1],
    ],
  )
}

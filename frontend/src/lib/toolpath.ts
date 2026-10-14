/**
 * #2072 — a background slice's toolpath, as `GET …/preview-slices/{job}/toolpath`
 * answers it: a little-endian `uint32` header length, the header as JSON, then eight
 * `float32` per segment: `x0, y0, z0, x1, y1, z1, speed (mm/s), kind`. `kind` 0 is a
 * travel move, else 1 + the index of its feature in `features`.
 */

export interface ToolpathHeader {
  version: number
  segment_count: number
  features: string[]
  layers: { z: number; first_segment: number }[]
  max_speed: number
  truncated: boolean
}

export interface Toolpath {
  header: ToolpathHeader
  segments: Float32Array
}

export const STRIDE = 8
export const TRAVEL = 0
/** The wire format this decodes: `backend/scadbuddy/bambuddy/toolpath.py`'s `VERSION`. */
export const TOOLPATH_VERSION = 1

export function decodeToolpath(buffer: ArrayBuffer): Toolpath {
  const view = new DataView(buffer)
  const size = view.getUint32(0, true)
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, size))) as ToolpathHeader
  // A later format would read as a RangeError or a garbled scene: say so instead.
  if (header.version !== TOOLPATH_VERSION) {
    throw new Error(`This toolpath is in format ${String(header.version)}, which this page cannot draw; reload it.`)
  }
  const segments = new Float32Array(buffer, 4 + size, header.segment_count * STRIDE)
  return { header, segments }
}

export async function fetchToolpath(url: string, signal?: AbortSignal): Promise<Toolpath> {
  const response = await fetch(url, { signal })
  if (!response.ok) {
    // The problem's detail says why (busy, still slicing, no G-code), when it has one.
    const problem = (await response.json().catch(() => null)) as { detail?: unknown } | null
    const detail = typeof problem?.detail === 'string' && problem.detail ? problem.detail : null
    throw new Error(detail ?? `The toolpath could not be read (HTTP ${response.status}).`)
  }
  return decodeToolpath(await response.arrayBuffer())
}

export type ToolpathView = 'speed' | 'movement' | 'layers'

type Rgb = [number, number, number]

/** Slow to fast: blue, cyan, green, yellow, red. */
const SPEED_RAMP: Rgb[] = [
  [0.16, 0.35, 0.95],
  [0.1, 0.8, 0.9],
  [0.25, 0.85, 0.3],
  [0.98, 0.85, 0.15],
  [0.95, 0.2, 0.15],
]

export function speedColour(speed: number, max: number): Rgb {
  const t = max > 0 ? Math.min(1, Math.max(0, speed / max)) : 0
  const at = t * (SPEED_RAMP.length - 1)
  const low = Math.floor(at)
  const high = Math.min(SPEED_RAMP.length - 1, low + 1)
  const mix = at - low
  const a = SPEED_RAMP[low]!
  const b = SPEED_RAMP[high]!
  return [a[0] + (b[0] - a[0]) * mix, a[1] + (b[1] - a[1]) * mix, a[2] + (b[2] - a[2]) * mix]
}

/** The CSS gradient of {@link speedColour}, for its legend. */
export const SPEED_GRADIENT = `linear-gradient(to right, ${SPEED_RAMP.map(
  ([r, g, b]) => `rgb(${Math.round(r * 255)} ${Math.round(g * 255)} ${Math.round(b * 255)})`,
).join(', ')})`

export const PRINT_MOVE: Rgb = [0.62, 0.66, 0.72]
export const TRAVEL_MOVE: Rgb = [0.98, 0.45, 0.2]

/** The slicer's own feature names (Bambu Studio and Orca), coloured as their previews do. */
const FEATURE_COLOURS: Record<string, Rgb> = {
  'outer wall': [1, 0.5, 0.1],
  'inner wall': [1, 0.85, 0.3],
  'overhang wall': [0.2, 0.3, 1],
  'sparse infill': [0.7, 0.2, 0.2],
  'internal solid infill': [0.6, 0.3, 0.75],
  'top surface': [0.95, 0.3, 0.3],
  'bottom surface': [0.4, 0.4, 0.9],
  bridge: [0.3, 0.55, 0.75],
  'gap infill': [1, 1, 1],
  support: [0.2, 0.75, 0.2],
  'support interface': [0.15, 0.55, 0.15],
  'support transition': [0.25, 0.6, 0.35],
  'prime tower': [0.75, 0.75, 0.5],
  brim: [0.2, 0.75, 0.75],
  skirt: [0.2, 0.75, 0.75],
  ironing: [1, 0.55, 0.55],
}
const FALLBACK: Rgb[] = [
  [0.55, 0.8, 0.95],
  [0.85, 0.6, 0.95],
  [0.6, 0.95, 0.6],
  [0.95, 0.75, 0.55],
]

export function featureColour(name: string, index: number): Rgb {
  return FEATURE_COLOURS[name.trim().toLowerCase()] ?? FALLBACK[index % FALLBACK.length]!
}

export function rgbCss([r, g, b]: Rgb): string {
  return `rgb(${Math.round(r * 255)} ${Math.round(g * 255)} ${Math.round(b * 255)})`
}

/**
 * How many segments a view draws: the whole path, or in Layers every layer up to and
 * including `layer` (an index into `header.layers`).
 */
export function drawnCount(path: Toolpath, layer: number | null): number {
  const { layers, segment_count } = path.header
  if (layer === null || layer >= layers.length - 1) return segment_count
  return layers[layer + 1]!.first_segment
}

/**
 * For Layers: how many lines {@link toolpathLines}' Layers view (which drops travel) holds
 * up to and including each layer, so a layer is shown by a draw range, not new arrays.
 */
export function printedUpTo(path: Toolpath): number[] {
  const { segments, header } = path
  const counts: number[] = []
  let count = 0
  let at = 0
  for (let layer = 0; layer < header.layers.length; layer++) {
    const end = drawnCount(path, layer)
    for (; at < end; at++) if (segments[at * STRIDE + 7] !== TRAVEL) count++
    counts.push(count)
  }
  return counts
}

/**
 * The vertex count a Layers draw range shows: every line up to ``layer`` (two vertices
 * a line, from `printedUpTo`), or all of them with no layer chosen or no layers at all.
 */
export function layerDrawCount(upTo: number[] | null, layer: number | null): number {
  const lines = upTo === null || layer === null ? undefined : upTo[Math.min(layer, upTo.length - 1)]
  return lines === undefined ? Infinity : 2 * lines
}

/**
 * The line positions and colours a view draws, in three.js's Y-up frame: the G-code's
 * X across, Z up, and Y away from the viewer. Travel moves are drawn only in Movement.
 */
export function toolpathLines(
  path: Toolpath,
  view: ToolpathView,
  layer: number | null,
): { positions: Float32Array; colours: Float32Array; count: number } {
  const { segments, header } = path
  const total = drawnCount(path, view === 'layers' ? layer : null)
  const positions = new Float32Array(total * 6)
  const colours = new Float32Array(total * 6)
  const featureRgb = header.features.map((name, index) => featureColour(name, index))
  let count = 0
  for (let i = 0; i < total; i++) {
    const at = i * STRIDE
    const kind = segments[at + 7]!
    if (kind === TRAVEL && view !== 'movement') continue
    const colour =
      view === 'movement'
        ? kind === TRAVEL
          ? TRAVEL_MOVE
          : PRINT_MOVE
        : view === 'speed'
          ? speedColour(segments[at + 6]!, header.max_speed)
          : (featureRgb[kind - 1] ?? FALLBACK[0]!)
    const out = count * 6
    positions[out] = segments[at]!
    positions[out + 1] = segments[at + 2]!
    positions[out + 2] = -segments[at + 1]!
    positions[out + 3] = segments[at + 3]!
    positions[out + 4] = segments[at + 5]!
    positions[out + 5] = -segments[at + 4]!
    colours.set(colour, out)
    colours.set(colour, out + 3)
    count++
  }
  return { positions: positions.subarray(0, count * 6), colours: colours.subarray(0, count * 6), count }
}

/**
 * #6258: the closest colour: the colour a part moves to when the material it prints in
 * does not offer its colour. This is the twin of the backend's
 * `backend/scadbuddy/orders/storefront/closest_colour.py`. The shared vectors in
 * `test-fixtures/storefront/closest-colour.json` pin both. Change them together.
 *
 * The rule, in order:
 * 1. The first candidate with the same colour name (case aside, never a missing name) or
 *    the same colour (`rgba`: case aside; `#RRGGBB` is `#RRGGBBFF`).
 * 2. Else the candidate at the least distance, `sqrt(dE00² + (50·dA)²)`: dE00 is
 *    CIEDE2000 in CIELAB (sRGB, D65), and dA is the difference of the alphas, each 0 to 1.
 * 3. A tie goes to the first candidate. A candidate hex that is not a colour is never
 *    chosen by distance.
 *
 * A swatch's alpha is its `alpha` field when it is set (a filament's `PublicFilament.alpha`,
 * 0 to 255), else the hex's AA byte, else opaque (`rgba`).
 */

export interface Swatch {
  /** `#RRGGBB` or `#RRGGBBAA`. */
  hex: string
  name: string | null | undefined
  /** The alpha (0 to 255) apart from the hex: a filament's `alpha`. Null or absent: the hex's. */
  alpha?: number | null
}

/** dE00 per unit of alpha difference: clear against opaque counts as 50. */
const ALPHA_WEIGHT = 50

const HEX = /^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/
/** The D65 white point (CIE 1931 2°). */
const WHITE = [0.95047, 1.0, 1.08883] as const

type Rgba = readonly [number, number, number, number]
type Lab = readonly [number, number, number]

export function parseHex(text: string): Rgba | null {
  const match = HEX.exec(text.trim())
  if (!match) return null
  const rgb = match[1]!
  return [parseInt(rgb.slice(0, 2), 16), parseInt(rgb.slice(2, 4), 16), parseInt(rgb.slice(4, 6), 16), match[2] ? parseInt(match[2], 16) : 255]
}

/** `swatch` as red, green, blue and alpha, or null when its hex is not one: its `alpha` wins over the hex's. */
export function rgba(swatch: Swatch): Rgba | null {
  const parsed = parseHex(swatch.hex)
  if (parsed === null || swatch.alpha == null) return parsed
  return [parsed[0], parsed[1], parsed[2], swatch.alpha]
}

function linear(channel: number): number {
  const value = channel / 255
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
}

function lab(red: number, green: number, blue: number): Lab {
  const r = linear(red)
  const g = linear(green)
  const b = linear(blue)
  const x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
  const y = 0.2126729 * r + 0.7151522 * g + 0.072175 * b
  const z = 0.0193339 * r + 0.119192 * g + 0.9503041 * b
  const f = (t: number) => (t > (6 / 29) ** 3 ? Math.cbrt(t) : t / (3 * (6 / 29) ** 2) + 4 / 29)
  const fx = f(x / WHITE[0])
  const fy = f(y / WHITE[1])
  const fz = f(z / WHITE[2])
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)]
}

const degrees = (radians: number) => (radians * 180) / Math.PI
const radians = (deg: number) => (deg * Math.PI) / 180
/** Python's `%`: the result has the sign of the divisor. */
const mod = (value: number, by: number) => ((value % by) + by) % by

/** CIEDE2000, kL = kC = kH = 1 (Sharma, Wu and Dalal, 2005). */
export function ciede2000(first: Lab, second: Lab): number {
  const [l1, a1, b1] = first
  const [l2, a2, b2] = second
  const cBar = (Math.hypot(a1, b1) + Math.hypot(a2, b2)) / 2
  const g = 0.5 * (1 - Math.sqrt(cBar ** 7 / (cBar ** 7 + 25 ** 7)))
  const a1p = (1 + g) * a1
  const a2p = (1 + g) * a2
  const c1p = Math.hypot(a1p, b1)
  const c2p = Math.hypot(a2p, b2)
  const h1p = c1p ? mod(degrees(Math.atan2(b1, a1p)), 360) : 0
  const h2p = c2p ? mod(degrees(Math.atan2(b2, a2p)), 360) : 0
  const dlp = l2 - l1
  const dcp = c2p - c1p
  let dhp: number
  if (c1p * c2p === 0) dhp = 0
  else if (Math.abs(h2p - h1p) <= 180) dhp = h2p - h1p
  else if (h2p - h1p > 180) dhp = h2p - h1p - 360
  else dhp = h2p - h1p + 360
  const dHp = 2 * Math.sqrt(c1p * c2p) * Math.sin(radians(dhp / 2))
  const lpBar = (l1 + l2) / 2
  const cpBar = (c1p + c2p) / 2
  let hpBar: number
  if (c1p * c2p === 0) hpBar = h1p + h2p
  else if (Math.abs(h1p - h2p) <= 180) hpBar = (h1p + h2p) / 2
  else if (h1p + h2p < 360) hpBar = (h1p + h2p + 360) / 2
  else hpBar = (h1p + h2p - 360) / 2
  const t =
    1 -
    0.17 * Math.cos(radians(hpBar - 30)) +
    0.24 * Math.cos(radians(2 * hpBar)) +
    0.32 * Math.cos(radians(3 * hpBar + 6)) -
    0.2 * Math.cos(radians(4 * hpBar - 63))
  const dTheta = 30 * Math.exp(-(((hpBar - 275) / 25) ** 2))
  const rC = 2 * Math.sqrt(cpBar ** 7 / (cpBar ** 7 + 25 ** 7))
  const sL = 1 + (0.015 * (lpBar - 50) ** 2) / Math.sqrt(20 + (lpBar - 50) ** 2)
  const sC = 1 + 0.045 * cpBar
  const sH = 1 + 0.015 * cpBar * t
  const rT = -Math.sin(radians(2 * dTheta)) * rC
  return Math.sqrt((dlp / sL) ** 2 + (dcp / sC) ** 2 + (dHp / sH) ** 2 + rT * (dcp / sC) * (dHp / sH))
}

function same(target: Swatch, candidate: Swatch): boolean {
  if (target.name != null && candidate.name != null && target.name.toLowerCase() === candidate.name.toLowerCase()) return true
  const parsed = rgba(target)
  const other = rgba(candidate)
  return parsed !== null && other !== null && parsed.every((channel, index) => channel === other[index])
}

/** The distance of rule 2; infinite when either hex is not a colour. */
export function colourDistance(target: Swatch, candidate: Swatch): number {
  const first = rgba(target)
  const second = rgba(candidate)
  if (first === null || second === null) return Number.POSITIVE_INFINITY
  const dE = ciede2000(lab(first[0], first[1], first[2]), lab(second[0], second[1], second[2]))
  const dA = (ALPHA_WEIGHT * Math.abs(first[3] - second[3])) / 255
  return Math.hypot(dE, dA)
}

/**
 * The index in `candidates` of the colour closest to `target`, or null when there are
 * none. With a target hex that is not a colour and no name match, the first candidate.
 */
export function closestColour(target: Swatch, candidates: readonly Swatch[]): number | null {
  if (candidates.length === 0) return null
  const named = candidates.findIndex((candidate) => same(target, candidate))
  if (named >= 0) return named
  let best = 0
  let bestDistance = Number.POSITIVE_INFINITY
  candidates.forEach((candidate, index) => {
    const each = colourDistance(target, candidate)
    if (each < bestDistance) {
      best = index
      bestDistance = each
    }
  })
  return best
}

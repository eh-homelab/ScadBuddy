import { fromScaled } from '../../../lib/decimal'

/** The smallest round number at or above `max` (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 × 10ⁿ). */
export function niceMax(max: number): number {
  if (!(max > 0)) return 1
  const power = 10 ** Math.floor(Math.log10(max))
  for (const step of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (step * power >= max) return step * power
  return 10 * power
}

/**
 * The y scale's bottom, top and three grid values. It starts at zero when the lowest value
 * is below 40 % of the rounded-up top; otherwise every value is in the top 60 %, where a
 * zero-based scale flattens the line, so it starts at a round number below the lowest value.
 */
export function yScale(values: readonly number[]): { lo: number; hi: number; grid: number[] } {
  const max = Math.max(...values, 0)
  const min = Math.min(...values, max)
  const top = niceMax(max)
  if (min < 0.4 * top) return { lo: 0, hi: top, grid: [0, top / 2, top] }
  // Two equal steps from a multiple of the step under the lowest value to at or over the highest.
  // A flat line still gets a span: 5 % of its value on each side.
  for (let half = niceMax(Math.max((max - min) / 2, max * 0.05)); ; half = niceMax(half * 1.01)) {
    const floor = Math.floor(min / half) * half
    const lo = floor === min ? floor - half : floor
    if (lo + 2 * half >= max) return { lo, hi: lo + 2 * half, grid: [lo, lo + half, lo + 2 * half] }
  }
}

/**
 * An axis tick (a round number `niceMax` and `yScale` give: a whole count of a power of ten)
 * as its exact decimal string, so money text goes through `lib/decimal.ts` and never
 * `toFixed` (R-T8-2). The float's last-bit noise (`0.30000000000000004`) is dropped.
 */
export function tickDecimal(value: number): string {
  for (let places = 0; places < 8; places++) {
    const scaled = value * 10 ** places
    if (Math.abs(scaled - Math.round(scaled)) < 1e-6) return fromScaled(BigInt(Math.round(scaled)), places)
  }
  return fromScaled(BigInt(Math.round(value * 1e8)), 8)
}

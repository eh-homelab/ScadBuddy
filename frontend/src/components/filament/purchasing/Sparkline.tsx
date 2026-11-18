import { toScaled } from '../../../lib/decimal'

const W = 96
const H = 28
const PAD = 3

/**
 * A small line of decimal strings, oldest first, as an inline SVG. The positions are pixels,
 * worked out from the scaled integers; no amount is turned into a float. Fewer than two
 * points draw one dot. `label` is what a screen reader hears.
 */
export function Sparkline({ values, label }: { values: string[]; label: string }) {
  if (values.length === 0) return null
  const scaled = values.map((value) => toScaled(value, 6))
  const low = scaled.reduce((a, b) => (b < a ? b : a))
  const high = scaled.reduce((a, b) => (b > a ? b : a))
  const range = Number(high - low)
  const x = (at: number) => (values.length === 1 ? W / 2 : PAD + (at * (W - 2 * PAD)) / (values.length - 1))
  const y = (value: bigint) => (range === 0 ? H / 2 : H - PAD - (Number(value - low) * (H - 2 * PAD)) / range)
  const points = scaled.map((value, at) => `${x(at).toFixed(1)},${y(value).toFixed(1)}`)
  const last = scaled.length - 1
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="shrink-0 text-primary">
      {values.length > 1 && <polyline points={points.join(' ')} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />}
      <circle cx={x(last)} cy={y(scaled[last]!)} r="2.5" fill="currentColor" />
    </svg>
  )
}

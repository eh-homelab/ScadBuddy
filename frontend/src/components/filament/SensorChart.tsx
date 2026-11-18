import type { SensorReading } from '../../api/filament'

interface Props {
  /** Oldest first, as the route answers. */
  readings: readonly SensorReading[]
  unit: string | null
  /** The sensor's name: it begins the chart's text for a screen reader. */
  label: string
  alertAbove?: number | null
  alertBelow?: number | null
}

const W = 320
const H = 72
const PAD = 6

const fmt = (value: number) => String(Math.round(value * 10) / 10)

/**
 * #6268 — one sensor's readings as a small line, with a dashed line at each limit that is
 * set. The range is stretched to take in the limits, so a line that crosses one is seen
 * crossing it. A plain SVG: the app has no chart library, and one line needs none.
 */
export function SensorChart({ readings, unit, label, alertAbove, alertBelow }: Props) {
  if (readings.length === 0) return <p className="text-[12px] text-muted-foreground">No readings in this time yet.</p>
  const values = readings.map((one) => one.value)
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const limits = [alertAbove, alertBelow].filter((limit): limit is number => limit != null)
  const min = Math.min(lo, ...limits)
  const max = Math.max(hi, ...limits)
  const span = max - min || 1
  const times = readings.map((one) => Date.parse(one.at))
  const t0 = times[0]!
  const tSpan = times[times.length - 1]! - t0 || 1
  const x = (time: number) => PAD + ((time - t0) / tSpan) * (W - 2 * PAD)
  const y = (value: number) => H - PAD - ((value - min) / span) * (H - 2 * PAD)
  const points = readings.map((one, i) => `${x(times[i]!).toFixed(1)},${y(one.value).toFixed(1)}`)
  const last = readings[readings.length - 1]!
  const out = (value: number) => (alertAbove != null && value > alertAbove) || (alertBelow != null && value < alertBelow)
  const suffix = unit ? ` ${unit}` : ''
  const name = `${label}: ${fmt(lo)} to ${fmt(hi)}${suffix}, ${readings.length} ${readings.length === 1 ? 'reading' : 'readings'}`
  return (
    <svg role="img" aria-label={name} viewBox={`0 0 ${W} ${H}`} className="h-[72px] w-full max-w-md overflow-visible text-foreground">
      {([['above', alertAbove], ['below', alertBelow]] as const).map(([side, limit]) =>
        limit == null ? null : (
          <g key={side} data-limit={side} className="text-warn">
            <line x1={PAD} x2={W - PAD} y1={y(limit)} y2={y(limit)} stroke="currentColor" strokeWidth={1} strokeDasharray="4 3" opacity={0.7} />
            <text x={W - PAD} y={y(limit) + (side === 'above' ? -3 : 10)} textAnchor="end" fontSize={9} fill="currentColor">
              {fmt(limit)}
            </text>
          </g>
        ),
      )}
      {points.length > 1 && <polyline points={points.join(' ')} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />}
      <circle cx={x(times[times.length - 1]!)} cy={y(last.value)} r={3} className={out(last.value) ? 'fill-warn' : 'fill-current'} />
    </svg>
  )
}

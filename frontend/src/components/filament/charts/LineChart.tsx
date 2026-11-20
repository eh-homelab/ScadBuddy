import { useState, type PointerEvent } from 'react'
import { yScale } from './scale'
import { axisLeft, useWidth } from './useWidth'

/** One point of the line: a time (epoch ms) and its value. */
export interface LinePoint {
  at: number
  value: number
  /** The hover text for the value, when the value's own text is not `formatValue`'s (money from its decimal string). */
  text?: string
}

interface Props {
  /** Oldest first. */
  points: readonly LinePoint[]
  /** The chart's whole text for a screen reader (`role="img"`). */
  label: string
  /** The y axis's labels, and the hover value of a point with no `text`. */
  formatValue: (value: number) => string
  /** The x axis's first and last labels. */
  formatTime?: (at: number) => string
  /** The hover's time. */
  formatPointTime?: (at: number) => string
  /** Points drawn as filled circles, each with the attribute `data-<name>`. */
  marks?: { name: string; points: readonly LinePoint[] }
  /** Times drawn as short ticks on the x axis in ink, each with the attribute `data-<name>`. */
  ticks?: { name: string; at: readonly number[] }
  /** A value holds until the next point (a step), rather than sliding to it. */
  step?: boolean
  /** The time the x axis ends at; a step line holds its last value to it. */
  until?: number
  /** The width drawn at until the container is measured. */
  width?: number
  height?: number
}

const MIN_LEFT = 36
const RIGHT = 8
const TOP = 8
const BOTTOM = 18
const TICK = 5
const FONT = 11

const dateText = (at: number) => new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
const pointTimeText = (at: number) => new Date(at).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const attr = (name: string) => ({ [`data-${name}`]: '' })

/**
 * #6269 — the one SVG line chart of the Filament section (the app has no chart library):
 * a y scale by `yScale` with three recessive grid lines, the first and last dates on the
 * x axis, a 2 px line in the accent ink, optional filled marks and axis ticks, and a
 * crosshair with the nearest point's value under the pointer. The marks and the line
 * share one colour: a single series needs no legend, and its label names it. The hover
 * value is hidden from screen readers; the label and the page's table view carry it.
 * It draws at its container's measured width (`useWidth`), so the axis text stays 11 px
 * on a phone (R-T8-5).
 */
export function LineChart({ points, label, formatValue, formatTime = dateText, formatPointTime = pointTimeText, marks, ticks, step = false, until, width: fallback = 320, height = 120 }: Props) {
  const [hover, setHover] = useState<number | null>(null)
  const [measure, width] = useWidth(fallback)
  if (points.length === 0) return null
  const times = points.map((one) => one.at)
  const t0 = Math.min(...times, ...(ticks?.at ?? []))
  const t1 = Math.max(...times, ...(ticks?.at ?? []), ...(until == null ? [] : [until]))
  const tSpan = t1 - t0 || 1
  const { lo, hi, grid } = yScale(points.map((one) => one.value))
  const left = axisLeft(grid.map(formatValue), FONT, MIN_LEFT)
  const plotW = width - left - RIGHT
  const plotH = height - TOP - BOTTOM
  const x = (at: number) => left + (t1 === t0 ? 0 : ((at - t0) / tSpan) * plotW)
  const y = (value: number) => TOP + plotH - ((Math.max(value, lo) - lo) / (hi - lo)) * plotH
  const fixed = (n: number) => n.toFixed(1)
  const d = points
    .map((one, i) => {
      if (i === 0) return `M${fixed(x(one.at))} ${fixed(y(one.value))}`
      return step ? `H${fixed(x(one.at))} V${fixed(y(one.value))}` : `L${fixed(x(one.at))} ${fixed(y(one.value))}`
    })
    .join(' ')
  const last = points[points.length - 1]!
  const tail = step && until != null && until > last.at ? ` H${fixed(x(until))}` : ''
  const near = hover == null ? null : points[hover]

  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width === 0 || points.length === 0) return
    const at = t0 + (((event.clientX - box.left) / box.width) * width - left) / plotW * tSpan
    let best = 0
    for (let i = 1; i < points.length; i++) if (Math.abs(points[i]!.at - at) < Math.abs(points[best]!.at - at)) best = i
    setHover(best)
  }

  return (
    <div ref={measure} className="relative w-full max-w-[34rem]">
      <svg role="img" aria-label={label} viewBox={`0 0 ${width} ${height}`} className="w-full overflow-visible text-primary" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <g className="text-border" aria-hidden="true">
          {grid.map((value) => (
            <line key={value} x1={left} x2={width - RIGHT} y1={y(value)} y2={y(value)} stroke="currentColor" strokeWidth={value === lo ? 1 : 0.5} />
          ))}
        </g>
        <g className="fill-muted-foreground" fontSize={FONT} aria-hidden="true">
          {grid.map((value) => (
            <text key={value} x={left - 4} y={y(value) + 4} textAnchor="end" className="tabular-nums">
              {formatValue(value)}
            </text>
          ))}
          {points.length > 0 && (
            <>
              <text x={left} y={height - 3} textAnchor="start">
                {formatTime(t0)}
              </text>
              {t1 !== t0 && (
                <text x={width - RIGHT} y={height - 3} textAnchor="end">
                  {formatTime(t1)}
                </text>
              )}
            </>
          )}
        </g>
        {ticks && (
          <g className="text-foreground" aria-hidden="true">
            {ticks.at.map((at, i) => (
              <line key={`${at}-${i}`} {...attr(ticks.name)} x1={x(at)} x2={x(at)} y1={TOP + plotH} y2={TOP + plotH - TICK} stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" />
            ))}
          </g>
        )}
        {(points.length > 1 || tail) && <path data-line d={d + tail} fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
        {points.length === 1 && !tail && <circle data-line cx={x(points[0]!.at)} cy={y(points[0]!.value)} r={3} className="fill-current" />}
        {marks?.points.map((one, i) => (
          <circle key={`${one.at}-${i}`} {...attr(marks.name)} cx={x(one.at)} cy={y(one.value)} r={3.5} className="fill-current stroke-background" strokeWidth={1.5} />
        ))}
        {near && (
          <g aria-hidden="true" pointerEvents="none">
            <line x1={x(near.at)} x2={x(near.at)} y1={TOP} y2={TOP + plotH} className="stroke-muted-foreground" strokeWidth={0.75} strokeDasharray="2 2" />
            <circle cx={x(near.at)} cy={y(near.value)} r={4} className="fill-background stroke-current" strokeWidth={2} />
          </g>
        )}
      </svg>
      {near && (
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute top-0 z-10 rounded-md ${x(near.at) < width / 4 ? '' : x(near.at) > (width * 3) / 4 ? '-translate-x-full' : '-translate-x-1/2'} border bg-popover px-2 py-1 text-[12px] whitespace-nowrap text-popover-foreground shadow-sm`}
          style={{ left: `${(x(near.at) / width) * 100}%` }}
        >
          <span className="block font-medium tabular-nums">{near.text ?? formatValue(near.value)}</span>
          <span className="block text-muted-foreground">{formatPointTime(near.at)}</span>
        </div>
      )}
    </div>
  )
}

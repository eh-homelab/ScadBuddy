import { useState, type PointerEvent } from 'react'
import { niceMax } from './scale'
import { axisLeft, useWidth } from './useWidth'

/** One series of the stack: its name and its colour class (`text-series-1` …), in stack order from the baseline. */
export interface ColumnSeries {
  id: string
  name: string
  colour: string
}

/** One column: its x label, its hover title, and each series' value (a series it lacks is 0). */
export interface Column {
  key: string
  label: string
  title: string
  values: Readonly<Record<string, number>>
}

interface Props {
  columns: readonly Column[]
  series: readonly ColumnSeries[]
  /** The chart's whole text for a screen reader (`role="img"`). */
  label: string
  formatValue: (value: number) => string
  /** The width drawn at until the container is measured. */
  width?: number
  height?: number
}

const MIN_LEFT = 40
const RIGHT = 4
const TOP = 8
const BOTTOM = 20
const FONT = 11
/** The surface gap between stacked segments (dataviz: 2 px). */
const GAP = 2
/** The rounded data end of a column (dataviz: 4 px, square at the baseline), and its widest. */
const END = 4
const MAX_BAR = 24

/** A segment as a path: square at the bottom, its top corners rounded when it ends the column. */
function segment(x: number, top: number, w: number, h: number, end: boolean): string {
  const r = end ? Math.min(END, w / 2, h) : 0
  const f = (n: number) => n.toFixed(1)
  if (r === 0) return `M${f(x)} ${f(top + h)} V${f(top)} H${f(x + w)} V${f(top + h)} Z`
  return `M${f(x)} ${f(top + h)} V${f(top + r)} A${f(r)} ${f(r)} 0 0 1 ${f(x + r)} ${f(top)} H${f(x + w - r)} A${f(r)} ${f(r)} 0 0 1 ${f(x + w)} ${f(top + r)} V${f(top + h)} Z`
}

/**
 * #6269 — stacked columns in plain SVG, one per bucket: a zero-based y scale rounded up
 * (`niceMax`, columns always start at zero), three recessive grid lines, the series stacked
 * from the baseline in their fixed order with a 2 px surface gap between segments and a 4 px
 * rounded end on top, and x labels thinned to fit. It draws at its container's measured width
 * (`useWidth`), so the axis text stays 11 px on a phone (R-T8-4, R-T8-5). Under the pointer a column shows each series' value; that tooltip is
 * hidden from screen readers, which read the label, and the page carries a table view.
 */
export function ColumnChart({ columns, series, label, formatValue, width: fallback = 520, height = 180 }: Props) {
  const [hover, setHover] = useState<number | null>(null)
  const [measure, width] = useWidth(fallback)
  if (columns.length === 0) return null
  const totals = columns.map((column) => series.reduce((sum, one) => sum + (column.values[one.id] ?? 0), 0))
  const top = niceMax(Math.max(...totals, 0))
  const ticks = [0, top / 2, top]
  const left = axisLeft(ticks.map(formatValue), FONT, MIN_LEFT)
  const plotW = width - left - RIGHT
  const plotH = height - TOP - BOTTOM
  const slot = plotW / columns.length
  const barW = Math.max(2, Math.min(MAX_BAR, slot * 0.7))
  const y = (value: number) => TOP + plotH - (value / top) * plotH
  const xOf = (i: number) => left + slot * i + (slot - barW) / 2
  const longest = Math.max(...columns.map((column) => column.label.length))
  const every = Math.max(1, Math.ceil(columns.length / Math.max(1, Math.floor(plotW / (longest * FONT * 0.62 + 12)))))
  const near = hover == null ? null : columns[hover]

  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width === 0) return
    const i = Math.floor((((event.clientX - box.left) / box.width) * width - left) / slot)
    setHover(i >= 0 && i < columns.length ? i : null)
  }

  return (
    <div ref={measure} className="relative w-full">
      <svg role="img" aria-label={label} viewBox={`0 0 ${width} ${height}`} className="w-full overflow-visible" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <g className="text-border" aria-hidden="true">
          {ticks.map((value) => (
            <line key={value} x1={left} x2={width - RIGHT} y1={y(value)} y2={y(value)} stroke="currentColor" strokeWidth={value === 0 ? 1 : 0.5} />
          ))}
        </g>
        <g className="fill-muted-foreground" fontSize={FONT} aria-hidden="true">
          {ticks.map((value) => (
            <text key={value} x={left - 4} y={y(value) + 4} textAnchor="end" className="tabular-nums">
              {formatValue(value)}
            </text>
          ))}
          {columns.map((column, i) =>
            i % every === 0 ? (
              <text key={column.key} x={xOf(i) + barW / 2} y={height - 4} textAnchor="middle">
                {column.label}
              </text>
            ) : null,
          )}
        </g>
        {hover != null && <rect aria-hidden="true" x={left + slot * hover} y={TOP} width={slot} height={plotH} className="fill-foreground" opacity={0.08} />}
        {columns.map((column, i) => {
          let base = 0
          const drawn = series.filter((one) => (column.values[one.id] ?? 0) > 0)
          return (
            <g key={column.key} data-column={column.key}>
              {drawn.map((one, k) => {
                const y0 = y(base)
                base += column.values[one.id]!
                const y1 = y(base)
                // The gap sits above each segment but the top one, so the column's height is its total.
                const end = k === drawn.length - 1
                const h = Math.max(0.5, y0 - y1 - (end ? 0 : GAP))
                return <path key={one.id} data-series={one.id} d={segment(xOf(i), end ? y1 : y1 + GAP, barW, h, end)} className={`fill-current ${one.colour}`} />
              })}
            </g>
          )
        })}
      </svg>
      {near && hover != null && (
        <div
          aria-hidden="true"
          className={`pointer-events-none absolute top-0 z-10 rounded-md border bg-popover px-2 py-1 text-[12px] whitespace-nowrap text-popover-foreground shadow-sm ${hover < columns.length / 4 ? '' : hover > (columns.length * 3) / 4 ? '-translate-x-full' : '-translate-x-1/2'}`}
          style={{ left: `${((xOf(hover) + barW / 2) / width) * 100}%` }}
        >
          <span className="block font-medium">{near.title}</span>
          {series
            .filter((one) => (near.values[one.id] ?? 0) > 0)
            .reverse()
            .map((one) => (
              <span key={one.id} className="flex items-center gap-1.5">
                <span className={`size-2 rounded-[2px] bg-current ${one.colour}`} />
                <span className="text-muted-foreground">{one.name}</span>
                <span className="ml-auto pl-3 tabular-nums">{formatValue(near.values[one.id] ?? 0)}</span>
              </span>
            ))}
          <span className="mt-0.5 flex border-t pt-0.5">
            <span className="text-muted-foreground">Total</span>
            <span className="ml-auto pl-3 font-medium tabular-nums">{formatValue(totals[hover] ?? 0)}</span>
          </span>
        </div>
      )}
    </div>
  )
}

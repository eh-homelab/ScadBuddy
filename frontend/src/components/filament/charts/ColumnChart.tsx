import { useState, type PointerEvent } from 'react'
import { niceMax } from './scale'

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
  width?: number
  height?: number
}

const LEFT = 40
const RIGHT = 4
const TOP = 8
const BOTTOM = 18
/** The surface gap between stacked segments. */
const GAP = 1

/**
 * #6269 — stacked columns in plain SVG, one per bucket: a zero-based y scale rounded up
 * (`niceMax`, columns always start at zero), three recessive grid lines, the series stacked
 * from the baseline in their fixed order with a surface gap between segments, and x labels
 * thinned to fit. Under the pointer a column shows each series' value; that tooltip is
 * hidden from screen readers, which read the label, and the page carries a table view.
 */
export function ColumnChart({ columns, series, label, formatValue, width = 520, height = 160 }: Props) {
  const [hover, setHover] = useState<number | null>(null)
  if (columns.length === 0) return null
  const totals = columns.map((column) => series.reduce((sum, one) => sum + (column.values[one.id] ?? 0), 0))
  const top = niceMax(Math.max(...totals, 0))
  const plotW = width - LEFT - RIGHT
  const plotH = height - TOP - BOTTOM
  const slot = plotW / columns.length
  const barW = Math.max(2, Math.min(28, slot * 0.7))
  const y = (value: number) => TOP + plotH - (value / top) * plotH
  const xOf = (i: number) => LEFT + slot * i + (slot - barW) / 2
  const every = Math.max(1, Math.ceil(columns.length / Math.max(1, Math.floor(plotW / 56))))
  const near = hover == null ? null : columns[hover]

  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    if (box.width === 0) return
    const i = Math.floor((((event.clientX - box.left) / box.width) * width - LEFT) / slot)
    setHover(i >= 0 && i < columns.length ? i : null)
  }

  return (
    <div className="relative w-full">
      <svg role="img" aria-label={label} viewBox={`0 0 ${width} ${height}`} className="w-full overflow-visible" onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <g className="text-border" aria-hidden="true">
          {[0, top / 2, top].map((value) => (
            <line key={value} x1={LEFT} x2={width - RIGHT} y1={y(value)} y2={y(value)} stroke="currentColor" strokeWidth={value === 0 ? 1 : 0.5} />
          ))}
        </g>
        <g className="fill-muted-foreground" fontSize={9} aria-hidden="true">
          {[0, top / 2, top].map((value) => (
            <text key={value} x={LEFT - 4} y={y(value) + 3} textAnchor="end" className="tabular-nums">
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
        {hover != null && <rect aria-hidden="true" x={LEFT + slot * hover} y={TOP} width={slot} height={plotH} className="fill-muted" opacity={0.6} />}
        {columns.map((column, i) => {
          let base = 0
          return (
            <g key={column.key} data-column={column.key}>
              {series.map((one) => {
                const value = column.values[one.id] ?? 0
                if (value <= 0) return null
                const y0 = y(base)
                base += value
                const y1 = y(base)
                return <rect key={one.id} data-series={one.id} x={xOf(i)} y={y1} width={barW} height={Math.max(0.5, y0 - y1 - GAP)} rx={1} className={`fill-current ${one.colour}`} />
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

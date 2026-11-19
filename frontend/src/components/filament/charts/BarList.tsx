import type { ReactNode } from 'react'

export interface BarRow {
  key: string
  label: ReactNode
  value: number
  /** The value as a person reads it, right of the label. */
  text: string
  /** A second line under the bar, such as the spool count. */
  note?: string
}

/**
 * #6269 — a ranked list of one measure: each row's label and value as text, over a bar in
 * one ink from zero (the largest row fills the track). It is a list, not an image: every
 * value is text, so it is its own table view and a screen reader reads it row by row. One
 * measure needs no legend; the list's label names it.
 */
export function BarList({ rows, label, empty }: { rows: readonly BarRow[]; label: string; empty: string }) {
  if (rows.length === 0) return <p className="text-[13px] text-muted-foreground">{empty}</p>
  const max = Math.max(...rows.map((row) => row.value), 0)
  return (
    <ul aria-label={label} className="flex flex-col gap-2.5">
      {rows.map((row) => (
        <li key={row.key} className="min-w-0">
          <div className="flex items-baseline justify-between gap-3 text-[13px]">
            <span className="min-w-0 truncate">{row.label}</span>
            <span className="shrink-0 font-medium tabular-nums">{row.text}</span>
          </div>
          <div aria-hidden="true" className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-foreground/70" style={{ width: `${max > 0 ? Math.max((row.value / max) * 100, row.value > 0 ? 1 : 0) : 0}%` }} />
          </div>
          {row.note && <p className="mt-0.5 text-[12px] text-muted-foreground">{row.note}</p>}
        </li>
      ))}
    </ul>
  )
}

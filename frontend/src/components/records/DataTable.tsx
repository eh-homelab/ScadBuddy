import type { ReactNode } from 'react'
import { Card } from '@/components/ui/card'

/**
 * #4249 — the records pages' table (spec 2026-10-18 §9): a typed `<table>` in a `Card`
 * with the Activity table's head and cell classes, extracted from it so the customers
 * and orders pages are thin pages over one table. `ActivityTable` is a wrapper over it.
 */

export const HEAD = 'h-9 px-3 font-medium whitespace-nowrap'
export const CELL = 'px-3 py-2'
export const ROW = 'border-t border-border align-top transition-colors hover:bg-muted/40'

export interface Column<Row> {
  key: string
  header: ReactNode
  /** The cell; `open` opens the row (the page's drawer or detail), when the table has `onOpen`. */
  cell: (row: Row, open: (() => void) | undefined) => ReactNode
  /** Classes on the cell, beside `CELL`; per row when a function. */
  className?: string | ((row: Row) => string)
  headerClassName?: string
}

export interface DataTableProps<Row> {
  columns: Column<Row>[]
  rows: Row[]
  rowKey: (row: Row) => string
  /** The table's accessible name. */
  label: string
  rowTestId?: (row: Row) => string
  onOpen?: (row: Row) => void
  /** Shown in place of the table when there are no rows. */
  empty?: ReactNode
}

export function DataTable<Row>({ columns, rows, rowKey, label, rowTestId, onOpen, empty }: DataTableProps<Row>) {
  if (rows.length === 0 && empty !== undefined) {
    return (
      <p className="rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">{empty}</p>
    )
  }
  return (
    <Card className="gap-0 py-0">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-[13px]" aria-label={label}>
          <thead className="bg-muted/50 text-xs text-muted-foreground">
            <tr>
              {columns.map((column) => (
                <th key={column.key} className={`${HEAD} ${column.headerClassName ?? ''}`.trim()}>
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const open = onOpen ? () => onOpen(row) : undefined
              return (
                <tr key={rowKey(row)} data-testid={rowTestId?.(row)} className={ROW}>
                  {columns.map((column) => {
                    const extra = typeof column.className === 'function' ? column.className(row) : column.className
                    return (
                      <td key={column.key} className={`${CELL} ${extra ?? ''}`.trim()}>
                        {column.cell(row, open)}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

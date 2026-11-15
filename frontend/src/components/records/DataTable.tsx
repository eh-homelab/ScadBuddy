import type { ReactNode } from 'react'
import { ChevronRightIcon } from 'lucide-react'
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
  /** The header's `aria-sort`, when the page sorts the rows by this column. */
  sort?: 'ascending' | 'descending' | 'none'
}

/** #6334 — rows under a header row of their own, after the table's `rows`, that the
 * header shows or hides. */
export interface RowGroup<Row> {
  key: string
  /** The header row's text. */
  label: ReactNode
  rows: Row[]
  open: boolean
  onToggle: (open: boolean) => void
  /** Classes on each of its rows, such as a quieter tone. */
  rowClassName?: string
}

export interface DataTableProps<Row> {
  columns: Column<Row>[]
  rows: Row[]
  rowKey: (row: Row) => string
  /** The table's accessible name. */
  label: string
  rowTestId?: (row: Row) => string
  onOpen?: (row: Row) => void
  /** A row's element id, the target of a deep link to it (#4693). */
  rowId?: (row: Row) => string
  /** The key of the row the page has open: marked, and `aria-current`. */
  selected?: string | null
  /** Shown in place of the table when there are no rows (in its body, under a `footer`). */
  empty?: ReactNode
  /** Rows under the body, such as an entry row (#4954): `<tr>`s, in a `<tfoot>`. */
  footer?: ReactNode
  /** Classes on a row, such as a tint for its state (#5327). */
  rowClassName?: (row: Row) => string
  /** #4955 — a heading and its actions above the table, inside the card: the card is then a
   * region named by `heading`, shown with its empty text even when there are no rows. */
  heading?: string
  toolbar?: ReactNode
  /** #6334 — groups of rows after `rows`, each under a header that opens and closes it. */
  groups?: RowGroup<Row>[]
}

export function DataTable<Row>({ columns, rows, rowKey, label, rowTestId, onOpen, rowId, selected, empty, footer, rowClassName, heading, toolbar, groups = [] }: DataTableProps<Row>) {
  const shownGroups = groups.filter((group) => group.rows.length > 0)
  const none = rows.length === 0 && shownGroups.length === 0
  if (none && empty !== undefined && !footer && heading === undefined) {
    return (
      <p className="rounded-xl border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">{empty}</p>
    )
  }
  function renderRow(row: Row, groupClass?: string) {
    const open = onOpen ? () => onOpen(row) : undefined
    const key = rowKey(row)
    const current = selected != null && key === selected
    return (
      <tr
        key={key}
        id={rowId?.(row)}
        data-testid={rowTestId?.(row)}
        aria-current={current ? 'true' : undefined}
        className={`${current ? `${ROW} bg-muted/60` : ROW} ${rowClassName?.(row) ?? ''} ${groupClass ?? ''}`.trim()}
      >
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
  }
  return (
    <Card className="gap-0 py-0" {...(heading !== undefined ? { role: 'region', 'aria-label': heading } : {})}>
      {heading !== undefined && (
        <div className="flex flex-wrap items-center gap-2 px-3 py-2">
          <h2 className="mr-auto text-sm font-medium">{heading}</h2>
          {toolbar}
        </div>
      )}
      {/* Relative, so a header's sr-only text (absolutely positioned) stays inside the scroll box
          rather than widening the page past a narrow screen (#4955). */}
      <div className="relative overflow-x-auto">
        <table className="w-full text-left text-[13px]" aria-label={label}>
          <thead className="bg-muted/50 text-xs text-muted-foreground">
            <tr>
              {columns.map((column) => (
                <th key={column.key} aria-sort={column.sort} className={`${HEAD} ${column.headerClassName ?? ''}`.trim()}>
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => renderRow(row))}
            {shownGroups.map((group) => (
              <GroupRows key={group.key} group={group} span={columns.length}>
                {group.rows.map((row) => renderRow(row, group.rowClassName))}
              </GroupRows>
            ))}
            {none && empty !== undefined && (
              <tr className={ROW}>
                <td colSpan={columns.length} className={`${CELL} text-center text-muted-foreground`}>
                  {empty}
                </td>
              </tr>
            )}
          </tbody>
          {footer && <tfoot>{footer}</tfoot>}
        </table>
      </div>
    </Card>
  )
}

/** A group's header row, a button that opens and closes it, then its rows while open. */
function GroupRows<Row>({ group, span, children }: { group: RowGroup<Row>; span: number; children: ReactNode }) {
  return (
    <>
      <tr className="border-t border-border bg-muted/30" data-testid={`row-group-${group.key}`}>
        <td colSpan={span} className="px-3 py-1.5">
          <button
            type="button"
            aria-expanded={group.open}
            onClick={() => group.onToggle(!group.open)}
            className="flex w-full items-center gap-1.5 rounded-sm text-left text-xs text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring"
          >
            <ChevronRightIcon aria-hidden className={`size-3.5 shrink-0 transition-transform motion-reduce:transition-none ${group.open ? 'rotate-90' : ''}`} />
            {group.label}
          </button>
        </td>
      </tr>
      {group.open && children}
    </>
  )
}

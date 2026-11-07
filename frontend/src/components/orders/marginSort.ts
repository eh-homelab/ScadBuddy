import type { OrderRow } from '../../api/types'

/** #5327 (job cost spec §6.2): the orders list's sort by margin, on the rows loaded. */
export type MarginSort = 'none' | 'ascending' | 'descending'
export const NEXT_SORT: Record<MarginSort, MarginSort> = { none: 'ascending', ascending: 'descending', descending: 'none' }

/** A row's place by margin: its percent, else (charged nothing) its money's sign; none last. */
function marginKey(order: OrderRow): number | null {
  if (order.margin_minor == null) return null
  if (order.margin_percent != null) return order.margin_percent
  // No percent: the order charged nothing, so a percent of it has no value. Only the
  // money's sign is left: a loss sorts first, and any other margin sorts as 0, next to a
  // 0 % margin, because no percent puts it higher or lower than that.
  return order.margin_minor < 0 ? -Infinity : 0
}

/** The rows by margin, worst first when ascending; a row with no margin stays last. */
export function sortByMargin(rows: OrderRow[], sort: MarginSort): OrderRow[] {
  if (sort === 'none') return rows
  const sign = sort === 'ascending' ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = marginKey(a)
    const y = marginKey(b)
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1
    return x === y ? 0 : x < y ? -sign : sign
  })
}

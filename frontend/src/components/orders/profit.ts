import type { ProfitReport, ProfitRow } from '../../api/types'
import { DEFAULT_ORDERS_QUERY, toOrdersParams } from '../../lib/ordersQuery'

const DAY = 24 * 60 * 60 * 1000

/** #5329 — the period as the route's `from` and `to`: from the start of the first day to the end of the last. */
export function periodOf(from: string, to: string): { from?: string; to?: string } {
  return {
    from: from ? `${from}T00:00:00Z` : undefined,
    to: to ? new Date(Date.parse(`${to}T00:00:00Z`) + DAY).toISOString() : undefined,
  }
}

/** The day before an exclusive end, as the date the person picked. */
export const lastDay = (end: string) => new Date(Date.parse(end) - 1).toISOString().slice(0, 10)

/** The orders list filtered to the row's orders (#5329): its product, the period and the currency. */
export function rowLink(report: ProfitReport, row: ProfitRow): string {
  const params = toOrdersParams({
    ...DEFAULT_ORDERS_QUERY,
    product: row.product ?? '',
    accepted_from: report.period_from,
    accepted_to: report.period_to,
    currency: row.currency,
  })
  return `/orders?${params.toString()}`
}

import type { ProfitReport, ProfitRow } from '../../api/types'
import { DEFAULT_ORDERS_QUERY, toOrdersParams } from '../../lib/ordersQuery'

/**
 * The period's default length in days. `backend/scadbuddy/orders/report.py` `DEFAULT_DAYS` is
 * the server's; the spec publishes it as `ProfitReport.default_days`, and `profit.test.ts`
 * checks that the two agree.
 */
export const DEFAULT_DAYS = 90

const pad = (n: number) => String(n).padStart(2, '0')

/** The browser's local midnight of a `YYYY-MM-DD` day, `offset` days later. */
function localMidnight(day: string, offset = 0): Date {
  const [y = NaN, m = NaN, d = NaN] = day.split('-').map(Number)
  return new Date(y, m - 1, d + offset)
}

/** An instant's day in the browser's time zone, as `YYYY-MM-DD`. */
export function localDay(at: Date | string): string {
  const date = typeof at === 'string' ? new Date(at) : at
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** A URL value that is a calendar day; any other is no value. */
const isDay = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value) && !Number.isNaN(localMidnight(value).getTime())

/** A day `offset` days from `day`, in the calendar. */
function addDays(day: string, offset: number): string {
  return localDay(localMidnight(day, offset))
}

/**
 * #5329, #5546 — the days the inputs show and the report covers, from the URL's `from` and
 * `to` (`YYYY-MM-DD`, either may be empty). With no `from`, the period is the
 * `DEFAULT_DAYS` days to `to` (or to today), as the server's default; with no `to`, it ends today.
 */
export function shownPeriod(from: string, to: string, now: Date = new Date()): { from: string; to: string } {
  const last = isDay(to) ? to : localDay(now)
  return { from: isDay(from) ? from : addDays(last, 1 - DEFAULT_DAYS), to: last }
}

/**
 * The period as the route's `from` and `to`: the days `shownPeriod` gives, from the browser's
 * local midnight of the first day to the local midnight after the last, as UTC instants. The
 * report then covers the days the inputs show, in the person's own time zone (#5546).
 */
export function periodOf(from: string, to: string, now: Date = new Date()): { from: string; to: string } {
  const days = shownPeriod(from, to, now)
  return { from: localMidnight(days.from).toISOString(), to: localMidnight(days.to, 1).toISOString() }
}

/**
 * The orders list filtered to the row's orders (#5329): its product, the period and the
 * currency. The `total` row has no product: its link lists every order in the currency.
 */
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

/** The link's accessible name. */
export function rowLinkLabel(row: ProfitRow): string {
  return row.group === 'total' ? `Every order in ${row.currency} in this period (${row.orders})` : `The ${row.orders} orders of this row`
}

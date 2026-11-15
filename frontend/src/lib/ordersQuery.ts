/**
 * The Orders page's filters, kept in the URL (`?status=&customer=&q=&from=&to=&margin=`) so
 * back/forward and deep links work, as the Activity page's are (`activityQuery.ts`).
 * `customer` is the API's `customer_id`. `margin` is `loss_thin` for the "Loss or thin"
 * filter (#5327), the API's `margin_state=loss&margin_state=thin`. `product`,
 * `accepted_from`, `accepted_to` and `currency` are a profit report row's orders (#5329),
 * passed to the API as they are. `source` is where the order was made (`back_office` or
 * `storefront`), and `unsubmitted` (`unsubmitted=1`) lists only the storefront's
 * unsubmitted carts, which the list otherwise leaves out (#5282). `own_model`,
 * `purchase_pending` and `import_stuck` (each `=1`) narrow to orders with a customer's
 * own-model request, a thing to buy not yet priced, or a file whose import is stuck (#6133).
 * #6334: `status` may name several, comma-joined (`status=draft,accepted`), sent to the API
 * as `status` repeated; `owing=1` lists orders with an unpaid payment request; `old=1` shows
 * the closed and cancelled orders the list otherwise hides after N days. `ended_since` is not
 * in the URL: the page sets it from N (`orderViews.ts` `endedSince`) unless `old` is on.
 */
export type OrdersQuery = {
  status: string
  customer: string
  q: string
  from: string
  to: string
  margin: string
  product: string
  accepted_from: string
  accepted_to: string
  currency: string
  source: string
  unsubmitted: boolean
  own_model: boolean
  purchase_pending: boolean
  import_stuck: boolean
  owing: boolean
  old: boolean
  ended_since: string
}

export const DEFAULT_ORDERS_QUERY: OrdersQuery = {
  status: '',
  customer: '',
  q: '',
  from: '',
  to: '',
  margin: '',
  product: '',
  accepted_from: '',
  accepted_to: '',
  currency: '',
  source: '',
  unsubmitted: false,
  own_model: false,
  purchase_pending: false,
  import_stuck: false,
  owing: false,
  old: false,
  ended_since: '',
}

/** The yes-or-no filters, each `=1` in the URL and `=true` to the API. */
export const FLAGS = ['unsubmitted', 'own_model', 'purchase_pending', 'import_stuck', 'owing'] as const
/** The URL's yes-or-no choices that are not API filters. */
const URL_FLAGS = ['old'] as const

/** The statuses `status` names, in their order, each once. */
export function statusesOf(query: Pick<OrdersQuery, 'status'>): string[] {
  return [...new Set(query.status.split(',').filter(Boolean))]
}

/** The "Loss or thin" filter's value, and the margin states it asks for. */
export const LOSS_OR_THIN = 'loss_thin'
const MARGIN_STATES: Record<string, string[]> = { [LOSS_OR_THIN]: ['loss', 'thin'] }

const KEYS = ['status', 'customer', 'q', 'from', 'to', 'margin', 'product', 'accepted_from', 'accepted_to', 'currency', 'source'] as const

/** The profit report's filters (#5329), which the page shows as one notice. */
export const REPORT_KEYS = ['product', 'accepted_from', 'accepted_to', 'currency'] as const

export function parseOrdersQuery(params: URLSearchParams): OrdersQuery {
  const query = { ...DEFAULT_ORDERS_QUERY }
  for (const key of KEYS) query[key] = params.get(key) ?? ''
  for (const flag of [...FLAGS, ...URL_FLAGS]) query[flag] = params.get(flag) === '1'
  return query
}

/** The query as URL parameters, leaving out every default. */
export function toOrdersParams(query: OrdersQuery): URLSearchParams {
  const params = new URLSearchParams()
  for (const key of KEYS) {
    const value = key === 'q' ? query.q.trim() : query[key]
    if (value) params.set(key, value)
  }
  for (const flag of [...FLAGS, ...URL_FLAGS]) if (query[flag]) params.set(flag, '1')
  return params
}

export function isOrdersFiltered(query: OrdersQuery): boolean {
  return toOrdersParams(query).size > 0
}

/** The filters behind the page's Filters button that are on: every one but the search and `old`. */
export function moreFiltersOn(query: OrdersQuery): number {
  const params = toOrdersParams(query)
  params.delete('q')
  params.delete('old')
  return params.size
}

/** The query narrows to a profit report row's orders. */
export function isReportFiltered(query: OrdersQuery): boolean {
  return REPORT_KEYS.some((key) => query[key] !== '')
}

/**
 * The query as `GET /api/v1/orders` parameters: `customer` is `customer_id`, and `margin`
 * is `margin_state`, repeated. An unknown `margin` value asks for nothing. Each of `FLAGS`
 * is `<flag>=true`. `status` is repeated, one per status; `old` is the page's alone, and
 * `ended_since` is passed as it is.
 */
export function ordersApiParams(query: OrdersQuery): URLSearchParams {
  const out = new URLSearchParams()
  for (const [key, value] of toOrdersParams(query)) {
    if (key === 'old') continue
    if (key === 'status') for (const status of statusesOf(query)) out.append('status', status)
    else if (key === 'margin') for (const state of MARGIN_STATES[value] ?? []) out.append('margin_state', state)
    else if ((FLAGS as readonly string[]).includes(key)) out.set(key, 'true')
    else out.set(key === 'customer' ? 'customer_id' : key, value)
  }
  if (query.ended_since) out.set('ended_since', query.ended_since)
  return out
}

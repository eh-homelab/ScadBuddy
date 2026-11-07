/**
 * The Orders page's filters, kept in the URL (`?status=&customer=&q=&from=&to=&margin=`) so
 * back/forward and deep links work, as the Activity page's are (`activityQuery.ts`).
 * `customer` is the API's `customer_id`. `margin` is `loss_thin` for the "Loss or thin"
 * filter (#5327), the API's `margin_state=loss&margin_state=thin`. `product`,
 * `accepted_from`, `accepted_to` and `currency` are a profit report row's orders (#5329),
 * passed to the API as they are.
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
}

/** The "Loss or thin" filter's value, and the margin states it asks for. */
export const LOSS_OR_THIN = 'loss_thin'
const MARGIN_STATES: Record<string, string[]> = { [LOSS_OR_THIN]: ['loss', 'thin'] }

const KEYS = ['status', 'customer', 'q', 'from', 'to', 'margin', 'product', 'accepted_from', 'accepted_to', 'currency'] as const

/** The profit report's filters (#5329), which the page shows as one notice. */
export const REPORT_KEYS = ['product', 'accepted_from', 'accepted_to', 'currency'] as const

export function parseOrdersQuery(params: URLSearchParams): OrdersQuery {
  const query = { ...DEFAULT_ORDERS_QUERY }
  for (const key of KEYS) query[key] = params.get(key) ?? ''
  return query
}

/** The query as URL parameters, leaving out every default. */
export function toOrdersParams(query: OrdersQuery): URLSearchParams {
  const params = new URLSearchParams()
  for (const key of KEYS) {
    const value = key === 'q' ? query.q.trim() : query[key]
    if (value) params.set(key, value)
  }
  return params
}

export function isOrdersFiltered(query: OrdersQuery): boolean {
  return toOrdersParams(query).size > 0
}

/** The query narrows to a profit report row's orders. */
export function isReportFiltered(query: OrdersQuery): boolean {
  return REPORT_KEYS.some((key) => query[key] !== '')
}

/**
 * The query as `GET /api/v1/orders` parameters: `customer` is `customer_id`, and `margin`
 * is `margin_state`, repeated. An unknown `margin` value asks for nothing.
 */
export function ordersApiParams(query: OrdersQuery): URLSearchParams {
  const out = new URLSearchParams()
  for (const [key, value] of toOrdersParams(query)) {
    if (key === 'margin') for (const state of MARGIN_STATES[value] ?? []) out.append('margin_state', state)
    else out.set(key === 'customer' ? 'customer_id' : key, value)
  }
  return out
}

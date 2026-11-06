/**
 * The Orders page's filters, kept in the URL (`?status=&customer=&q=&from=&to=&margin=`) so
 * back/forward and deep links work, as the Activity page's are (`activityQuery.ts`).
 * `customer` is the API's `customer_id`. `margin` is `loss_thin` for the "Loss or thin"
 * filter (#5327), the API's `margin_state=loss&margin_state=thin`.
 */
export type OrdersQuery = {
  status: string
  customer: string
  q: string
  from: string
  to: string
  margin: string
}

export const DEFAULT_ORDERS_QUERY: OrdersQuery = { status: '', customer: '', q: '', from: '', to: '', margin: '' }

/** The "Loss or thin" filter's value, and the margin states it asks for. */
export const LOSS_OR_THIN = 'loss_thin'
const MARGIN_STATES: Record<string, string[]> = { [LOSS_OR_THIN]: ['loss', 'thin'] }

const KEYS = ['status', 'customer', 'q', 'from', 'to', 'margin'] as const

export function parseOrdersQuery(params: URLSearchParams): OrdersQuery {
  return {
    status: params.get('status') ?? '',
    customer: params.get('customer') ?? '',
    q: params.get('q') ?? '',
    from: params.get('from') ?? '',
    to: params.get('to') ?? '',
    margin: params.get('margin') ?? '',
  }
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

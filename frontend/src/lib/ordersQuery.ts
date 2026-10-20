/**
 * The Orders page's filters, kept in the URL (`?status=&customer=&q=&from=&to=`) so
 * back/forward and deep links work, as the Activity page's are (`activityQuery.ts`).
 * `customer` is the API's `customer_id`.
 */
export type OrdersQuery = {
  status: string
  customer: string
  q: string
  from: string
  to: string
}

export const DEFAULT_ORDERS_QUERY: OrdersQuery = { status: '', customer: '', q: '', from: '', to: '' }

const KEYS = ['status', 'customer', 'q', 'from', 'to'] as const

export function parseOrdersQuery(params: URLSearchParams): OrdersQuery {
  return {
    status: params.get('status') ?? '',
    customer: params.get('customer') ?? '',
    q: params.get('q') ?? '',
    from: params.get('from') ?? '',
    to: params.get('to') ?? '',
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

/** The query as `GET /api/v1/orders` parameters (`customer` is `customer_id`). */
export function ordersApiParams(query: OrdersQuery): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of toOrdersParams(query)) out[key === 'customer' ? 'customer_id' : key] = value
  return out
}

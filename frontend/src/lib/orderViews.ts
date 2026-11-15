import { DEFAULT_ORDERS_QUERY, type OrdersQuery } from './ordersQuery'

/**
 * #6334 — the Orders page's views: each a fixed set of URL filters, so a view is a link
 * like any other filter. The active view is the one whose filters the query holds exactly,
 * the search and `old` aside (they go with every view).
 */
export interface OrderView {
  key: string
  label: string
  /** What the view sets; every other filter is cleared. */
  filters: Partial<OrdersQuery>
  /** Said under the views while it is on. */
  rule: string
}

export const ORDER_VIEWS: readonly OrderView[] = [
  { key: 'all', label: 'All', filters: {}, rule: 'Every order.' },
  {
    key: 'needs_action',
    label: 'Needs action',
    filters: { status: 'draft,accepted,ready' },
    rule: 'Draft, accepted or ready: the next step is yours (quote it, start it, ship it).',
  },
  { key: 'in_progress', label: 'In progress', filters: { status: 'accepted,in_production' }, rule: 'Accepted or in production.' },
  { key: 'ready', label: 'Ready to ship', filters: { status: 'ready' }, rule: 'Ready: every item made, waiting to ship.' },
  { key: 'owing', label: 'Awaiting payment', filters: { owing: true }, rule: 'A payment request is open, pending or failed.' },
  { key: 'closed', label: 'Recently closed', filters: { status: 'closed,cancelled' }, rule: 'Closed or cancelled.' },
]

/** The query for `view`: its filters, with the search and `old` kept. */
export function applyView(query: OrdersQuery, view: OrderView): OrdersQuery {
  return { ...DEFAULT_ORDERS_QUERY, ...view.filters, q: query.q, old: query.old, ended_since: query.ended_since }
}

/** The view whose filters the query holds exactly, or null for a query of its own. */
export function viewOf(query: OrdersQuery): OrderView | null {
  const bare = (q: OrdersQuery) => JSON.stringify({ ...q, q: '', old: false, ended_since: '' })
  return ORDER_VIEWS.find((view) => bare(applyView(query, view)) === bare(query)) ?? null
}

/** The choices for how many days a closed or cancelled order stays listed. */
export const HIDE_DAYS = [7, 14, 30, 60, 90] as const
export const DEFAULT_HIDE_DAYS = 30

/**
 * The bound the API's `ended_since` takes: local midnight `days` days before `now`, so the
 * value (and the list's cache key) changes once a day, not on every render.
 */
export function endedSince(days: number, now: Date = new Date()): string {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - days)
  return day.toISOString()
}

/**
 * What this browser remembers for the Orders page: the days, whether to remember the view,
 * the last view's URL query, and the items table's groups. Storage that throws (private
 * mode, blocked site data, Bambuddy's sandboxed iframe) keeps it in memory for this page
 * only, as the Prints view does (`printsQuery.ts`).
 */
export const ORDERS_MEMORY_KEY = 'scadbuddy.orders.list'

export interface OrdersMemory {
  hideDays: number
  remember: boolean
  /** The last query, as URL parameters; '' is All. */
  query: string
  /** The items table's groups that are open, by key (`fulfilled`, `cancelled`). */
  itemGroups: Record<string, boolean>
}

export const DEFAULT_MEMORY: OrdersMemory = { hideDays: DEFAULT_HIDE_DAYS, remember: true, query: '', itemGroups: { fulfilled: true, cancelled: false } }

let fallback: OrdersMemory | null = null

export function readOrdersMemory(): OrdersMemory {
  if (fallback) return fallback
  try {
    const raw = window.localStorage.getItem(ORDERS_MEMORY_KEY)
    if (!raw) return DEFAULT_MEMORY
    const read = JSON.parse(raw) as Partial<OrdersMemory>
    return {
      hideDays: (HIDE_DAYS as readonly number[]).includes(read.hideDays ?? -1) ? (read.hideDays as number) : DEFAULT_HIDE_DAYS,
      remember: read.remember !== false,
      query: typeof read.query === 'string' ? read.query : '',
      itemGroups: { ...DEFAULT_MEMORY.itemGroups, ...(typeof read.itemGroups === 'object' && read.itemGroups ? read.itemGroups : {}) },
    }
  } catch {
    return DEFAULT_MEMORY
  }
}

export function storeOrdersMemory(change: Partial<OrdersMemory>): OrdersMemory {
  const next = { ...readOrdersMemory(), ...change }
  try {
    window.localStorage.setItem(ORDERS_MEMORY_KEY, JSON.stringify(next))
    fallback = null
  } catch {
    fallback = next
  }
  return next
}

/** For tests: forget the in-memory choice. */
export function resetOrdersMemory(): void {
  fallback = null
}

import type { OrderItemDetail } from '../../api/types'

/**
 * #6334 — an order's items in the order a reader works them: the live lines first, then
 * the fulfilled ones, then the cancelled ones, each kept in its own order. The first
 * non-empty kind is the table's own rows; each later kind is a group that opens and
 * closes. So an order whose items are all fulfilled (or all cancelled) lists them plainly.
 */
export type ItemGroupKey = 'fulfilled' | 'cancelled'

export interface ItemGroups {
  main: OrderItemDetail[]
  /** The groups after the main rows, in order; a group with no items is left out. */
  groups: { key: ItemGroupKey; items: OrderItemDetail[] }[]
}

export function groupItems(items: OrderItemDetail[]): ItemGroups {
  const live = items.filter((item) => item.status !== 'fulfilled' && item.status !== 'cancelled')
  const fulfilled = items.filter((item) => item.status === 'fulfilled')
  const cancelled = items.filter((item) => item.status === 'cancelled')
  const kinds: [ItemGroupKey | 'live', OrderItemDetail[]][] = [
    ['live', live],
    ['fulfilled', fulfilled],
    ['cancelled', cancelled],
  ]
  const present = kinds.filter(([, rows]) => rows.length > 0)
  const [first, ...rest] = present
  return {
    main: first?.[1] ?? [],
    groups: rest.map(([key, rows]) => ({ key: key as ItemGroupKey, items: rows })),
  }
}

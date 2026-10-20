import type { OrderItemDetail } from '../../api/types'

/** The accepted quote's total, the item's price (spec 2026-10-18 §3.4); null while unpriced. */
export function itemPrice(item: OrderItemDetail): number | null {
  return item.quotes.find((q) => q.accepted)?.total_minor ?? null
}

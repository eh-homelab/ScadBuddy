import type { OrderItemDetail } from '../../api/types'

/**
 * The item's price as the order's totals count it (`api/orders.py` `item_price`): the
 * accepted quote's total, or before one is accepted the newest quote's unit price times
 * the quantity now, so a draft adds up as it is entered. Null with no quote at all.
 */
export function itemPrice(item: OrderItemDetail): { amount: number; accepted: boolean } | null {
  const accepted = item.quotes.find((q) => q.accepted)
  if (accepted) return { amount: accepted.total_minor, accepted: true }
  const newest = [...item.quotes].sort(byCreated).at(-1)
  return newest ? { amount: newest.unit_price_minor * item.quantity, accepted: false } : null
}

/**
 * Oldest first by `created_at` as an instant, then `id`, as the backend's `max` over
 * `(created_at, id)`. Never by the text: `…00Z` (no fraction) sorts after `…00.123Z`.
 */
export function byCreated(a: { created_at: string; id: string }, b: { created_at: string; id: string }): number {
  return Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id)
}

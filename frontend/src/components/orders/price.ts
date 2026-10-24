import type { OrderItemDetail } from '../../api/types'

/**
 * The item's price as the order's totals count it (`api/orders.py` `item_price`): the
 * accepted quote's total, or before one is accepted the newest quote's unit price times
 * the quantity now, so a draft adds up as it is entered. Null with no quote at all.
 */
export function itemPrice(item: OrderItemDetail): { amount: number; accepted: boolean } | null {
  const accepted = item.quotes.find((q) => q.accepted)
  if (accepted) return { amount: accepted.total_minor, accepted: true }
  const newest = [...item.quotes].sort((a, b) => (a.created_at === b.created_at ? a.id.localeCompare(b.id) : a.created_at.localeCompare(b.created_at))).at(-1)
  return newest ? { amount: newest.unit_price_minor * item.quantity, accepted: false } : null
}

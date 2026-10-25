import { api } from '../../api/client'
import type { ManualQuoteInput, OrderItemDetail, OrderStatus, Quote } from '../../api/types'
import { toMinor } from '../../lib/money'

/** The order statuses that take a price (§3.4): past `quoted` the accepted prices are pinned. */
export const PRICED_WHILE: ReadonlySet<OrderStatus> = new Set<OrderStatus>(['draft', 'quoted'])

/**
 * The quote that prices the item (`api/orders.py` `item_price`): the accepted one, or
 * before one is accepted the newest by `byCreated`. Null with no quote at all.
 */
export function pricingQuote(item: OrderItemDetail): { quote: Quote; accepted: boolean } | null {
  const accepted = item.quotes.find((q) => q.accepted)
  if (accepted) return { quote: accepted, accepted: true }
  const newest = [...item.quotes].sort(byCreated).at(-1)
  return newest ? { quote: newest, accepted: false } : null
}

/**
 * The item's price as the order's totals count it (`api/orders.py` `item_price`): the
 * accepted quote's total, or before one is accepted the newest quote's unit price times
 * the quantity now, so a draft adds up as it is entered. Null with no quote at all.
 */
export function itemPrice(item: OrderItemDetail): { amount: number; accepted: boolean } | null {
  const priced = pricingQuote(item)
  if (!priced) return null
  const { quote, accepted } = priced
  return { amount: accepted ? quote.total_minor : quote.unit_price_minor * item.quantity, accepted }
}

/** The item's unit price in minor units, read off the same quote `itemPrice` counts. */
export function unitPrice(item: OrderItemDetail): { minor: number; accepted: boolean } | null {
  const priced = pricingQuote(item)
  return priced ? { minor: priced.quote.unit_price_minor, accepted: priced.accepted } : null
}

/** What to say when a typed unit price is not one. */
export const UNIT_PRICE_HINT = 'Enter a price per unit, such as 12.50.'

/**
 * A typed unit price as minor units at the order's `minor_exponent` (§3.6: never a float,
 * never `Intl`'s digits), or null for anything that is not a price: text, a negative
 * amount, or one finer than the currency's minor unit.
 */
export function parseUnitPrice(text: string, exponent: number): number | null {
  const minor = toMinor(text, exponent)
  return minor === null || minor < 0 ? null : minor
}

/**
 * A typed (`manual`) quote for the item, accepted at once when `accept`: the drawer's
 * "Record and accept" (`QuoteEditor`) and the items table's unit price alike. A refused
 * accept leaves the quote recorded, as the backend keeps it, and throws.
 */
export async function recordQuote(orderId: string, itemId: string, body: ManualQuoteInput, accept: boolean): Promise<Quote> {
  const quote = await api.quoteOrderItem(orderId, itemId, body)
  if (accept) await api.acceptOrderQuote(orderId, itemId, quote.id)
  return quote
}

/**
 * Oldest first by `created_at` as an instant, then `id`, as the backend's `max` over
 * `(created_at, id)`. Never by the text: `…00Z` (no fraction) sorts after `…00.123Z`.
 */
export function byCreated(a: { created_at: string; id: string }, b: { created_at: string; id: string }): number {
  return Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id)
}

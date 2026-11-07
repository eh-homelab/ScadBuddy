import { api } from '../../api/client'
import type { Order, OrderItem, OrderItemCreate, Quote } from '../../api/types'

/**
 * #5349 — the backend's rules for adding an item (`orders/lifecycle.py` `_takes_item`,
 * `item_open`), which the order page, `AddItemDialog` and `AddToOrderDialog` follow.
 */

/** The states an order's items and prices change in freely. */
export const OPEN_FOR_EDITS: ReadonlySet<string> = new Set(['draft', 'quoted'])
/** The states past acceptance an order still takes a new item in. */
export const TAKES_ITEMS: ReadonlySet<string> = new Set(['accepted', 'in_production', 'ready'])

/** How an item is added to the order now: on its plain route, after acceptance, or not at all
 * (shipped, ended, or on hold past `quoted`). */
export type AddMode = 'open' | 'after_acceptance'

export function addMode(order: Pick<Order, 'status' | 'on_hold'>): AddMode | null {
  if (OPEN_FOR_EDITS.has(order.status)) return 'open'
  if (TAKES_ITEMS.has(order.status) && !order.on_hold) return 'after_acceptance'
  return null
}

/** Whether the item's design, quantity and price still change: on an open order, or, past
 * `quoted`, while the item has no accepted quote (one added after acceptance). */
export function itemOpen(order: Pick<Order, 'status'>, quotes: readonly Pick<Quote, 'accepted'>[]): boolean {
  if (OPEN_FOR_EDITS.has(order.status)) return true
  return TAKES_ITEMS.has(order.status) && !quotes.some((quote) => quote.accepted)
}

/** What the confirm says before an item is added to an accepted order. */
export function afterAcceptanceWarning(number: string): string {
  return (
    `${number} is already accepted. Adding an item changes what the customer gets. ` +
    'The new item needs its own quote, and the customer must accept that quote before it is made. ' +
    'The accepted prices and charges do not change.'
  )
}

/** Adds the item on the route `mode` names. After acceptance a custom item's price is never
 * accepted as it is saved. */
export function addItem(orderId: string, body: OrderItemCreate, mode: AddMode): Promise<OrderItem> {
  if (mode === 'open') return api.addOrderItem(orderId, body)
  return api.addOrderItemAfterAcceptance(orderId, body.kind === 'custom' ? { ...body, accept: false } : body)
}

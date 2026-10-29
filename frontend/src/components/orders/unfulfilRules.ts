import type { Order, OrderItemDetail, Shipment } from '../../api/types'

const ENDED = new Set(['closed', 'cancelled'])

/** Whether the item offers Unfulfil (#4952): a fulfilled item of an order not ended. */
export function offersUnfulfil(order: Pick<Order, 'status'>, item: Pick<OrderItemDetail, 'status'>): boolean {
  return item.status === 'fulfilled' && !ENDED.has(order.status)
}

/**
 * Why Unfulfil cannot run now, as the backend would refuse it, or null when it can: an
 * order on hold (`order_on_hold`), or an item a shipment carries (`order_item_shipped`).
 */
export function unfulfilBlock(order: Pick<Order, 'on_hold'>, shipments: Shipment[], item: Pick<OrderItemDetail, 'id'>): string | null {
  if (order.on_hold) return 'The order is on hold. Release it first.'
  if (shipments.some((s) => s.items.some((line) => line.order_item_id === item.id))) {
    return 'A shipment carries this item. Take it off the shipment first.'
  }
  return null
}

import type { Order, OrderItemDetail, Shipment } from '../../api/types'
import { isCart, OPEN_FOR_EDITS, priceOpen, TAKES_ITEMS } from './afterAcceptance'
import { orderItemSource } from '../../lib/printSource'
import { offersUnfulfil, UNFULFIL_OFF, unfulfilBlock } from './unfulfilRules'

const ENDED = new Set(['closed', 'cancelled'])
const IN_PRODUCTION = new Set(['accepted', 'in_production', 'ready', 'shipped'])
const ON_HOLD = 'The order is on hold. Release it first.'
/** Why a slice quote is refused for an item with no output or library file: the drawer's words. */
export const NO_SLICE_SOURCE = 'Render the item first: a slice quote slices its output.'

/** Whether the item can be printed now: a model or library file item of an accepted order
 * not on hold, `approved` (or `failed`, for a reprint), with the output (or the library
 * file) its quote was made from. */
export function offersPrint(order: Order, item: OrderItemDetail): boolean {
  return (
    orderItemSource(item) !== null &&
    IN_PRODUCTION.has(order.status) &&
    !order.on_hold &&
    (item.status === 'approved' || item.status === 'failed') &&
    item.quotes.some((quote) => quote.accepted)
  )
}

/** The drawer's sections an action opens it at. */
export type ItemSection = 'quote' | 'print' | 'fulfil'

export type ItemActionKey = 'open' | 'quote' | 'print' | 'fulfil' | 'unfulfil' | 'reopen' | 'delete' | 'cancel'

export interface ItemAction {
  key: ItemActionKey
  label: string
  /** Why the action is refused now, in words; null when it runs. */
  blocked: string | null
}

/**
 * #4955, spec 2026-11-07 §4.1 — the row's ⋯ menu: the actions allowed on the item now, in
 * order, each refused one with its reason. The rules are the drawer's (`OrderPage.tsx`
 * `ItemDrawer`), so the menu and the drawer offer the same things.
 */
export function itemActions(
  order: Order,
  item: OrderItemDetail,
  { shipments, unfulfilAllowed }: { shipments: Shipment[]; unfulfilAllowed: boolean },
): ItemAction[] {
  const actions: ItemAction[] = [{ key: 'open', label: 'Open details', blocked: null }]
  // A visitor's cart (#5282): nobody quotes, prints or settles it from the back office.
  if (ENDED.has(order.status) || isCart(order)) return actions
  const settled = item.status === 'fulfilled' || item.status === 'cancelled'
  if (item.kind !== 'custom' && !settled && priceOpen(order, item, item.quotes)) {
    actions.push({ key: 'quote', label: 'Quote from a slice…', blocked: item.output_id || item.library_file ? null : NO_SLICE_SOURCE })
  }
  // Print's own rule, with the hold left out: on hold it shows, refused, with the reason.
  if (offersPrint({ ...order, on_hold: false }, item)) {
    actions.push({ key: 'print', label: 'Print…', blocked: order.on_hold ? ON_HOLD : null })
  }
  if (IN_PRODUCTION.has(order.status) && !settled) {
    actions.push({ key: 'fulfil', label: 'Fulfil…', blocked: order.on_hold ? ON_HOLD : null })
  }
  if (offersUnfulfil(order, item)) {
    actions.push({ key: 'unfulfil', label: 'Unfulfil…', blocked: unfulfilAllowed ? unfulfilBlock(order, shipments, item) : UNFULFIL_OFF })
  }
  if (item.status === 'cancelled') {
    const takesBack = OPEN_FOR_EDITS.has(order.status) || TAKES_ITEMS.has(order.status) || item.quotes.some((q) => q.accepted)
    actions.push({
      key: 'reopen',
      label: 'Reopen item',
      // `item_not_priced`: a shipped order takes back a priced item only (#5349).
      blocked: takesBack ? null : "This item had no accepted price, so it can't come back into a shipped order.",
    })
  }
  if (!settled) {
    actions.push(item.status === 'draft' ? { key: 'delete', label: 'Delete item', blocked: null } : { key: 'cancel', label: 'Cancel item', blocked: null })
  }
  return actions
}

export type ItemSourceName = 'model' | 'output' | 'file' | 'past print' | 'custom' | 'request'

/** #4955 — the row's source chip: where the item came from. */
export function itemSource(item: Pick<OrderItemDetail, 'kind' | 'output_id' | 'origin_archive_ids'>): ItemSourceName {
  if ((item.origin_archive_ids ?? []).length > 0) return 'past print'
  if (item.kind === 'custom') return 'custom'
  if (item.kind === 'request') return 'request'
  if (item.kind === 'library_file') return 'file'
  return item.output_id ? 'output' : 'model'
}

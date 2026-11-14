import { HttpResponse, http } from 'msw'
import type { OrderItemSource, PurchaseRecordInput, PurchaseSource, PurchaseUpdateInput } from '../../../api/types'
import { event, lock, notEnded, refused, transition } from './orders'
import { Refused, now, state } from './state'

/**
 * #6133 (spec 2026-11-13 §7, §10, §11) — a storefront customer's files and links on the
 * order page: the check's preview, a staged file's download, "Check again", "Decline",
 * "Retry import", and the things to buy (`backend/scadbuddy/api/order_sources.py`,
 * `api/order_purchases.py`, `orders/lifecycle.py`). The sources live in the orders
 * mocks' state (`state.ts` `sources`); a recorded purchase adds its `purchase` charge.
 */

const BASE = '/api/v1/orders/:id'

/** A 1×1 PNG, as bytes: the preview the mock serves for every checked file. */
const PREVIEW_PNG = new Uint8Array([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00,
  0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0x00, 0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0d, 0x0a, 0x2d,
  0xb4, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
])

function sourceOf(orderId: string, sourceId: string): OrderItemSource {
  lock(orderId)
  const found = state.sources.find((s) => s.id === sourceId && s.order_id === orderId)
  if (!found) throw new Refused('source_not_found', `no source ${sourceId}`, [], true)
  return found
}

function purchaseOf(orderId: string, sourceId: string): OrderItemSource {
  const found = sourceOf(orderId, sourceId)
  if (found.purpose !== 'purchase') throw new Refused('source_not_found', `no purchase link ${sourceId}`, [], true)
  return found
}

function asPurchase(s: OrderItemSource): PurchaseSource {
  return {
    id: s.id,
    order_id: s.order_id,
    item_id: s.item_id,
    url: s.url ?? '',
    note: s.note,
    status: s.status as PurchaseSource['status'],
    amount_minor: s.amount_minor ?? null,
    cost_kind: s.cost_kind ?? null,
    cost_entry_id: null,
    charge_id: s.status === 'recorded' ? `h-${s.id}` : null,
    shop_note: s.shop_note,
  }
}

/** `Lifecycle._purchase_open`: a purchase changes only while the order is `draft` or `quoted`. */
function purchaseOpen(orderId: string) {
  const order = lock(orderId)
  if (order.status !== 'draft' && order.status !== 'quoted') throw new Refused('purchase_locked', `order ${order.number} is ${order.status}`)
  return order
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

function setPurchaseCharge(s: OrderItemSource, amount: number | null) {
  state.charges = state.charges.filter((c) => c.id !== `h-${s.id}`)
  if (amount === null) return
  const position = state.charges.filter((c) => c.order_id === s.order_id && c.kind === 'purchase').length
  state.charges.push({
    id: `h-${s.id}`,
    order_id: s.order_id,
    kind: 'purchase',
    label: `Bought for your project: ${s.note || hostOf(s.url ?? '')}`.slice(0, 200),
    amount_minor: amount,
    position,
    auto: false,
  })
}

export const handlers = [
  http.get(`${BASE}/sources/:source/preview`, ({ request, params }) => {
    try {
      const found = sourceOf(String(params.id), String(params.source))
      if (!found.has_preview) return HttpResponse.json({ detail: 'no preview' }, { status: 404 })
    } catch (error) {
      return refused(request, error)
    }
    return new HttpResponse(PREVIEW_PNG, {
      headers: { 'Content-Type': 'image/png', 'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=60' },
    })
  }),
  http.get(`${BASE}/sources/:source/download`, ({ request, params }) =>
    transition(request, () => {
      const found = sourceOf(String(params.id), String(params.source))
      if (found.kind !== 'file' || !found.staged) throw new Refused('source_not_staged', `source ${found.id} has no staged file`)
      return { url: `https://store.example.test/staging/${found.id}.frozen?X-Amz-Signature=mock`, expires_at: new Date(Date.now() + 5 * 60_000).toISOString() }
    }),
  ),
  http.post(`${BASE}/sources/:source/check`, ({ request, params }) =>
    transition(request, () => {
      notEnded(lock(String(params.id)))
      const found = sourceOf(String(params.id), String(params.source))
      if (found.kind !== 'file' || (found.check_status !== 'failed' && found.check_status !== 'checking')) {
        throw new Refused('source_not_failed', `source ${found.id} has no failed check to run again`)
      }
      if (!found.staged) throw new Refused('source_not_staged', `source ${found.id} has no staged file to check`)
      if (found.check_status === 'failed') {
        found.check_status = 'checking'
        found.check_reason = null
        event(lock(String(params.id)), 'source.rechecked', { source_id: found.id, item_id: found.item_id })
      }
      return found
    }),
  ),
  http.post(`${BASE}/sources/:source/decline`, async ({ request, params }) => {
    const body = (await request.json().catch(() => ({}))) as { reason?: string }
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = sourceOf(order.id, String(params.source))
      if (found.purpose !== 'print') throw new Refused('source_not_print', `source ${found.id} is a thing to buy, not a model`)
      if (found.status === 'declined') return found
      if (found.status === 'imported') throw new Refused('source_imported', `source ${found.id} is in the library already`)
      found.status = 'declined'
      found.status_reason = (body.reason ?? '').trim() || 'Declined by the shop.'
      found.import_stuck = false
      // The file's watcher deletes its staged copy.
      found.staged = false
      event(order, 'source.declined', { source_id: found.id, item_id: found.item_id })
      return found
    })
  }),
  http.post(`${BASE}/sources/:source/retry-import`, ({ request, params }) =>
    transition(request, () => {
      const found = sourceOf(String(params.id), String(params.source))
      if (!found.import_stuck || !found.staged) throw new Refused('import_not_stuck', `source ${found.id} has no stuck import to retry`)
      event(lock(String(params.id)), 'source.import_retry', { source_id: found.id, item_id: found.item_id })
      return found
    }),
  ),
  http.get(`${BASE}/purchases`, ({ request, params }) =>
    transition(request, () => {
      lock(String(params.id))
      return state.sources.filter((s) => s.order_id === String(params.id) && s.purpose === 'purchase').map(asPurchase)
    }),
  ),
  http.post(`${BASE}/purchases/:source/record`, async ({ request, params }) => {
    const body = (await request.json()) as PurchaseRecordInput
    try {
      const found = purchaseOf(String(params.id), String(params.source))
      if (found.status === 'recorded') return HttpResponse.json(asPurchase(found))
      if (found.status === 'not_needed') throw new Refused('purchase_not_pending', `purchase link ${found.id} is not needed`)
      const order = purchaseOpen(found.order_id)
      found.status = 'recorded'
      found.amount_minor = body.amount_minor
      found.cost_kind = body.cost_kind ?? 'material'
      setPurchaseCharge(found, body.amount_minor)
      event(order, 'purchase.recorded', { source_id: found.id, amount_minor: body.amount_minor, at: now() })
      return HttpResponse.json(asPurchase(found), { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.patch(`${BASE}/purchases/:source`, async ({ request, params }) => {
    const body = (await request.json()) as PurchaseUpdateInput
    return transition(request, () => {
      const found = purchaseOf(String(params.id), String(params.source))
      if (found.status !== 'recorded') throw new Refused('purchase_not_recorded', `purchase link ${found.id} is not recorded`)
      const order = purchaseOpen(found.order_id)
      if (body.amount_minor != null) {
        found.amount_minor = body.amount_minor
        setPurchaseCharge(found, body.amount_minor)
      }
      if (body.cost_kind != null) found.cost_kind = body.cost_kind
      event(order, 'purchase.updated', { source_id: found.id })
      return asPurchase(found)
    })
  }),
  http.post(`${BASE}/purchases/:source/decline`, async ({ request, params }) => {
    const body = (await request.json().catch(() => ({}))) as { shop_note?: string }
    return transition(request, () => {
      const found = purchaseOf(String(params.id), String(params.source))
      if (found.status === 'not_needed') return asPurchase(found)
      const order = purchaseOpen(found.order_id)
      found.status = 'not_needed'
      found.amount_minor = null
      found.cost_kind = null
      found.shop_note = (body.shop_note ?? '').trim()
      setPurchaseCharge(found, null)
      event(order, 'purchase.declined', { source_id: found.id })
      return asPurchase(found)
    })
  }),
]

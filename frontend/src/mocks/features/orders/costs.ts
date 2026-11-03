import { HttpResponse, http } from 'msw'
import type { AttemptCost, ItemCost, OrderCosts, Quote } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { minorExponent, problem, state } from './state'

/**
 * #4251 §6.4 — `GET /orders/{id}/costs` (`backend/scadbuddy/orders/costs.py`): each item's
 * accepted quote beside its attempts, the attempts' grams priced at the quote's own price
 * per gram, failures included. With a Bambuddy URL, an attempt with archives has a
 * Bambuddy cost of 0.05 a gram, shown and never summed.
 */

type Plate = { print_seconds: number; filaments: { used_g: string }[] }

function perUnit(quote: Quote | undefined): { seconds: number; grams: number; filament: number } | null {
  if (!quote || quote.source !== 'slice' || quote.plates.length === 0) return null
  const plates = quote.plates as unknown as Plate[]
  return {
    seconds: plates.reduce((sum, p) => sum + p.print_seconds, 0),
    grams: plates.reduce((sum, p) => sum + p.filaments.reduce((s, f) => s + Number(f.used_g), 0), 0),
    filament: quote.lines.filter((l) => l.kind === 'filament').reduce((sum, l) => sum + l.amount_minor, 0),
  }
}

function sum(values: (number | null | undefined)[]): number | null {
  const known = values.filter((v): v is number => v != null)
  return known.length ? known.reduce((a, b) => a + b, 0) : null
}

const grams = (value: number | null) => (value === null ? null : Math.round(value * 100) / 100)

export function costsOf(orderId: string): OrderCosts | null {
  const order = state.orders.find((o) => o.id === orderId)
  if (!order) return null
  const linked = Boolean(mockSettings().bambuddy_url)
  const items: ItemCost[] = state.items
    .filter((i) => i.order_id === order.id)
    .map((item) => {
      const quote = state.quotes.find((q) => q.order_item_id === item.id && q.accepted)
      const unit = perUnit(quote)
      const rate = unit && unit.grams > 0 ? unit.filament / unit.grams : null
      const attempts: AttemptCost[] = state.fulfilments
        .filter((f) => f.order_item_id === item.id)
        .map((f) => ({
          fulfilment_id: f.id,
          attempt: f.attempt,
          outcome: f.outcome,
          quantity: f.quantity,
          accepted: f.accepted_at !== null,
          actual_seconds: f.actual_seconds ?? null,
          actual_grams: f.actual_grams ?? null,
          filament_minor: f.actual_grams != null && rate !== null ? Math.round(f.actual_grams * rate) : null,
          archive_ids: f.archive_ids ?? [],
          bambuddy_cost: linked && f.actual_grams != null && (f.archive_ids ?? []).length ? Math.round(f.actual_grams * 5) / 100 : null,
        }))
      const failed = attempts.filter((a) => a.outcome === 'failed')
      return {
        item_id: item.id,
        title: item.title,
        status: item.status,
        quantity: item.quantity,
        quote_id: quote?.id ?? null,
        quote_source: quote?.source ?? null,
        quoted_seconds: unit ? unit.seconds * item.quantity : null,
        quoted_grams: unit ? grams(unit.grams * item.quantity) : null,
        quoted_filament_minor: unit ? unit.filament * item.quantity : null,
        quoted_cost_minor: quote ? quote.unit_cost_minor * item.quantity : null,
        quoted_price_minor: quote ? quote.total_minor : null,
        actual_seconds: sum(attempts.map((a) => a.actual_seconds)),
        actual_grams: grams(sum(attempts.map((a) => a.actual_grams))),
        actual_filament_minor: sum(attempts.map((a) => a.filament_minor)),
        failed_attempts: failed.length,
        failed_grams: grams(sum(failed.map((a) => a.actual_grams))),
        failed_filament_minor: sum(failed.map((a) => a.filament_minor)),
        attempts,
      }
    })
  return {
    order_id: order.id,
    currency: order.currency,
    minor_exponent: minorExponent(order.currency),
    items,
    quoted_seconds: sum(items.map((i) => i.quoted_seconds)),
    quoted_grams: grams(sum(items.map((i) => i.quoted_grams))),
    quoted_filament_minor: sum(items.map((i) => i.quoted_filament_minor)),
    actual_seconds: sum(items.map((i) => i.actual_seconds)),
    actual_grams: grams(sum(items.map((i) => i.actual_grams))),
    actual_filament_minor: sum(items.map((i) => i.actual_filament_minor)),
    bambuddy_unavailable: false,
  }
}

export const handlers = [
  http.get('/api/v1/orders/:id/costs', ({ request, params }) => {
    const costs = costsOf(String(params.id))
    return costs ? HttpResponse.json(costs) : problem(request, 404, `no order '${String(params.id)}'`)
  }),
]

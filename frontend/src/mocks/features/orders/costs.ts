import { HttpResponse, http } from 'msw'
import type {
  AttemptCost,
  CostEntry,
  CostEntryCreate,
  Fulfilment,
  ItemCost,
  Order,
  OrderCosts,
  OrderItem,
  Quote,
  TimeEntry,
  TimeEntryCreate,
} from '../../../api/types'
import { mockSettings } from '../../handlers'
import { event, itemOf, lock, refused } from './orders'
import { BROWSER, Refused, fieldProblem, minorExponent, newId, now, problem, state } from './state'

/**
 * #4251 §6.4, #5326 — `GET /orders/{id}/costs` (`backend/scadbuddy/orders/costs.py`) and
 * the time and cost entries' routes. Each attempt is costed from its actual figures, else
 * its quote's (estimated); at the quote's rates, else the price book and today's machine
 * rate (estimated). An item adds its labour (entries, else a default) and its cost
 * entries; the order its own entries, its shipments and every charge but tax. With a
 * Bambuddy URL, an attempt with archives has a Bambuddy cost of 0.05 a gram, shown and
 * never summed.
 */

type Plate = { print_seconds: number; filaments: { slot: number; used_g: string }[] }
type Part = 'prints' | 'labour'
type Figure = AttemptCost['estimated'] extends (infer F)[] | undefined ? F : never

class Unit {
  seconds: number
  grams: number
  filament: number
  slotGrams: Record<number, number> = {}
  perGram: number | null
  perSecond: number | null
  constructor(quote: Quote) {
    const plates = quote.plates as unknown as Plate[]
    this.seconds = plates.reduce((sum, p) => sum + p.print_seconds, 0)
    this.grams = 0
    for (const plate of plates) {
      for (const used of plate.filaments) {
        const grams = Number(used.used_g)
        this.grams += grams
        // A stored plate's slot is the item's colour slot already (`quote.py` `read_plate`).
        this.slotGrams[used.slot] = (this.slotGrams[used.slot] ?? 0) + grams
      }
    }
    this.filament = quote.lines.filter((l) => l.kind === 'filament').reduce((sum, l) => sum + l.amount_minor, 0)
    const machine = quote.lines.filter((l) => l.kind === 'machine_time')
    this.perGram = this.grams > 0 ? this.filament / this.grams : null
    this.perSecond = machine.length && this.seconds > 0 ? machine.reduce((s, l) => s + l.amount_minor, 0) / this.seconds : null
  }
}

const unitOf = (quote: Quote | undefined) => (quote && quote.source === 'slice' && quote.plates.length ? new Unit(quote) : null)

function sum(values: (number | null | undefined)[]): number | null {
  const known = values.filter((v): v is number => v != null)
  return known.length ? known.reduce((a, b) => a + b, 0) : null
}

const grams = (value: number | null) => (value === null ? null : Math.round(value * 100) / 100)
const round = (value: number | null) => (value === null ? null : Math.round(value))

function margin<T extends { revenue_minor?: number | null; cost_minor?: number; unknown?: Part[] }>(view: T, thin: number): T {
  if (view.revenue_minor == null) return { ...view, margin_minor: null, margin_percent: null, margin_state: null }
  const minor = view.revenue_minor - (view.cost_minor ?? 0)
  const percent = view.revenue_minor > 0 ? Math.round((minor * 1000) / view.revenue_minor) / 10 : null
  const state = minor < 0 ? 'loss' : (view.unknown ?? []).length ? 'unknown' : percent === null ? null : percent < thin ? 'thin' : 'ok'
  return { ...view, margin_minor: minor, margin_percent: percent, margin_state: state }
}

function entriesLabour(entries: TimeEntry[]): [number, boolean] {
  let total = 0
  let unknown = false
  for (const e of entries) {
    if (e.rate_minor_per_hour == null) unknown = true
    else total += Math.round((e.minutes * e.rate_minor_per_hour) / 60)
  }
  return [total, unknown]
}

function itemCost(order: Order, item: OrderItem, time: TimeEntry[], other: CostEntry[], thin: number): ItemCost {
  const settings = mockSettings()
  const today = order.currency === settings.orders_currency
  const exp = 10 ** minorExponent(order.currency)
  const quotes = state.quotes.filter((q) => q.order_item_id === item.id)
  const accepted = quotes.find((q) => q.accepted)
  const linked = Boolean(settings.bambuddy_url)

  const attemptCost = (f: Fulfilment): AttemptCost => {
    const quote = (f.sliced_sha256 && quotes.find((q) => q.sliced_sha256 === f.sliced_sha256)) || accepted
    const unit = unitOf(quote)
    const units = f.copies || f.quantity || 1
    const estimated: Figure[] = []
    let g: number | null = f.actual_grams ?? null
    if (g === null && unit) {
      g = unit.grams * units
      estimated.push('grams')
    }
    let s: number | null = f.actual_seconds ?? null
    if (s === null && unit) {
      s = unit.seconds * units
      estimated.push('seconds')
    }
    let filament: number | null = null
    if (g !== null) {
      let perGram = unit?.perGram ?? null
      if (perGram === null) {
        const priced = item.colours
          .map((c) => ({ slot: c.slot, record: state.filaments.find((r) => r.id === c.filament_id) }))
          .filter((c) => c.record)
        if (today && priced.length) {
          const weights = priced.map((c) => unit?.slotGrams[c.slot] ?? 0)
          const total = weights.reduce((a, b) => a + b, 0)
          const perKg = total > 0 ? priced.reduce((a, c, i) => a + Number(c.record!.cost_per_kg) * weights[i]!, 0) / total : priced.reduce((a, c) => a + Number(c.record!.cost_per_kg), 0) / priced.length
          perGram = (perKg * exp) / 1000
          estimated.push('filament')
        }
      } else if (estimated.includes('grams')) estimated.push('filament')
      filament = perGram === null ? null : g * perGram
    }
    let machine: number | null = null
    if (s !== null) {
      let perSecond = unit?.perSecond ?? null
      if (perSecond === null && today) {
        perSecond = (Number(settings.pricing_machine_rate_per_hour) * exp) / 3600
        estimated.push('machine')
      } else if (perSecond !== null && estimated.includes('seconds')) estimated.push('machine')
      machine = perSecond === null ? null : s * perSecond
    }
    const filamentMinor = round(filament)
    const machineMinor = round(machine)
    return {
      fulfilment_id: f.id,
      attempt: f.attempt,
      purpose: f.purpose ?? 'production',
      outcome: f.outcome,
      quantity: f.quantity,
      accepted: f.accepted_at !== null,
      actual_seconds: f.actual_seconds ?? null,
      actual_grams: f.actual_grams ?? null,
      grams: grams(g),
      seconds: s,
      filament_minor: filamentMinor,
      machine_minor: machineMinor,
      cost_minor: (filamentMinor ?? 0) + (machineMinor ?? 0),
      estimated,
      unknown: filamentMinor === null || machineMinor === null,
      archive_ids: f.archive_ids ?? [],
      bambuddy_cost: linked && f.actual_grams != null && (f.archive_ids ?? []).length ? Math.round(f.actual_grams * 5) / 100 : null,
    }
  }

  const attempts = state.fulfilments.filter((f) => f.order_item_id === item.id).sort((a, b) => a.attempt - b.attempt).map(attemptCost)
  const failed = attempts.filter((a) => a.outcome === 'failed')
  const unit = unitOf(accepted)
  const estimated: Part[] = attempts.some((a) => (a.estimated ?? []).length) ? ['prints'] : []
  const unknown: Part[] = attempts.some((a) => a.unknown) ? ['prints'] : []
  let [labour, labourUnknown] = entriesLabour(time)
  let labourDefault = false
  if (!time.length && item.kind !== 'custom' && item.status !== 'cancelled') {
    labourDefault = true
    estimated.push('labour')
    const lines = accepted?.lines.filter((l) => l.kind === 'labour') ?? []
    if (lines.length) labour = lines.reduce((s, l) => s + l.amount_minor, 0) * item.quantity
    else if (today) labour = Math.round(Number(settings.pricing_labour_per_item) * exp * item.quantity)
    else labourUnknown = true
  }
  if (labourUnknown) unknown.push('labour')
  const prints = attempts.reduce((s, a) => s + (a.cost_minor ?? 0), 0)
  const others = other.reduce((s, e) => s + e.amount_minor, 0)
  return margin(
    {
      item_id: item.id,
      title: item.title,
      status: item.status,
      quantity: item.quantity,
      quote_id: accepted?.id ?? null,
      quote_source: accepted?.source ?? null,
      quoted_seconds: unit ? unit.seconds * item.quantity : null,
      quoted_grams: unit ? grams(unit.grams * item.quantity) : null,
      quoted_filament_minor: unit ? unit.filament * item.quantity : null,
      quoted_cost_minor: accepted ? accepted.unit_cost_minor * item.quantity : null,
      quoted_price_minor: accepted ? accepted.total_minor : null,
      actual_seconds: sum(attempts.map((a) => a.actual_seconds)),
      actual_grams: grams(sum(attempts.map((a) => a.actual_grams))),
      actual_filament_minor: sum(attempts.map((a) => a.filament_minor)),
      failed_attempts: failed.length,
      failed_grams: grams(sum(failed.map((a) => a.actual_grams))),
      failed_filament_minor: sum(failed.map((a) => a.filament_minor)),
      labour_default: labourDefault,
      attempts,
      time_entries: time,
      cost_entries: other,
      prints_minor: prints,
      machine_minor: attempts.reduce((s, a) => s + (a.machine_minor ?? 0), 0),
      labour_minor: labour,
      other_minor: others,
      cost_minor: prints + labour + others,
      revenue_minor: item.status === 'cancelled' ? 0 : accepted ? accepted.total_minor : null,
      estimated,
      unknown,
    },
    thin,
  )
}

export function costsOf(orderId: string): OrderCosts | null {
  const order = state.orders.find((o) => o.id === orderId)
  if (!order) return null
  const thinText = mockSettings().orders_thin_margin_percent ?? '15'
  const thin = Number(thinText)
  const time = state.timeEntries.filter((e) => e.order_id === order.id)
  const other = state.costEntries.filter((e) => e.order_id === order.id)
  const items = state.items
    .filter((i) => i.order_id === order.id)
    .sort((a, b) => a.position - b.position)
    .map((item) =>
      itemCost(
        order,
        item,
        time.filter((e) => e.order_item_id === item.id),
        other.filter((e) => e.order_item_id === item.id),
        thin,
      ),
    )
  const ownTime = time.filter((e) => !e.order_item_id)
  const ownOther = other.filter((e) => !e.order_item_id)
  const [ownLabour, ownUnknown] = entriesLabour(ownTime)
  const shipping = state.shipments.filter((s) => s.order_id === order.id).reduce((s, x) => s + (x.cost_minor ?? 0), 0)
  const charges = state.charges.filter((c) => c.order_id === order.id && c.kind !== 'tax').reduce((s, c) => s + c.amount_minor, 0)
  const open = items.filter((i) => i.status !== 'cancelled')
  const revenue = items.length && open.every((i) => i.revenue_minor != null) ? items.reduce((s, i) => s + (i.revenue_minor ?? 0), 0) + charges : null
  const labour = items.reduce((s, i) => s + (i.labour_minor ?? 0), 0) + ownLabour
  const others = items.reduce((s, i) => s + (i.other_minor ?? 0), 0) + ownOther.reduce((s, e) => s + e.amount_minor, 0)
  const prints = items.reduce((s, i) => s + (i.prints_minor ?? 0), 0)
  const parts: Part[] = ['prints', 'labour']
  const unknown = parts.filter((p) => items.some((i) => (i.unknown ?? []).includes(p)) || (p === 'labour' && ownUnknown))
  return margin(
    {
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
      time_entries: ownTime,
      cost_entries: ownOther,
      thin_margin_percent: thinText,
      prints_minor: prints,
      machine_minor: items.reduce((s, i) => s + (i.machine_minor ?? 0), 0),
      labour_minor: labour,
      other_minor: others + shipping,
      shipping_minor: shipping,
      charges_minor: charges,
      cost_minor: prints + labour + others + shipping,
      revenue_minor: revenue,
      estimated: parts.filter((p) => items.some((i) => (i.estimated ?? []).includes(p))),
      unknown,
      bambuddy_unavailable: false,
    },
    thin,
  )
}

/** A write's answer, 201 (or `status`), or the lifecycle's refusal. */
function created<T>(request: Request, run: () => [T, number]) {
  try {
    const [body, status] = run()
    return HttpResponse.json(body as unknown as Record<string, unknown>, { status })
  } catch (error) {
    return refused(request, error)
  }
}

function removed(request: Request, run: () => void) {
  try {
    run()
    return new HttpResponse(null, { status: 204 })
  } catch (error) {
    return refused(request, error)
  }
}

export const handlers = [
  http.get('/api/v1/orders/:id/costs', ({ request, params }) => {
    const costs = costsOf(String(params.id))
    return costs ? HttpResponse.json(costs) : problem(request, 404, `no order '${String(params.id)}'`)
  }),
  http.post('/api/v1/orders/:id/time-entries', async ({ request, params }) => {
    const body = (await request.json()) as TimeEntryCreate
    if (!Number.isInteger(body.minutes) || body.minutes < 1) return fieldProblem(request, 'minutes', 'Input should be greater than or equal to 1')
    return created(request, () => {
      const order = lock(String(params.id))
      if (body.order_item_id) itemOf(order, body.order_item_id)
      const settings = mockSettings()
      const rate =
        order.currency === settings.orders_currency ? Math.round(Number(settings.pricing_labour_rate_per_hour ?? '0') * 10 ** minorExponent(order.currency)) : null
      const entry: TimeEntry = {
        id: newId('te'),
        order_id: order.id,
        order_item_id: body.order_item_id ?? null,
        kind: body.kind,
        minutes: body.minutes,
        note: body.note ?? '',
        started_at: body.started_at ?? null,
        rate_minor_per_hour: rate,
        created_by: BROWSER,
        created_at: now(),
      }
      state.timeEntries.push(entry)
      event(order, 'time.added', { entry_id: entry.id, item_id: entry.order_item_id, entry_kind: entry.kind, minutes: entry.minutes })
      return [entry, 201]
    })
  }),
  http.delete('/api/v1/orders/:id/time-entries/:entry', ({ request, params }) =>
    removed(request, () => {
      const order = lock(String(params.id))
      const index = state.timeEntries.findIndex((e) => e.id === params.entry && e.order_id === order.id)
      if (index < 0) throw new Refused('time_entry_not_found', `no time entry ${String(params.entry)}`, [], true)
      const [entry] = state.timeEntries.splice(index, 1)
      event(order, 'time.removed', { entry_id: entry!.id, item_id: entry!.order_item_id, entry_kind: entry!.kind, minutes: entry!.minutes })
    }),
  ),
  http.post('/api/v1/orders/:id/cost-entries', async ({ request, params }) => {
    const body = (await request.json()) as CostEntryCreate
    if (!Number.isInteger(body.amount_minor) || body.amount_minor < 1) return fieldProblem(request, 'amount_minor', 'Input should be greater than or equal to 1')
    return created(request, () => {
      const order = lock(String(params.id))
      if (body.order_item_id) itemOf(order, body.order_item_id)
      const same = body.source && state.costEntries.find((e) => e.order_id === order.id && e.kind === body.kind && e.source === body.source)
      if (same) return [same, 200]
      const entry: CostEntry = {
        id: newId('ce'),
        order_id: order.id,
        order_item_id: body.order_item_id ?? null,
        kind: body.kind,
        amount_minor: body.amount_minor,
        note: body.note ?? '',
        source: body.source ?? null,
        created_by: BROWSER,
        created_at: now(),
      }
      state.costEntries.push(entry)
      event(order, 'cost.added', { entry_id: entry.id, item_id: entry.order_item_id, entry_kind: entry.kind, amount_minor: entry.amount_minor })
      return [entry, 201]
    })
  }),
  http.delete('/api/v1/orders/:id/cost-entries/:entry', ({ request, params }) =>
    removed(request, () => {
      const order = lock(String(params.id))
      const index = state.costEntries.findIndex((e) => e.id === params.entry && e.order_id === order.id)
      if (index < 0) throw new Refused('cost_entry_not_found', `no cost entry ${String(params.entry)}`, [], true)
      const [entry] = state.costEntries.splice(index, 1)
      event(order, 'cost.removed', { entry_id: entry!.id, item_id: entry!.order_item_id, entry_kind: entry!.kind, amount_minor: entry!.amount_minor })
    }),
  ),
]

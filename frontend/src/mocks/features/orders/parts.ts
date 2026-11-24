import { HttpResponse, http } from 'msw'
import type { ItemPartRow, Order, OrderItem, PartRule, PartRuleLine } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { estimateUnitCost, giveBack, inventoryItem, isPartItem, partsMovedOf, takeStock } from '../inventory'
import { event, itemOf, lock, notEnded, transition } from './orders'
import { BROWSER, fieldProblem, now, problem, PROBLEMS, Refused, state } from './state'

/**
 * #5543, job cost spec §11 — an order item's parts and a model's parts rule
 * (`backend/scadbuddy/api/parts.py`, `orders/parts.py`). A part is an inventory item of a kind
 * tracked in lots (#7067, `../inventory.ts`). An item's parts leave stock FIFO when it becomes
 * fulfilled (`useParts`; job cost reads what they cost from the moves as the item's
 * `parts_minor`, no cost entry) and come back at the cost they were taken at when it is
 * unfulfilled (`returnParts`); `orders.ts` calls both, and `fillFromRule` when a model item is
 * added. The `/parts` part-row aliases are not mocked: the UI reads `/inventory`.
 */

type Parts = {
  rules: Map<string, PartRule>
  /** `order_item_parts`, keyed by item id. */
  items: Map<string, ItemPartRow[]>
  /** The order items whose order is not in the shop's currency: their parts are 0 and unknown. */
  foreign: Set<string>
}

let parts: Parts = fresh()

function fresh(): Parts {
  return { rules: new Map(), items: new Map(), foreign: new Set() }
}

export function reset(): void {
  parts = fresh()
}

const decimal = (n: number) => String(Number(n.toFixed(6)))

/** The part's item, or `part_not_found` for no item or one not tracked in lots. */
function find(id: string) {
  const item = isPartItem(id) ? inventoryItem(id) : undefined
  if (!item) throw new Refused('part_not_found', `no part '${id}'`, [], true)
  return item
}

/** `rule_parts`: the first `when` whose colour the item has, else the line's part. */
export function fillFromRule(order: Order, item: OrderItem): void {
  const slug = item.reference?.slug
  const rule = slug ? parts.rules.get(slug) : undefined
  if (!rule) return
  const have = new Set(item.colours.map((c) => c.colour.toLowerCase()))
  const found = new Map<string, number>()
  for (const line of rule.lines) {
    const chosen = (line.when ?? []).find((w) => have.has(w.colour.toLowerCase()))?.item_id ?? line.item_id ?? null
    if (chosen) found.set(chosen, (found.get(chosen) ?? 0) + line.per_unit)
  }
  if (found.size === 0) return
  const rows = [...found].map(([item_id, per_unit]) => ({ item_id, part_id: item_id, per_unit, used: 0 }))
  parts.items.set(item.id, rows)
  event(order, 'item.parts_set', { item_id: item.id, parts: rows.map(({ item_id, per_unit }) => ({ item_id, per_unit })), from_rule: true })
}

/** `Lifecycle._use_parts`: out of the lots FIFO, each move at its lot's cost now. */
export function useParts(order: Order, item: OrderItem): void {
  const rows = (parts.items.get(item.id) ?? []).filter((r) => r.used === 0)
  if (rows.length === 0) return
  for (const row of rows) {
    const count = row.per_unit * item.quantity
    takeStock(row.item_id, count, { order_id: order.id, order_item_id: item.id })
    row.used = count
  }
  if (order.currency !== mockSettings().orders_currency) parts.foreign.add(item.id)
  event(order, 'parts.used', { item_id: item.id, currency: mockSettings().orders_currency })
}

/** `Lifecycle._return_parts`: each use given back to its lot at the cost it was taken at. */
export function returnParts(order: Order, item: OrderItem): void {
  const rows = (parts.items.get(item.id) ?? []).filter((r) => r.used > 0)
  if (rows.length === 0) return
  for (const row of rows) {
    giveBack(row.item_id, item.id)
    row.used = 0
  }
  parts.foreign.delete(item.id)
  event(order, 'parts.returned', { item_id: item.id, currency: mockSettings().orders_currency })
}

/** `CostRows.parts` for `costs.ts`: the item's parts cost from its moves, and whether it is unknown.
 * Stock costs are in the shop's currency: in an order in another currency they are 0 and unknown. */
export function partsCostOf(itemId: string): { minor: number; unknown: boolean } {
  const moved = partsMovedOf(itemId)
  if (parts.foreign.has(itemId)) return { minor: 0, unknown: moved.open }
  return { minor: moved.minor, unknown: moved.unknown }
}

/** `Lifecycle.parts_unit_cost`: one unit of the item's parts at what the lots cost now (known
 * ones only), and whether some of it is unknown. In another currency the parts are 0, and
 * unknown when the item has any (R-P2-33). */
export function partsUnitOf(itemId: string, currency: string): { minor: number; unknown: boolean } {
  const rows = parts.items.get(itemId) ?? []
  if (currency !== mockSettings().orders_currency) return { minor: 0, unknown: rows.length > 0 }
  let minor = 0
  let unknown = false
  for (const row of rows) {
    const each = estimateUnitCost(row.item_id)
    minor += row.per_unit * each.minor
    unknown ||= each.unknown
  }
  return { minor, unknown }
}

function refusedPart(request: Request, error: unknown) {
  if (!(error instanceof Refused)) throw error
  return error.notFound ? problem(request, 404, error.message, PROBLEMS + error.slug) : problem(request, 409, error.message, PROBLEMS + error.slug)
}

export const handlers = [
  http.get('/api/v1/parts/rules/:slug', ({ params }) => {
    const slug = String(params.slug)
    return HttpResponse.json(parts.rules.get(slug) ?? { slug, lines: [], updated_at: null, updated_by: null })
  }),
  http.put('/api/v1/parts/rules/:slug', async ({ request, params }) => {
    const slug = String(params.slug)
    const { lines } = (await request.json()) as { lines: PartRuleLine[] }
    try {
      for (const line of lines) {
        if (line.item_id) find(line.item_id)
        for (const w of line.when ?? []) find(w.item_id)
      }
    } catch (error) {
      return refusedPart(request, error)
    }
    if (lines.length === 0) {
      parts.rules.delete(slug)
      return HttpResponse.json({ slug, lines: [], updated_at: null, updated_by: null })
    }
    const rule: PartRule = { slug, lines, updated_at: now(), updated_by: BROWSER }
    parts.rules.set(slug, rule)
    return HttpResponse.json(rule)
  }),
  http.get('/api/v1/orders/:id/parts', ({ request, params }) => {
    const order = state.orders.find((o) => o.id === params.id)
    if (!order) return problem(request, 404, `no order '${String(params.id)}'`)
    const items = state.items.filter((i) => i.order_id === order.id)
    const needed = new Map<string, number>()
    for (const item of items) {
      if (item.status === 'fulfilled' || item.status === 'cancelled') continue
      for (const row of parts.items.get(item.id) ?? []) needed.set(row.item_id, (needed.get(row.item_id) ?? 0) + row.per_unit * item.quantity)
    }
    const needs = [...needed].map(([id, n]) => {
      const item = find(id)
      const onHand = Number(item.on_hand ?? 0)
      // `PartNeed`'s decimals are strings (#7067: lots hold `numeric`).
      return { item_id: id, part_id: id, name: item.name, needed: n, on_hand: decimal(onHand), short: decimal(Math.max(0, n - onHand)), low: item.low }
    })
    return HttpResponse.json({
      order_id: order.id,
      items: items.map((i) => ({ item_id: i.id, parts: parts.items.get(i.id) ?? [] })),
      needs,
      short: needs.filter((n) => Number(n.short) > 0),
    })
  }),
  http.put('/api/v1/orders/:id/items/:item/parts', async ({ request, params }) => {
    const body = (await request.json()) as { parts: { item_id: string; per_unit: number }[] }
    const ids = body.parts.map((p) => p.item_id)
    if (new Set(ids).size !== ids.length) return fieldProblem(request, 'parts', 'Value error, a part is named twice: give it one row with its count')
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (found.status === 'fulfilled') {
        throw new Refused('item_parts_used', `item ${found.id} is fulfilled: its parts are out of stock; unfulfil it to change them`, [found.id])
      }
      for (const id of ids) find(id)
      const rows = body.parts.map((p) => ({ ...p, part_id: p.item_id, used: 0 }))
      parts.items.set(found.id, rows)
      event(order, 'item.parts_set', { item_id: found.id, parts: body.parts })
      return rows
    })
  }),
]

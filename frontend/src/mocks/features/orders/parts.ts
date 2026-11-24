import { HttpResponse, http } from 'msw'
import type { InventoryPart, ItemPartRow, Order, OrderItem, PartCreate, PartPatch, PartRule, PartRuleLine, StockAdd, StockAdjust, StockMove } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { event, itemOf, lock, notEnded, transition } from './orders'
import { BROWSER, fieldProblem, minorExponent, newId, now, problem, PROBLEMS, Refused, state } from './state'

/**
 * #5543, job cost spec §11 — the parts inventory (`backend/scadbuddy/api/parts.py`,
 * `orders/parts.py`). A part's unit cost is a rate kept to the millionth of a minor unit and
 * answered as a decimal string. An item's parts leave stock when it becomes fulfilled
 * (`useParts`; job cost reads what they cost as the item's `parts_minor`, #7067, no cost
 * entry) and come back when it is unfulfilled (`returnParts`); `orders.ts` calls both, and `fillFromRule` when a model
 * item is added.
 */

type MockPart = Omit<InventoryPart, 'unit_cost_minor' | 'low' | 'minor_exponent'> & { unit_cost: number }

type Parts = {
  parts: MockPart[]
  moves: StockMove[]
  rules: Map<string, PartRule>
  /** `order_item_parts`, keyed by item id. */
  items: Map<string, ItemPartRow[]>
  /** What each order item's parts cost when taken (its `use` moves, #7067), and whether a
   * part had no cost in the order's currency. */
  taken: Map<string, { minor: number; unknown: boolean }>
}

let parts: Parts = fresh()

function fresh(): Parts {
  return { parts: [], moves: [], rules: new Map(), items: new Map(), taken: new Map() }
}

export function reset(): void {
  parts = fresh()
}

const decimal = (n: number) => String(Number(n.toFixed(6)))
const round6 = (n: number) => Number(n.toFixed(6))

function answer(p: MockPart): InventoryPart {
  const { unit_cost, ...rest } = p
  return { ...rest, unit_cost_minor: decimal(unit_cost), low: p.low_stock > 0 && p.on_hand <= p.low_stock, minor_exponent: minorExponent(p.currency) }
}

/** Seeds a part for a test, as `POST /parts` makes it; at a supplier unless one is named
 * (`supplier: ''` seeds a part with no supplier, R-P2-25). */
export function seedPart(body: PartCreate & { currency?: string }): InventoryPart {
  return answer(create({ ...body, supplier: body.supplier ?? 'Bolt Depot' }, body.currency ?? mockSettings().orders_currency))
}

function find(id: string): MockPart {
  const found = parts.parts.find((p) => p.id === id)
  if (!found) throw new Refused('part_not_found', `no part '${id}'`, [], true)
  return found
}

/** `average`: the weighted average, or the new units' own cost with nothing on hand. */
function average(p: MockPart, count: number, amount: number): number {
  if (p.on_hand <= 0) return round6(amount / count)
  return round6((p.on_hand * p.unit_cost + amount) / (p.on_hand + count))
}

function move(p: MockPart, kind: StockMove['kind'], delta: number, extra: Partial<StockMove> = {}): void {
  p.on_hand += delta
  p.updated_at = now()
  parts.moves.push({
    id: newId('mv'),
    part_id: p.id,
    kind,
    delta,
    on_hand_after: p.on_hand,
    unit_cost_after_minor: decimal(p.unit_cost),
    amount_minor: null,
    note: '',
    order_id: null,
    order_item_id: null,
    created_by: BROWSER,
    created_at: now(),
    ...extra,
  })
}

function nameTaken(name: string, own: string | null): MockPart | undefined {
  return parts.parts.find((p) => p.name.toLowerCase() === name.toLowerCase() && p.id !== own)
}

/** R-P2-34: a part with no supplier has no offer to hold a pack. */
const NO_PACK = 'a part with no supplier has no pack to change: name its supplier'

function hasPack(size: number | null | undefined, price: number | null | undefined): boolean {
  return (size != null && size !== 1) || (price != null && price !== 0)
}

function create(body: PartCreate, currency: string): MockPart {
  const supplied = !!body.supplier?.trim()
  const p: MockPart = {
    id: newId('part'),
    name: body.name,
    supplier: body.supplier ?? '',
    link: body.link ?? '',
    currency,
    pack_size: body.pack_size,
    pack_price_minor: body.pack_price_minor,
    // R-P2-35: with no supplier there is no price point, so nothing says what a unit costs.
    unit_cost: supplied ? round6(body.pack_price_minor / body.pack_size) : 0,
    cost_unknown: !supplied,
    on_hand: 0,
    low_stock: body.low_stock ?? 0,
    created_at: now(),
    updated_at: now(),
  }
  parts.parts.push(p)
  // The count on the shelf is received as a lot: an `add` move, of unknown cost with no supplier.
  const shelf = body.on_hand ?? 0
  if (shelf > 0) move(p, 'add', shelf, { amount_minor: supplied ? Math.round(shelf * p.unit_cost) : null, note: 'Count when the part was added' })
  return p
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

/** `Lifecycle._use_parts`: out of stock, each use at the part's unit cost now; no cost entry
 * (`costs.py` reads the item's `parts_minor`, #7067). Stock costs are in the shop's currency:
 * a part in another currency is of unknown cost, and in an order in another currency the
 * parts are 0 and unknown, as `costs.py` reads them. */
export function useParts(order: Order, item: OrderItem): void {
  const rows = (parts.items.get(item.id) ?? []).filter((r) => r.used === 0)
  if (rows.length === 0) return
  const shop = mockSettings().orders_currency
  let minor = 0
  let unknown = order.currency !== shop
  for (const row of rows) {
    const p = find(row.item_id)
    const count = row.per_unit * item.quantity
    const amount = p.unit_cost * count
    if (p.currency === shop) minor += amount
    else unknown = true
    move(p, 'use', -count, { amount_minor: p.currency === shop ? Math.round(amount) : null, order_id: order.id, order_item_id: item.id })
    row.used = count
  }
  parts.taken.set(item.id, { minor: order.currency === shop ? Math.round(minor) : 0, unknown })
  event(order, 'parts.used', { item_id: item.id })
}

/** `Lifecycle._return_parts`: back in stock at the cost they were taken at. */
export function returnParts(order: Order, item: OrderItem): void {
  const rows = (parts.items.get(item.id) ?? []).filter((r) => r.used > 0)
  if (rows.length === 0) return
  for (const row of rows) {
    const p = find(row.item_id)
    const taken = [...parts.moves].reverse().find((m) => m.kind === 'use' && m.order_item_id === item.id && m.part_id === p.id)
    if (taken) p.unit_cost = average(p, row.used, Number(taken.unit_cost_after_minor) * row.used)
    move(p, 'return', row.used, { amount_minor: taken?.amount_minor ?? null, order_id: order.id, order_item_id: item.id })
    row.used = 0
  }
  parts.taken.delete(item.id)
  event(order, 'parts.returned', { item_id: item.id })
}

/** `CostRows.parts` for `costs.ts`: the item's parts cost, rounded once, and whether a part is unknown. */
export function partsCostOf(itemId: string): { minor: number; unknown: boolean } {
  return parts.taken.get(itemId) ?? { minor: 0, unknown: false }
}

/** `Lifecycle.parts_unit_cost`: one unit of the item's parts at today's unit costs, and
 * whether some of it is of unknown cost. Stock costs are in the shop's currency: in another
 * currency the parts are 0, and unknown when the item has any (R-P2-33). */
export function partsUnitOf(itemId: string, currency: string): { minor: number; unknown: boolean } {
  const rows = parts.items.get(itemId) ?? []
  const shop = mockSettings().orders_currency
  if (currency !== shop) return { minor: 0, unknown: rows.length > 0 }
  let minor = 0
  let unknown = false
  for (const row of rows) {
    const p = parts.parts.find((x) => x.id === row.item_id)
    if (p && p.currency === shop && !p.cost_unknown) minor += row.per_unit * p.unit_cost
    else unknown = true
  }
  return { minor, unknown }
}

function refusedPart(request: Request, error: unknown) {
  if (!(error instanceof Refused)) throw error
  return error.notFound ? problem(request, 404, error.message, PROBLEMS + error.slug) : problem(request, 409, error.message, PROBLEMS + error.slug)
}

export const handlers = [
  http.get('/api/v1/parts', ({ request }) => {
    const low = new URL(request.url).searchParams.get('low') === 'true'
    const all = [...parts.parts].sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())).map(answer)
    return HttpResponse.json(low ? all.filter((p) => p.low) : all)
  }),
  http.post('/api/v1/parts', async ({ request }) => {
    const body = (await request.json()) as PartCreate
    if (!body.name?.trim()) return fieldProblem(request, 'name', 'String should have at least 1 character')
    if (!(body.pack_size >= 1)) return fieldProblem(request, 'pack_size', 'Input should be greater than or equal to 1')
    if (!body.supplier?.trim() && hasPack(body.pack_size, body.pack_price_minor)) return fieldProblem(request, 'supplier', NO_PACK)
    const taken = nameTaken(body.name, null)
    if (taken) return problem(request, 409, `part '${taken.id}' has that name: ${taken.id}`, PROBLEMS + 'part_exists', { part_id: taken.id })
    return HttpResponse.json(answer(create(body, mockSettings().orders_currency)), { status: 201 })
  }),
  http.patch('/api/v1/parts/:id', async ({ request, params }) => {
    const body = (await request.json()) as PartPatch
    try {
      const p = find(String(params.id))
      if (body.name) {
        const taken = nameTaken(body.name, p.id)
        if (taken) return problem(request, 409, `part '${taken.id}' has that name`, PROBLEMS + 'part_exists', { part_id: taken.id })
      }
      const packChanged = body.pack_size != null || body.pack_price_minor != null
      const supplier = body.supplier ?? p.supplier
      if (!supplier.trim() && packChanged) return fieldProblem(request, 'supplier', NO_PACK)
      if (body.supplier != null && !body.supplier.trim() && p.supplier.trim()) {
        // The offer is archived: a pack of 1 at 0; its link stays, as a purchase link.
        Object.assign(p, { pack_size: 1, pack_price_minor: 0 })
      }
      Object.assign(p, Object.fromEntries(Object.entries(body).filter(([, v]) => v != null)))
      if (packChanged) p.cost_unknown = false
      if (p.on_hand <= 0 && packChanged) p.unit_cost = round6(p.pack_price_minor / p.pack_size)
      p.updated_at = now()
      return HttpResponse.json(answer(p))
    } catch (error) {
      return refusedPart(request, error)
    }
  }),
  http.post('/api/v1/parts/:id/stock', async ({ request, params }) => {
    const body = (await request.json()) as StockAdd
    try {
      const p = find(String(params.id))
      const packs = body.packs ?? 1
      const supplied = !!p.supplier.trim()
      // R-P2-25: with no supplier, single units at the body's price, and no price point.
      if (supplied && body.pack_price_minor != null) p.pack_price_minor = body.pack_price_minor
      const each = supplied ? p.pack_price_minor : body.pack_price_minor
      const count = packs * p.pack_size
      if (each == null) {
        move(p, 'add', count, { amount_minor: null, note: body.note ?? '' })
      } else {
        const amount = packs * each
        p.unit_cost = p.cost_unknown ? round6(amount / count) : average(p, count, amount)
        p.cost_unknown = false
        move(p, 'add', count, { amount_minor: amount, note: body.note ?? '' })
      }
      return HttpResponse.json(answer(p))
    } catch (error) {
      return refusedPart(request, error)
    }
  }),
  http.post('/api/v1/parts/:id/adjust', async ({ request, params }) => {
    const body = (await request.json()) as StockAdjust
    if (!body.note?.trim()) return fieldProblem(request, 'note', 'String should have at least 1 character')
    if (!(body.on_hand >= 0)) return fieldProblem(request, 'on_hand', 'Input should be greater than or equal to 0')
    try {
      const p = find(String(params.id))
      move(p, 'adjust', body.on_hand - p.on_hand, { note: body.note })
      return HttpResponse.json(answer(p))
    } catch (error) {
      return refusedPart(request, error)
    }
  }),
  http.get('/api/v1/parts/:id/moves', ({ request, params }) => {
    try {
      find(String(params.id))
    } catch (error) {
      return refusedPart(request, error)
    }
    return HttpResponse.json(parts.moves.filter((m) => m.part_id === params.id).reverse())
  }),
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
      const p = answer(find(id))
      // `PartNeed`'s decimals are strings (#7067: lots hold `numeric`).
      return { item_id: id, part_id: id, name: p.name, needed: n, on_hand: decimal(p.on_hand), short: decimal(Math.max(0, n - p.on_hand)), low: p.low }
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
      for (const id of ids) {
        if (!parts.parts.some((p) => p.id === id)) throw new Refused('part_not_found', `no part '${id}'`)
      }
      const rows = body.parts.map((p) => ({ ...p, part_id: p.item_id, used: 0 }))
      parts.items.set(found.id, rows)
      event(order, 'item.parts_set', { item_id: found.id, parts: body.parts })
      return rows
    })
  }),
]

import { HttpResponse, http } from 'msw'
import type {
  Fulfilment,
  FulfilmentInput,
  ManualQuoteInput,
  Order,
  OrderCreate,
  OrderDetail,
  OrderItem,
  OrderItemCreate,
  OrderItemPatch,
  OrderPatch,
  OrderStatus,
  PaymentCreate,
  Quote,
  Shipment,
  ShipmentEdit,
  ShipmentInput,
} from '../../../api/types'
import { mockModels, mockOutput, mockSettings } from '../../handlers'
import { versionIds } from '../../fixtures'
import { BAD_CURSOR, BROWSER, fieldProblem, minorExponent, newId, now, problem, PROBLEMS, Refused, state, UNKNOWN_PARAMETER } from './state'

/**
 * `/api/v1/orders` (backend/scadbuddy/api/orders.py) with the lifecycle's rules
 * (`orders/lifecycle.py`) mirrored: the status past `accepted` is derived from the items
 * and shipments, a refusal is a 409 problem typed `orders/<slug>` naming the ids in the
 * way, and on hold every step that advances the order is refused.
 */

const LIST_PARAMS = new Set(['status', 'customer_id', 'q', 'from', 'to', 'cursor', 'limit'])
const STATUSES = new Set<OrderStatus>(['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled'])
export const OPEN_FOR_EDITS = new Set<OrderStatus>(['draft', 'quoted'])
const ENDED = new Set<OrderStatus>(['closed', 'cancelled'])
const DERIVED = new Set<OrderStatus>(['accepted', 'in_production', 'ready', 'shipped'])
export const SETTLED = new Set(['fulfilled', 'cancelled'])
const FULFILLABLE = new Set(['approved', 'failed'])

const encodeCursor = (o: Order) => btoa(`${o.created_at}|${o.id}`).replaceAll('+', '-').replaceAll('/', '_')
function decodeCursor(cursor: string): [string, string] | null {
  try {
    const raw = atob(cursor.replaceAll('-', '+').replaceAll('_', '/'))
    const at = raw.indexOf('|')
    if (at < 0) return null
    const [created, id] = [raw.slice(0, at), raw.slice(at + 1)]
    return id && Number.isFinite(Date.parse(created)) ? [created, id] : null
  } catch {
    return null
  }
}

const itemsOf = (orderId: string) => state.items.filter((i) => i.order_id === orderId).sort((a, b) => a.position - b.position)
export const quotesOf = (itemId: string) => state.quotes.filter((q) => q.order_item_id === itemId)
const attemptsOf = (itemId: string) => state.fulfilments.filter((f) => f.order_item_id === itemId).sort((a, b) => a.attempt - b.attempt)
const shipmentsOf = (orderId: string) => state.shipments.filter((s) => s.order_id === orderId)

export function lock(orderId: string): Order {
  const order = state.orders.find((o) => o.id === orderId)
  if (!order) throw new Refused('order_not_found', `no order ${orderId}`, [], true)
  return order
}
export function itemOf(order: Order, itemId: string): OrderItem {
  const found = state.items.find((i) => i.id === itemId && i.order_id === order.id)
  if (!found) throw new Refused('item_not_found', `no item ${itemId} of order ${order.number}`, [], true)
  return found
}
export function notEnded(order: Order) {
  if (ENDED.has(order.status)) throw new Refused(`order_${order.status}`, `order ${order.number} is ${order.status}`)
}
function notOnHold(order: Order) {
  if (order.on_hold) throw new Refused('order_on_hold', `order ${order.number} is on hold`)
}
export function openForItems(order: Order, what: string) {
  if (!OPEN_FOR_EDITS.has(order.status)) {
    throw new Refused('item_locked', `order ${order.number} is ${order.status}: ${what} is pinned once accepted`)
  }
}
export function event(order: Order, kind: string, detail: Record<string, unknown> = {}) {
  state.events.push({ id: state.events.length + 1, order_id: order.id, at: now(), by: BROWSER, kind, detail })
}
function setStatus(order: Order, status: OrderStatus) {
  event(order, 'order.status', { from_status: order.status, to: status })
  order.status = status
  order.updated_at = now()
  if (status === 'accepted') order.accepted_at ??= now()
  if (status === 'closed') order.closed_at ??= now()
  if (status === 'cancelled') order.cancelled_at ??= now()
}

function uncovered(items: OrderItem[], shipments: Shipment[]): string[] {
  const shipped: Record<string, number> = {}
  for (const s of shipments) for (const line of s.items) shipped[line.order_item_id] = (shipped[line.order_item_id] ?? 0) + line.quantity
  return items.filter((i) => i.status === 'fulfilled' && (shipped[i.id] ?? 0) < i.quantity).map((i) => i.id)
}

/** `orders/lifecycle.py` `derive`, and the `draft`/`quoted` settling before accept. */
export function rederive(order: Order) {
  const items = itemsOf(order.id)
  let status: OrderStatus = order.status
  if (OPEN_FOR_EDITS.has(order.status)) {
    const open = items.filter((i) => i.status !== 'cancelled')
    status = open.length > 0 && open.every((i) => i.status === 'approved') ? 'quoted' : 'draft'
  } else if (DERIVED.has(order.status)) {
    const settled = items.every((i) => SETTLED.has(i.status))
    const fulfilled = items.filter((i) => i.status === 'fulfilled')
    if (settled && fulfilled.length > 0) status = uncovered(fulfilled, shipmentsOf(order.id)).length > 0 ? 'ready' : 'shipped'
    else if (items.some((i) => attemptsOf(i.id).length > 0)) status = 'in_production'
    else status = 'accepted'
  }
  if (status !== order.status) setStatus(order, status)
}

function detail(order: Order): OrderDetail {
  const items = itemsOf(order.id).map((i) => ({ ...i, quotes: quotesOf(i.id), fulfilments: attemptsOf(i.id) }))
  const charges = state.charges.filter((c) => c.order_id === order.id).sort((a, b) => a.position - b.position)
  const payments = state.payments.filter((p) => p.order_id === order.id)
  // `api/orders.py` `_totals`: an accepted quote's total, else the newest quote's unit
  // price times the quantity now; an item with no quote is in no sum.
  let subtotal = 0
  let pending = 0
  const unpriced: string[] = []
  for (const i of items.filter((each) => each.status !== 'cancelled')) {
    const accepted = i.quotes.find((q) => q.accepted)
    const newest = i.quotes.at(-1)
    if (accepted) subtotal += accepted.total_minor
    else if (newest) {
      subtotal += newest.unit_price_minor * i.quantity
      pending += newest.unit_price_minor * i.quantity
    } else unpriced.push(i.id)
  }
  const charged = charges.reduce((sum, c) => sum + c.amount_minor, 0)
  const paid = payments.reduce((sum, p) => sum + p.amount_minor, 0)
  return {
    order,
    items,
    charges,
    payments,
    shipments: shipmentsOf(order.id),
    attachments: state.attachments.filter((a) => a.order_id === order.id),
    totals: { subtotal_minor: subtotal, pending_minor: pending, unpriced_items: unpriced, charges_minor: charged, total_minor: subtotal + charged, paid_minor: paid, balance_minor: subtotal + charged - paid },
  }
}

/** The backend's answer for a `Refused`: 404 for a missing row, 409 typed by the slug otherwise. */
export function refused(request: Request, error: unknown) {
  if (error instanceof FilamentsRefused) return fieldProblem(request, 'filaments', error.message)
  if (!(error instanceof Refused)) throw error
  const extensions = error.items.length > 0 ? { items: error.items } : {}
  return error.notFound ? problem(request, 404, error.message) : problem(request, 409, error.message, PROBLEMS + error.slug, extensions)
}

/** A body naming a slot or a filament that cannot be (#4250): a 422 on `filaments`. */
export class FilamentsRefused extends Error {}

/**
 * The item's slots with `filaments` (`{slot: filament_id | null}`) applied, as
 * `orders/lifecycle.py` `update_item` does (#4250 Ruling 6): a slot the item lacks, or a
 * filament not in the book or archived, is refused; whether a slot changed (a repin).
 */
export function withFilaments(found: OrderItem, filaments: Record<string, string | null>): { colours: OrderItem['colours']; changed: boolean } {
  const known = new Set(found.colours.map((c) => c.slot))
  for (const [slot, id] of Object.entries(filaments)) {
    if (!known.has(Number(slot))) throw new FilamentsRefused(`the item has no colour slot ${slot}`)
    if (id === null) continue
    const record = state.filaments.find((f) => f.id === id)
    if (!record) throw new FilamentsRefused(`slot ${slot}: no filament ${id} in the price book`)
    if (record.archived_at) throw new FilamentsRefused(`slot ${slot}: filament ${id} is archived`)
  }
  const colours = found.colours.map((c) => (String(c.slot) in filaments ? { ...c, filament_id: filaments[String(c.slot)] ?? null } : c))
  return { colours, changed: colours.some((c, i) => c.filament_id !== found.colours[i]?.filament_id) }
}

/** A transition: the lifecycle's rules, then the derivation, then the answer. */
export function transition<T>(request: Request, run: () => T) {
  try {
    return HttpResponse.json(run() as unknown as Record<string, unknown>)
  } catch (error) {
    return refused(request, error)
  }
}

type Resolved = { reference: OrderItem['reference']; colours: OrderItem['colours'] }

/** `orders/reference.py`: from the output, or by hand against the catalogue; a problem names the field. */
function resolveReference(body: { slug?: string | null; commit?: string | null; inputs?: Record<string, unknown> | null; output_id?: string | null }): Resolved | [string, string] {
  let { slug, commit, inputs } = body
  let colours: OrderItem['colours'] = []
  let libraries: OrderItem['reference']['libraries'] = []
  if (body.output_id) {
    const output = mockOutput(body.output_id)
    if (!output) return ['output_id', `no output '${body.output_id}'`]
    if (slug && slug !== output.slug) return ['output_id', `output ${body.output_id} is a render of ${output.slug}, not ${slug}`]
    slug = output.slug
    commit ??= output.model_version ?? versionIds.added
    inputs ??= output.inputs ?? { params: output.params ?? {}, v: 0 }
    colours = (output.colors ?? []).map((colour, slot) => ({ slot, colour, filament_id: null }))
    libraries = output.libraries ?? []
  }
  if (!slug) return ['slug', 'a line item needs a template slug or an output']
  if (!commit) return ['commit', 'a line item pins one commit of its template']
  const model = mockModels().find((m) => m.slug === slug)
  if (!model) return ['slug', `no model named '${slug}'`]
  if (!/^[0-9a-f]{7,40}$/.test(commit)) return ['commit', `'${commit}' is not a commit id (7 to 40 hex characters)`]
  const full = commit.length === 40 ? commit : commit.padEnd(40, '0')
  const params = (inputs ?? {}).params ?? {}
  if (typeof params !== 'object' || params === null || Array.isArray(params)) return ['inputs', 'inputs.params must be an object of parameter values']
  return {
    reference: { slug, commit: full, inputs: { ...(inputs ?? {}), params, v: (inputs as { v?: number } | undefined)?.v ?? 0 }, libraries, template_name: model.name, render_key: 'd'.repeat(64) },
    colours,
  }
}

function unknownKeys(request: Request, body: Record<string, unknown>, allowed: string[]) {
  for (const key of Object.keys(body)) if (!allowed.includes(key)) return fieldProblem(request, key, 'Extra inputs are not permitted')
  return null
}

export const handlers = [
  http.get('/api/v1/orders', ({ request }) => {
    const sp = new URL(request.url).searchParams
    for (const name of sp.keys()) if (!LIST_PARAMS.has(name)) return problem(request, 400, `unknown query parameter '${name}'`, UNKNOWN_PARAMETER)
    const status = sp.get('status')
    if (status !== null && !STATUSES.has(status as OrderStatus)) return problem(request, 422, 'status is not an order status')
    const limit = Number(sp.get('limit') ?? '50')
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) return problem(request, 422, 'limit must be an integer from 1 to 100')
    let after: [string, string] | null = null
    const cursor = sp.get('cursor')
    if (cursor !== null) {
      after = decodeCursor(cursor)
      if (!after) return problem(request, 400, 'cursor is not a next_cursor', BAD_CURSOR)
    }
    const q = (sp.get('q') ?? '').toLowerCase()
    const customer = sp.get('customer_id')
    const from = sp.get('from')
    const to = sp.get('to')
    const rows = [...state.orders]
      .sort((a, b) => (a.created_at === b.created_at ? (a.id < b.id ? 1 : -1) : a.created_at < b.created_at ? 1 : -1))
      .filter((o) => !status || o.status === status)
      .filter((o) => !customer || o.customer_id === customer)
      .filter((o) => !from || Date.parse(o.created_at) >= Date.parse(from))
      .filter((o) => !to || Date.parse(o.created_at) <= Date.parse(to))
      .filter((o) => {
        if (!q) return true
        const owner = state.customers.find((c) => c.id === o.customer_id)
        return o.number.toLowerCase().includes(q) || o.notes.toLowerCase().includes(q) || (owner?.display_name ?? '').toLowerCase().includes(q) || (owner?.company ?? '').toLowerCase().includes(q)
      })
      .filter((o) => !after || o.created_at < after[0] || (o.created_at === after[0] && o.id < after[1]))
    const page = rows.slice(0, limit)
    const last = page.at(-1)
    return HttpResponse.json({ items: page, next_cursor: rows.length > limit && last ? encodeCursor(last) : null })
  }),

  http.post('/api/v1/orders', async ({ request }) => {
    const body = (await request.json()) as OrderCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['customer_id', 'notes', 'due_at'])
    if (extra) return extra
    const customer = state.customers.find((c) => c.id === body.customer_id)
    if (!customer) return problem(request, 404, `no customer ${body.customer_id}`)
    if (customer.archived_at) return problem(request, 409, `customer ${customer.id} is archived`, PROBLEMS + 'customer_archived')
    const year = new Date().getUTCFullYear()
    const taken = (state.numbers[String(year)] ?? 0) + 1
    state.numbers[String(year)] = taken
    const at = now()
    const order: Order = {
      id: newId('o'),
      number: `ORD-${year}-${taken}`,
      customer_id: customer.id,
      status: 'draft',
      currency: mockSettings().orders_currency,
      minor_exponent: minorExponent(mockSettings().orders_currency),
      on_hold: false,
      shipping_address_id: customer.addresses.find((a) => a.default_shipping)?.id ?? null,
      billing_address_id: customer.addresses.find((a) => a.default_billing)?.id ?? null,
      shipping_address: null,
      billing_address: null,
      notes: body.notes ?? '',
      due_at: body.due_at ?? null,
      accepted_at: null,
      closed_at: null,
      cancelled_at: null,
      created_at: at,
      updated_at: at,
      created_by: BROWSER,
    }
    state.orders.push(order)
    event(order, 'order.created')
    return HttpResponse.json(order, { status: 201 })
  }),

  http.get('/api/v1/orders/:id', ({ request, params }) => {
    // `priceBook.ts` answers these: a path is matched by the first handler that answers.
    if (params.id === 'price-book' || params.id === 'estimate') return undefined
    const order = state.orders.find((o) => o.id === params.id)
    return order ? HttpResponse.json(detail(order)) : problem(request, 404, `no order '${String(params.id)}'`)
  }),
  http.get('/api/v1/orders/:id/events', ({ request, params }) => {
    const order = state.orders.find((o) => o.id === params.id)
    return order ? HttpResponse.json(state.events.filter((e) => e.order_id === order.id)) : problem(request, 404, `no order '${String(params.id)}'`)
  }),

  http.patch('/api/v1/orders/:id', async ({ request, params }) => {
    const body = (await request.json()) as OrderPatch & Record<string, unknown>
    const extra = unknownKeys(request, body, ['notes', 'due_at', 'shipping_address_id', 'billing_address_id', 'charges'])
    if (extra) return extra
    try {
      const order = lock(String(params.id))
      notEnded(order)
      const structural = body.charges != null || 'shipping_address_id' in body || 'billing_address_id' in body
      if (structural && !OPEN_FOR_EDITS.has(order.status)) {
        throw new Refused('order_not_editable', `order ${order.number} is ${order.status}: only its notes and due date may change now`)
      }
      const owner = state.customers.find((c) => c.id === order.customer_id)
      for (const field of ['shipping_address_id', 'billing_address_id'] as const) {
        const wanted = body[field]
        if (typeof wanted === 'string' && !owner?.addresses.some((a) => a.id === wanted)) {
          return fieldProblem(request, field, `no address ${wanted} of the order's customer`)
        }
        if (field in body) order[field] = wanted ?? null
      }
      if (body.notes != null) order.notes = body.notes
      if ('due_at' in body) order.due_at = body.due_at ?? null
      if (body.charges) {
        state.charges = state.charges.filter((c) => c.order_id !== order.id)
        body.charges.forEach((c, position) => state.charges.push({ id: newId('h'), order_id: order.id, kind: c.kind, label: c.label, amount_minor: c.amount_minor, position }))
        event(order, 'charges.replaced', { count: body.charges.length })
      }
      order.updated_at = now()
      return HttpResponse.json(order)
    } catch (error) {
      return refused(request, error)
    }
  }),

  http.post('/api/v1/orders/:id/accept', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      if (!OPEN_FOR_EDITS.has(order.status)) throw new Refused('order_not_acceptable', `order ${order.number} is ${order.status}`)
      notOnHold(order)
      const open = itemsOf(order.id).filter((i) => i.status !== 'cancelled')
      if (open.length === 0) throw new Refused('no_open_items', `order ${order.number} has no open items`)
      const unquoted = open.filter((i) => i.status !== 'approved').map((i) => i.id)
      if (unquoted.length > 0) throw new Refused('items_not_quoted', `${unquoted.length} item(s) of order ${order.number} have no accepted quote`, unquoted)
      const owner = state.customers.find((c) => c.id === order.customer_id)
      for (const field of ['shipping', 'billing'] as const) {
        const found = owner?.addresses.find((a) => a.id === order[`${field}_address_id`])
        if (found) {
          const { position: _position, ...snapshot } = found
          order[`${field}_address`] = snapshot
        }
      }
      setStatus(order, 'accepted')
      return order
    }),
  ),
  http.post('/api/v1/orders/:id/cancel', async ({ request, params }) => {
    const body = ((await request.text()) ? JSON.parse(await Promise.resolve('{}')) : {}) as { reason?: string | null }
    void body
    return transition(request, () => {
      const order = lock(String(params.id))
      if (order.status === 'shipped' || ENDED.has(order.status)) throw new Refused('order_not_cancellable', `order ${order.number} is ${order.status}`)
      const cancelled: string[] = []
      for (const i of itemsOf(order.id)) {
        if (!SETTLED.has(i.status)) {
          i.status = 'cancelled'
          cancelled.push(i.id)
        }
      }
      // As `Lifecycle.cancel_order` records it: what it was, and the items it took.
      event(order, 'order.status', { from_status: order.status, to: 'cancelled', items: cancelled })
      order.status = 'cancelled'
      order.cancelled_at ??= now()
      order.updated_at = now()
      return order
    })
  }),
  http.post('/api/v1/orders/:id/hold', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      if (!order.on_hold) {
        order.on_hold = true
        event(order, 'order.hold')
      }
      return order
    }),
  ),
  http.post('/api/v1/orders/:id/release', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      if (order.on_hold) {
        order.on_hold = false
        event(order, 'order.release')
      }
      return order
    }),
  ),
  // `Lifecycle.reopen_order`: a closed order back to what its rows give it; a cancelled
  // one back to the status it was cancelled from, with the items that cancel took.
  http.post('/api/v1/orders/:id/reopen', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      if (order.status !== 'closed' && order.status !== 'cancelled') {
        throw new Refused('order_not_ended', `order ${order.number} is ${order.status}: only a closed or cancelled order reopens`)
      }
      let base: OrderStatus = 'accepted'
      const restored: string[] = []
      if (order.status === 'cancelled') {
        const cancel = [...state.events].reverse().find((e) => e.order_id === order.id && e.kind === 'order.status' && e.detail.to === 'cancelled')
        const from = cancel?.detail.from_status as OrderStatus | undefined
        base = from && (OPEN_FOR_EDITS.has(from) || DERIVED.has(from)) ? from : order.accepted_at ? 'accepted' : 'draft'
        for (const id of (cancel?.detail.items as string[] | undefined) ?? []) {
          const found = state.items.find((i) => i.id === id && i.order_id === order.id && i.status === 'cancelled')
          if (!found) continue
          const quotes = quotesOf(found.id)
          const accepted = quotes.some((q) => q.accepted)
          if (DERIVED.has(base) && !accepted) continue
          const latest = attemptsOf(found.id).at(-1)
          found.status = accepted ? (latest?.outcome === 'failed' ? 'failed' : 'approved') : quotes.length > 0 ? 'quoted' : 'draft'
          restored.push(found.id)
        }
        if (DERIVED.has(base)) base = 'accepted'
      }
      const from = order.status
      order.status = base
      order.closed_at = null
      order.cancelled_at = null
      order.updated_at = now()
      const before = state.events.length
      rederive(order)
      // One audit row, as the backend writes: the rederive's own, if it wrote one, is folded in.
      state.events.splice(before)
      event(order, 'order.status', { from_status: from, to: order.status, reopened: true, items: restored })
      return order
    }),
  ),
  http.post('/api/v1/orders/:id/close', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      notOnHold(order)
      const items = itemsOf(order.id)
      const unsettled = items.filter((i) => !SETTLED.has(i.status)).map((i) => i.id)
      const unshipped = uncovered(items, shipmentsOf(order.id))
      const fulfilled = items.some((i) => i.status === 'fulfilled')
      if (!DERIVED.has(order.status) || unsettled.length > 0 || unshipped.length > 0 || !fulfilled) {
        throw new Refused(
          'order_not_closable',
          `order ${order.number} cannot close: ${unsettled.length} item(s) still open, ${unshipped.length} fulfilled item(s) not shipped${fulfilled ? '' : ', nothing fulfilled'}`,
          [...unsettled, ...unshipped],
        )
      }
      setStatus(order, 'closed')
      return order
    }),
  ),
  http.post('/api/v1/orders/:id/payments', async ({ request, params }) => {
    const body = (await request.json()) as PaymentCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['amount_minor', 'method', 'reference', 'received_at', 'notes'])
    if (extra) return extra
    if (typeof body.amount_minor !== 'number') return fieldProblem(request, 'amount_minor', 'Field required')
    try {
      const order = lock(String(params.id))
      if (order.status === 'cancelled') throw new Refused('order_cancelled', `order ${order.number} is cancelled`)
      const payment = { id: newId('p'), order_id: order.id, amount_minor: body.amount_minor, method: body.method ?? null, reference: body.reference ?? null, received_at: body.received_at ?? null, notes: body.notes ?? '', created_at: now() }
      state.payments.push(payment)
      event(order, 'payment.added', { payment_id: payment.id })
      return HttpResponse.json(payment, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),

  http.post('/api/v1/orders/:id/items', async ({ request, params }) => {
    const body = (await request.json()) as OrderItemCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['slug', 'commit', 'inputs', 'output_id', 'quantity', 'title', 'notes'])
    if (extra) return extra
    if (!Number.isInteger(body.quantity) || (body.quantity as number) < 1) return fieldProblem(request, 'quantity', 'Input should be greater than or equal to 1')
    const order = state.orders.find((o) => o.id === params.id)
    if (!order) return problem(request, 404, `no order '${String(params.id)}'`)
    const resolved = resolveReference(body)
    if (Array.isArray(resolved)) return fieldProblem(request, resolved[0], resolved[1])
    try {
      notEnded(order)
      openForItems(order, 'the list of items')
      const at = now()
      const position = itemsOf(order.id).length
      const created: OrderItem = {
        id: newId('i'),
        order_id: order.id,
        position,
        status: 'draft',
        quantity: body.quantity as number,
        title: body.title ?? resolved.reference.template_name,
        notes: body.notes ?? '',
        reference: resolved.reference,
        colours: resolved.colours,
        output_id: body.output_id ?? null,
        created_at: at,
        updated_at: at,
      }
      state.items.push(created)
      event(order, 'item.added', { item_id: created.id })
      rederive(order)
      return HttpResponse.json(created, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.patch('/api/v1/orders/:id/items/:item', async ({ request, params }) => {
    const body = (await request.json()) as OrderItemPatch & Record<string, unknown>
    const extra = unknownKeys(request, body, ['reference', 'output_id', 'quantity', 'title', 'notes', 'filaments'])
    if (extra) return extra
    let resolved: Resolved | undefined
    if (body.reference || body.output_id) {
      const found = resolveReference({ ...(body.reference ?? {}), output_id: body.output_id ?? null })
      if (Array.isArray(found)) return fieldProblem(request, found[0], found[1])
      resolved = found
    }
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      const refiled = body.filaments ? withFilaments(found, body.filaments) : undefined
      const repins = resolved !== undefined || body.quantity != null || refiled?.changed === true
      if (repins) openForItems(order, "an item's reference, quantity and filaments")
      if (resolved) {
        found.reference = resolved.reference
        found.colours = resolved.colours
      }
      if (refiled?.changed) found.colours = refiled.colours
      if ('output_id' in body) found.output_id = body.output_id ?? null
      if (body.quantity != null) found.quantity = body.quantity
      if (body.title != null) found.title = body.title
      if (body.notes != null) found.notes = body.notes
      if (repins) {
        for (const q of quotesOf(found.id)) q.accepted = false
        found.status = 'draft'
      }
      found.updated_at = now()
      event(order, 'item.updated', { item_id: found.id, repinned: repins })
      rederive(order)
      return found
    })
  }),
  http.delete('/api/v1/orders/:id/items/:item', ({ request, params }) => {
    try {
      const order = lock(String(params.id))
      const found = itemOf(order, String(params.item))
      if (found.status !== 'draft') {
        throw new Refused('item_not_draft', `item ${found.id} is ${found.status}: cancel it instead (POST /orders/${order.id}/items/${found.id}/cancel)`, [found.id])
      }
      // Its files go with it (`Lifecycle.delete_item`), each on the audit line.
      for (const attachment of state.attachments.filter((a) => a.item_id === found.id)) {
        event(order, 'attachment.removed', { attachment_id: attachment.id, item_id: found.id, filename: attachment.filename, was_delivered: found.delivered_attachment_id === attachment.id })
        delete state.files[attachment.id]
      }
      state.attachments = state.attachments.filter((a) => a.item_id !== found.id)
      state.items = state.items.filter((i) => i.id !== found.id)
      event(order, 'item.deleted', { item_id: found.id })
      rederive(order)
      return new HttpResponse(null, { status: 204 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  // `Lifecycle.reopen_item`: the state its quotes and attempts give it.
  http.post('/api/v1/orders/:id/items/:item/reopen', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (found.status !== 'cancelled') throw new Refused('item_not_cancelled', `item ${found.id} is ${found.status}: only a cancelled item reopens`, [found.id])
      const quotes = quotesOf(found.id)
      const accepted = quotes.some((q) => q.accepted)
      if (!OPEN_FOR_EDITS.has(order.status) && !accepted) {
        throw new Refused('item_not_priced', `item ${found.id} has no accepted quote, and order ${order.number} is ${order.status}: an accepted order holds priced items only`, [found.id])
      }
      const latest = attemptsOf(found.id).at(-1)
      found.status = accepted ? (latest?.outcome === 'failed' ? 'failed' : 'approved') : quotes.length > 0 ? 'quoted' : 'draft'
      found.updated_at = now()
      event(order, 'item.reopened', { item_id: found.id, to: found.status })
      rederive(order)
      return found
    }),
  ),
  http.post('/api/v1/orders/:id/items/:item/cancel', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      found.status = 'cancelled'
      found.updated_at = now()
      event(order, 'item.cancelled', { item_id: found.id })
      rederive(order)
      return found
    }),
  ),

  http.post('/api/v1/orders/:id/items/:item/quotes', async ({ request, params }) => {
    const body = (await request.json()) as ManualQuoteInput & Record<string, unknown>
    const extra = unknownKeys(request, body, ['unit_price_minor', 'label'])
    if (extra) return extra
    if (!Number.isInteger(body.unit_price_minor) || (body.unit_price_minor as number) < 0) return fieldProblem(request, 'unit_price_minor', 'Input should be greater than or equal to 0')
    try {
      const order = lock(String(params.id))
      notEnded(order)
      openForItems(order, "an item's price")
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      const price = body.unit_price_minor as number
      const quote: Quote = {
        id: newId('q'),
        order_item_id: found.id,
        created_at: now(),
        source: 'manual',
        accepted: false,
        sliced_sha256: null,
        sliced_blob_key: null,
        plates: [],
        lines: [{ kind: 'manual', label: body.label ?? 'Quoted by hand', quantity: 1, unit_minor: price, amount_minor: price }],
        unit_cost_minor: price,
        unit_price_minor: price,
        quantity: found.quantity,
        total_minor: price * found.quantity,
        created_by: BROWSER,
      }
      state.quotes.push(quote)
      if (found.status === 'draft') found.status = 'quoted'
      event(order, 'quote.recorded', { item_id: found.id, quote_id: quote.id })
      return HttpResponse.json(quote, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.post('/api/v1/orders/:id/items/:item/quotes/:quote/accept', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      openForItems(order, "an item's price")
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      const quote = state.quotes.find((q) => q.id === params.quote && q.order_item_id === found.id)
      if (!quote) throw new Refused('quote_not_found', `no quote ${String(params.quote)} of item ${found.id}`, [], true)
      if (quote.quantity !== found.quantity) throw new Refused('quote_stale', `quote ${quote.id} priced ${quote.quantity}, the item is now ${found.quantity}`)
      for (const q of quotesOf(found.id)) q.accepted = q.id === quote.id
      found.status = 'approved'
      found.updated_at = now()
      event(order, 'quote.accepted', { item_id: found.id, quote_id: quote.id })
      rederive(order)
      return found
    }),
  ),

  http.post('/api/v1/orders/:id/items/:item/fulfilments', async ({ request, params }) => {
    const body = (await request.json()) as FulfilmentInput & Record<string, unknown>
    const extra = unknownKeys(request, body, ['quantity', 'outcome', 'failure_reason', 'started_at', 'finished_at', 'notes'])
    if (extra) return extra
    try {
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      if (!FULFILLABLE.has(found.status)) throw new Refused(SETTLED.has(found.status) ? 'item_not_open' : 'item_not_approved', `item ${found.id} is ${found.status}`)
      const attempt: Fulfilment = {
        id: newId('f'),
        order_item_id: found.id,
        attempt: attemptsOf(found.id).length + 1,
        quantity: body.quantity,
        outcome: body.outcome,
        failure_reason: body.failure_reason ?? null,
        started_at: body.started_at ?? null,
        finished_at: body.finished_at ?? null,
        notes: body.notes ?? '',
        accepted_at: null,
        created_by: BROWSER,
        created_at: now(),
      }
      state.fulfilments.push(attempt)
      found.status = body.outcome === 'failed' ? 'failed' : 'approved'
      event(order, 'fulfilment.recorded', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, outcome: attempt.outcome })
      rederive(order)
      return HttpResponse.json(attempt, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.patch('/api/v1/orders/:id/items/:item/fulfilments/:f', async ({ request, params }) => {
    const body = (await request.json()) as FulfilmentInput
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      const attempt = state.fulfilments.find((f) => f.id === params.f && f.order_item_id === found.id)
      if (!attempt) throw new Refused('fulfilment_not_found', `no attempt ${String(params.f)} of item ${found.id}`, [], true)
      if (attempt.accepted_at) throw new Refused('fulfilment_accepted', `attempt ${attempt.attempt} is accepted`)
      if (!FULFILLABLE.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      Object.assign(attempt, { quantity: body.quantity, outcome: body.outcome, failure_reason: body.failure_reason ?? null, started_at: body.started_at ?? null, finished_at: body.finished_at ?? null, notes: body.notes ?? '' })
      const latest = attemptsOf(found.id).at(-1)!
      found.status = latest.outcome === 'failed' ? 'failed' : 'approved'
      event(order, 'fulfilment.updated', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, outcome: attempt.outcome })
      rederive(order)
      return attempt
    })
  }),
  http.post('/api/v1/orders/:id/items/:item/fulfilments/:f/accept', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      const attempt = state.fulfilments.find((f) => f.id === params.f && f.order_item_id === found.id)
      if (!attempt) throw new Refused('fulfilment_not_found', `no attempt ${String(params.f)} of item ${found.id}`, [], true)
      if (attempt.accepted_at) throw new Refused('fulfilment_accepted', `attempt ${attempt.attempt} is already accepted`)
      if (attempt.outcome === 'failed') throw new Refused('fulfilment_failed', `attempt ${attempt.attempt} failed; nothing was made`)
      if (!FULFILLABLE.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      attempt.accepted_at = now()
      const made = attemptsOf(found.id).filter((f) => f.accepted_at).reduce((sum, f) => sum + f.quantity, 0)
      if (made >= found.quantity) found.status = 'fulfilled'
      event(order, 'fulfilment.accepted', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, made })
      rederive(order)
      return attempt
    }),
  ),

  http.post('/api/v1/orders/:id/shipments', async ({ request, params }) => {
    const body = (await request.json()) as ShipmentInput
    try {
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const items = itemsOf(order.id)
      const lines = body.items ?? []
      const wrong = lines.filter((l) => !items.some((i) => i.id === l.order_item_id)).map((l) => l.order_item_id)
      if (wrong.length > 0) throw new Refused('item_not_found', `not items of order ${order.number}`, wrong, true)
      const unmade = lines.filter((l) => items.find((i) => i.id === l.order_item_id)?.status !== 'fulfilled').map((l) => l.order_item_id)
      if (unmade.length > 0) throw new Refused('items_not_fulfilled', 'a shipment carries fulfilled items only', unmade)
      const shipment: Shipment = {
        id: newId('s'),
        order_id: order.id,
        carrier: body.carrier ?? null,
        service: body.service ?? null,
        tracking: body.tracking ?? null,
        shipped_at: body.shipped_at ?? null,
        address: body.address ?? order.shipping_address ?? null,
        cost_minor: body.cost_minor ?? null,
        notes: body.notes ?? '',
        created_at: now(),
        items: lines,
      }
      state.shipments.push(shipment)
      event(order, 'shipment.added', { shipment_id: shipment.id })
      rederive(order)
      return HttpResponse.json(shipment, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.patch('/api/v1/orders/:id/shipments/:s', async ({ request, params }) => {
    const body = (await request.json()) as ShipmentEdit
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      notOnHold(order)
      const shipment = state.shipments.find((s) => s.id === params.s && s.order_id === order.id)
      if (!shipment) throw new Refused('shipment_not_found', `no shipment ${String(params.s)} of order ${order.number}`, [], true)
      const items = itemsOf(order.id)
      if (body.items) {
        const unmade = body.items.filter((l) => items.find((i) => i.id === l.order_item_id)?.status !== 'fulfilled').map((l) => l.order_item_id)
        if (unmade.length > 0) throw new Refused('items_not_fulfilled', 'a shipment carries fulfilled items only', unmade)
      }
      // Only what the body carries changes; a field left out keeps the shipment's value.
      for (const key of ['carrier', 'service', 'tracking', 'shipped_at', 'cost_minor', 'notes', 'items'] as const) {
        if (body[key] !== undefined) Object.assign(shipment, { [key]: body[key] })
      }
      if (body.address) shipment.address = body.address
      event(order, 'shipment.updated', { shipment_id: shipment.id })
      rederive(order)
      return shipment
    })
  }),
]

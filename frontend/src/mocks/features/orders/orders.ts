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
  OrderRow,
  OrderStatus,
  Payment,
  PaymentCreate,
  PaymentRequestCreate,
  PaymentRequestView,
  Quote,
  Shipment,
  ShipmentEdit,
  ShipmentInput,
} from '../../../api/types'
import { mockModels, mockOutput, mockSettings } from '../../handlers'
import { versionIds } from '../../fixtures'
import { printersState } from '../printers'
import { mockLibraryFile } from '../library'
import { byCreated } from '../../../components/orders/price'
import { BAD_CURSOR, BROWSER, fieldProblem, minorExponent, newId, now, problem, PROBLEMS, Refused, revisionsOf, seedOrders, state, UNKNOWN_PARAMETER } from './state'
import { pushRevision } from './revisions'
import { orderRow } from './costs'
import { productOf } from './report'
import { fillFromRule, returnParts, useParts } from './parts'
import { archiveCopies, pastPrint } from './pastPrints'

/**
 * `/api/v1/orders` (backend/scadbuddy/api/orders.py) with the lifecycle's rules
 * (`orders/lifecycle.py`) mirrored: the status past `accepted` is derived from the items
 * and shipments, a refusal is a 409 problem typed `orders/<slug>` naming the ids in the
 * way, and on hold every step that advances the order is refused.
 */

const LIST_PARAMS = new Set(['status', 'customer_id', 'q', 'from', 'to', 'accepted_from', 'accepted_to', 'currency', 'product', 'margin_state', 'source', 'unsubmitted', 'cursor', 'limit'])
const MARGIN_STATES = new Set(['ok', 'thin', 'loss', 'unknown'])
/** `SCAN_PAGE_SIZE` and `MAX_SCAN_PAGES` in backend/scadbuddy/api/orders.py (#5515). */
export const SCAN_PAGE_SIZE = 100
export const MAX_SCAN_PAGES = 10
const STATUSES = new Set<OrderStatus>(['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled'])
export const OPEN_FOR_EDITS = new Set<OrderStatus>(['draft', 'quoted'])
/** `lifecycle.py` `TAKES_ITEMS` (#5349): past acceptance, the states that still take an item. */
export const TAKES_ITEMS = new Set<OrderStatus>(['accepted', 'in_production', 'ready'])
const ENDED = new Set<OrderStatus>(['closed', 'cancelled'])
const DERIVED = new Set<OrderStatus>(['accepted', 'in_production', 'ready', 'shipped'])
export const SETTLED = new Set(['fulfilled', 'cancelled'])
const FULFILLABLE = new Set(['approved', 'failed'])
/** #4719 — the print path's states: an attempt is edited, or accepted once printed, in them. */
const PRINTING = new Set(['queued', 'printing', 'printed'])

const CHARGE_KINDS = new Set(['shipping', 'discount', 'fee', 'tax'])
const CHARGE_KEYS = new Set(['kind', 'label', 'amount_minor'])

/**
 * Pydantic's lax `int` (#5573, measured with `ChargeInput.model_validate_json`): an integral
 * number, a boolean, or a string of one integer with `_` separators and an optional zero
 * fraction (" +1_000.0 " is 1000); "5.5", "5." and "1e3" are refused.
 */
function laxInt(value: unknown): number | null {
  if (typeof value === 'number') return Number.isInteger(value) ? value : null
  if (typeof value === 'boolean') return value ? 1 : 0
  if (typeof value === 'string' && /^\s*[+-]?\d+(_\d+)*(\.0+)?\s*$/.test(value)) return Number(value.replaceAll('_', ''))
  return null
}

/**
 * The backend's `ChargeInput` 422: known kind, label 1..200 code points (as Pydantic counts),
 * integer amount, no other keys. Pydantic's lax `int` also takes an integer string such as "4".
 */
function chargeProblem(c: unknown): string | null {
  if (typeof c !== 'object' || c === null) return 'Input should be an object'
  const row = c as Record<string, unknown>
  const extra = Object.keys(row).find((k) => !CHARGE_KEYS.has(k))
  if (extra) return `${extra}: Extra inputs are not permitted`
  if (typeof row.kind !== 'string' || !CHARGE_KINDS.has(row.kind)) return "kind: Input should be 'shipping', 'discount', 'fee' or 'tax'"
  const points = typeof row.label === 'string' ? [...row.label].length : 0
  if (points < 1 || points > 200) return 'label: String should have 1 to 200 characters'
  if (laxInt(row.amount_minor) === null) return 'amount_minor: Input should be a valid integer'
  return null
}

/** `Lifecycle._after`: the status the item's latest attempt gives it. */
function after(latest: Fulfilment, current: string): OrderItem['status'] {
  if (latest.outcome === 'failed') return 'failed'
  if (!latest.print_run_id) return 'approved'
  if (latest.outcome === 'succeeded' || latest.outcome === 'partial') return 'printed'
  return PRINTING.has(current) ? (current as OrderItem['status']) : 'queued'
}

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
/** `lifecycle.py` `stale_attempt` (#5582, #5592): a production attempt older than the revision
 * the accepted quote prices printed an earlier design. Its accept is refused, and the detail
 * marks it `stale`. */
function staleAttempt(attempt: Fulfilment): boolean {
  if (attempt.purpose !== 'production') return false
  const pricedId = quotesOf(attempt.order_item_id).find((q) => q.accepted)?.revision_id
  const priced = pricedId == null ? undefined : revisionsOf(attempt.order_item_id).find((r) => r.id === pricedId)
  return priced !== undefined && Date.parse(attempt.created_at) < Date.parse(priced.created_at)
}
/** The attempts that count (#5280): a test print is never one of the item's production. */
const productionOf = (itemId: string) => attemptsOf(itemId).filter((f) => f.purpose !== 'test')
/** `OrderStore.print_starting`: the items whose `order_item_print` operation is still
 * running. The mock attaches a run at once, so a test adds an item here to stand for a
 * start still under way. */
export const printsStarting = new Set<string>()

export function reset(): void {
  printsStarting.clear()
}

/** `OrderStore.print_in_flight`: the item's attempt still open, of either purpose. A claim
 * counts by its `print_request_id`, its run attached or not yet. */
export const printInFlight = (itemId: string) => attemptsOf(itemId).find((f) => f.print_request_id && f.outcome === 'unknown' && f.finished_at === null)
/** `orders/printing.py` `print_request_id`: one claim per item and Idempotency-Key. The
 * backend hashes the pair; the mock joins it. */
export const printRequestId = (itemId: string, key: string) => `order-item-print-${itemId}-${key}`
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
export function notOnHold(order: Order) {
  if (order.on_hold) throw new Refused('order_on_hold', `order ${order.number} is on hold`)
}
export function openForItems(order: Order, what: string) {
  if (!OPEN_FOR_EDITS.has(order.status)) {
    throw new Refused('item_locked', `order ${order.number} is ${order.status}: ${what} is pinned once accepted`)
  }
}
/** `Lifecycle._open_for_item` (#5349): past `quoted`, an item with no accepted quote stays open. */
export function openForItem(order: Order, item: OrderItem, what: string) {
  if (OPEN_FOR_EDITS.has(order.status)) return
  if (TAKES_ITEMS.has(order.status) && !quotesOf(item.id).some((q) => q.accepted)) return
  throw new Refused('item_locked', `order ${order.number} is ${order.status}: ${what} is pinned once accepted`, [item.id])
}
/** `lifecycle.py` `requote_open` (#5350): open as `openForItem`, and past `quoted` while
 * the item was revised after acceptance. */
export function requoteOpen(order: Order, item: OrderItem): boolean {
  if (OPEN_FOR_EDITS.has(order.status)) return true
  if (!TAKES_ITEMS.has(order.status)) return false
  return item.revised_after_acceptance || !quotesOf(item.id).some((q) => q.accepted)
}
/** `Lifecycle._open_for_requote` (#5350): the refusal names the route that revises an accepted item. */
export function openForRequote(order: Order, item: OrderItem, what: string) {
  if (requoteOpen(order, item)) return
  const route = TAKES_ITEMS.has(order.status) && item.kind !== 'custom' ? `; revise it with POST /orders/${order.id}/items/${item.id}/revisions/after-acceptance` : ''
  throw new Refused('item_locked', `order ${order.number} is ${order.status}: ${what} is pinned once accepted${route}`, [item.id])
}
/** `Lifecycle._takes_item` (#5349): the plain add, or the add after acceptance. */
export function takesItem(order: Order, afterAcceptance: boolean) {
  notEnded(order)
  if (!afterAcceptance) {
    if (TAKES_ITEMS.has(order.status)) {
      throw new Refused(
        'item_locked',
        `order ${order.number} is ${order.status}: the list of items is pinned once accepted; add an item with POST /orders/${order.id}/items/after-acceptance`,
      )
    }
    openForItems(order, 'the list of items')
    return
  }
  if (OPEN_FOR_EDITS.has(order.status)) {
    throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted: add the item with POST /orders/${order.id}/items`)
  }
  if (!TAKES_ITEMS.has(order.status)) throw new Refused('item_locked', `order ${order.number} is ${order.status}: it takes no new item once shipped`)
  notOnHold(order)
}
/** `Lifecycle._added`: the add's audit row. */
function added(order: Order, item: OrderItem, afterAcceptance: boolean, detail: Record<string, unknown> = {}) {
  if (afterAcceptance) {
    event(order, 'item.added_after_acceptance', { item_id: item.id, title: item.title, quantity: item.quantity, order_status: order.status, ...detail })
  } else {
    event(order, 'item.added', { item_id: item.id, ...detail })
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
    else if (items.some((i) => productionOf(i.id).length > 0)) status = 'in_production'
    else status = 'accepted'
  }
  if (status !== order.status) setStatus(order, status)
}

function detail(order: Order): OrderDetail {
  const items = itemsOf(order.id).map((i) => ({ ...i, quotes: quotesOf(i.id), fulfilments: attemptsOf(i.id).map((f) => ({ ...f, stale: staleAttempt(f) })), revisions: revisionsOf(i.id) }))
  const charges = state.charges.filter((c) => c.order_id === order.id).sort((a, b) => a.position - b.position)
  const payments = state.payments.filter((p) => p.order_id === order.id)
  // `orders/totals.py` `order_totals`: an accepted quote's total, else the newest quote's unit
  // price times the quantity now; an item with no quote is in no sum.
  let subtotal = 0
  let pending = 0
  const unpriced: string[] = []
  for (const i of items.filter((each) => each.status !== 'cancelled')) {
    const accepted = i.quotes.find((q) => q.accepted)
    // `item_price`'s newest: the latest by (created_at, id), as the page orders them.
    const newest = [...i.quotes].sort(byCreated).at(-1)
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
    payment_requests: state.paymentRequests.filter((r) => r.order_id === order.id),
    shipments: shipmentsOf(order.id),
    attachments: state.attachments.filter((a) => a.order_id === order.id),
    projects: state.projects.filter((p) => p.order_id === order.id).map(({ order_id: _order, ...project }) => project),
    totals: { subtotal_minor: subtotal, pending_minor: pending, unpriced_items: unpriced, charges_minor: charged, total_minor: subtotal + charged, paid_minor: paid, balance_minor: subtotal + charged - paid },
  }
}

/** The balance now: the total minus what was paid (`totals_of`), refunds included. */
export const balanceOf = (order: Order) => detail(order).totals.balance_minor

/** `payments.set_request_status`: the request's new state, and the payment that paid it. */
export function settleRequest(request: PaymentRequestView, status: PaymentRequestView['status'], paymentId?: string) {
  request.status = status
  if (status === 'paid') {
    request.paid_at = now()
    request.payment_id = paymentId ?? null
  }
}

/** `Lifecycle._close_balance`: a balance at zero or below closes the open balance requests. */
export function closeBalance(order: Order, payment: Payment) {
  if (balanceOf(order) > 0) return
  for (const open of state.paymentRequests) {
    if (open.order_id === order.id && open.status === 'open' && open.kind === 'balance') settleRequest(open, 'paid', payment.id)
  }
}

function requestOf(order: Order, requestId: string): PaymentRequestView {
  const found = state.paymentRequests.find((r) => r.id === requestId && r.order_id === order.id)
  if (!found) throw new Refused('payment_request_not_found', `no payment request ${requestId} of order ${order.number}`, [], true)
  return found
}

function openRequestOf(order: Order, requestId: string): PaymentRequestView {
  const found = requestOf(order, requestId)
  if (found.status !== 'open') throw new Refused('payment_request_not_open', `payment request ${requestId} is ${found.status}`)
  return found
}

/** `Lifecycle._reopened_status`: what a cancelled item comes back as, and whether priced. */
function reopenedStatus(item: OrderItem): [OrderItem['status'], boolean] {
  const quotes = quotesOf(item.id)
  const accepted = quotes.some((q) => q.accepted)
  // #5350: priced at its kept quote, but its new design waits for its own.
  if (accepted && item.revised_after_acceptance) return [quotes.some((q) => !q.accepted && q.revision_id === item.current_revision_id) ? 'quoted' : 'draft', true]
  if (accepted) return [productionOf(item.id).at(-1)?.outcome === 'failed' ? 'failed' : 'approved', true]
  return [quotes.length > 0 ? 'quoted' : 'draft', false]
}

/** The backend's answer for a `Refused`: 404 for a missing row, else 409, each typed by the slug. */
export function refused(request: Request, error: unknown) {
  if (error instanceof FilamentsRefused) return fieldProblem(request, 'filaments', error.message)
  if (error instanceof FieldRefused) return fieldProblem(request, error.field, error.message)
  if (!(error instanceof Refused)) throw error
  const extensions = {
    ...(error.items.length > 0 ? { items: error.items } : {}),
    ...(error.shipments.length > 0 ? { shipments: error.shipments } : {}),
  }
  // A 404 is typed by its slug too, as `orders/problems.py` `refused` types it.
  return problem(request, error.notFound ? 404 : 409, error.message, PROBLEMS + error.slug, extensions)
}

/** A body naming a slot or a filament that cannot be (#4250): a 422 on `filaments`. */
export class FilamentsRefused extends Error {}

/** A body field the item cannot take (#4954, a custom item's fields on a model item): a 422 on it. */
class FieldRefused extends Error {
  readonly field: string
  constructor(field: string, message: string) {
    super(message)
    this.field = field
  }
}

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

/** The refusals of `acceptQuote`, for a caller that must know before it writes. */
export function checkQuote(found: OrderItem, quote: Quote) {
  if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
  if (quote.quantity !== found.quantity) throw new Refused('quote_stale', `quote ${quote.id} priced ${quote.quantity}, the item is now ${found.quantity}`)
  if (found.kind !== 'custom') {
    if (quote.revision_id != null && quote.revision_id !== found.current_revision_id) {
      throw new Refused('quote_stale', `quote ${quote.id} prices an earlier revision of the design`)
    }
    if (found.approval === 'design_first') {
      const revisionId = quote.revision_id ?? found.current_revision_id
      if (revisionsOf(found.id).find((r) => r.id === revisionId)?.answer !== 'accepted') {
        throw new Refused('revision_not_accepted', `the customer has not accepted the design of item ${found.id}`)
      }
    }
  }
}

/**
 * `Lifecycle._accept_quote`, once the caller has checked the order and the item: a quote of
 * another quantity or of an earlier revision is stale, and a `design_first` item's quote
 * needs its revision accepted (#5280).
 */
export function acceptQuote(order: Order, found: OrderItem, quote: Quote): OrderItem {
  checkQuote(found, quote)
  for (const q of quotesOf(found.id)) q.accepted = q.id === quote.id
  found.status = 'approved'
  // #5350: the new price replaces the one kept since a revision after acceptance.
  found.revised_after_acceptance = false
  found.updated_at = now()
  event(order, 'quote.accepted', { item_id: found.id, quote_id: quote.id })
  rederive(order)
  return found
}

/** A transition: the lifecycle's rules, then the derivation, then the answer. */
export function transition<T>(request: Request, run: () => T) {
  try {
    return HttpResponse.json(run() as unknown as Record<string, unknown>)
  } catch (error) {
    return refused(request, error)
  }
}

export type Resolved = { reference: NonNullable<OrderItem['reference']>; colours: OrderItem['colours'] }

/**
 * `Lifecycle._custom_price` (#4954): a custom item's typed price as a `manual` quote,
 * accepted (the item `approved`) or, with `accept` false (the agent's), left `quoted`.
 */
function customPrice(order: Order, found: OrderItem, price: number, accept: boolean): Quote {
  for (const q of quotesOf(found.id)) q.accepted = false
  const quote: Quote = {
    id: newId('q'),
    order_item_id: found.id,
    created_at: now(),
    source: 'manual',
    accepted: accept,
    sliced_sha256: null,
    sliced_blob_key: null,
    plates: [],
    lines: [{ kind: 'manual', label: 'Custom item', quantity: 1, unit_minor: price, amount_minor: price }],
    unit_cost_minor: price,
    unit_price_minor: price,
    quantity: found.quantity,
    total_minor: price * found.quantity,
    created_by: BROWSER,
  }
  state.quotes.push(quote)
  found.status = accept ? 'approved' : 'quoted'
  event(order, accept ? 'quote.accepted' : 'quote.recorded', { item_id: found.id, quote_id: quote.id })
  return quote
}

/** `orders/reference.py`: from the output, or by hand against the catalogue; a problem names the field. */
export function resolveReference(body: { slug?: string | null; commit?: string | null; inputs?: Record<string, unknown> | null; output_id?: string | null }): Resolved | [string, string] {
  let { slug, commit, inputs } = body
  let colours: OrderItem['colours'] = []
  let libraries: Resolved['reference']['libraries'] = []
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

/** Whether two references name the same design: the template, its commit and its inputs. */
export function sameDesign(a: OrderItem['reference'], b: OrderItem['reference']): boolean {
  if (a === null || b === null) return a === b
  return a.slug === b.slug && a.commit === b.commit && JSON.stringify(a.inputs) === JSON.stringify(b.inputs)
}

/** `print_library/ids.py` `NATIVE_ID_BASE`: a native library file's id is at least this. */
const NATIVE_ID_BASE = 1_000_000_000

/**
 * #5544 — a library file item, as `ItemCreate._shape` and `api/orders.py` `_library_file`
 * answer: the file only (any other source, kind or custom field is 422), and a file the
 * library does not have or does not print is 422 on `library_file_id`.
 */
function addLibraryItem(
  request: Request,
  order: Order,
  body: OrderItemCreate & Record<string, unknown>,
  afterAcceptance: boolean,
  // #4777 — a past print's copy in the native library, which the library mock does not list.
  copy?: { id: number; filename: string },
  origin: number[] = [],
) {
  if ('kind' in body && body.kind !== 'library_file') return fieldProblem(request, 'body', `library_file_id names a library file item: drop kind '${String(body.kind)}'`)
  const fileId = copy?.id ?? body.library_file_id
  if (fileId == null) return fieldProblem(request, 'body', 'a library_file item needs a library_file_id')
  if (!Number.isInteger(fileId) || fileId < 1) return fieldProblem(request, 'library_file_id', 'Input should be greater than or equal to 1')
  const named = (['slug', 'commit', 'inputs', 'output_id', 'unit_price_minor', 'requires_printing', 'accept'] as const).filter((name) => body[name] != null)
  if (named.length) return fieldProblem(request, 'body', `a library file item names its file only: drop ${named.join(', ')}`)
  const listed = copy ? undefined : mockLibraryFile(fileId)
  const file = copy ?? listed
  if (!file) return fieldProblem(request, 'library_file_id', `no library file ${fileId} in the library that serves`)
  if (listed && !['3mf', 'gcode.3mf', 'stl'].includes((listed.file_type ?? '').toLowerCase())) {
    return fieldProblem(request, 'library_file_id', `library file ${listed.filename} is a ${listed.file_type ?? 'file of unknown type'}: ScadBuddy prints only 3MF and STL files`)
  }
  try {
    takesItem(order, afterAcceptance)
    const at = now()
    const created: OrderItem = {
      id: newId('i'),
      order_id: order.id,
      position: itemsOf(order.id).length,
      status: 'draft',
      quantity: body.quantity as number,
      title: body.title?.trim() || file.filename,
      notes: body.notes ?? '',
      kind: 'library_file',
      requires_printing: true,
      sku: body.sku ?? null,
      reference: null,
      library_file: { id: file.id, filename: file.filename, library: file.id >= NATIVE_ID_BASE ? 'native' : 'bambuddy' },
      colours: [],
      output_id: null,
      approval: 'together',
      current_revision_id: null,
      revised_after_acceptance: false,
      origin_archive_ids: origin,
      created_at: at,
      updated_at: at,
    }
    state.items.push(created)
    added(order, created, afterAcceptance, { item_kind: 'library_file', library_file_id: file.id, ...(origin.length ? { origin_archive_ids: origin } : {}) })
    rederive(order)
    return HttpResponse.json(created, { status: 201 })
  } catch (error) {
    return refused(request, error)
  }
}

/** `MAX_BATCH_ITEMS` of `api/orders.py` (#4777). */
const MAX_BATCH_ITEMS = 50

/**
 * `POST /orders/:id/items/batch` and its after-acceptance route (#4777): each item through
 * the single add, in order; a refusal puts the orders' state back as it was and answers
 * that refusal, naming the item's place (`item_index`) as the route does.
 */
function addItems(afterAcceptance: boolean): Parameters<typeof http.post>[1] {
  const single = addItem(afterAcceptance)
  return async (info) => {
    const { request } = info
    const body = (await request.json()) as { items?: unknown }
    if (!Array.isArray(body.items) || body.items.length < 1 || body.items.length > MAX_BATCH_ITEMS) {
      return fieldProblem(request, 'items', `List should have at least 1 and at most ${MAX_BATCH_ITEMS} items`)
    }
    const saved = structuredClone(state)
    const created: unknown[] = []
    for (const [index, item] of body.items.entries()) {
      const one = new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify(item) })
      const answer = (await single({ ...info, request: one } as never)) as Response
      if (answer.status !== 201) {
        seedOrders(saved)
        const problem = (await answer.json()) as Record<string, unknown>
        // `api/orders.py` `_in_item`: each field refusal's place inside `items[index]`.
        const errors = Array.isArray(problem['errors'])
          ? (problem['errors'] as { loc?: unknown[] }[]).map((e) =>
              Array.isArray(e.loc) && e.loc[0] === 'body' ? { ...e, loc: ['body', 'items', index, ...e.loc.slice(1)] } : e,
            )
          : undefined
        return HttpResponse.json(
          { ...problem, ...(errors ? { errors } : {}), detail: `items[${index}]: ${String(problem['detail'])}`, item_index: index },
          { status: answer.status, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }
      created.push(await answer.json())
    }
    return HttpResponse.json(created, { status: 201 })
  }
}

/**
 * #4777 — a past print's item, as `api/orders.py` `_archive_item` makes it: the archive alone
 * names it; it is the item of the output or library file that printed it, else of its copy
 * in the native library (one per archive, reused), and keeps its run's archives.
 */
async function addArchiveItem(info: Parameters<Parameters<typeof http.post>[1]>[0], order: Order, body: OrderItemCreate & Record<string, unknown>, afterAcceptance: boolean) {
  const { request } = info
  const named = (['slug', 'commit', 'inputs', 'output_id', 'library_file_id', 'unit_price_minor', 'requires_printing', 'accept'] as const).filter((name) => body[name] != null)
  if ('kind' in body) named.push('kind' as never)
  if (named.length) return fieldProblem(request, 'body', `archive_id names a past print, the item's only source: drop ${named.join(', ')}`)
  const archiveId = body.archive_id as number
  const found = pastPrint(archiveId)
  const origin = found ? [...new Set([...found.run, archiveId])].sort((a, b) => a - b) : [archiveId]
  const { archive_id: _picked, ...rest } = body
  if (found?.output_id || found?.library_file_id) {
    const source = found.output_id ? { output_id: found.output_id } : { library_file_id: found.library_file_id }
    const one = new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify({ ...rest, ...source }) })
    const answer = (await addItem(afterAcceptance)({ ...info, request: one } as never)) as Response
    if (answer.status !== 201) return answer
    const created = (await answer.json()) as OrderItem
    const stored = state.items.find((i) => i.id === created.id)
    if (stored) stored.origin_archive_ids = origin
    return HttpResponse.json(stored ?? created, { status: 201 })
  }
  if (!found) return problem(request, 409, `Bambuddy has no archive ${archiveId} any more`, PROBLEMS + 'archive_gone', { archive_id: archiveId })
  if (mockSettings().library_backend !== 'native') {
    return problem(request, 409, `archive ${archiveId} was printed by nothing of ScadBuddy's, so its file is copied into the native library, and the native library does not serve`, PROBLEMS + 'archive_copy_not_native', { archive_id: archiveId })
  }
  const copy = archiveCopies.get(archiveId) ?? { id: NATIVE_ID_BASE + archiveId, filename: found.filename }
  archiveCopies.set(archiveId, copy)
  return addLibraryItem(request, order, rest, afterAcceptance, copy, origin)
}

/** `POST /orders/:id/items` and, with `afterAcceptance`, `…/items/after-acceptance` (#5349). */
function addItem(afterAcceptance: boolean): Parameters<typeof http.post>[1] {
  return async (info) => {
    const { request, params } = info
    const body = (await request.json()) as OrderItemCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['kind', 'slug', 'commit', 'inputs', 'output_id', 'library_file_id', 'archive_id', 'quantity', 'title', 'notes', 'sku', 'unit_price_minor', 'requires_printing', 'accept'])
    if (extra) return extra
    if (!Number.isInteger(body.quantity) || (body.quantity as number) < 1) return fieldProblem(request, 'quantity', 'Input should be greater than or equal to 1')
    const order = state.orders.find((o) => o.id === params.id)
    if (!order) return problem(request, 404, `no order '${String(params.id)}'`)
    if (body.archive_id != null) return addArchiveItem(info, order, body, afterAcceptance)
    if (body.library_file_id != null || body.kind === 'library_file') return addLibraryItem(request, order, body, afterAcceptance)
    if (body.kind === 'custom') {
      // `ItemCreate._shape` and `Lifecycle.add_custom_item` (#4954).
      const named = (['slug', 'commit', 'inputs', 'output_id'] as const).filter((name) => body[name] != null)
      if (named.length) return fieldProblem(request, 'body', `a custom item names no source: drop ${named.join(', ')}`)
      if (!body.title?.trim()) return fieldProblem(request, 'body', 'a custom item needs a title')
      const price = body.unit_price_minor
      // After acceptance an unset accept means false, and a true one is refused (#5349).
      const accept = afterAcceptance ? body.accept === true : body.accept !== false
      if (afterAcceptance && accept) return fieldProblem(request, 'accept', 'accept: an item added after acceptance needs its quote accepted on its own')
      if (price == null || !Number.isInteger(price) || price < 0) return fieldProblem(request, 'body', 'a custom item needs a unit_price_minor')
      try {
        takesItem(order, afterAcceptance)
        if (accept) notOnHold(order)
        const at = now()
        const created: OrderItem = {
          id: newId('i'),
          order_id: order.id,
          position: itemsOf(order.id).length,
          status: 'draft',
          quantity: body.quantity as number,
          title: body.title.trim(),
          notes: body.notes ?? '',
          kind: 'custom',
          requires_printing: body.requires_printing ?? false,
          sku: body.sku ?? null,
          reference: null,
          colours: [],
          output_id: null,
          approval: 'together',
          current_revision_id: null,
          revised_after_acceptance: false,
          created_at: at,
          updated_at: at,
        }
        state.items.push(created)
        added(order, created, afterAcceptance, { item_kind: 'custom' })
        customPrice(order, created, price, accept)
        rederive(order)
        return HttpResponse.json(created, { status: 201 })
      } catch (error) {
        return refused(request, error)
      }
    }
    if (body.kind === 'request') {
      // `ItemCreate._shape` and `Lifecycle.add_request_item` (#5280): a title, no source.
      const named = (['slug', 'commit', 'inputs', 'output_id', 'unit_price_minor', 'requires_printing', 'accept'] as const).filter((name) => body[name] != null)
      if (named.length) return fieldProblem(request, 'body', `a request item takes no ${named.join(', ')}`)
      if (!body.title?.trim()) return fieldProblem(request, 'body', 'a request item needs a title')
      try {
        takesItem(order, afterAcceptance)
        const at = now()
        const created: OrderItem = {
          id: newId('i'),
          order_id: order.id,
          position: itemsOf(order.id).length,
          status: 'draft',
          quantity: body.quantity as number,
          title: body.title.trim(),
          notes: body.notes ?? '',
          kind: 'request',
          requires_printing: true,
          sku: body.sku ?? null,
          reference: null,
          colours: [],
          output_id: null,
          approval: 'design_first',
          current_revision_id: null,
          revised_after_acceptance: false,
          created_at: at,
          updated_at: at,
        }
        state.items.push(created)
        added(order, created, afterAcceptance, { item_kind: 'request' })
        rederive(order)
        return HttpResponse.json(created, { status: 201 })
      } catch (error) {
        return refused(request, error)
      }
    }
    if (body.unit_price_minor != null || body.requires_printing != null || body.accept != null) return fieldProblem(request, 'body', 'only a custom item takes unit_price_minor, requires_printing or accept')
    const resolved = resolveReference(body)
    if (Array.isArray(resolved)) return fieldProblem(request, resolved[0], resolved[1])
    try {
      takesItem(order, afterAcceptance)
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
        kind: 'model',
        requires_printing: true,
        sku: body.sku ?? null,
        reference: resolved.reference,
        colours: resolved.colours,
        output_id: body.output_id ?? null,
        approval: 'together',
        current_revision_id: null,
        revised_after_acceptance: false,
        created_at: at,
        updated_at: at,
      }
      state.items.push(created)
      added(order, created, afterAcceptance)
      // `Lifecycle.add_item`: the design it is made with is its revision 1.
      pushRevision(order, created, resolved.reference, resolved.colours, '')
      fillFromRule(order, created)
      rederive(order)
      return HttpResponse.json(created, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }
}

export const handlers = [
  http.get('/api/v1/orders', ({ request }) => {
    const sp = new URL(request.url).searchParams
    for (const name of sp.keys()) if (!LIST_PARAMS.has(name)) return problem(request, 400, `unknown query parameter '${name}'`, UNKNOWN_PARAMETER)
    // Repeated, any of them (#5276).
    const statuses = sp.getAll('status')
    if (statuses.some((s) => !STATUSES.has(s as OrderStatus))) return problem(request, 422, 'status is not an order status')
    // #5327: repeated, any of them. An order with no charged figure has no state.
    const margins = sp.getAll('margin_state')
    if (margins.some((m) => !MARGIN_STATES.has(m))) return problem(request, 422, 'margin_state is not a margin state')
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
    // #5329: a profit report row's orders, as `OrderStore.orders` narrows them.
    const acceptedFrom = sp.get('accepted_from')
    const acceptedTo = sp.get('accepted_to')
    const currency = sp.get('currency')
    if (currency !== null && !/^[A-Z]{3}$/.test(currency)) return fieldProblem(request, 'currency', 'String should match pattern')
    // #5282: the storefront's unsubmitted carts only with `unsubmitted=true`.
    const source = sp.get('source')
    if (source !== null && source !== 'back_office' && source !== 'storefront') return fieldProblem(request, 'source', "Input should be 'back_office' or 'storefront'")
    const unsubmitted = sp.get('unsubmitted') === 'true'
    const product = sp.get('product')
    if (product !== null && product !== 'custom' && !/^(model|sku):.+$/.test(product)) return fieldProblem(request, 'product', 'product is model:<slug>, sku:<sku> or custom')
    const rows = [...state.orders]
      .sort((a, b) => (a.created_at === b.created_at ? (a.id < b.id ? 1 : -1) : a.created_at < b.created_at ? 1 : -1))
      .filter((o) => (o.source === 'storefront' && o.submitted_at == null) === unsubmitted)
      .filter((o) => !source || o.source === source)
      .filter((o) => statuses.length === 0 || statuses.includes(o.status))
      .filter((o) => !customer || o.customer_id === customer)
      .filter((o) => !from || Date.parse(o.created_at) >= Date.parse(from))
      .filter((o) => !to || Date.parse(o.created_at) <= Date.parse(to))
      .filter((o) => !acceptedFrom || (o.accepted_at != null && Date.parse(o.accepted_at) >= Date.parse(acceptedFrom)))
      .filter((o) => !acceptedTo || (o.accepted_at != null && Date.parse(o.accepted_at) < Date.parse(acceptedTo)))
      .filter((o) => !currency || o.currency === currency)
      .filter((o) => !product || state.items.some((i) => i.order_id === o.id && productOf(i) === product))
      .filter((o) => {
        if (!q) return true
        const owner = state.customers.find((c) => c.id === o.customer_id)
        return (o.number ?? '').toLowerCase().includes(q) || o.notes.toLowerCase().includes(q) || (owner?.display_name ?? '').toLowerCase().includes(q) || (owner?.company ?? '').toLowerCase().includes(q)
      })
      .filter((o) => !after || o.created_at < after[0] || (o.created_at === after[0] && o.id < after[1]))
    if (margins.length === 0) {
      const page = rows.slice(0, limit).map(orderRow)
      const last = page.at(-1)
      return HttpResponse.json({ items: page, next_cursor: rows.length > limit && last ? encodeCursor(last) : null })
    }
    // `_order_page`'s margin scan (#5515): read SCAN_PAGE_SIZE rows at a time, at most
    // MAX_SCAN_PAGES times. At the cap the page can be short or empty, and its cursor is
    // the last row read, not the last match.
    const found: OrderRow[] = []
    let stopped: Order | undefined
    for (let start = 0; start < MAX_SCAN_PAGES * SCAN_PAGE_SIZE; start += SCAN_PAGE_SIZE) {
      const read = rows.slice(start, start + SCAN_PAGE_SIZE)
      for (const order of read.map(orderRow)) {
        if (order.margin_state == null || !margins.includes(order.margin_state)) continue
        const last = found.at(-1)
        if (last && found.length === limit) return HttpResponse.json({ items: found, next_cursor: encodeCursor(last) })
        found.push(order)
      }
      if (rows.length <= start + SCAN_PAGE_SIZE) return HttpResponse.json({ items: found, next_cursor: null })
      stopped = read.at(-1)
    }
    return HttpResponse.json({ items: found, next_cursor: stopped ? encodeCursor(stopped) : null })
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
      source: 'back_office',
      submitted_at: null,
      storefront_visitor_id: null,
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
        // #5351: past `quoted` the charges change on their own route, which the refusal names.
        const route = body.charges != null && TAKES_ITEMS.has(order.status) ? `; change the charges with PUT /orders/${order.id}/charges/after-acceptance` : ''
        throw new Refused('order_not_editable', `order ${order.number} is ${order.status}: only its notes and due date may change now${route}`)
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

  /** `PUT /orders/:id/charges/after-acceptance` (#5351): `Lifecycle.replace_charges_after_acceptance`. */
  http.put('/api/v1/orders/:id/charges/after-acceptance', async ({ request, params }) => {
    const body = (await request.json()) as { charges?: OrderPatch['charges'] } & Record<string, unknown>
    const extra = unknownKeys(request, body, ['charges'])
    if (extra) return extra
    if (!Array.isArray(body.charges)) return fieldProblem(request, 'charges', 'Field required')
    const charges = body.charges
    for (const [i, c] of charges.entries()) {
      const bad = chargeProblem(c)
      if (bad) return fieldProblem(request, `charges.${i}`, bad)
    }
    const parsed = charges.map((c) => ({ ...c, amount_minor: laxInt(c.amount_minor) as number }))
    try {
      const order = lock(String(params.id))
      notEnded(order)
      if (OPEN_FOR_EDITS.has(order.status)) {
        throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted: change the charges with PATCH /orders/${order.id}`)
      }
      if (!TAKES_ITEMS.has(order.status)) throw new Refused('order_not_editable', `order ${order.number} is ${order.status}: its charges are final once shipped`)
      notOnHold(order)
      const line = (c: { kind: string; label: string; amount_minor: number }) => ({ kind: c.kind, label: c.label, amount_minor: c.amount_minor })
      const before = state.charges.filter((c) => c.order_id === order.id).sort((a, b) => a.position - b.position).map(line)
      state.charges = state.charges.filter((c) => c.order_id !== order.id)
      parsed.forEach((c, position) => state.charges.push({ id: newId('h'), order_id: order.id, kind: c.kind, label: c.label, amount_minor: c.amount_minor, position }))
      event(order, 'charges.replaced_after_acceptance', { order_status: order.status, before, after: parsed.map(line) })
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
      // #4253 §8.1 — with Bambuddy linked, the order's project and one per item that prints.
      if (mockSettings().bambuddy_url && !state.projects.some((p) => p.order_id === order.id)) {
        const top = 900 + state.projects.length
        state.projects.push({ order_id: order.id, subject: `order:${order.id}`, project_id: top, item_id: null })
        for (const item of open.filter((i) => i.reference !== null)) {
          state.projects.push({ order_id: order.id, subject: `order_item:${item.id}`, project_id: 900 + state.projects.length, item_id: item.id })
        }
      }
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
      // No one pays a cancelled order: its open requests end with it.
      for (const open of state.paymentRequests.filter((r) => r.order_id === order.id && r.status === 'open')) {
        settleRequest(open, 'cancelled')
        event(order, 'payment_request.cancelled', { request_id: open.id })
      }
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
        const taken = ((cancel?.detail.items as string[] | undefined) ?? [])
          .map((id) => state.items.find((i) => i.id === id && i.order_id === order.id && i.status === 'cancelled'))
          .filter((i): i is OrderItem => i !== undefined)
          .map((i) => [i, ...reopenedStatus(i)] as const)
        const othersOpen = itemsOf(order.id).some((i) => i.status !== 'cancelled')
        // Nothing would come back priced: reopened open for edits, with every item taken.
        if (DERIVED.has(base) && !othersOpen && !taken.some(([, , priced]) => priced)) base = 'draft'
        for (const [found, status, priced] of taken) {
          if (!priced && !OPEN_FOR_EDITS.has(base) && !TAKES_ITEMS.has(base)) continue
          found.status = status
          restored.push(found.id)
        }
        if (DERIVED.has(base)) base = 'accepted'
      }
      const from = order.status
      order.status = base
      order.closed_at = null
      order.cancelled_at = null
      // `reopen_order_row`: an order back before acceptance is no longer accepted.
      if (base === 'draft' || base === 'quoted') order.accepted_at = null
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
  http.get('/api/v1/orders/:id/payment-requests', ({ request, params }) => transition(request, () => detail(lock(String(params.id))).payment_requests ?? [])),
  http.post('/api/v1/orders/:id/payment-requests', async ({ request, params }) => {
    const body = (await request.json()) as PaymentRequestCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['kind', 'amount_minor', 'provider'])
    if (extra) return extra
    if (body.amount_minor !== undefined && body.amount_minor !== null && (!Number.isInteger(body.amount_minor) || body.amount_minor < 1)) {
      return fieldProblem(request, 'amount_minor', 'Input should be greater than or equal to 1')
    }
    try {
      const order = lock(String(params.id))
      notEnded(order)
      const amount = body.amount_minor ?? balanceOf(order)
      if (amount <= 0) throw new Refused('nothing_due', `order ${order.number} has nothing due`)
      // `provider` left out is the default one; null collects by hand.
      let provider: string | null
      if (!('provider' in body)) provider = state.providers.find((p) => p.is_default)?.id ?? null
      else if (body.provider !== null && !state.providers.some((p) => p.id === body.provider)) return fieldProblem(request, 'provider', `no connected payment provider '${String(body.provider)}'`)
      else provider = body.provider ?? null
      const id = newId('pr')
      const opened: PaymentRequestView = {
        id,
        order_id: order.id,
        kind: body.kind ?? 'balance',
        provider,
        amount_minor: amount,
        currency: order.currency,
        hosted_url: provider ? `https://pay.${provider}.example/${id}` : null,
        status: 'open',
        created_at: now(),
        paid_at: null,
        provider_ref: provider ? `${provider}_${id}` : null,
        link_error: null,
        payment_id: null,
        created_by: BROWSER,
      }
      state.paymentRequests.push(opened)
      event(order, 'payment_request.opened', { request_id: id, kind: opened.kind, amount_minor: amount })
      return HttpResponse.json(opened, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.post('/api/v1/orders/:id/payment-requests/:request/cancel', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      const open = openRequestOf(order, String(params.request))
      settleRequest(open, 'cancelled')
      event(order, 'payment_request.cancelled', { request_id: open.id })
      return open
    }),
  ),
  http.post('/api/v1/orders/:id/payment-requests/:request/link', ({ request, params }) =>
    transition(request, () => {
      const found = requestOf(lock(String(params.id)), String(params.request))
      // Retried: a request with a provider, still open and not yet linked gets its page.
      if (found.provider && found.status === 'open' && !found.hosted_url) {
        found.hosted_url = `https://pay.${found.provider}.example/${found.id}`
        found.link_error = null
      }
      return found
    }),
  ),
  http.post('/api/v1/orders/:id/payments', async ({ request, params }) => {
    const body = (await request.json()) as PaymentCreate & Record<string, unknown>
    const extra = unknownKeys(request, body, ['amount_minor', 'method', 'reference', 'received_at', 'notes', 'payment_request_id'])
    if (extra) return extra
    if (typeof body.amount_minor !== 'number') return fieldProblem(request, 'amount_minor', 'Field required')
    try {
      const order = lock(String(params.id))
      if (order.status === 'cancelled') throw new Refused('order_cancelled', `order ${order.number} is cancelled`)
      let named: PaymentRequestView | null = null
      if (body.payment_request_id) {
        named = openRequestOf(order, body.payment_request_id)
        if (body.amount_minor < 1) return fieldProblem(request, 'amount_minor', 'a payment that closes a request must be above zero')
      }
      const payment: Payment = { id: newId('p'), order_id: order.id, amount_minor: body.amount_minor, method: body.method ?? null, reference: body.reference ?? null, received_at: body.received_at ?? null, notes: body.notes ?? '', created_at: now(), payment_request_id: body.payment_request_id ?? null, provider: null }
      state.payments.push(payment)
      if (named) settleRequest(named, 'paid', payment.id)
      if (payment.amount_minor > 0) closeBalance(order, payment)
      event(order, 'payment.added', { payment_id: payment.id, payment_request_id: body.payment_request_id ?? null })
      return HttpResponse.json(payment, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),

  http.post('/api/v1/orders/:id/items', addItem(false)),
  http.post('/api/v1/orders/:id/items/after-acceptance', addItem(true)),
  http.post('/api/v1/orders/:id/items/batch', addItems(false)),
  http.post('/api/v1/orders/:id/items/after-acceptance/batch', addItems(true)),
  http.patch('/api/v1/orders/:id/items/:item', async ({ request, params }) => {
    const body = (await request.json()) as OrderItemPatch & Record<string, unknown>
    const extra = unknownKeys(request, body, ['reference', 'output_id', 'quantity', 'title', 'notes', 'filaments', 'sku', 'unit_price_minor', 'requires_printing', 'accept', 'approval'])
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
      // #4954: a custom item is priced again at once; a source makes it a model item.
      const custom = found.kind === 'custom' && resolved === undefined
      for (const name of ['unit_price_minor', 'requires_printing'] as const) {
        if (body[name] != null && !custom) throw new FieldRefused(name, `${name}: only a custom item takes one`)
      }
      if (body.approval != null && found.kind === 'custom' && resolved === undefined) throw new FieldRefused('approval', 'approval: a custom item has no design')
      const accept = body.accept !== false
      let price: number | null = null
      if (custom) {
        // The price it has: its accepted quote's, else its newest's.
        const quotes = quotesOf(found.id)
        const accepted = quotes.find((q) => q.accepted)
        const current = (accepted ?? [...quotes].sort((a, b) => (a.created_at < b.created_at ? -1 : 1)).at(-1))?.unit_price_minor ?? null
        const wanted = body.unit_price_minor ?? current
        if (body.quantity === found.quantity) delete body.quantity
        const unpinned = accept && !accepted && body.unit_price_minor != null
        if (wanted !== null && (body.quantity != null || wanted !== current || unpinned)) price = wanted
      }
      const refiled = body.filaments ? withFilaments(found, body.filaments) : undefined
      // #5280: a different reference, or a slot's filament, is the item's next revision; the
      // item's own reference sent again changes nothing and makes none.
      // The colours' fill: an item with no slots given its first render's amends the current
      // revision in place (`Lifecycle.update_item`): no new revision, the quotes and status kept.
      const filled =
        resolved !== undefined && found.kind !== 'custom' && found.colours.length === 0 && resolved.colours.length > 0 && found.current_revision_id !== null && sameDesign(resolved.reference, found.reference)
      const moved = resolved !== undefined && !filled && (found.kind === 'custom' || !sameDesign(resolved.reference, found.reference) || (resolved.colours.length > 0 && JSON.stringify(resolved.colours) !== JSON.stringify(found.colours)))
      const revised = moved || refiled?.changed === true
      const repins = revised || body.quantity != null || price !== null
      if (repins && (body.quantity != null || price !== null)) openForItem(order, found, "an item's reference, quantity and filaments")
      // A design change alone: an item revised after acceptance takes it (#5350).
      else if (repins) openForRequote(order, found, "an item's reference and filaments")
      if (price !== null && accept) notOnHold(order)
      if (revised) {
        const reference = resolved?.reference ?? found.reference!
        const referenceMoved = resolved !== undefined && !sameDesign(resolved.reference, found.reference)
        pushRevision(order, found, reference, refiled?.changed ? refiled.colours : referenceMoved ? resolved!.colours : found.colours, '')
        // An output no longer matches a reference that moved.
        if (referenceMoved && !('output_id' in body)) found.output_id = null
      }
      if (filled) {
        const revision = revisionsOf(found.id).find((r) => r.id === found.current_revision_id)!
        revision.colours = resolved!.colours
        found.colours = resolved!.colours
        event(order, 'revision.amended', { item_id: found.id, revision_id: revision.id })
      }
      if (resolved) {
        found.kind = 'model'
        found.requires_printing = true
      }
      if (body.approval != null) found.approval = body.approval
      if ('sku' in body) found.sku = body.sku ?? null
      if (body.requires_printing != null) found.requires_printing = body.requires_printing
      if ('output_id' in body) found.output_id = body.output_id ?? null
      if (body.quantity != null) found.quantity = body.quantity
      if (body.title != null) found.title = body.title
      if (body.notes != null) found.notes = body.notes
      if (repins && !revised) {
        // A revision drops the quotes itself; a quantity or price change here.
        for (const q of quotesOf(found.id)) q.accepted = false
        found.status = 'draft'
      }
      if (price !== null) customPrice(order, found, price, accept)
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
      if (state.timeEntries.some((e) => e.order_item_id === found.id) || state.costEntries.some((e) => e.order_item_id === found.id)) {
        throw new Refused(
          'item_has_entries',
          `item ${found.id} has time or cost entries: remove them or cancel it (POST /orders/${order.id}/items/${found.id}/cancel)`,
          [found.id],
        )
      }
      // Its files go with it (`Lifecycle.delete_item`), each on the audit line.
      for (const attachment of state.attachments.filter((a) => a.item_id === found.id)) {
        event(order, 'attachment.removed', { attachment_id: attachment.id, item_id: found.id, filename: attachment.filename, was_delivered: found.delivered_attachment_id === attachment.id })
        delete state.files[attachment.id]
      }
      state.attachments = state.attachments.filter((a) => a.item_id !== found.id)
      state.revisions = state.revisions.filter((r) => r.item_id !== found.id)
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
      const [status, priced] = reopenedStatus(found)
      // #5349: an order that takes a new item takes back an unpriced one; a shipped one does not.
      if (!priced && !OPEN_FOR_EDITS.has(order.status) && !TAKES_ITEMS.has(order.status)) {
        throw new Refused('item_not_priced', `item ${found.id} has no accepted quote, and order ${order.number} is ${order.status}: a shipped order takes back a priced item only`, [found.id])
      }
      found.status = status
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
      const found = itemOf(order, String(params.item))
      openForRequote(order, found, "an item's price")
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      if (found.kind === 'custom') {
        throw new Refused('item_custom', `item ${found.id} is a custom item: its price is its own field (PATCH the item's unit_price_minor)`, [found.id])
      }
      // `lifecycle.py` `has_design`: a template reference, or a library file (#5544).
      if (found.reference === null && !found.library_file) throw new Refused('item_has_no_design', `item ${found.id} has no design yet`, [found.id])
      const price = body.unit_price_minor as number
      const quote: Quote = {
        id: newId('q'),
        order_item_id: found.id,
        revision_id: found.current_revision_id,
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
      const found = itemOf(order, String(params.item))
      openForRequote(order, found, "an item's price")
      notOnHold(order)
      const quote = state.quotes.find((q) => q.id === params.quote && q.order_item_id === found.id)
      if (!quote) throw new Refused('quote_not_found', `no quote ${String(params.quote)} of item ${found.id}`, [], true)
      return acceptQuote(order, found, quote)
    }),
  ),

  http.post('/api/v1/orders/:id/items/:item/fulfilments', async ({ request, params }) => {
    const body = (await request.json()) as FulfilmentInput & Record<string, unknown>
    const extra = unknownKeys(request, body, ['quantity', 'outcome', 'failure_reason', 'started_at', 'finished_at', 'notes', 'archive_id'])
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
        purpose: 'production',
        archive_ids: body.archive_id == null ? [] : [body.archive_id],
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
    const body = (await request.json()) as FulfilmentInput & Record<string, unknown>
    const extra = unknownKeys(request, body, ['quantity', 'outcome', 'failure_reason', 'started_at', 'finished_at', 'notes', 'archive_id'])
    if (extra) return extra
    return transition(request, () => {
      const order = lock(String(params.id))
      notEnded(order)
      const test = state.fulfilments.find((f) => f.id === params.f)?.purpose === 'test'
      // `Lifecycle.update_fulfilment`: a test print's attempt (#5280) is settled on an order
      // not yet accepted too, for an item in any state, and never moves the item.
      if (!test && !DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      const attempt = state.fulfilments.find((f) => f.id === params.f && f.order_item_id === found.id)
      if (!attempt) throw new Refused('fulfilment_not_found', `no attempt ${String(params.f)} of item ${found.id}`, [], true)
      if (attempt.accepted_at) throw new Refused('fulfilment_accepted', `attempt ${attempt.attempt} is accepted`)
      if (!test && !FULFILLABLE.has(found.status) && !PRINTING.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      if (attempt.print_request_id && !attempt.print_run_id && printsStarting.has(found.id)) {
        throw new Refused('print_starting', `attempt ${attempt.attempt}'s print is still starting`, [found.id])
      }
      Object.assign(attempt, { quantity: body.quantity, outcome: body.outcome, failure_reason: body.failure_reason ?? null, started_at: body.started_at ?? null, finished_at: body.finished_at ?? null, notes: body.notes ?? '' })
      const latest = productionOf(found.id).at(-1)
      if (!test && latest) found.status = after(latest, found.status)
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
      if (!FULFILLABLE.has(found.status) && found.status !== 'printed') throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      // #5582 — `_before_priced_design`: an attempt older than the accepted quote's revision printed an earlier design.
      if (staleAttempt(attempt)) {
        throw new Refused('fulfilment_stale', `attempt ${attempt.attempt} printed an earlier design than the accepted quote's`, [found.id])
      }
      attempt.accepted_at = now()
      const made = attemptsOf(found.id).filter((f) => f.accepted_at).reduce((sum, f) => sum + f.quantity, 0)
      if (made >= found.quantity) {
        found.status = 'fulfilled'
        useParts(order, found)
      } else if (found.status === 'printed') found.status = 'approved'
      event(order, 'fulfilment.accepted', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, made })
      rederive(order)
      return attempt
    }),
  ),
  // #4719 — `order_item_print` (`orders/printing.py` `print_check`, then
  // `Lifecycle.claim_print`, `attach_print`): answered at once with the attempt, the item queued.
  // The mock prints nothing, so the item stays queued until an attempt is edited by hand.
  http.post('/api/v1/orders/:id/items/:item/print', async ({ request, params }) => {
    if (!request.headers.get('Idempotency-Key')) {
      return problem(request, 428, 'This request needs an Idempotency-Key header', 'https://scadbuddy.dev/problems/idempotency-key-required')
    }
    const body = (await request.json()) as { printer?: string | null; printer_id?: number | null }
    try {
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      // `quote.py` `reference_of`, skipped for a library file item (#5544), which names its file.
      if (found.reference === null && !found.library_file) {
        if (found.kind === 'request') throw new Refused('item_has_no_design', `item ${found.id} is a request with no design yet: add a revision first`, [found.id])
        throw new Refused('item_custom', `item ${found.id} is a custom item`, [found.id])
      }
      if (!FULFILLABLE.has(found.status)) {
        throw new Refused(SETTLED.has(found.status) ? 'item_not_open' : 'item_not_approved', `item ${found.id} is ${found.status}`, [found.id])
      }
      const attempts = attemptsOf(found.id)
      const open = printInFlight(found.id)
      if (open) throw new Refused('print_in_flight', `item ${found.id}'s attempt ${open.attempt} (${open.purpose}) is still printing: settle it first`, [found.id])
      const copies = found.quantity - productionOf(found.id).filter((f) => f.outcome === 'succeeded' || f.outcome === 'partial').reduce((sum, f) => sum + f.quantity, 0)
      if (copies < 1) throw new Refused('item_made', `item ${found.id}'s attempts made its ${found.quantity} already: accept them`, [found.id])
      const quote = quotesOf(found.id).find((q) => q.accepted)
      if (!quote || (!found.library_file && found.output_id === null)) {
        throw new Refused('requote_needed', `item ${found.id} needs a new quote before it prints`, [found.id])
      }
      // `printing.py` `_library_stands`: the file must still be in the library that serves.
      if (found.library_file && !mockLibraryFile(found.library_file.id)) {
        throw new Refused('library_file_gone', `item ${found.id}'s library file ${found.library_file.filename} (id ${found.library_file.id}) is no longer in the library`, [found.id])
      }
      // §8.4: the registry printer the run used (`run.printer`), as it was named then. The run
      // has one only when the body names a row that is still there (`print_run.py`
      // `_named_printer`): `printer_id` alone, or a row gone since, prints on no registry row.
      const printer = body.printer == null ? undefined : printersState().find((row) => row.id === body.printer)
      const attempt: Fulfilment = {
        id: newId('f'),
        order_item_id: found.id,
        attempt: attempts.length + 1,
        quantity: 0,
        outcome: 'unknown',
        failure_reason: null,
        started_at: now(),
        finished_at: null,
        notes: '',
        accepted_at: null,
        created_by: BROWSER,
        created_at: now(),
        purpose: 'production',
        print_request_id: printRequestId(found.id, request.headers.get('Idempotency-Key')!),
        print_run_id: newId('run'),
        copies,
        sliced_sha256: quote.sliced_sha256,
        sliced_blob_key: quote.sliced_blob_key,
        printer: printer?.id ?? null,
        printer_serial: printer?.serial ?? null,
        printer_name: printer?.name ?? null,
        bambuddy_printer_id: printer?.bambuddy_printer_id ?? null,
      }
      state.fulfilments.push(attempt)
      found.status = 'queued'
      found.updated_at = now()
      event(order, 'print.started', { item_id: found.id, fulfilment_id: attempt.id, attempt: attempt.attempt, print_run_id: attempt.print_run_id, copies })
      rederive(order)
      return HttpResponse.json(attempt)
    } catch (error) {
      return refused(request, error)
    }
  }),
  // `Lifecycle.unfulfil_item` (#4952): the latest accepted attempt is no longer counted.
  http.post('/api/v1/orders/:id/items/:item/unfulfil', ({ request, params }) =>
    transition(request, () => {
      // #4953 — the stored setting, checked before anything is read.
      if (mockSettings().orders_allow_unfulfil === false) {
        throw new Refused('unfulfil_disabled', 'unfulfilling a line item is turned off in Settings (orders_allow_unfulfil)')
      }
      const order = lock(String(params.id))
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      notOnHold(order)
      const found = itemOf(order, String(params.item))
      if (found.status !== 'fulfilled') throw new Refused('item_not_fulfilled', `item ${found.id} is ${found.status}: only a fulfilled item unfulfils`, [found.id])
      const carrying = shipmentsOf(order.id)
        .filter((s) => s.items.some((line) => line.order_item_id === found.id))
        .map((s) => s.id)
      if (carrying.length > 0) {
        throw new Refused('order_item_shipped', `item ${found.id} is on a shipment: take it off the shipment first`, [found.id], false, carrying)
      }
      const attempts = attemptsOf(found.id)
      const accepted = attempts
        .filter((f) => f.accepted_at)
        .sort((a, b) => (a.accepted_at! < b.accepted_at! ? -1 : a.accepted_at! > b.accepted_at! ? 1 : a.attempt - b.attempt))
        .at(-1)!
      accepted.accepted_at = null
      found.status = productionOf(found.id).at(-1)!.outcome === 'failed' ? 'failed' : 'approved'
      found.updated_at = now()
      returnParts(order, found)
      event(order, 'item.unfulfilled', { item_id: found.id, fulfilment_id: accepted.id, attempt: accepted.attempt, to: found.status })
      rederive(order)
      return found
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

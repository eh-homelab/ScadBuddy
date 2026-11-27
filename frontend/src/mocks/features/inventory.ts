import { HttpResponse, http } from 'msw'
import { addDecimals, cmpDecimals, divDecimal, mulDecimals, roundDecimal, subDecimals } from '../../lib/decimal'
import type { components } from '../../api/schema'
import { mockSettings } from '../handlers'
import { itemKeysInUse, marksOf, rowPropertiesRefusal, saveRowProperties, schemaProblem } from './entities'
import { locationExists } from './filament'
import { supplierById } from './filamentPurchasing'
import { SUPPLIER } from './filamentPurchasingSeed'
import { INVENTORY } from './inventorySeed'
import { BROWSER, fieldProblem, now, problem, PROBLEMS as ORDER_PROBLEMS, state as orders } from './orders/state'

/**
 * #7067 — the inventory (`backend/scadbuddy/api/inventory.py`, `inventory_stock.py`): item
 * kinds, items, supplier offers and their pack prices, lots, units and stock moves. Costs are
 * in minor units of the shop currency (`orders_currency`), kept to the millionth as decimal
 * strings (`lib/decimal`), never floats, and answered normalized as the backend's `_Decimals` does. A use takes FIFO (the named lot first); a shortfall goes on the newest
 * lot with `needs_review`, or on a new zero-cost lot of unknown cost. A move's cost is a
 * snapshot until "Re-cost past uses". The order-parts mock (`orders/parts.ts`) takes and gives
 * back stock through `takeStock` and `giveBack`.
 */

type S = components['schemas']
type Tracking = S['ItemKind']['tracking']

const PROBLEMS = 'https://scadbuddy.dev/problems/inventory/'
const KIND_ID = /^[a-z][a-z0-9_]{0,39}$/

type MockKind = Omit<S['ItemKind'], 'item_count'>
type MockItem = {
  id: string
  kind_id: string
  name: string
  manufacturer: string
  compatible_with: string[]
  low_stock: string
  life_hours: string | null
  life_months: number | null
  saleable: boolean | null
  source: string
  source_ref: string | null
  edited_fields: string[]
  note: string
  archived_at: string | null
}
type MockOffer = Omit<S['Offer'], 'supplier_name' | 'item_name' | 'latest_price'>
type MockPrice = S['OfferPricePoint'] & { offer_id: string; written: number }
type MockLot = {
  id: string
  item_id: string
  location_id: string | null
  received_qty: string
  unit_cost: string
  cost_unknown: boolean
  cost_currency: string | null
  foreign_unit_cost: string | null
  origin: S['Lot']['origin']
  note: string
  received_at: string
}
type MockUnit = Omit<S['Unit'], 'cost_minor' | 'life_hours'> & { cost: string; life_hours: string | null }
type MockMove = {
  id: string
  at: string
  item_id: string
  lot_id: string | null
  unit_id: string | null
  kind: S['Move']['kind']
  qty: string
  unit_cost: string
  cost_unknown: boolean
  needs_review: boolean
  reverses_move_id: string | null
  reason: string
  order_id: string | null
  order_item_id: string | null
  entity_id: string | null
}

export { INVENTORY }

const SEEDED_AT = '2026-11-20T09:00:00Z'

function kind(id: string, name: string, parent_id: string | null, tracking: Tracking, uom: string, life_basis: MockKind['life_basis'], saleable = false): MockKind {
  return { id, name, parent_id, tracking, uom, attribute_schema: { type: 'object', properties: {} }, life_basis, saleable, builtin: true, archived_at: null }
}

/** The built-in kinds as `20261126T0218Z_inventory_class.sql` (inventory K1) leaves them:
 * the three roots, and `tracking` and `life_basis` derived from the class (R-K1-6). */
function builtinKinds(): MockKind[] {
  const kinds = [
    kind('consumable', 'Consumable', null, 'lot', 'each', 'none'),
    kind('filament', 'Filament', 'consumable', 'filament', 'g', 'none'),
    kind('general_consumable', 'General consumable', 'consumable', 'lot', 'each', 'none'),
    kind('desiccant', 'Desiccant', 'consumable', 'lot', 'g', 'none'),
    kind('packaging', 'Packaging', 'consumable', 'lot', 'each', 'none'),
    kind('lubricant', 'Lubricant', 'consumable', 'lot', 'ml', 'none'),
    kind('hardware', 'Hardware', 'consumable', 'lot', 'each', 'none'),
    kind('ams_part', 'AMS part', 'consumable', 'lot', 'each', 'none'),
    kind('printed_part', 'Printed part', 'consumable', 'lot', 'each', 'none', true),
    kind('assembly', 'Assembly', 'consumable', 'lot', 'each', 'none', true),
    kind('durable', 'Durable', null, 'unit', 'each', 'none'),
    kind('printer', 'Printer', 'durable', 'unit', 'each', 'hours'),
    kind('part', 'Part', 'durable', 'unit', 'each', 'none'),
    kind('nozzle', 'Nozzle', 'part', 'unit', 'each', 'hours'),
    kind('hotend', 'Hotend', 'part', 'unit', 'each', 'hours'),
    kind('belt', 'Belt', 'part', 'unit', 'each', 'hours'),
    kind('build_plate', 'Build plate', 'part', 'unit', 'each', 'none'),
    kind('spool_part', 'Spool insert', 'durable', 'unit', 'each', 'none'),
    kind('equipment', 'Equipment', 'durable', 'unit', 'each', 'months'),
    kind('dry_bin', 'Dry bin', 'equipment', 'unit', 'each', 'months'),
    kind('tool', 'Tool', 'equipment', 'unit', 'each', 'months'),
    kind('service', 'Service', null, 'none', 'each', 'none'),
    kind('warranty', 'Warranty', 'service', 'none', 'each', 'none'),
    kind('service_plan', 'Service plan', 'service', 'none', 'each', 'none'),
    kind('labour', 'Labour', 'service', 'none', 'each', 'none'),
    kind('flat_service', 'Flat-rate service', 'service', 'none', 'each', 'none'),
  ]
  kinds.find((k) => k.id === 'spool_part')!.attribute_schema = {
    type: 'object',
    properties: {
      empty_weight_g: { type: 'integer', exclusiveMinimum: 0, description: 'Weight of the empty part in grams' },
      role: { type: 'string', enum: ['shell', 'addon'], description: 'A reusable shell, or an addon such as an AMS adaptor' },
    },
  }
  return kinds
}

function fresh() {
  return {
    kinds: builtinKinds(),
    items: [] as MockItem[],
    offers: [] as MockOffer[],
    prices: [] as MockPrice[],
    lots: [] as MockLot[],
    units: [] as MockUnit[],
    moves: [] as MockMove[],
    /** Each request body the routes took, by route name, for the tests. */
    bodies: [] as { route: string; body: unknown }[],
    /** Each write's `Idempotency-Key`, by route name, for the tests. */
    keys: [] as { route: string; key: string | null }[],
    /** #6949 K1 — the writes answered 202 (`slowNextWrite`), running until `settleSlowWrites`. */
    slow: [] as SlowWrite[],
    counter: 0,
  }
}

/** A write the mock did at once but answered 202, as Go does past the API's wait. */
interface SlowWrite {
  id: string
  key: string | null
  created_at: string
  result: Record<string, unknown> | null
  settled: boolean
}

export const inventoryMock = fresh()

function id(): string {
  inventoryMock.counter += 1
  return `ab7f0000-0000-4000-8000-${inventoryMock.counter.toString(16).padStart(12, '0')}`
}

const shop = () => mockSettings().orders_currency
/** A quantity or cost kept to the millionth, normalized (`"5.99"`, never `"5.990000"`). */
const dec = (value: string | number) => roundDecimal(String(value), 6)
const sum = (values: string[]) => dec(values.reduce(addDecimals, '0'))
const neg = (value: string) => subDecimals('0', value)
const above = (value: string, than = '0') => cmpDecimals(value, than) > 0
const below = (value: string, than = '0') => cmpDecimals(value, than) < 0
/** Lets the next recost answer 409 `recost_busy` once, as the backend does when new order moves keep reaching the lot. */
let recostBusy = false
export function failNextRecost(): void {
  recostBusy = true
}
/** #6949 K1 — the next `n` writes answer 202; their operations run until `settleSlowWrites`. */
let slowWrites = 0
export function slowNextWrite(n = 1): void {
  slowWrites = n
}
/** Ends every slow write's operation with its result, or (`gone`) with none, as a pruned answer reads. */
export function settleSlowWrites({ gone = false }: { gone?: boolean } = {}): void {
  for (const op of inventoryMock.slow) Object.assign(op, { settled: true }, gone ? { result: null } : {})
}

// -- seeds ----------------------------------------------------------------------------------

export function seedItem(fields: Partial<MockItem> & { name: string; kind_id: string }): S['Item'] {
  ready()
  const item: MockItem = {
    id: id(),
    manufacturer: '',
    compatible_with: [],
    low_stock: '0',
    life_hours: null,
    life_months: null,
    saleable: null,
    source: 'manual',
    source_ref: null,
    edited_fields: [],
    note: '',
    archived_at: null,
    ...fields,
  }
  inventoryMock.items.push(item)
  return answerItem(item)
}

/** A received lot: `unit_cost_minor` null opens it with its cost unknown. */
export function seedLot(itemId: string, qty: number | string, unitCostMinor: number | string | null, fields: Partial<MockLot> = {}): S['Lot'] {
  ready()
  const lot = openLot(itemId, dec(qty), dec(unitCostMinor ?? 0), unitCostMinor === null, 'manual', fields)
  return answerLot(lot)
}

export function seedOffer(fields: Partial<MockOffer> & { item_id: string; supplier_id: string; sku: string }, price?: { price: string; currency?: string; on_date?: string }): S['Offer'] {
  ready()
  const offer: MockOffer = { id: id(), url: null, pack_size: '1', pack_uom: 'each', note: null, archived_at: null, ...fields }
  offer.pack_size = dec(offer.pack_size)
  inventoryMock.offers.push(offer)
  if (price) addPrice(offer.id, price.price, price.currency ?? shop(), price.on_date ?? SEEDED_AT.slice(0, 10))
  return answerOffer(offer)
}

export function seedUnit(itemId: string, fields: Partial<MockUnit> = {}): S['Unit'] {
  ready()
  const unit = newUnit(itemId, dec(fields.cost ?? '0'), fields.cost_unknown ?? false, fields)
  return answerUnit(unit)
}

function seed() {
  seedItem({ id: INVENTORY.HOOK, kind_id: 'hardware', name: 'Silver keychain hook', manufacturer: 'Generic', low_stock: '50' })
  openLot(INVENTORY.HOOK, '200', '5.99', false, 'manual', { id: INVENTORY.HOOK_LOT, received_at: SEEDED_AT, note: 'Opening count' })
  seedOffer({ id: INVENTORY.HOOK_OFFER, item_id: INVENTORY.HOOK, supplier_id: SUPPLIER.AMAZON, sku: 'B0HOOK100', url: 'https://www.amazon.com/dp/B0HOOK100', pack_size: '100' }, { price: '5.99' })
  seedItem({ id: INVENTORY.NOZZLE, kind_id: 'nozzle', name: 'Hardened steel nozzle 0.4', manufacturer: 'Bambu Lab', compatible_with: ['H2C'] })
  newUnit(INVENTORY.NOZZLE, '1999', false, { id: INVENTORY.NOZZLE_UNIT, serial: 'HS-0001', received_at: SEEDED_AT })
  newUnit(INVENTORY.NOZZLE, '1999', false, { serial: 'HS-0002', received_at: SEEDED_AT })
  seedItem({ id: INVENTORY.ENGRAVING, kind_id: 'flat_service', name: 'Laser engraving' })
}

let seeded = false

/**
 * The seed is laid on first use, not when the module loads: it reads the settings and the
 * suppliers of other mock modules, which an import cycle may not have loaded yet.
 */
function ready(): void {
  if (seeded) return
  seeded = true
  seed()
}

export function reset(): void {
  Object.assign(inventoryMock, fresh())
  seeded = false
  recostBusy = false
  slowWrites = 0
  ready()
}

// -- reading --------------------------------------------------------------------------------

const kindOf = (kindId: string) => inventoryMock.kinds.find((k) => k.id === kindId)

/** The kind and its ancestors, root first. */
function kindPath(kindId: string): MockKind[] {
  const path: MockKind[] = []
  for (let at = kindOf(kindId); at && !path.includes(at); at = at.parent_id ? kindOf(at.parent_id) : undefined) path.unshift(at)
  return path
}

/** The kind and every kind below it. */
function subtree(kindId: string): Set<string> {
  const found = new Set([kindId])
  for (let grew = true; grew; ) {
    grew = false
    for (const k of inventoryMock.kinds) {
      if (k.parent_id && found.has(k.parent_id) && !found.has(k.id)) {
        found.add(k.id)
        grew = true
      }
    }
  }
  return found
}

/** `kinds.item_schema`: each ancestor's `properties`, the kind's own last, and each `required` key, ancestor first and once (R-K1-33). */
export function kindSchema(kindId: string): Record<string, unknown> {
  ready()
  const properties: Record<string, unknown> = {}
  const required: string[] = []
  for (const k of kindPath(kindId)) {
    Object.assign(properties, (k.attribute_schema.properties ?? {}) as Record<string, unknown>)
    for (const key of (k.attribute_schema.required ?? []) as string[]) if (!required.includes(key)) required.push(key)
  }
  return required.length ? { type: 'object', properties, required } : { type: 'object', properties }
}

/** Go's special types' `requires_properties` (`handlers/catalog.go`), by special type. */
const SPECIAL_REQUIRES: Record<string, string[]> = { filament_spool: ['rgba', 'diameter_mm'], printer: ['model_code'] }

/** The keys an item of the kind must have (Go's `item_required`): the schema's, then its special type's. */
export function itemRequired(kindId: string): string[] {
  const required = [...((kindSchema(kindId).required ?? []) as string[])]
  const special = [...kindPath(kindId)].reverse().find((k) => k.special_type)?.special_type
  for (const key of SPECIAL_REQUIRES[special ?? ''] ?? []) if (!required.includes(key)) required.push(key)
  return required
}

/** An item's property schema (`schema_for`), or undefined for no such item. Its `required` is the item's, special type's included, so the marks and the save check match Go. */
export function itemSchemaOf(itemId: string): Record<string, unknown> | undefined {
  ready()
  const item = inventoryMock.items.find((i) => i.id === itemId)
  if (!item) return undefined
  const required = itemRequired(item.kind_id)
  return required.length ? { ...kindSchema(item.kind_id), required } : kindSchema(item.kind_id)
}

/** The ids of the items of the kinds, archived ones too. */
const itemsOfKinds = (kinds: Set<string>) => inventoryMock.items.filter((i) => kinds.has(i.kind_id)).map((i) => i.id)

/** The items of the item's kind and of its descendants, for `keys_in_use`. */
export function itemsTypedLike(itemId: string): string[] {
  ready()
  const item = inventoryMock.items.find((i) => i.id === itemId)
  if (!item) return []
  return itemsOfKinds(subtree(item.kind_id))
}

const trackingOf = (item: MockItem): Tracking => kindOf(item.kind_id)?.tracking ?? 'none'
const lotOnHand = (lotId: string) => sum(inventoryMock.moves.filter((m) => m.lot_id === lotId).map((m) => m.qty))

function answerItem(item: MockItem): S['Item'] {
  const k = kindOf(item.kind_id)!
  const tracking = k.tracking
  let onHand = '0'
  let value = '0'
  let unknown = false
  if (tracking === 'lot') {
    for (const lot of inventoryMock.lots.filter((l) => l.item_id === item.id)) {
      const held = lotOnHand(lot.id)
      onHand = addDecimals(onHand, held)
      if (above(held)) {
        value = addDecimals(value, mulDecimals(held, lot.unit_cost))
        unknown ||= lot.cost_unknown
      }
    }
  } else if (tracking === 'unit') {
    for (const unit of inventoryMock.units.filter((u) => u.item_id === item.id && u.status === 'in_stock')) {
      onHand = addDecimals(onHand, '1')
      value = addDecimals(value, unit.cost)
      unknown ||= unit.cost_unknown
    }
  }
  return {
    id: item.id,
    kind_id: item.kind_id,
    kind_path: kindPath(item.kind_id).map((p) => p.id),
    tracking,
    uom: k.uom,
    name: item.name,
    manufacturer: item.manufacturer,
    compatible_with: item.compatible_with,
    low_stock: item.low_stock,
    life_hours: item.life_hours,
    life_months: item.life_months,
    saleable: item.saleable,
    effective_saleable: item.saleable ?? k.saleable,
    source: item.source,
    source_ref: item.source_ref,
    edited_fields: item.edited_fields,
    note: item.note,
    archived_at: item.archived_at,
    on_hand: dec(onHand),
    stock_value_minor: dec(value),
    cost_unknown: unknown,
    low: tracking !== 'none' && above(item.low_stock) && !above(onHand, item.low_stock),
    offer_count: inventoryMock.offers.filter((o) => o.item_id === item.id && !o.archived_at).length,
    ...marksOf(`item:${item.id}`, item.archived_at !== null),
  }
}

/** One item as the routes answer it, for the order-parts mock. */
export function inventoryItem(itemId: string): S['Item'] | undefined {
  ready()
  const item = inventoryMock.items.find((i) => i.id === itemId)
  return item ? answerItem(item) : undefined
}

function answerKind(k: MockKind): S['ItemKind'] {
  return { ...k, item_count: inventoryMock.items.filter((i) => i.kind_id === k.id && !i.archived_at).length }
}

function latestPrice(offerId: string): S['OfferPricePoint'] | null {
  const newest = inventoryMock.prices.filter((p) => p.offer_id === offerId).sort((a, b) => b.on_date.localeCompare(a.on_date) || b.written - a.written)[0]
  if (!newest) return null
  const { offer_id: _offer, written: _written, ...point } = newest
  return point
}

function answerOffer(offer: MockOffer): S['Offer'] {
  return {
    ...offer,
    supplier_name: supplierById(offer.supplier_id)?.name ?? '',
    item_name: inventoryMock.items.find((i) => i.id === offer.item_id)?.name ?? '',
    latest_price: latestPrice(offer.id),
  }
}

function addPrice(offerId: string, price: string, currency: string, onDate: string): S['OfferPricePoint'] {
  const point: MockPrice = { id: id(), offer_id: offerId, price: dec(price), currency, on_date: onDate, source: 'manual', invoice_line_id: null, written: inventoryMock.prices.length }
  inventoryMock.prices.push(point)
  return latestPrice(offerId)!.id === point.id ? latestPrice(offerId)! : { ...point }
}

function answerLot(lot: MockLot): S['Lot'] {
  return {
    id: lot.id,
    item_id: lot.item_id,
    location_id: lot.location_id,
    received_qty: lot.received_qty,
    on_hand: lotOnHand(lot.id),
    unit_cost_minor: lot.unit_cost,
    cost_unknown: lot.cost_unknown,
    cost_currency: lot.cost_currency,
    foreign_unit_cost_minor: lot.foreign_unit_cost,
    origin: lot.origin,
    invoice_line_id: null,
    production_run_id: null,
    note: lot.note,
    received_at: lot.received_at,
    created_by: BROWSER,
  }
}

function answerUnit(unit: MockUnit): S['Unit'] {
  const { cost, life_hours, ...rest } = unit
  return { ...rest, cost_minor: cost, life_hours }
}

function answerMove(m: MockMove): S['Move'] {
  return {
    id: m.id,
    at: m.at,
    item_id: m.item_id,
    lot_id: m.lot_id,
    unit_id: m.unit_id,
    kind: m.kind,
    qty: m.qty,
    unit_cost_minor: m.unit_cost,
    amount_minor: dec(mulDecimals(m.qty, m.unit_cost)),
    cost_unknown: m.cost_unknown,
    needs_review: m.needs_review,
    reverses_move_id: m.reverses_move_id,
    reason: m.reason,
    order_id: m.order_id,
    order_item_id: m.order_item_id,
    production_run_id: null,
    print_run_id: null,
    entity_id: m.entity_id,
    actor: BROWSER,
  }
}

// -- writing --------------------------------------------------------------------------------

class Refused extends Error {
  readonly status: number
  readonly code: string
  readonly base: string
  constructor(status: number, code: string, detail: string, base = PROBLEMS) {
    super(detail)
    this.status = status
    this.code = code
    this.base = base
  }
}

/** A refusal another mock already wrote as a response (`rowPropertiesRefusal`). */
class Answered extends Error {
  readonly response: Response
  constructor(response: Response) {
    super('answered')
    this.response = response
  }
}

/** FastAPI's own 422 for a body field (no inventory code), as `fieldProblem` answers it. */
class FieldRefused extends Error {
  readonly field: string
  constructor(field: string, msg: string) {
    super(msg)
    this.field = field
  }
}

function refused(request: Request, error: unknown) {
  if (error instanceof FieldRefused) return fieldProblem(request, error.field, error.message)
  if (error instanceof Answered) return error.response
  if (!(error instanceof Refused)) throw error
  return problem(request, error.status, error.message, error.base + error.code, { code: error.code })
}

const accepted = (op: SlowWrite, repeated: boolean) =>
  HttpResponse.json({ id: op.id, kind: 'inventory.write', subject: '', status: 'running', created_at: op.created_at, repeated }, { status: 202 })

/**
 * The answer of `run()`, or its refusal as the route's problem. A write while `slowNextWrite`
 * is set runs at once and answers 202; the same `Idempotency-Key` again attaches to it.
 */
async function answer(request: Request, run: () => object | Promise<object>, status = 200) {
  const key = request.headers.get('Idempotency-Key')
  const running = key ? inventoryMock.slow.find((op) => op.key === key && !op.settled) : undefined
  if (running) return accepted(running, true)
  try {
    const result = (await run()) as Record<string, unknown>
    if (request.method !== 'GET' && slowWrites > 0) {
      slowWrites -= 1
      const op: SlowWrite = { id: `op-${id()}`, key, created_at: now(), result, settled: false }
      inventoryMock.slow.push(op)
      return accepted(op, false)
    }
    return HttpResponse.json(result, { status })
  } catch (error) {
    return refused(request, error)
  }
}

function findItem(itemId: string): MockItem {
  const item = inventoryMock.items.find((i) => i.id === itemId)
  if (!item) throw new Refused(404, 'item_not_found', `no item '${itemId}'`)
  return item
}

/**
 * The item, refused unless its kind counts it in `tracking`. On an item that is not counted,
 * a receive or a count is 409 `step_not_applicable`; a use (`take`) is 409 `handler_not_built`,
 * because a service's consume step is `consume.service`, which K1 does not build (#6949 K1).
 */
function counted(itemId: string, tracking: 'lot' | 'unit', take = false): MockItem {
  const item = findItem(itemId)
  const has = trackingOf(item)
  if (has === 'none' && take) throw new Refused(409, 'handler_not_built', "the item's consume step has a handler K1 does not build")
  if (has === 'none') throw new Refused(409, 'step_not_applicable', "the item's kind is not counted (tracking `none`): no lots, units or moves")
  if (has === 'filament') throw new Refused(409, 'filament_item_refused', 'filament items and their offers wait for the filament phase (#7068)')
  if (has !== tracking) throw new Refused(409, 'wrong_counting', 'a lot call on an item counted in units, or a unit call on one counted in lots')
  return item
}

function findLot(lotId: string): MockLot {
  const lot = inventoryMock.lots.find((l) => l.id === lotId)
  if (!lot) throw new Refused(404, 'lot_not_found', `no lot '${lotId}'`)
  return lot
}

function findUnit(unitId: string): MockUnit {
  const unit = inventoryMock.units.find((u) => u.id === unitId)
  if (!unit) throw new Refused(404, 'unit_not_found', `no unit '${unitId}'`)
  return unit
}

/** An `each` item takes whole quantities only. */
function quantity(item: MockItem, value: unknown, field: string, allowZero = false): string {
  const text = String(value ?? '').trim()
  if (!/^-?\d+(\.\d+)?$/.test(text)) throw new Refused(422, 'bad_quantity', `${field}: a quantity above 0`)
  const n = dec(text)
  if (allowZero ? below(n) : !above(n)) throw new Refused(422, 'bad_quantity', `${field}: a quantity above 0`)
  if (kindOf(item.kind_id)?.uom === 'each' && n.includes('.')) throw new Refused(422, 'bad_quantity', `${field}: an \`each\` item takes whole quantities only`)
  return n
}

/** A location id the request names must be one (404 `location_not_found`). */
function place(locationId: string | null | undefined): string | null {
  if (locationId && !locationExists(locationId)) throw new Refused(404, 'location_not_found', 'no location')
  return locationId ?? null
}

/** No other unit of the item has the serial (409 `serial_exists`). */
function freeSerial(itemId: string, serial: string | null, own: string | null = null): void {
  if (serial && inventoryMock.units.some((u) => u.item_id === itemId && u.id !== own && u.serial === serial)) {
    throw new Refused(409, 'serial_exists', 'another unit of the item has that serial')
  }
}

function move(fields: Partial<MockMove> & Pick<MockMove, 'item_id' | 'kind' | 'qty' | 'unit_cost' | 'cost_unknown'>): MockMove {
  const m: MockMove = { id: id(), at: now(), lot_id: null, unit_id: null, needs_review: false, reverses_move_id: null, reason: '', order_id: null, order_item_id: null, entity_id: null, ...fields }
  inventoryMock.moves.push(m)
  return m
}

function openLot(itemId: string, qty: string, unitCost: string, unknown: boolean, origin: MockLot['origin'], fields: Partial<MockLot> = {}): MockLot {
  const lot: MockLot = { id: id(), item_id: itemId, location_id: null, received_qty: qty, unit_cost: unitCost, cost_unknown: unknown, cost_currency: null, foreign_unit_cost: null, origin, note: '', received_at: now(), ...fields }
  inventoryMock.lots.push(lot)
  if (cmpDecimals(qty, '0') !== 0) move({ item_id: itemId, lot_id: lot.id, kind: 'receive', qty, unit_cost: lot.unit_cost, cost_unknown: lot.cost_unknown, reason: lot.note })
  return lot
}

function newUnit(itemId: string, cost: string, unknown: boolean, fields: Partial<MockUnit> = {}): MockUnit {
  const unit: MockUnit = {
    id: id(),
    item_id: itemId,
    serial: null,
    name: '',
    location_id: null,
    status: 'in_stock',
    installed_in_unit_id: null,
    cost,
    cost_unknown: unknown,
    origin: 'manual',
    invoice_line_id: null,
    production_run_id: null,
    in_service_at: null,
    retired_at: null,
    life_hours: null,
    life_months: null,
    printer_id: null,
    location_ref_id: null,
    received_at: now(),
    created_by: BROWSER,
    ...fields,
  }
  inventoryMock.units.push(unit)
  move({ item_id: itemId, unit_id: unit.id, kind: 'receive', qty: '1', unit_cost: cost, cost_unknown: unknown })
  return unit
}

/** FIFO order: received time, then id. */
const fifo = (itemId: string) => inventoryMock.lots.filter((l) => l.item_id === itemId).sort((a, b) => a.received_at.localeCompare(b.received_at) || a.id.localeCompare(b.id))

export interface TakeContext {
  kind?: 'use' | 'adjust'
  lot_id?: string | null
  reason?: string
  order_id?: string | null
  order_item_id?: string | null
}

/**
 * `StockStore.take`: the named lot first (409 `lot_empty` when it holds nothing), then FIFO;
 * what no lot holds goes on the newest lot below 0 with `needs_review`, or on a new zero-cost
 * `adjustment` lot of unknown cost when there is none.
 */
export function takeStock(itemId: string, qtyIn: number | string, context: TakeContext = {}): S['Move'][] {
  ready()
  const qty = dec(qtyIn)
  const kindName = context.kind ?? 'use'
  const lots = fifo(itemId)
  const order: MockLot[] = []
  if (context.lot_id) {
    const named = lots.find((l) => l.id === context.lot_id)
    if (!named) throw new Refused(404, 'lot_not_found', `no lot '${context.lot_id}'`)
    if (!above(lotOnHand(named.id))) throw new Refused(409, 'lot_empty', 'the named lot has no stock on hand')
    order.push(named)
  }
  order.push(...lots.filter((l) => l.id !== context.lot_id))
  const fields = { reason: context.reason ?? '', order_id: context.order_id ?? null, order_item_id: context.order_item_id ?? null }
  const made: MockMove[] = []
  let left = qty
  for (const lot of order) {
    if (!above(left)) break
    const held = lotOnHand(lot.id)
    if (!above(held)) continue
    const take = below(held, left) ? held : left
    made.push(move({ item_id: itemId, lot_id: lot.id, kind: kindName, qty: neg(take), unit_cost: lot.unit_cost, cost_unknown: lot.cost_unknown, ...fields }))
    left = dec(subDecimals(left, take))
  }
  if (above(left)) {
    const newest = lots[lots.length - 1] ?? openLot(itemId, '0', '0', true, 'adjustment')
    made.push(move({ item_id: itemId, lot_id: newest.id, kind: kindName, qty: neg(left), unit_cost: newest.unit_cost, cost_unknown: newest.cost_unknown, needs_review: true, ...fields }))
  }
  return made.map(answerMove)
}

/** `StockStore.give_back`: each use of the order item that no return reverses, back to its lot at its own cost. */
export function giveBack(itemId: string, orderItemId: string): void {
  ready()
  for (const use of openUses(orderItemId).filter((m) => m.item_id === itemId)) {
    move({ item_id: itemId, lot_id: use.lot_id, kind: 'return', qty: dec(neg(use.qty)), unit_cost: use.unit_cost, cost_unknown: use.cost_unknown, reverses_move_id: use.id, order_id: use.order_id, order_item_id: orderItemId })
  }
}

function openUses(orderItemId: string, moves: MockMove[] = inventoryMock.moves): MockMove[] {
  const reversed = new Set(moves.filter((m) => m.kind === 'return' && m.reverses_move_id).map((m) => m.reverses_move_id))
  return moves.filter((m) => m.kind === 'use' && m.order_item_id === orderItemId && !reversed.has(m.id))
}

/** `stock.parts_moved`: the known-cost use and return moves' cost, rounded once; unknown while an open use is. */
export function partsMovedOf(orderItemId: string, moves: MockMove[] = inventoryMock.moves): { minor: string; unknown: boolean; open: boolean } {
  ready()
  const own = moves.filter((m) => m.order_item_id === orderItemId && (m.kind === 'use' || m.kind === 'return'))
  const minor = roundDecimal(neg(sum(own.filter((m) => !m.cost_unknown).map((m) => dec(mulDecimals(m.qty, m.unit_cost))))), 0)
  const open = openUses(orderItemId, moves)
  return { minor, unknown: open.some((m) => m.cost_unknown), open: open.length > 0 }
}

/** `estimate_unit_cost`: one unit at what the lots on hand cost now (known ones only), and whether some is unknown. */
export function estimateUnitCost(itemId: string): { minor: string; unknown: boolean } {
  ready()
  const held = fifo(itemId).map((lot) => ({ lot, qty: lotOnHand(lot.id) })).filter((x) => above(x.qty))
  if (held.length === 0) return { minor: '0', unknown: true }
  const qty = sum(held.map((x) => x.qty))
  const known = held.filter((x) => !x.lot.cost_unknown)
  return { minor: dec(divDecimal(sum(known.map((x) => mulDecimals(x.qty, x.lot.unit_cost))), qty, 6)), unknown: known.length < held.length }
}

/** Whether the item is one an order's parts may name: an item of a kind tracked in lots. */
export function isPartItem(itemId: string): boolean {
  ready()
  const item = inventoryMock.items.find((i) => i.id === itemId)
  return !!item && trackingOf(item) === 'lot'
}

function receiveLot(itemId: string, body: S['ReceiveLot']): MockLot {
  const item = counted(itemId, 'lot')
  const qty = quantity(item, body.qty, 'qty')
  if (body.unit_cost_minor != null && body.total_cost_minor != null) throw new Refused(422, 'bad_quantity', 'give unit_cost_minor or total_cost_minor, not both')
  const each = body.unit_cost_minor != null ? dec(body.unit_cost_minor) : body.total_cost_minor != null ? dec(divDecimal(String(body.total_cost_minor), qty, 6)) : null
  const foreign = body.currency != null && body.currency !== shop()
  const fields: Partial<MockLot> = { note: body.note ?? '', location_id: place(body.location_id), ...(body.received_at ? { received_at: body.received_at } : {}) }
  if (each === null) return openLot(itemId, qty, '0', true, 'manual', fields)
  if (foreign) return openLot(itemId, qty, '0', true, 'manual', { ...fields, cost_currency: body.currency!, foreign_unit_cost: each })
  return openLot(itemId, qty, each, false, 'manual', fields)
}

/** `count_item`: negative lots to 0 first, then the difference FIFO, or onto the newest lot. */
function countItem(itemId: string, onHand: string, note: string): MockMove[] {
  const item = counted(itemId, 'lot')
  const target = quantity(item, onHand, 'on_hand', true)
  const made: MockMove[] = []
  for (const lot of fifo(itemId)) {
    const held = lotOnHand(lot.id)
    if (below(held)) made.push(move({ item_id: itemId, lot_id: lot.id, kind: 'adjust', qty: dec(neg(held)), unit_cost: lot.unit_cost, cost_unknown: lot.cost_unknown, reason: note }))
  }
  const total = sum(fifo(itemId).map((l) => lotOnHand(l.id)))
  const diff = dec(subDecimals(target, total))
  if (below(diff)) {
    for (const m of takeStock(itemId, neg(diff), { kind: 'adjust', reason: note })) made.push(inventoryMock.moves.find((x) => x.id === m.id)!)
  } else if (above(diff)) {
    const lots = fifo(itemId)
    const newest = lots[lots.length - 1]
    if (newest) made.push(move({ item_id: itemId, lot_id: newest.id, kind: 'adjust', qty: diff, unit_cost: newest.unit_cost, cost_unknown: newest.cost_unknown, reason: note }))
    else {
      const lot = openLot(itemId, '0', '0', true, 'adjustment', { note })
      made.push(move({ item_id: itemId, lot_id: lot.id, kind: 'adjust', qty: diff, unit_cost: '0', cost_unknown: true, reason: note }))
    }
  }
  return made
}

/** `recost_lot`: every use, return and adjust of the lot takes the lot's cost now; the order items whose parts cost changed. */
function recost(lotId: string): S['RecostResult'] {
  const lot = findLot(lotId)
  counted(lot.item_id, 'lot')
  if (recostBusy) {
    recostBusy = false
    throw new Refused(409, 'recost_busy', `lot ${lotId} kept taking new order moves: try again`, ORDER_PROBLEMS)
  }
  const before = inventoryMock.moves.map((m) => ({ ...m }))
  const changed = inventoryMock.moves.filter((m) => m.lot_id === lotId && ['use', 'return', 'adjust'].includes(m.kind) && (m.unit_cost !== lot.unit_cost || m.cost_unknown !== lot.cost_unknown))
  const costs = [...new Set(changed.map((m) => m.unit_cost))].sort(cmpDecimals)
  for (const m of changed) Object.assign(m, { unit_cost: lot.unit_cost, cost_unknown: lot.cost_unknown })
  const touched = [...new Set(changed.map((m) => m.order_item_id).filter((x): x is string => !!x))]
  const rows = touched.flatMap((orderItemId) => {
    const was = partsMovedOf(orderItemId, before)
    const is = partsMovedOf(orderItemId)
    if (was.minor === is.minor && was.unknown === is.unknown) return []
    const orderId = changed.find((m) => m.order_item_id === orderItemId)?.order_id ?? null
    return [{ order_id: orders.orders.some((o) => o.id === orderId) ? orderId : null, order_item_id: orderItemId, before_minor: was.minor, after_minor: is.minor, before_unknown: was.unknown, after_unknown: is.unknown }]
  })
  return { item_id: lot.item_id, unit_cost_minor: lot.unit_cost, cost_unknown: lot.cost_unknown, before_unit_costs_minor: costs, moves: changed.length, orders: rows }
}

const STATUS_MOVE: Record<string, S['Move']['kind']> = { in_service: 'use', sold: 'sell', retired: 'retire', scrapped: 'scrap' }

function setUnitStatus(unitId: string, body: S['UnitStatusInput']): MockUnit {
  const unit = findUnit(unitId)
  if (unit.installed_in_unit_id) throw new Refused(409, 'unit_installed', 'the unit is fitted in another unit')
  const from = unit.status
  const to = body.status
  if (from === to) return unit
  if (from === 'in_stock') move({ item_id: unit.item_id, unit_id: unit.id, kind: STATUS_MOVE[to]!, qty: '-1', unit_cost: unit.cost, cost_unknown: unit.cost_unknown, reason: body.note ?? '' })
  else if (to === 'in_stock') move({ item_id: unit.item_id, unit_id: unit.id, kind: 'return', qty: '1', unit_cost: unit.cost, cost_unknown: unit.cost_unknown, reason: body.note ?? '' })
  unit.status = to
  if (to === 'in_service') unit.in_service_at = now()
  if (to === 'retired' || to === 'scrapped') unit.retired_at = now()
  return unit
}

function kindProblem(body: { parent_id?: string | null; attribute_schema?: Record<string, unknown> | null }, own: string | null) {
  if (body.parent_id) {
    const parent = kindOf(body.parent_id)
    if (!parent) throw new Refused(404, 'kind_not_found', `no item kind '${body.parent_id}'`)
    if (parent.archived_at) throw new Refused(409, 'kind_archived', 'the item kind is archived; restore it first')
    if (own && kindPath(body.parent_id).some((k) => k.id === own)) throw new Refused(422, 'kind_cycle', 'the parent is the kind or below it')
  }
  if (body.attribute_schema != null) {
    // Go's checkSchema: a required key may be one a parent defines (the parent's merged keys).
    const parent = body.parent_id !== undefined ? body.parent_id : own ? (kindOf(own)?.parent_id ?? null) : null
    const inherited = parent ? Object.keys((kindSchema(parent).properties ?? {}) as Record<string, unknown>) : []
    const reason = schemaProblem(body.attribute_schema, 'item', inherited)
    if (reason) throw new Refused(422, 'bad_property_schema', reason)
  }
}

const params = (request: Request) => new URL(request.url).searchParams

async function body<T>(request: Request, route: string): Promise<T> {
  const read = (await request.json()) as T
  inventoryMock.bodies.push({ route, body: read })
  inventoryMock.keys.push({ route, key: request.headers.get('Idempotency-Key') })
  return read
}

const B = '/api/v1/inventory'

export const handlers = [
  // Lays the seed before any inventory route answers; answers nothing itself.
  http.all(`${B}/*`, () => {
    ready()
  }),
  // -- operations (#6949 K1): a slow write's, running until `settleSlowWrites`
  http.get(`${B}/operations/:id`, ({ request, params: p }) =>
    answer(request, () => {
      const op = inventoryMock.slow.find((o) => o.id === p.id)
      if (!op) throw new Refused(404, 'operation_not_found', `no operation '${String(p.id)}'`)
      const base = { id: op.id, kind: 'inventory.write', subject: '', created_at: op.created_at }
      return op.settled ? { ...base, status: 'succeeded', finished_at: now(), result: op.result } : { ...base, status: 'running' }
    }),
  ),
  // -- kinds
  http.get(`${B}/kinds`, ({ request }) => {
    const archived = params(request).get('archived') === 'true'
    return HttpResponse.json({ kinds: inventoryMock.kinds.filter((k) => archived || !k.archived_at).sort((a, b) => a.id.localeCompare(b.id)).map(answerKind) })
  }),
  http.post(`${B}/kinds`, async ({ request }) => {
    const input = await body<S['KindInput']>(request, 'createKind')
    return answer(
      request,
      () => {
        if (!KIND_ID.test(input.id)) throw new FieldRefused('id', "String should match pattern '^[a-z][a-z0-9_]{0,39}$'")
        if (kindOf(input.id)) throw new Refused(409, 'kind_exists', `an item kind has that id: ${input.id}`)
        kindProblem(input, null)
        const made: MockKind = {
          id: input.id,
          name: input.name,
          parent_id: input.parent_id ?? null,
          // #6949 K1: a kind sent with no tracking is counted as its parent is.
          tracking: input.tracking ?? (input.parent_id ? kindOf(input.parent_id)?.tracking : undefined) ?? 'none',
          uom: input.uom ?? 'each',
          attribute_schema: input.attribute_schema ?? { type: 'object', properties: {} },
          life_basis: input.life_basis ?? 'none',
          saleable: input.saleable ?? false,
          builtin: false,
          archived_at: null,
          special_type: input.special_type ?? null,
        }
        inventoryMock.kinds.push(made)
        return answerKind(made)
      },
      201,
    )
  }),
  http.patch(`${B}/kinds/:id`, async ({ request, params: p }) => {
    const input = await body<S['KindPatch']>(request, 'patchKind')
    return answer(request, () => {
      const k = kindOf(String(p.id))
      if (!k) throw new Refused(404, 'kind_not_found', `no item kind '${String(p.id)}'`)
      kindProblem(input, k.id)
      if (input.tracking && input.tracking !== k.tracking && (k.builtin || inventoryMock.items.some((i) => i.kind_id === k.id))) {
        throw new Refused(409, 'kind_in_use', "the kind's items or built-in status forbid it")
      }
      if ('parent_id' in input) k.parent_id = input.parent_id ?? null
      for (const key of ['name', 'tracking', 'uom', 'attribute_schema', 'life_basis', 'saleable'] as const) {
        if (input[key] != null) Object.assign(k, { [key]: input[key] })
      }
      return answerKind(k)
    })
  }),
  http.post(`${B}/kinds/:id/archive`, ({ request, params: p }) =>
    answer(request, () => {
      const k = kindOf(String(p.id))
      if (!k) throw new Refused(404, 'kind_not_found', `no item kind '${String(p.id)}'`)
      if (inventoryMock.items.some((i) => i.kind_id === k.id && !i.archived_at)) throw new Refused(409, 'kind_in_use', "the kind's items or built-in status forbid it")
      k.archived_at = now()
      return answerKind(k)
    }),
  ),
  http.post(`${B}/kinds/:id/restore`, ({ request, params: p }) =>
    answer(request, () => {
      const k = kindOf(String(p.id))
      if (!k) throw new Refused(404, 'kind_not_found', `no item kind '${String(p.id)}'`)
      k.archived_at = null
      return answerKind(k)
    }),
  ),
  http.get(`${B}/kinds/:id/schema`, ({ request, params: p }) =>
    answer(request, () => {
      const k = kindOf(String(p.id))
      if (!k) throw new Refused(404, 'kind_not_found', `no item kind '${String(p.id)}'`)
      return { kind_id: k.id, schema: kindSchema(k.id), own_schema: k.attribute_schema, keys_in_use: itemKeysInUse(itemsOfKinds(subtree(k.id))), item_required: itemRequired(k.id) }
    }),
  ),

  // -- items
  http.get(`${B}/items`, ({ request }) => {
    const search = params(request)
    const kindId = search.get('kind')
    const kinds = kindId ? (search.get('descendants') === 'false' ? new Set([kindId]) : subtree(kindId)) : null
    const q = search.get('q')?.trim().toLowerCase()
    const rows = inventoryMock.items
      .filter((i) => search.get('archived') === 'true' || !i.archived_at)
      .filter((i) => !kinds || kinds.has(i.kind_id))
      .filter((i) => !q || `${i.name} ${i.manufacturer}`.toLowerCase().includes(q))
      .map(answerItem)
      .filter((i) => search.get('low') !== 'true' || i.low)
      .sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()))
    return HttpResponse.json({ items: rows })
  }),
  http.post(`${B}/items`, async ({ request }) => {
    const input = await body<S['ItemInput']>(request, 'createItem')
    return answer(
      request,
      () => {
        const k = kindOf(input.kind_id)
        if (!k) throw new Refused(404, 'kind_not_found', `no item kind '${input.kind_id}'`)
        if (k.archived_at) throw new Refused(409, 'kind_archived', 'the item kind is archived; restore it first')
        if (k.tracking === 'filament') throw new Refused(409, 'filament_item_refused', 'filament items and their offers wait for the filament phase (#7068)')
        if (inventoryMock.items.some((i) => i.kind_id === k.id && !i.archived_at && i.name.toLowerCase() === input.name.trim().toLowerCase())) {
          throw new Refused(409, 'item_exists', 'an unarchived item of the kind has that name in any case, or that import ref')
        }
        const made = seedItem({
          kind_id: k.id,
          name: input.name,
          manufacturer: input.manufacturer ?? '',
          low_stock: dec(input.low_stock ?? '0'),
          saleable: input.saleable ?? null,
          note: input.note ?? '',
        })
        // R-K1-33: the item and its properties in one write, or nothing.
        const refusal = rowPropertiesRefusal(request, `item:${made.id}`, input.properties, true)
        if (refusal) {
          inventoryMock.items = inventoryMock.items.filter((i) => i.id !== made.id)
          throw new Answered(refusal)
        }
        saveRowProperties(`item:${made.id}`, input.properties)
        return answerItem(findItem(made.id))
      },
      201,
    )
  }),
  http.get(`${B}/items/:id`, ({ request, params: p }) => answer(request, () => answerItem(findItem(String(p.id))))),
  http.patch(`${B}/items/:id`, async ({ request, params: p }) => {
    const input = await body<S['InventoryItemPatch']>(request, 'patchItem')
    return answer(request, () => {
      const item = findItem(String(p.id))
      // R-K1-33: a patch that sends a field is checked; an empty one is not. `properties: null`
      // alone counts as empty, as `_item_body` drops it before Go sees the patch.
      const sent = Object.entries(input).filter(([key, value]) => !(key === 'properties' && value === null))
      const refusal = rowPropertiesRefusal(request, `item:${item.id}`, input.properties, sent.length > 0)
      if (refusal) throw new Answered(refusal)
      saveRowProperties(`item:${item.id}`, input.properties)
      if (input.name != null) item.name = input.name
      if (input.manufacturer != null) item.manufacturer = input.manufacturer
      if (input.note != null) item.note = input.note
      if (input.low_stock != null) item.low_stock = dec(input.low_stock)
      if ('saleable' in input) item.saleable = input.saleable ?? null
      if ('life_hours' in input) item.life_hours = input.life_hours == null ? null : dec(input.life_hours)
      if ('life_months' in input) item.life_months = input.life_months ?? null
      return answerItem(item)
    })
  }),
  http.post(`${B}/items/:id/archive`, ({ request, params: p }) =>
    answer(request, () => {
      const item = findItem(String(p.id))
      item.archived_at = now()
      return answerItem(item)
    }),
  ),
  http.post(`${B}/items/:id/restore`, ({ request, params: p }) =>
    answer(request, () => {
      const item = findItem(String(p.id))
      item.archived_at = null
      return answerItem(item)
    }),
  ),

  // -- offers
  http.get(`${B}/items/:id/offers`, ({ request, params: p }) =>
    answer(request, () => {
      findItem(String(p.id))
      const all = params(request).get('archived') === 'true'
      return { offers: inventoryMock.offers.filter((o) => o.item_id === p.id && (all || !o.archived_at)).map(answerOffer) }
    }),
  ),
  http.get(`${B}/suppliers/:id/offers`, ({ request, params: p }) =>
    answer(request, () => {
      if (!supplierById(String(p.id))) throw new Refused(404, 'supplier_not_found', `no supplier '${String(p.id)}'`)
      const all = params(request).get('archived') === 'true'
      return { offers: inventoryMock.offers.filter((o) => o.supplier_id === p.id && (all || !o.archived_at)).map(answerOffer) }
    }),
  ),
  http.post(`${B}/offers`, async ({ request }) => {
    const input = await body<S['OfferInput']>(request, 'createOffer')
    return answer(
      request,
      () => {
        const supplier = supplierById(input.supplier_id)
        if (!supplier || supplier.archived_at) throw new Refused(404, 'supplier_not_found', 'no supplier, or it is archived')
        const item = findItem(input.item_id)
        if (trackingOf(item) === 'filament') throw new Refused(409, 'filament_item_refused', 'filament items and their offers wait for the filament phase (#7068)')
        const sku = input.sku.trim()
        if (inventoryMock.offers.some((o) => !o.archived_at && o.supplier_id === supplier.id && o.item_id === item.id && o.sku.toUpperCase() === sku.toUpperCase())) {
          throw new Refused(409, 'offer_exists', 'the item already has an unarchived offer with that SKU at the supplier')
        }
        return seedOffer({ item_id: item.id, supplier_id: supplier.id, sku, url: input.url?.trim() || null, pack_size: dec(input.pack_size ?? '1'), pack_uom: input.pack_uom ?? 'each', note: input.note ?? null })
      },
      201,
    )
  }),
  http.post(`${B}/offers/:id/archive`, ({ request, params: p }) =>
    answer(request, () => {
      const offer = inventoryMock.offers.find((o) => o.id === p.id)
      if (!offer) throw new Refused(404, 'offer_not_found', `no offer '${String(p.id)}'`)
      offer.archived_at = now()
      return answerOffer(offer)
    }),
  ),
  http.get(`${B}/offers/:id/prices`, ({ request, params: p }) =>
    answer(request, () => {
      if (!inventoryMock.offers.some((o) => o.id === p.id)) throw new Refused(404, 'offer_not_found', `no offer '${String(p.id)}'`)
      return {
        prices: inventoryMock.prices
          .filter((x) => x.offer_id === p.id)
          .sort((a, b) => b.on_date.localeCompare(a.on_date) || b.written - a.written)
          .map(({ offer_id: _o, written: _w, ...point }) => point),
      }
    }),
  ),
  http.post(`${B}/offers/:id/prices`, async ({ request, params: p }) => {
    const input = await body<S['PriceInput']>(request, 'addPrice')
    return answer(
      request,
      () => {
        if (!inventoryMock.offers.some((o) => o.id === p.id)) throw new Refused(404, 'offer_not_found', `no offer '${String(p.id)}'`)
        return addPrice(String(p.id), String(input.price), input.currency ?? shop(), input.on_date ?? now().slice(0, 10))
      },
      201,
    )
  }),

  // -- lots
  http.get(`${B}/items/:id/lots`, ({ request, params: p }) =>
    answer(request, () => {
      counted(String(p.id), 'lot')
      return { lots: fifo(String(p.id)).map(answerLot) }
    }),
  ),
  http.post(`${B}/items/:id/lots`, async ({ request, params: p }) => {
    const input = await body<S['ReceiveLot']>(request, 'receiveLot')
    return answer(request, () => answerLot(receiveLot(String(p.id), input)), 201)
  }),
  http.get(`${B}/lots/:id`, ({ request, params: p }) => answer(request, () => answerLot(findLot(String(p.id))))),
  http.patch(`${B}/lots/:id`, async ({ request, params: p }) => {
    const input = await body<S['LotPatch']>(request, 'patchLot')
    return answer(request, () => {
      const lot = findLot(String(p.id))
      if (input.unit_cost_minor != null) Object.assign(lot, { unit_cost: dec(input.unit_cost_minor), cost_unknown: false })
      if (input.note != null) lot.note = input.note
      if ('location_id' in input) lot.location_id = place(input.location_id)
      return answerLot(lot)
    })
  }),
  http.post(`${B}/lots/:id/recost`, ({ request, params: p }) => answer(request, () => recost(String(p.id)))),
  http.post(`${B}/items/:id/use`, async ({ request, params: p }) => {
    const input = await body<S['UseInput']>(request, 'use')
    return answer(request, () => {
      const item = counted(String(p.id), 'lot', true)
      return { moves: takeStock(item.id, quantity(item, input.qty, 'qty'), { lot_id: input.lot_id, reason: input.reason }) }
    })
  }),
  http.post(`${B}/items/:id/count`, async ({ request, params: p }) => {
    const input = await body<S['CountInput']>(request, 'count')
    return answer(request, () => {
      if (!input.note?.trim()) throw new FieldRefused('note', 'Value error, a note is not blank')
      return { moves: countItem(String(p.id), String(input.on_hand), input.note).map(answerMove) }
    })
  }),

  // -- units
  http.get(`${B}/items/:id/units`, ({ request, params: p }) =>
    answer(request, () => {
      counted(String(p.id), 'unit')
      const status = params(request).get('status')
      return { units: inventoryMock.units.filter((u) => u.item_id === p.id && (!status || u.status === status)).map(answerUnit) }
    }),
  ),
  http.post(`${B}/items/:id/units`, async ({ request, params: p }) => {
    const input = await body<S['ReceiveUnits']>(request, 'receiveUnits')
    return answer(
      request,
      () => {
        const item = counted(String(p.id), 'unit')
        const cost = input.cost_minor == null ? null : dec(input.cost_minor)
        const locationId = place(input.location_id)
        const serials = Array.from({ length: input.count }, (_, at) => input.serials?.[at]?.trim() || null)
        const named = serials.filter((x): x is string => !!x)
        if (new Set(named).size < named.length) throw new Refused(409, 'serial_exists', 'another unit of the item has that serial')
        for (const serial of named) freeSerial(item.id, serial)
        const made = serials.map((serial) => newUnit(item.id, cost ?? '0', cost === null, { serial, name: input.name ?? '', location_id: locationId }))
        return { units: made.map(answerUnit) }
      },
      201,
    )
  }),
  http.get(`${B}/units/:id`, ({ request, params: p }) => answer(request, () => answerUnit(findUnit(String(p.id))))),
  http.patch(`${B}/units/:id`, async ({ request, params: p }) => {
    const input = await body<S['UnitPatch']>(request, 'patchUnit')
    return answer(request, () => {
      const unit = findUnit(String(p.id))
      if (input.name != null) unit.name = input.name
      if ('serial' in input) {
        const serial = input.serial?.trim() || null
        freeSerial(unit.item_id, serial, unit.id)
        unit.serial = serial
      }
      if ('location_id' in input) unit.location_id = place(input.location_id)
      return answerUnit(unit)
    })
  }),
  http.post(`${B}/units/:id/status`, async ({ request, params: p }) => {
    const input = await body<S['UnitStatusInput']>(request, 'unitStatus')
    return answer(request, () => answerUnit(setUnitStatus(String(p.id), input)))
  }),

  // -- moves
  http.get(`${B}/moves`, ({ request }) => {
    const search = params(request)
    const limit = Number(search.get('limit') ?? 200)
    const rows = inventoryMock.moves.filter(
      (m) =>
        (!search.get('item_id') || m.item_id === search.get('item_id')) &&
        (!search.get('lot_id') || m.lot_id === search.get('lot_id')) &&
        (!search.get('unit_id') || m.unit_id === search.get('unit_id')) &&
        (!search.get('order_item_id') || m.order_item_id === search.get('order_item_id')),
    )
    return HttpResponse.json({ moves: [...rows].reverse().slice(0, limit).map(answerMove) })
  }),
]

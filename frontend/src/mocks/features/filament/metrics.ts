import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import type { FilamentSpool } from '../../../api/types'
import { addDecimals, cmpDecimals, divDecimal, fromScaled, mulDecimal, toScaled } from '../../../lib/decimal'
// Cycles (`handlers.ts` loads every feature; the purchasing mock imports the Filament one): only inside functions.
import { mockSettings } from '../../handlers'
import { purchasingMetrics } from '../filamentPurchasing'
import { burnOf, emptyOn, MAX_FORECAST_DAYS, metricsInput, nonPrintWaste, type MockPlate } from './index'

type S = components['schemas']
type Dimension = S['InventoryMetrics']['dimension']
type Bucket = S['InventoryMetrics']['bucket']
type WasteDimension = S['WasteRow']['dimension']
type Input = ReturnType<typeof metricsInput>

/**
 * #6269 — `GET /filament/forecast`, `GET /filament/metrics` and `GET /filament/metrics/purchasing`
 * (and #7152's `GET /filament/metrics/material-numbers` and `GET /filament/cost-per-kg`)
 * (`backend/scadbuddy/api/filament.py`) over the Filament and purchasing mocks' state, by
 * `filament/metrics_store.py`'s rules: the plates are `index.ts`'s one plate rule
 * (`metrics.plates`), a type's or a group's burn is the burn of the union of its spools'
 * plates born at the oldest spool, a key with no value is labelled "No material" (and the
 * rest), and money is the spools' grams at their effective cost, to the cent. The labels are
 * catalog names as they are: `other` and `none` are folded only in the Prometheus export.
 */

const BASE = '/api/v1/filament'
const DAY_MS = 86_400_000
/** `api/filament.py` `METRICS_MAX_DAYS`: three years and a leap day. */
const METRICS_MAX_DAYS = 1096
const DIMENSIONS: readonly Dimension[] = ['material', 'brand', 'type', 'supplier', 'location']
const BUCKETS: readonly Bucket[] = ['week', 'month']
const WASTE: readonly WasteDimension[] = ['material', 'type', 'printer']
/** The non-print waste has no printer (`metrics_store.py` `_NON_PRINT`). */
const NON_PRINT: readonly WasteDimension[] = ['material', 'type']
const NONE_LABEL: Record<Dimension | 'printer', string> = {
  material: 'No material',
  brand: 'No brand',
  type: 'No type',
  supplier: 'No supplier',
  location: 'No location',
  printer: 'No printer',
}

/** Code-point order of the upper-cased text, as Python sorts `str.upper()`. */
const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)
const upper = (text: string) => text.toUpperCase()

const problem = (detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: 'Unprocessable Content', status: 422, detail }, { status: 422, headers: { 'Content-Type': 'application/problem+json' } })

function settings() {
  const stored = mockSettings()
  return {
    window: stored.filament_burn_window_days ?? 30,
    lead: stored.filament_lead_time_days ?? 7,
    margin: stored.filament_safety_margin_days ?? 3,
    currency: stored.orders_currency ?? 'USD',
    defaultCost: stored.filament_default_cost_per_kg ?? null,
  }
}

const day = (ms: number) => new Date(ms).toISOString().slice(0, 10)
const addDays = (iso: string, days: number) => day(Date.parse(`${iso}T00:00:00Z`) + days * DAY_MS)

/** `_union_burn`: the burn of the union of `spools`' plates, born at the oldest one. */
function unionBurn(spools: readonly FilamentSpool[], input: Input, window: number, nowMs: number): number | null {
  if (spools.length === 0) return null
  const plates = spools.flatMap((spool) => input.plates.get(spool.id) ?? [])
  const born = spools.map((spool) => spool.created_at).reduce((a, b) => (Date.parse(a) <= Date.parse(b) ? a : b))
  return burnOf(plates, window, born, nowMs)
}

/** `_days_of_stock`: grams over the burn; `null` with no burn above 0 or past `MAX_FORECAST_DAYS`. */
function daysOfStock(grams: number, burn: number | null): number | null {
  if (burn == null || !Number.isFinite(burn) || burn <= 0) return null
  const days = grams / burn
  return Number.isFinite(days) && days <= MAX_FORECAST_DAYS ? days : null
}

/** `MetricsStore.forecast`. */
export function forecast(nowMs = Date.now()): S['Forecast'] {
  const input = metricsInput()
  const { window, lead, margin } = settings()
  const stocked = input.spools.filter((spool) => !spool.archived_at)
  const today = day(nowMs)
  const types: S['TypeForecast'][] = []
  for (const type of input.types) {
    const group = input.spools.filter((spool) => spool.type_id === type.id)
    const inStock = group.filter((spool) => !spool.archived_at)
    if (group.length === 0 || (type.archived_at && inStock.length === 0)) continue
    const burn = unionBurn(group, input, window, nowMs)
    const grams = inStock.reduce((sum, spool) => sum + (spool.grams_left ?? 0), 0)
    const empty = emptyOn(grams, burn, nowMs)
    const leadTime = type.lead_time_days ?? lead
    const safety = type.safety_margin_days ?? margin
    const reorder = empty ? addDays(empty, -(leadTime + safety)) : null
    types.push({
      type_id: type.id,
      label: type.code,
      material: type.material,
      spools: inStock.length,
      grams_left: grams,
      burn_g_per_day: burn,
      days_of_stock: daysOfStock(grams, burn),
      empty_on: empty,
      lead_time_days: leadTime,
      safety_margin_days: safety,
      reorder_on: reorder,
      reorder_now: reorder != null && reorder <= today,
      low_stock: inStock.length > 0 && inStock.every((spool) => spool.low_stock),
    })
  }
  return {
    window_days: window,
    spools: stocked.map((spool) => {
      const burn = burnOf(input.plates.get(spool.id) ?? [], window, spool.created_at, nowMs)
      return { spool_id: spool.id, number: spool.number, type_id: spool.type_id, grams_left: spool.grams_left, burn_g_per_day: burn, empty_on: emptyOn(spool.grams_left, burn, nowMs) }
    }),
    types: types.sort((a, b) => cmp(upper(a.label), upper(b.label)) || cmp(a.type_id, b.type_id)),
  }
}

/** `_key`: the spool's id and label in `dimension`. */
function keyOf(input: Input, spool: FilamentSpool, dimension: Dimension): [string | null, string] {
  let found: [string | null | undefined, string | null | undefined]
  if (dimension === 'material') found = [spool.effective?.material_id, spool.names?.material]
  else if (dimension === 'brand') found = [spool.effective?.brand_id, spool.names?.brand]
  else if (dimension === 'type') found = [spool.type_id, input.types.find((type) => type.id === spool.type_id)?.code]
  else if (dimension === 'supplier') {
    const supplier = input.suppliers.get(spool.id)
    found = [supplier?.id, supplier?.name]
  } else found = [spool.location_id, spool.names?.location]
  const [key, label] = found
  return key ? [key, label || key] : [null, NONE_LABEL[dimension]]
}

function printerOf(input: Input, plate: MockPlate): [string | null, string] {
  if (!plate.printer_id) return [null, NONE_LABEL.printer]
  return [plate.printer_id, input.printers.get(plate.printer_id) ?? plate.printer_id]
}

/**
 * `_value`'s grams × cost per kg, in thousandths of the currency: the stock rows sum them and
 * divide once. The grams are exact, as `Decimal(str(grams_left))` reads them (R-T8-9).
 */
function milliValue(spool: FilamentSpool, defaultCost: string | null): string | null {
  const cost = spool.effective?.cost_per_kg ?? defaultCost
  if (cost == null || spool.grams_left == null) return null
  const grams = String(spool.grams_left)
  const scale = (text: string) => text.split('.')[1]?.length ?? 0
  return fromScaled(toScaled(cost, scale(cost)) * toScaled(grams, scale(grams)), scale(cost) + scale(grams))
}

const bucketOf = (iso: string, bucket: Bucket) => {
  if (bucket === 'month') return `${iso.slice(0, 7)}-01`
  const weekday = (new Date(`${iso}T00:00:00Z`).getUTCDay() + 6) % 7
  return addDays(iso, -weekday)
}

/** `MetricsStore.inventory`. */
export function inventory(dimension: Dimension, bucket: Bucket, since: string, until: string): S['InventoryMetrics'] {
  const input = metricsInput()
  const { currency, defaultCost } = settings()
  const stock = new Map<string | null, { label: string; spools: number; grams: number; milli: string | null }>()
  for (const spool of input.spools.filter((one) => !one.archived_at)) {
    const [key, label] = keyOf(input, spool, dimension)
    const row = stock.get(key) ?? { label, spools: 0, grams: 0, milli: null }
    row.spools += 1
    row.grams += spool.grams_left ?? 0
    const milli = milliValue(spool, defaultCost)
    if (milli != null) row.milli = row.milli == null ? milli : addDecimals(row.milli, milli)
    stock.set(key, row)
  }
  const used = new Map<string, { bucket: string; key: string | null; label: string; used: number; wasted: number }>()
  const waste = new Map<string, { dimension: WasteDimension; key: string | null; label: string; used: number; wasted: number }>()
  for (const spool of input.spools) {
    for (const plate of input.plates.get(spool.id) ?? []) {
      const at = plate.at.slice(0, 10)
      if (at < since || at > until) continue
      const lost = plate.outcome === 'failed' || plate.outcome === 'cancelled' ? plate.grams : 0
      const [key, label] = keyOf(input, spool, dimension)
      const first = bucketOf(at, bucket)
      const row = used.get(`${first}|${key}`) ?? { bucket: first, key, label, used: 0, wasted: 0 }
      row.used += plate.grams
      row.wasted += lost
      used.set(`${first}|${key}`, row)
      for (const name of WASTE) {
        const [wasteKey, wasteLabel] = name === 'printer' ? printerOf(input, plate) : keyOf(input, spool, name)
        const found = waste.get(`${name}|${wasteKey}`) ?? { dimension: name, key: wasteKey, label: wasteLabel, used: 0, wasted: 0 }
        found.used += plate.grams
        found.wasted += lost
        waste.set(`${name}|${wasteKey}`, found)
      }
    }
  }
  const dumped = new Map<string, { dimension: WasteDimension; key: string | null; label: string; wasted: number }>()
  let dumpedTotal = 0
  for (const spool of input.spools) {
    for (const event of input.waste.get(spool.id) ?? []) {
      const at = event.at.slice(0, 10)
      if (at < since || at > until) continue
      const grams = nonPrintWaste(event)
      dumpedTotal += grams
      for (const name of NON_PRINT) {
        const [key, label] = keyOf(input, spool, name as Dimension)
        const found = dumped.get(`${name}|${key}`) ?? { dimension: name, key, label, wasted: 0 }
        found.wasted += grams
        dumped.set(`${name}|${key}`, found)
      }
    }
  }
  return {
    currency,
    dimension,
    bucket,
    since,
    until,
    non_print_waste_g: dumpedTotal,
    non_print_waste: [...dumped.values()]
      .map((row) => ({ dimension: row.dimension, key: row.key, label: row.label, wasted_g: row.wasted }))
      .sort((a, b) => NON_PRINT.indexOf(a.dimension) - NON_PRINT.indexOf(b.dimension) || b.wasted_g - a.wasted_g || cmp(upper(a.label), upper(b.label))),
    stock: [...stock.entries()]
      .map(([key, row]) => ({ key, label: row.label, spools: row.spools, grams_left: row.grams, value: row.milli == null ? null : divDecimal(row.milli, '1000', 2) }))
      .sort((a, b) => b.grams_left - a.grams_left || cmp(upper(a.label), upper(b.label))),
    consumption: [...used.values()]
      .map((row) => ({ bucket: row.bucket, key: row.key, label: row.label, used_g: row.used, wasted_g: row.wasted }))
      .sort((a, b) => cmp(a.bucket, b.bucket) || cmp(upper(a.label), upper(b.label)) || cmp(a.key ?? '', b.key ?? '')),
    waste: [...waste.values()]
      .map((row) => ({ dimension: row.dimension, key: row.key, label: row.label, used_g: row.used, wasted_g: row.wasted, rate: row.used > 0 ? row.wasted / row.used : null }))
      .sort((a, b) => WASTE.indexOf(a.dimension) - WASTE.indexOf(b.dimension) || b.wasted_g - a.wasted_g || cmp(upper(a.label), upper(b.label))),
  }
}

/**
 * #7152 — `MetricsStore.material_numbers`: per type code, case folded (archived types of the
 * code in it; the label the live type's code, else the newest archived one's), the stocked
 * spools, their grams and value, and the range's plates as `inventory` counts them. A key
 * with no spool in stock and no plate in the range is left out; the untyped row comes last.
 */
export function materialNumbers(since: string, until: string): S['MaterialNumberStats'] {
  const input = metricsInput()
  const { currency, defaultCost } = settings()
  const rows = new Map<string | null, { spools: number; grams: number; used: number; wasted: number; milli: string | null; listed: boolean }>()
  for (const spool of input.spools) {
    const type = input.types.find((one) => one.id === spool.type_id)
    const key = type ? upper(type.code) : null
    const row = rows.get(key) ?? { spools: 0, grams: 0, used: 0, wasted: 0, milli: null, listed: false }
    if (!spool.archived_at) {
      row.spools += 1
      row.grams += spool.grams_left ?? 0
      row.listed = true
      const milli = milliValue(spool, defaultCost)
      if (milli != null) row.milli = row.milli == null ? milli : addDecimals(row.milli, milli)
    }
    for (const plate of input.plates.get(spool.id) ?? []) {
      const at = plate.at.slice(0, 10)
      if (at < since || at > until) continue
      row.used += plate.grams
      if (plate.outcome === 'failed' || plate.outcome === 'cancelled') row.wasted += plate.grams
      row.listed = true
    }
    rows.set(key, row)
  }
  const found: S['MaterialNumberRow'][] = []
  for (const [key, row] of rows) {
    if (!row.listed) continue
    const types = key == null ? [] : input.types.filter((type) => upper(type.code) === key)
    const live = types.find((type) => !type.archived_at)
    const newest = [...types].sort((a, b) => cmp(b.archived_at ?? '', a.archived_at ?? ''))[0]
    found.push({
      key,
      label: key == null ? 'No material number' : (live ?? newest)!.code,
      types: types.map((type) => ({ id: type.id, code: type.code, archived: !!type.archived_at })).sort((a, b) => cmp(a.code, b.code) || cmp(a.id, b.id)),
      spools: row.spools,
      grams_left: row.grams,
      used_g: row.used,
      wasted_g: row.wasted,
      value: row.milli == null ? null : divDecimal(row.milli, '1000', 2),
    })
  }
  return {
    currency,
    since,
    until,
    rows: found.sort((a, b) => Number(a.key == null) - Number(b.key == null) || b.grams_left - a.grams_left || cmp(upper(a.label), upper(b.label))),
  }
}

/** `filament/cost_calc.py` `CostQuery`'s money: at least 0, at most eight whole digits and four places. */
const MONEY = /^\d{1,8}(?:\.\d{1,4})?$/

/** #7152 — `GET /filament/cost-per-kg`: `cost_calc.calculate` on decimal strings, never a float. */
export function costPerKg(url: URL): S['CostCalculation'] | Response {
  const known = new Set(['price', 'tax', 'shipping', 'quantity', 'net_weight_g'])
  for (const name of url.searchParams.keys()) if (!known.has(name)) return problem(`${name}: Extra inputs are not permitted`)
  const price = url.searchParams.get('price')
  const tax = url.searchParams.get('tax') ?? '0'
  const shipping = url.searchParams.get('shipping') ?? '0'
  if (price == null) return problem('price: Field required')
  for (const [name, value] of [['price', price], ['tax', tax], ['shipping', shipping]] as const) {
    if (!MONEY.test(value)) return problem(`${name}: Input should be a decimal of at most four places, 0 or more`)
  }
  const quantity = Number(url.searchParams.get('quantity') ?? '1')
  const net = Number(url.searchParams.get('net_weight_g'))
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 1000) return problem('quantity: Input should be from 1 to 1000')
  if (!Number.isInteger(net) || net < 1 || net > 100_000) return problem('net_weight_g: Input should be from 1 to 100000')
  const withTax = addDecimals(price, tax)
  const total = addDecimals(withTax, shipping)
  const perKg = (amount: string) => divDecimal(mulDecimal(amount, 1000), String(quantity * net), 4)
  const cost = perKg(total)
  if (cmpDecimals(cost, '100000000') >= 0) return problem('the cost per kg is more than a cost per kg can hold (10^8)')
  return { currency: settings().currency, price, tax, shipping, quantity, net_weight_g: net, total, cost_per_kg: cost, price_per_kg: perKg(withTax) }
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/

/** `api/filament.py` `_range`: `until` defaults to today (UTC), `since` to `defaultDays` before it. */
function range(url: URL, defaultDays: number): [string, string] | Response {
  const sinceText = url.searchParams.get('since')
  const untilText = url.searchParams.get('until')
  for (const value of [sinceText, untilText]) if (value != null && (!ISO_DAY.test(value) || Number.isNaN(Date.parse(`${value}T00:00:00Z`)))) return problem('Input should be a valid date')
  const until = untilText ?? day(Date.now())
  const since = sinceText ?? addDays(until, -defaultDays)
  if (since > until) return problem('since must not be after until')
  if ((Date.parse(until) - Date.parse(since)) / DAY_MS > METRICS_MAX_DAYS) return problem(`the range is longer than ${METRICS_MAX_DAYS} days (three years)`)
  return [since, until]
}

export const handlers = [
  http.get(`${BASE}/forecast`, () => HttpResponse.json(forecast())),
  http.get(`${BASE}/metrics`, ({ request }) => {
    const url = new URL(request.url)
    const dimension = (url.searchParams.get('dimension') ?? 'material') as Dimension
    const bucket = (url.searchParams.get('bucket') ?? 'month') as Bucket
    if (!DIMENSIONS.includes(dimension)) return problem(`dimension: Input should be ${DIMENSIONS.map((one) => `'${one}'`).join(', ')}`)
    if (!BUCKETS.includes(bucket)) return problem("bucket: Input should be 'week' or 'month'")
    const found = range(url, 180)
    if (found instanceof Response) return found
    return HttpResponse.json(inventory(dimension, bucket, ...found))
  }),
  http.get(`${BASE}/metrics/material-numbers`, ({ request }) => {
    const found = range(new URL(request.url), 180)
    if (found instanceof Response) return found
    return HttpResponse.json(materialNumbers(...found))
  }),
  http.get(`${BASE}/cost-per-kg`, ({ request }) => {
    const found = costPerKg(new URL(request.url))
    return found instanceof Response ? found : HttpResponse.json(found)
  }),
  http.get(`${BASE}/metrics/purchasing`, ({ request }) => {
    const found = range(new URL(request.url), 365)
    if (found instanceof Response) return found
    return HttpResponse.json(purchasingMetrics(...found))
  }),
]

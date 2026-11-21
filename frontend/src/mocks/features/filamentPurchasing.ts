import { HttpResponse, http } from 'msw'
import { newId } from '../newId'
import type { InvoiceLine } from '../../api/filamentPurchasing'
import type { components } from '../../api/schema'
import { addDecimals, cmpDecimals, divDecimal, fromScaled, mulDecimal, subDecimals, toScaled } from '../../lib/decimal'
// Two cycles (`filament/index.ts` imports `invoiceRefOf` and `typeSuppliersOf` from here): use them only inside functions.
import { addInvoicedSpools, checkTypeProposal, createType, spoolClaimRefusal, spoolLabelWeight, spoolUsage, typeCodeTaken, typeInfo, updateInvoicedSpool } from './filament'
import { isSessionLive, startImportSession } from './assistantSessions'
import { seedInput, SHOP_CURRENCY, type SeedInvoice } from './filamentPurchasingSeed'

type S = components['schemas']

/**
 * #6266 — every purchasing route of `backend/scadbuddy/api/filament_purchasing.py` over an
 * in-memory seed (`filamentPurchasingSeed.ts`), with `reset`: suppliers, their products
 * and price points, the price sheet, a type's price history, supplier stats, invoice
 * drafts (lines, file, confirm) and the shopping list. Money is a decimal string; the
 * arithmetic is `lib/decimal.ts`, with the backend's rules (`filament/invoice_money.py`:
 * four decimals, half up, tax and shipping spread by the largest remainder). Confirm calls
 * the Filament mock's `addInvoicedSpools`. It refuses what the routes refuse, with the same
 * status and text. A line's `update_spool_ids` fill existing spools (Decision 11), and
 * every refusal is checked before anything is written.
 */

const BASE = '/api/v1/filament'
const UNIT = 4
const TOLERANCE = '0.05'

type Supplier = Omit<S['Supplier'], 'product_count'>
type Product = Omit<S['SupplierProduct'], 'newest_price'>
type Price = Omit<S['PricePoint'], 'price_per_kg'>
type Item = S['ShoppingItem']
type Line = Omit<InvoiceLine, 'unit_cost'>
type Head = Omit<S['Invoice'], 'difference' | 'reconciles' | 'line_count' | 'spool_count' | 'minor_exponent'>
type Stored = Head & { lines: Line[] }

const now = () => new Date().toISOString()
const today = () => now().slice(0, 10)

const TITLES = { 404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable Content' } as const

/** A refusal as `core/problems.py` `problem_response` writes it; `extra` members ride along (`existing_invoice_id`). */
const problem = (status: keyof typeof TITLES, detail: string, extra: Record<string, unknown> = {}) =>
  HttpResponse.json(
    { type: 'about:blank', title: TITLES[status], status, detail, ...extra },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

// -- the state -----------------------------------------------------------------------------

const money = (value: string) => fromScaled(toScaled(value, UNIT), UNIT)

function build() {
  const input = seedInput()
  const suppliers: Supplier[] = input.suppliers.map((row) => ({ ...row, archived_at: null, created_at: '2026-08-01T09:00:00Z', updated_at: '2026-08-01T09:00:00Z' }))
  const products: Product[] = input.products.map((row) => ({ ...row, archived_at: null, created_at: '2026-08-01T09:00:00Z', updated_at: '2026-08-01T09:00:00Z' }))
  const prices: Price[] = input.manualPrices.map((row, at) => ({
    id: `62660002-0000-4000-8000-${String(at + 1).padStart(12, '0')}`,
    product_id: row.product_id,
    price: money(row.price),
    currency: SHOP_CURRENCY,
    on_date: row.on_date,
    source: 'manual',
    invoice_line_id: null,
    created_at: `${row.on_date}T09:00:00Z`,
    updated_at: `${row.on_date}T09:00:00Z`,
  }))
  const invoices: Stored[] = input.invoices.map((row) => stored(row))
  for (const invoice of invoices) {
    if (invoice.status === 'confirmed') for (const line of invoice.lines) prices.push(invoicePrice(invoice, line, products))
  }
  return { suppliers, products, prices, invoices, shopping: input.shopping.map((row, at) => ({ ...row, created_at: `2026-11-1${at}T10:00:00Z`, updated_at: `2026-11-1${at}T10:00:00Z` })) as Item[] }
}

/** An invoice from its seed inputs, its lines worked out. */
function stored(row: SeedInvoice): Stored {
  const at = row.status === 'confirmed' ? '2026-10-22T12:00:00Z' : AT
  const invoice: Stored = {
    id: row.id,
    supplier_id: row.supplier_id,
    number: row.number,
    invoice_date: row.invoice_date,
    currency: SHOP_CURRENCY,
    subtotal: money(row.subtotal),
    shipping: money(row.shipping),
    tax: money(row.tax),
    total: money(row.total),
    status: row.status,
    import_run_id: null,
    confirmed_at: row.status === 'confirmed' ? at : null,
    note: row.note,
    file: row.file,
    created_at: at,
    updated_at: at,
    lines: row.lines.map((line, index) => ({
      id: line.id,
      invoice_id: row.id,
      position: index + 1,
      description: line.description,
      sku: line.sku,
      quantity: line.quantity,
      unit_price: money(line.unit_price),
      line_subtotal: money('0'),
      line_tax: money('0'),
      line_total: money('0'),
      allocated_cost: money('0'),
      tax_basis: 'none',
      colour_code: null,
      type_id: line.type_id,
      supplier_product_id: line.supplier_product_id,
      proposed_type: (line.proposed_type ?? null) as Line['proposed_type'],
      update_spool_ids: [],
      spool_ids: line.spool_ids ?? [],
      created_at: at,
      updated_at: at,
    })),
  }
  for (const line of invoice.lines) line.line_subtotal = money(mulDecimal(line.unit_price, line.quantity))
  recompute(invoice)
  return invoice
}

// -- the arithmetic (`filament/invoice_money.py`) ----------------------------------------------

class MoneyError extends Error {}

/** `total` shared by `weights` by the largest remainder; equal shares when every weight is 0. */
function spread(total: string, weights: string[]): string[] {
  if (!weights.length) return []
  const sum = weights.reduce((acc, weight) => acc + toScaled(weight, UNIT), 0n)
  const scaled = weights.map((weight) => (sum === 0n ? 1n : toScaled(weight, UNIT)))
  const divisor = sum === 0n ? BigInt(weights.length) : sum
  const whole = toScaled(total, UNIT)
  const floors = scaled.map((weight) => (whole * weight) / divisor)
  const remainders = scaled.map((weight) => (whole * weight) % divisor)
  let leftover = whole - floors.reduce((acc, floor) => acc + floor, 0n)
  const order = remainders.map((_, at) => at).sort((a, b) => (remainders[a]! === remainders[b]! ? a - b : remainders[a]! < remainders[b]! ? 1 : -1))
  for (const at of order) {
    if (leftover <= 0n) break
    floors[at]! += 1n
    leftover -= 1n
  }
  return floors.map((floor) => fromScaled(floor, UNIT))
}

/** Tax, line totals and allocated costs of every line from the header and the lines' subtotals. */
function recompute(invoice: Stored) {
  const lines = invoice.lines
  if (!lines.length) return
  const subtotals = lines.map((line) => line.line_subtotal)
  const given = lines.map((line) => (line.tax_basis === 'line' ? line.line_tax : null))
  const set = given.filter((value) => value !== null)
  let taxes: string[]
  let basis: string
  if (set.length && set.length === given.length) {
    taxes = set.map(money)
    basis = 'line'
  } else if (set.length) {
    throw new MoneyError('some lines give a tax and others do not')
  } else if (cmpDecimals(invoice.tax, '0') > 0) {
    const sum = subtotals.reduce(addDecimals)
    if (cmpDecimals(sum, '0') === 0) throw new MoneyError('tax on a receipt whose lines cost nothing')
    taxes = spread(invoice.tax, subtotals)
    basis = `receipt-wide ${divDecimal(mulDecimal(invoice.tax, 100), sum, 1)}%`
  } else {
    taxes = subtotals.map(() => money('0'))
    basis = 'none'
  }
  const totals = subtotals.map((subtotal, at) => addDecimals(subtotal, taxes[at]!))
  const shares = spread(invoice.shipping, totals)
  lines.forEach((line, at) => {
    line.line_tax = money(taxes[at]!)
    line.line_total = money(totals[at]!)
    line.allocated_cost = money(addDecimals(totals[at]!, shares[at]!))
    line.tax_basis = basis
    line.updated_at = now()
  })
}

const difference = (invoice: Stored) =>
  money(subDecimals(invoice.total, addDecimals(invoice.lines.map((line) => line.line_total).reduce(addDecimals, '0'), invoice.shipping)))

const unitCost = (line: Line) => money(divDecimal(line.allocated_cost, String(line.quantity), UNIT))
const costPerKg = (line: Line, labelG: number) => money(divDecimal(mulDecimal(line.allocated_cost, 1000), String(line.quantity * labelG), UNIT))
const pricePerPack = (line: Line, packSize: number) => money(divDecimal(mulDecimal(line.line_total, packSize), String(line.quantity), UNIT))
const pricePerKg = (price: string, packSize: number, labelG: number) => money(divDecimal(mulDecimal(price, 1000), String(packSize * labelG), UNIT))

function invoicePrice(invoice: Stored, line: Line, products: Product[]): Price {
  const pack = products.find((product) => product.id === line.supplier_product_id)?.pack_size ?? 1
  return {
    id: newId(),
    product_id: line.supplier_product_id!,
    price: pricePerPack(line, pack),
    currency: invoice.currency,
    on_date: invoice.invoice_date,
    source: 'invoice',
    invoice_line_id: line.id,
    created_at: now(),
    updated_at: now(),
  }
}

// -- reads ------------------------------------------------------------------------------------

const supplierOf = (id: string) => state.suppliers.find((row) => row.id === id)
const productOf = (id: string) => state.products.find((row) => row.id === id)
const invoiceOf = (id: string) => state.invoices.find((row) => row.id === id)

/** Newest `on_date` first; a tie goes to the one made last. */
const pricesOf = (productId: string) =>
  state.prices
    .map((price, at) => ({ price, at }))
    .filter(({ price }) => price.product_id === productId)
    .sort((a, b) => b.price.on_date.localeCompare(a.price.on_date) || b.at - a.at)
    .map(({ price }) => price)

function pricePoint(price: Price): S['PricePoint'] {
  const product = productOf(price.product_id)
  const label = product ? typeInfo(product.type_id)?.label_weight_g : undefined
  return { ...price, price_per_kg: product && label ? pricePerKg(price.price, product.pack_size, label) : null }
}

function productView(product: Product): S['SupplierProduct'] {
  const newest = pricesOf(product.id)[0]
  return { ...product, newest_price: newest ? pricePoint(newest) : null }
}

const supplierView = (row: Supplier): S['Supplier'] => ({
  ...row,
  product_count: state.products.filter((product) => product.supplier_id === row.id && !product.archived_at).length,
})

const lineView = (line: Line): InvoiceLine => ({ ...line, unit_cost: unitCost(line) })

/** A short copy of `backend/scadbuddy/orders/currency.py` `minor_exponent` (ISO 4217): the codes the mock's tests use. */
const minorExponent = (currency: string) => (['JPY', 'KRW', 'VND'].includes(currency) ? 0 : 2)

function invoiceView(invoice: Stored): S['Invoice'] {
  const { lines: _lines, ...head } = invoice
  const diff = difference(invoice)
  return { ...head, minor_exponent: minorExponent(invoice.currency), difference: diff, reconciles: cmpDecimals(diff.replace('-', ''), TOLERANCE) <= 0, line_count: invoice.lines.length, spool_count: invoice.lines.reduce((n, line) => n + line.spool_ids.length, 0) }
}

const detailView = (invoice: Stored): S['InvoiceDetail'] => ({ ...invoiceView(invoice), lines: invoice.lines.map(lineView) })

/** For the Filament mock's spool detail (#6266): the invoice line a spool was bought on. */
export function invoiceRefOf(spoolId: string, lineId?: string): S['SpoolInvoiceRef'] | null {
  for (const invoice of state.invoices) {
    const line = invoice.lines.find((one) => (lineId ? one.id === lineId : one.spool_ids.includes(spoolId)))
    if (!line) continue
    const supplier = supplierOf(invoice.supplier_id)
    return {
      invoice_id: invoice.id,
      number: invoice.number,
      invoice_date: invoice.invoice_date,
      supplier_id: invoice.supplier_id,
      supplier_name: supplier?.name ?? '',
      line_id: line.id,
      line_position: line.position,
      unit_cost: unitCost(line),
    }
  }
  return null
}

/** For the Filament mock's spool detail (#6266): the unarchived products of unarchived suppliers for a type, by supplier name. */
export function typeSuppliersOf(typeId: string): S['SpoolTypeSupplier'][] {
  return state.products
    .filter((product) => product.type_id === typeId && !product.archived_at && !supplierOf(product.supplier_id)?.archived_at)
    .map((product) => {
      const newest = productView(product).newest_price
      return {
        supplier_id: product.supplier_id,
        supplier_name: supplierOf(product.supplier_id)?.name ?? '',
        sku: product.sku,
        product_url: product.product_url,
        newest_price: newest?.price ?? null,
        price_per_kg: newest?.price_per_kg ?? null,
      }
    })
    .sort((a, b) => a.supplier_name.localeCompare(b.supplier_name))
}

/**
 * For the metrics mock (#6269, `metrics_store.purchasing`): each type's price per kg at each
 * supplier, one series per (type, supplier, currency), its points by date, then as made;
 * and the confirmed invoices' totals per supplier, month and currency, never converted.
 * From `since` to `until`, both included; archived suppliers and products count.
 */
export function purchasingMetrics(since: string, until: string): S['PurchasingMetrics'] {
  const series = new Map<string, S['PriceSeries']>()
  // Code-point order, as Python compares strings (the backend sorts in SQL and Python).
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)
  const points = state.prices
    .filter((price) => since <= price.on_date && price.on_date <= until)
    .sort((a, b) => cmp(a.on_date, b.on_date) || cmp(a.created_at, b.created_at) || cmp(a.id, b.id))
  for (const price of points) {
    const product = productOf(price.product_id)
    const type = product ? typeInfo(product.type_id) : null
    if (!product || !type) continue
    const key = `${product.type_id}|${product.supplier_id}|${price.currency}`
    const found = series.get(key) ?? {
      type_id: product.type_id,
      type_label: type.code,
      supplier_id: product.supplier_id,
      supplier_name: supplierOf(product.supplier_id)?.name ?? '',
      currency: price.currency,
      points: [],
    }
    found.points.push({ on_date: price.on_date, price_per_kg: pricePerKg(price.price, product.pack_size, type.label_weight_g) })
    series.set(key, found)
  }
  const spend = new Map<string, S['SpendRow']>()
  for (const invoice of state.invoices) {
    if (invoice.status !== 'confirmed' || invoice.invoice_date < since || invoice.invoice_date > until) continue
    const month = `${invoice.invoice_date.slice(0, 7)}-01`
    const key = `${invoice.supplier_id}|${month}|${invoice.currency}`
    const found = spend.get(key)
    if (found) spend.set(key, { ...found, total: addDecimals(found.total, invoice.total), invoices: found.invoices + 1 })
    else spend.set(key, { supplier_id: invoice.supplier_id, supplier_name: supplierOf(invoice.supplier_id)?.name ?? '', month, currency: invoice.currency, total: invoice.total, invoices: 1 })
  }
  const upper = (text: string) => text.toUpperCase()
  return {
    prices: [...series.values()].sort((a, b) => cmp(upper(a.type_label), upper(b.type_label)) || cmp(upper(a.supplier_name), upper(b.supplier_name)) || cmp(a.currency, b.currency)),
    spend: [...spend.values()].sort(
      (a, b) => cmp(a.month, b.month) || cmp(upper(a.supplier_name), upper(b.supplier_name)) || cmp(a.supplier_id, b.supplier_id) || cmp(a.currency, b.currency),
    ),
  }
}

// -- bodies -----------------------------------------------------------------------------------

const FIELDS = {
  supplier: ['name', 'website', 'customer_number', 'note'],
  product: ['type_id', 'sku', 'product_url', 'pack_size', 'note'],
  price: ['price', 'currency', 'on_date'],
  invoice: ['supplier_id', 'number', 'invoice_date', 'currency', 'subtotal', 'shipping', 'tax', 'total', 'note', 'import_run_id', 'lines'],
  line: ['description', 'sku', 'quantity', 'unit_price', 'line_subtotal', 'line_tax', 'colour_code', 'type_id', 'supplier_product_id', 'proposed_type', 'update_spool_ids'],
  shopping: ['type_id', 'material', 'subtype', 'brand', 'colour_name', 'quantity_spools', 'note'],
} as const

/** The 422 for a key outside `allowed` (every body model forbids extras), or `null`. */
function extraKeys(body: unknown, allowed: readonly string[]): Response | null {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) return problem(422, 'body: Input should be a valid dictionary')
  const extra = Object.keys(body).find((field) => !allowed.includes(field))
  return extra ? problem(422, `${extra}: Extra inputs are not permitted`) : null
}

/** A money field: a non-negative decimal of at most 12 digits, 4 of them after the point; the 422 text, or `null`. */
function badMoney(field: string, value: unknown): string | null {
  const text = typeof value === 'number' ? String(value) : value
  if (typeof text !== 'string' || !/^\d+(\.\d+)?$/.test(text)) return `${field}: Input should be a valid decimal`
  if ((text.split('.')[1] ?? '').length > UNIT) return `${field}: Decimal input should have no more than ${UNIT} decimal places`
  if (text.replace('.', '').replace(/^0+/, '').length > 12) return `${field}: Decimal input should have no more than 12 digits in total`
  return null
}

/** A URL a person typed: http or https only. */
const badUrl = (field: string, value: unknown): string | null =>
  value == null || (typeof value === 'string' && /^https?:\/\/\S+$/i.test(value) && value.length <= 500) ? null : `${field}: a URL must start with http:// or https://`

const text = (value: unknown) => (typeof value === 'string' ? value.trim() : '')
const isUuid = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
const sameName = (a: string, b: string) => a.trim().toUpperCase() === b.trim().toUpperCase()

const REFUSED = {
  supplierExists: 'a supplier with that name exists',
  productExists: 'the supplier has a product with that SKU',
  noType: 'no such spool type',
  invoiceRef: 'no such supplier, spool type or supplier product',
  priceFromInvoice: 'a price from an invoice goes with its invoice, not alone',
  confirmed: 'a confirmed invoice cannot be changed; edit its spools instead',
  noLines: 'an invoice with no lines cannot be confirmed',
  duplicateNumber: 'the supplier has an invoice with that number',
  supplierArchived: 'the supplier is archived: restore it or pick another',
} as const

// -- invoices: writes -------------------------------------------------------------------------

/** The refusal for a line body, or `null`. */
function lineRefusal(line: unknown): Response | null {
  const extra = extraKeys(line, FIELDS.line)
  if (extra) return extra
  const body = line as Record<string, unknown>
  if (!text(body.description)) return problem(422, 'description: String should have at least 1 character')
  if (!Number.isInteger(body.quantity) || (body.quantity as number) < 1 || (body.quantity as number) > 1_000) return problem(422, 'quantity: Input should be greater than 0')
  for (const field of ['unit_price', 'line_subtotal', 'line_tax'] as const) {
    if (body[field] == null && field !== 'unit_price') continue
    const bad = badMoney(field, body[field])
    if (bad) return problem(422, bad)
  }
  if (Array.isArray(body.update_spool_ids) && body.update_spool_ids.length > (body.quantity as number)) return problem(422, "update_spool_ids names more spools than the line's quantity")
  if (body.type_id != null && !typeInfo(String(body.type_id))) return problem(422, REFUSED.invoiceRef)
  if (body.supplier_product_id != null && !productOf(String(body.supplier_product_id))) return problem(422, REFUSED.invoiceRef)
  return null
}

function newLine(invoiceId: string, body: Record<string, unknown>, position: number): Line {
  const unitPrice = money(String(body.unit_price))
  const quantity = body.quantity as number
  const given = body.line_tax != null
  return {
    id: newId(),
    invoice_id: invoiceId,
    position,
    description: text(body.description),
    sku: text(body.sku) || null,
    quantity,
    unit_price: unitPrice,
    line_subtotal: money(body.line_subtotal != null ? String(body.line_subtotal) : mulDecimal(unitPrice, quantity)),
    line_tax: money(given ? String(body.line_tax) : '0'),
    line_total: money('0'),
    allocated_cost: money('0'),
    tax_basis: given ? 'line' : 'none',
    colour_code: typeof body.colour_code === 'string' ? body.colour_code : null,
    type_id: (body.type_id as string | null | undefined) ?? null,
    supplier_product_id: (body.supplier_product_id as string | null | undefined) ?? null,
    proposed_type: (body.proposed_type ?? null) as Line['proposed_type'],
    update_spool_ids: ((body.update_spool_ids as string[] | undefined) ?? []),
    spool_ids: [],
    created_at: now(),
    updated_at: now(),
  }
}

/** The invoice that already has `number` at the supplier (another one than `self`), as the 409. */
function numberTaken(supplierId: string, number: unknown, self?: string): Response | null {
  const taken = typeof number === 'string' && number ? state.invoices.find((one) => one.id !== self && one.supplier_id === supplierId && one.number === number) : undefined
  return taken ? problem(409, REFUSED.duplicateNumber, { existing_invoice_id: taken.id }) : null
}

/** Runs `recompute`, turning its refusal into the route's 422. */
function worked(invoice: Stored): Response | null {
  try {
    recompute(invoice)
    return null
  } catch (error) {
    if (error instanceof MoneyError) return problem(422, error.message)
    throw error
  }
}

/** The 422 for the header fields a body gives (`required` on a create). */
function headerRefusal(body: Record<string, unknown>, required: boolean): Response | null {
  if (required) {
    for (const field of ['supplier_id', 'invoice_date', 'currency', 'subtotal', 'total']) if (body[field] == null) return problem(422, `${field}: Field required`)
  }
  for (const field of ['subtotal', 'shipping', 'tax', 'total'] as const) {
    if (body[field] === undefined || body[field] === null) continue
    const bad = badMoney(field, body[field])
    if (bad) return problem(422, bad)
  }
  if (body.currency !== undefined && !/^[A-Z]{3}$/.test(String(body.currency))) return problem(422, 'currency: String should match pattern')
  if (body.invoice_date !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(String(body.invoice_date))) return problem(422, 'invoice_date: Input should be a valid date')
  return null
}

const HEADER_MONEY = ['subtotal', 'shipping', 'tax', 'total'] as const

async function confirmRefusal(invoice: Stored): Promise<Response | null> {
  if (!invoice.lines.length) return problem(409, REFUSED.noLines)
  if (supplierOf(invoice.supplier_id)?.archived_at) return problem(409, 'the supplier is archived: restore it or move the invoice to another')
  if (invoice.currency !== SHOP_CURRENCY) {
    return problem(409, `the invoice is in ${invoice.currency} but the shop's currency is ${SHOP_CURRENCY}: change the shop's currency or retype the invoice`)
  }
  const diff = difference(invoice)
  if (cmpDecimals(diff.replace('-', ''), TOLERANCE) > 0) {
    return problem(409, `the total is ${diff} away from the lines plus shipping; confirm needs it within ${TOLERANCE}`)
  }
  const codes = new Set<string>()
  const claimed = new Set<string>()
  for (const line of invoice.lines) {
    const fail = (reason: string) => problem(409, `line ${line.position}: ${reason}`)
    if (line.type_id) {
      if (!typeInfo(line.type_id)) return fail('its spool type no longer exists')
    } else {
      if (!line.proposed_type) return fail('it has no spool type and proposes none')
      const code = String((line.proposed_type as { code?: unknown }).code ?? '')
      if (!codes.has(code.toUpperCase())) {
        if (typeCodeTaken(code)) return fail(`a spool type like '${code}' already exists; choose it instead`)
        const invalid = checkTypeProposal(line.proposed_type)
        if (invalid) return fail(`its proposed spool type is invalid (${((await invalid.json()) as { detail: string }).detail})`)
      }
      codes.add(code.toUpperCase())
    }
    // `_line_product`: the product the line names (or its SKU finds) must be this supplier's, live, and for the line's type.
    const named = line.supplier_product_id ? productOf(line.supplier_product_id) : undefined
    if (line.supplier_product_id && (!named || named.supplier_id !== invoice.supplier_id || named.archived_at)) return fail("its supplier product is archived or not this supplier's")
    const sku = line.sku || line.description
    const product = named ?? (line.supplier_product_id ? undefined : state.products.find((one) => one.supplier_id === invoice.supplier_id && !one.archived_at && sameName(one.sku, sku)))
    if (product && product.type_id !== line.type_id) return fail('its supplier product is for another spool type')
    // Decision 11: the spools the line fills in are all checked before anything is written.
    const ids = line.update_spool_ids
    if (ids.length > line.quantity) return fail(`it fills ${ids.length} spools but its quantity is ${line.quantity}`)
    if (new Set(ids).size !== ids.length) return fail('it names a spool twice')
    for (const id of ids) {
      const reason = claimed.has(id) ? 'already belongs to an invoice line' : spoolClaimRefusal(id, line.type_id)
      if (reason) return fail(`spool ${id} ${reason}`)
    }
    for (const id of ids) claimed.add(id)
  }
  return null
}

/** Confirm: types, products, spools with their cost and one price point per line, after every refusal is checked. */
async function confirm(invoice: Stored): Promise<Response | null> {
  const refused = await confirmRefusal(invoice)
  if (refused) return refused
  const made = new Map<string, string>()
  for (const line of invoice.lines) {
    if (!line.type_id) {
      const proposed = line.proposed_type as unknown as Record<string, unknown>
      const key = String(proposed.code).toUpperCase()
      if (!made.has(key)) {
        const type = createType(proposed)
        if (type instanceof Response) return type
        made.set(key, type.id)
      }
      line.type_id = made.get(key)!
    }
    const info = typeInfo(line.type_id)!
    let product = line.supplier_product_id ? productOf(line.supplier_product_id) : undefined
    const sku = line.sku || line.description
    product ??= state.products.find((one) => one.supplier_id === invoice.supplier_id && !one.archived_at && sameName(one.sku, sku))
    if (!product) {
      product = { id: newId(), supplier_id: invoice.supplier_id, type_id: line.type_id, sku, product_url: null, pack_size: 1, note: null, archived_at: null, created_at: now(), updated_at: now() }
      state.products.push(product)
    }
    line.supplier_product_id = product.id
    const filled = line.update_spool_ids
    for (const id of filled) updateInvoicedSpool(id, line.type_id, costPerKg(line, spoolLabelWeight(id) || info.label_weight_g), line.id)
    line.spool_ids = [...filled, ...addInvoicedSpools(line.type_id, line.quantity - filled.length, costPerKg(line, info.label_weight_g), line.id)]
    state.prices.push(invoicePrice(invoice, line, state.products))
  }
  invoice.status = 'confirmed'
  invoice.confirmed_at = now()
  invoice.updated_at = now()
  return null
}

const AT = '2026-11-15T07:00:00Z'

let state = build()

export function reset() {
  state = build()
}

// -- the routes -------------------------------------------------------------------------------

/** A 1 x 1 PNG: the bytes a photo's file answers with. */
const PNG = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='), (c) => c.charCodeAt(0))

const flag = (request: Request, name: string) => new URL(request.url).searchParams.get(name) === 'true'

const itemsOrdered = () => {
  const rows = state.shopping.map((item, at) => ({ item, at }))
  return rows.sort((a, b) => Number(a.item.status === 'purchased') - Number(b.item.status === 'purchased') || b.at - a.at).map(({ item }) => item)
}

export const handlers = [
  // -- suppliers
  http.get(`${BASE}/suppliers`, ({ request }) => {
    const all = flag(request, 'include_archived')
    return HttpResponse.json(state.suppliers.filter((row) => all || !row.archived_at).sort((a, b) => a.name.localeCompare(b.name)).map(supplierView))
  }),
  http.post(`${BASE}/suppliers`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.supplier)
    if (extra) return extra
    if (!text(body.name)) return problem(422, 'name: String should have at least 1 character')
    const bad = badUrl('website', body.website)
    if (bad) return problem(422, bad)
    if (state.suppliers.some((row) => !row.archived_at && sameName(row.name, text(body.name)))) return problem(409, REFUSED.supplierExists)
    const made: Supplier = {
      id: newId(),
      name: text(body.name),
      website: (body.website as string | null | undefined) ?? null,
      customer_number: text(body.customer_number) || null,
      note: text(body.note) || null,
      archived_at: null,
      created_at: now(),
      updated_at: now(),
    }
    state.suppliers.push(made)
    return HttpResponse.json(supplierView(made), { status: 201 })
  }),
  http.get(`${BASE}/suppliers/stats`, () => {
    const rows = state.suppliers.flatMap((supplier) => {
      const confirmed = state.invoices.filter((invoice) => invoice.supplier_id === supplier.id && invoice.status === 'confirmed')
      if (!confirmed.length) return []
      const lines = confirmed.flatMap((invoice) => invoice.lines)
      const usage = spoolUsage(lines.flatMap((line) => line.spool_ids))
      return [
        {
          supplier_id: supplier.id,
          supplier_name: supplier.name,
          spool_count: usage.count,
          remaining_g: usage.remaining_g,
          consumed_g: usage.consumed_g,
          spend: lines.map((line) => line.allocated_cost).reduce(addDecimals, money('0')),
          archived: !!supplier.archived_at,
        } satisfies S['SupplierStats'],
      ]
    })
    return HttpResponse.json(rows.sort((a, b) => b.consumed_g - a.consumed_g || a.supplier_name.localeCompare(b.supplier_name)))
  }),
  http.get(`${BASE}/suppliers/:id`, ({ params }) => {
    const row = supplierOf(String(params.id))
    return row ? HttpResponse.json(supplierView(row)) : problem(404, `no supplier ${String(params.id)}`)
  }),
  http.patch(`${BASE}/suppliers/:id`, async ({ params, request }) => {
    const row = supplierOf(String(params.id))
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, [...FIELDS.supplier, 'archived'])
    if (extra) return extra
    if (!row) return problem(404, `no supplier ${String(params.id)}`)
    if ('name' in body && !text(body.name)) return problem(422, 'name cannot be cleared')
    if ('archived' in body && typeof body.archived !== 'boolean') return problem(422, 'archived cannot be cleared')
    const bad = badUrl('website', body.website)
    if (bad) return problem(422, bad)
    const { archived, ...rest } = body
    const next: Supplier = { ...row, ...rest, updated_at: now() } as Supplier
    if ('name' in body) next.name = text(body.name)
    if (archived === true && !row.archived_at) next.archived_at = now()
    if (archived === false) next.archived_at = null
    if (!next.archived_at && state.suppliers.some((one) => one.id !== row.id && !one.archived_at && sameName(one.name, next.name))) return problem(409, REFUSED.supplierExists)
    state.suppliers[state.suppliers.indexOf(row)] = next
    return HttpResponse.json(supplierView(next))
  }),
  http.get(`${BASE}/suppliers/:id/price-sheet`, ({ params }) => {
    if (!supplierOf(String(params.id))) return problem(404, `no supplier ${String(params.id)}`)
    const rows = state.products
      .filter((product) => product.supplier_id === params.id && !product.archived_at)
      .map((product): S['PriceSheetRow'] => {
        const [newest, previous] = pricesOf(product.id).map(pricePoint)
        const info = typeInfo(product.type_id)
        const [a, b] = [newest?.price_per_kg, previous?.price_per_kg]
        const change = a && b && cmpDecimals(b, '0') > 0 ? divDecimal(mulDecimal(subDecimals(a, b), 100), b, 1) : null
        return { product: productView(product), type_code: info?.code ?? '', type_label: info?.label ?? '', newest: newest ?? null, previous: previous ?? null, change_pct: change }
      })
    return HttpResponse.json(rows.sort((a, b) => a.type_code.localeCompare(b.type_code)))
  }),
  // -- products and prices
  http.get(`${BASE}/suppliers/:id/products`, ({ params, request }) => {
    if (!supplierOf(String(params.id))) return problem(404, `no supplier ${String(params.id)}`)
    const all = flag(request, 'include_archived')
    return HttpResponse.json(state.products.filter((row) => row.supplier_id === params.id && (all || !row.archived_at)).sort((a, b) => a.sku.localeCompare(b.sku)).map(productView))
  }),
  http.post(`${BASE}/suppliers/:id/products`, async ({ params, request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.product)
    if (extra) return extra
    if (!isUuid(body.type_id)) return problem(422, 'type_id: Input should be a valid UUID')
    if (!text(body.sku)) return problem(422, 'sku: String should have at least 1 character')
    const size = body.pack_size ?? 1
    if (!Number.isInteger(size) || (size as number) < 1 || (size as number) > 1000) return problem(422, 'pack_size: Input should be greater than 0')
    const bad = badUrl('product_url', body.product_url)
    if (bad) return problem(422, bad)
    if (!supplierOf(String(params.id))) return problem(404, `no supplier ${String(params.id)}`)
    if (!typeInfo(body.type_id)) return problem(422, REFUSED.noType)
    if (state.products.some((row) => row.supplier_id === params.id && !row.archived_at && sameName(row.sku, text(body.sku)))) return problem(409, REFUSED.productExists)
    const made: Product = {
      id: newId(),
      supplier_id: String(params.id),
      type_id: body.type_id,
      sku: text(body.sku),
      product_url: (body.product_url as string | null | undefined) ?? null,
      pack_size: size as number,
      note: text(body.note) || null,
      archived_at: null,
      created_at: now(),
      updated_at: now(),
    }
    state.products.push(made)
    return HttpResponse.json(productView(made), { status: 201 })
  }),
  http.patch(`${BASE}/products/:id`, async ({ params, request }) => {
    const row = productOf(String(params.id))
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, [...FIELDS.product, 'archived'])
    if (extra) return extra
    if (!row) return problem(404, `no supplier product ${String(params.id)}`)
    for (const field of ['type_id', 'sku', 'pack_size', 'archived']) if (field in body && body[field] === null) return problem(422, `${field} cannot be cleared`)
    const bad = badUrl('product_url', body.product_url)
    if (bad) return problem(422, bad)
    if (body.type_id != null && !typeInfo(String(body.type_id))) return problem(422, REFUSED.noType)
    const { archived, ...rest } = body
    const next = { ...row, ...rest, updated_at: now() } as Product
    if (archived === true && !row.archived_at) next.archived_at = now()
    if (archived === false) next.archived_at = null
    if (!next.archived_at && state.products.some((one) => one.id !== row.id && one.supplier_id === row.supplier_id && !one.archived_at && sameName(one.sku, next.sku))) return problem(409, REFUSED.productExists)
    state.products[state.products.indexOf(row)] = next
    return HttpResponse.json(productView(next))
  }),
  http.get(`${BASE}/products/:id/prices`, ({ params }) =>
    productOf(String(params.id)) ? HttpResponse.json(pricesOf(String(params.id)).map(pricePoint)) : problem(404, `no supplier product ${String(params.id)}`),
  ),
  http.post(`${BASE}/products/:id/prices`, async ({ params, request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.price)
    if (extra) return extra
    if (body.price == null) return problem(422, 'price: Field required')
    const bad = badMoney('price', body.price)
    if (bad) return problem(422, bad)
    if (body.currency != null && !/^[A-Z]{3}$/.test(String(body.currency))) return problem(422, 'currency: String should match pattern')
    if (body.on_date != null && !/^\d{4}-\d{2}-\d{2}$/.test(String(body.on_date))) return problem(422, 'on_date: Input should be a valid date')
    if (!productOf(String(params.id))) return problem(404, `no supplier product ${String(params.id)}`)
    const made: Price = {
      id: newId(),
      product_id: String(params.id),
      price: money(String(body.price)),
      currency: (body.currency as string | null | undefined) ?? SHOP_CURRENCY,
      on_date: (body.on_date as string | null | undefined) ?? today(),
      source: 'manual',
      invoice_line_id: null,
      created_at: now(),
      updated_at: now(),
    }
    state.prices.push(made)
    return HttpResponse.json(pricePoint(made), { status: 201 })
  }),
  http.delete(`${BASE}/prices/:id`, ({ params }) => {
    const row = state.prices.find((price) => price.id === params.id)
    if (!row) return problem(404, `no price point ${String(params.id)}`)
    if (row.source === 'invoice') return problem(409, REFUSED.priceFromInvoice)
    state.prices = state.prices.filter((price) => price !== row)
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${BASE}/types/:id/prices`, ({ params }) => {
    if (!typeInfo(String(params.id))) return problem(404, `no spool type ${String(params.id)}`)
    const rows = state.products
      .filter((product) => product.type_id === params.id)
      .flatMap((product) => {
        const supplier = supplierOf(product.supplier_id)
        return pricesOf(product.id).map(
          (price): S['TypePricePoint'] => ({
            ...pricePoint(price),
            supplier_id: product.supplier_id,
            supplier_name: supplier?.name ?? '',
            sku: product.sku,
            supplier_archived: !!supplier?.archived_at,
            product_archived: !!product.archived_at,
          }),
        )
      })
    return HttpResponse.json(rows.sort((a, b) => b.on_date.localeCompare(a.on_date)))
  }),
  // -- invoices
  http.get(`${BASE}/invoices`, ({ request }) => {
    const url = new URL(request.url)
    const supplier = url.searchParams.get('supplier_id')
    const status = url.searchParams.get('status')
    if (status && status !== 'draft' && status !== 'confirmed') return problem(422, 'status: Input should be \'draft\' or \'confirmed\'')
    const rows = state.invoices.filter((row) => (!supplier || row.supplier_id === supplier) && (!status || row.status === status))
    return HttpResponse.json(rows.sort((a, b) => b.invoice_date.localeCompare(a.invoice_date) || b.created_at.localeCompare(a.created_at)).map(invoiceView))
  }),
  http.post(`${BASE}/invoices`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.invoice)
    if (extra) return extra
    const refused = headerRefusal(body, true)
    if (refused) return refused
    const lines = (body.lines ?? []) as unknown[]
    if (!Array.isArray(lines)) return problem(422, 'lines: Input should be a valid list')
    for (const line of lines) {
      const bad = lineRefusal(line)
      if (bad) return bad
    }
    const runId = text(body.import_run_id) || null
    const repeat = runId ? state.invoices.find((one) => one.import_run_id === runId) : undefined
    if (repeat) return HttpResponse.json(detailView(repeat), { status: 200 })
    const supplier = supplierOf(String(body.supplier_id))
    if (!supplier) return problem(422, REFUSED.invoiceRef)
    if (supplier.archived_at) return problem(422, REFUSED.supplierArchived)
    const taken = numberTaken(String(body.supplier_id), text(body.number))
    if (taken) return taken
    const id = newId()
    const made: Stored = {
      id,
      supplier_id: String(body.supplier_id),
      number: text(body.number) || null,
      invoice_date: String(body.invoice_date),
      currency: String(body.currency),
      subtotal: money(String(body.subtotal)),
      shipping: money(String(body.shipping ?? '0')),
      tax: money(String(body.tax ?? '0')),
      total: money(String(body.total)),
      status: 'draft',
      import_run_id: runId,
      confirmed_at: null,
      note: text(body.note) || null,
      file: null,
      created_at: now(),
      updated_at: now(),
      lines: (lines as Record<string, unknown>[]).map((line, at) => newLine(id, line, at + 1)),
    }
    const failed = worked(made)
    if (failed) return failed
    state.invoices.push(made)
    return HttpResponse.json(detailView(made), { status: 201 })
  }),
  http.get(`${BASE}/invoices/:id`, ({ params }) => {
    const row = invoiceOf(String(params.id))
    return row ? HttpResponse.json(detailView(row)) : problem(404, `no invoice ${String(params.id)}`)
  }),
  http.patch(`${BASE}/invoices/:id`, async ({ params, request }) => {
    const row = invoiceOf(String(params.id))
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.invoice.filter((field) => field !== 'lines' && field !== 'import_run_id'))
    if (extra) return extra
    for (const field of ['supplier_id', 'invoice_date', 'currency', 'subtotal', 'shipping', 'tax', 'total']) if (field in body && body[field] === null) return problem(422, `${field} cannot be cleared`)
    if ('supplier_id' in body && !isUuid(body.supplier_id)) return problem(422, 'supplier_id: Input should be a valid UUID')
    const refused = headerRefusal(body, false)
    if (refused) return refused
    if (!row) return problem(404, `no invoice ${String(params.id)}`)
    if (row.status === 'confirmed' && Object.keys(body).some((field) => field !== 'note')) return problem(409, REFUSED.confirmed)
    const supplier = typeof body.supplier_id === 'string' ? supplierOf(body.supplier_id) : undefined
    if (typeof body.supplier_id === 'string' && !supplier) return problem(422, REFUSED.invoiceRef)
    if (supplier?.archived_at) return problem(422, REFUSED.supplierArchived)
    const supplierId = supplier?.id ?? row.supplier_id
    const number = 'number' in body ? text(body.number) : row.number
    const taken = 'number' in body || supplierId !== row.supplier_id ? numberTaken(supplierId, number ?? '', row.id) : null
    if (taken) return taken
    const next: Stored = { ...row, lines: row.lines.map((line) => ({ ...line })), updated_at: now() }
    // A move drops the lines' products of the old supplier (the backend's `_move`).
    if (supplierId !== row.supplier_id) for (const line of next.lines) line.supplier_product_id = null
    next.supplier_id = supplierId
    for (const field of ['invoice_date', 'currency'] as const) if (field in body) next[field] = String(body[field])
    for (const field of HEADER_MONEY) if (field in body) next[field] = money(String(body[field]))
    if ('number' in body) next.number = text(body.number) || null
    if ('note' in body) next.note = text(body.note) || null
    const failed = worked(next)
    if (failed) return failed
    state.invoices[state.invoices.indexOf(row)] = next
    return HttpResponse.json(detailView(next))
  }),
  http.put(`${BASE}/invoices/:id/lines`, async ({ params, request }) => {
    const row = invoiceOf(String(params.id))
    const body = await request.json()
    if (!Array.isArray(body)) return problem(422, 'body: Input should be a valid list')
    for (const line of body) {
      const bad = lineRefusal(line)
      if (bad) return bad
    }
    if (!row) return problem(404, `no invoice ${String(params.id)}`)
    if (row.status === 'confirmed') return problem(409, REFUSED.confirmed)
    const next: Stored = { ...row, lines: (body as Record<string, unknown>[]).map((line, at) => newLine(row.id, line, at + 1)), updated_at: now() }
    const failed = worked(next)
    if (failed) return failed
    state.invoices[state.invoices.indexOf(row)] = next
    return HttpResponse.json(detailView(next))
  }),
  http.delete(`${BASE}/invoices/:id`, ({ params }) => {
    const row = invoiceOf(String(params.id))
    if (!row) return problem(404, `no invoice ${String(params.id)}`)
    if (row.status === 'confirmed') return problem(409, 'a confirmed invoice cannot be changed; edit its spools instead')
    state.invoices = state.invoices.filter((one) => one !== row)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post(`${BASE}/invoices/:id/confirm`, async ({ params }) => {
    const row = invoiceOf(String(params.id))
    if (!row) return problem(404, `no invoice ${String(params.id)}`)
    if (row.status === 'draft') {
      const refused = await confirm(row)
      if (refused) return refused
    }
    return HttpResponse.json(detailView(row))
  }),
  http.post(`${BASE}/invoices/:id/file`, async ({ params, request }) => {
    const row = invoiceOf(String(params.id))
    if (!row) return problem(404, `no invoice ${String(params.id)}`)
    // Read as text, not `formData()`: undici's parser refuses the body jsdom's XHR sends
    // (`printAttachments.ts`). The part's type decides, its bytes give the size.
    const raw = await request.text()
    const part = /name="file"; filename="([^"]*)"\r\nContent-Type: ([^\r]+)\r\n\r\n([\s\S]*)\r\n--/.exec(raw)
    if (!part || part[3]!.length === 0) return problem(422, 'the upload has no `file` part, or it is empty')
    const type = part[2]!
    if (type !== 'application/pdf' && !type.startsWith('image/')) return problem(422, "an invoice's file is a PDF or a photo")
    row.file = { name: part[1] || 'invoice', media_type: type, size: new TextEncoder().encode(part[3]!).length }
    row.updated_at = now()
    return HttpResponse.json(row.file)
  }),
  http.get(`${BASE}/invoices/:id/file`, ({ params }) => {
    const row = invoiceOf(String(params.id))
    if (!row?.file) return problem(404, `invoice ${String(params.id)} has no file`)
    const bytes = row.file.media_type.startsWith('image/') ? PNG : new TextEncoder().encode('%PDF-1.4\n% mock invoice\n')
    return new HttpResponse(bytes, {
      headers: { 'Content-Type': row.file.media_type, 'Content-Disposition': `inline; filename="${row.file.name}"`, 'Cache-Control': 'private, no-cache' },
    })
  }),
  // #6267 — the agent's start route (`agent/src/routes/invoiceImport.ts`): its refusals, its answers, and the link it writes.
  http.post('/api/v1/ai/filament/invoice-import', async ({ request }) => {
    const { invoice_id } = (await request.json()) as { invoice_id: string }
    const row = invoiceOf(invoice_id)
    const refuse = (status: number, body: Record<string, unknown>) => HttpResponse.json(body, { status })
    if (!row) return refuse(404, { detail: 'the backend refused to read the invoice', type: 'backend_refused' })
    if (row.status === 'confirmed') return refuse(409, { detail: 'This invoice is confirmed. There is nothing left to import.', type: 'confirmed' })
    if (!row.file) return refuse(422, { detail: 'This invoice has no file. Add its PDF or photo first, then import it.' })
    const kind = row.file.media_type.toLowerCase()
    if (kind !== 'application/pdf' && !/^image\/(png|jpeg|gif|webp)$/.test(kind)) {
      return refuse(422, { detail: `This file is a ${kind}. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.` })
    }
    if (row.import_run_id && isSessionLive(row.import_run_id)) {
      return refuse(409, { detail: 'This invoice is already being imported. Open its session.', type: 'import_running', session_id: row.import_run_id })
    }
    row.import_run_id = startImportSession(`Import invoice ${row.number ?? row.file.name}`)
    row.updated_at = now()
    return HttpResponse.json({ session_id: row.import_run_id })
  }),
  // -- the shopping list
  http.get(`${BASE}/shopping-list`, ({ request }) => {
    const status = new URL(request.url).searchParams.get('status')
    if (status && status !== 'pending' && status !== 'purchased') return problem(422, "status: Input should be 'pending' or 'purchased'")
    return HttpResponse.json(itemsOrdered().filter((item) => !status || item.status === status))
  }),
  http.post(`${BASE}/shopping-list`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, FIELDS.shopping)
    if (extra) return extra
    const quantity = body.quantity_spools ?? 1
    if (!Number.isInteger(quantity) || (quantity as number) < 1 || (quantity as number) > 1000) return problem(422, 'quantity_spools: Input should be greater than 0')
    if (body.type_id == null && !text(body.material)) return problem(422, 'material is required when no type_id is given')
    const type = body.type_id == null ? null : typeInfo(String(body.type_id))
    if (body.type_id != null && !type) return problem(422, REFUSED.noType)
    const made: Item = {
      id: newId(),
      type_id: (body.type_id as string | null | undefined) ?? null,
      material: text(body.material) || type?.material || '',
      subtype: text(body.subtype) || type?.subtype || null,
      brand: text(body.brand) || type?.brand || null,
      colour_name: text(body.colour_name) || type?.colour_name || null,
      quantity_spools: quantity as number,
      note: text(body.note) || null,
      status: 'pending',
      purchased_at: null,
      // #6868: a person's item; only an empty spool's move into empty names one.
      empty_spool_id: null,
      created_at: now(),
      updated_at: now(),
    }
    state.shopping.push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.delete(`${BASE}/shopping-list`, ({ request }) => {
    const only = flag(request, 'purchased_only')
    const before = state.shopping.length
    state.shopping = state.shopping.filter((item) => only && item.status !== 'purchased')
    return HttpResponse.json({ removed: before - state.shopping.length } satisfies S['ShoppingRemoved'])
  }),
  http.patch(`${BASE}/shopping-list/:id`, async ({ params, request }) => {
    const row = state.shopping.find((item) => item.id === params.id)
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, ['quantity_spools', 'note', 'status'])
    if (extra) return extra
    if (body.quantity_spools === null || body.status === null) return problem(422, 'quantity_spools and status cannot be cleared')
    if ('quantity_spools' in body && (!Number.isInteger(body.quantity_spools) || (body.quantity_spools as number) < 1 || (body.quantity_spools as number) > 1000)) return problem(422, 'quantity_spools: Input should be greater than 0')
    if ('status' in body && body.status !== 'pending' && body.status !== 'purchased') return problem(422, "status: Input should be 'pending' or 'purchased'")
    if (!row) return problem(404, `no shopping list item ${String(params.id)}`)
    const next: Item = { ...row, updated_at: now() }
    if ('quantity_spools' in body) next.quantity_spools = body.quantity_spools as number
    if ('note' in body) next.note = text(body.note) || null
    if ('status' in body) {
      next.status = body.status as Item['status']
      next.purchased_at = next.status === 'purchased' ? now() : null
    }
    state.shopping[state.shopping.indexOf(row)] = next
    return HttpResponse.json(next)
  }),
  http.delete(`${BASE}/shopping-list/:id`, ({ params }) => {
    const row = state.shopping.find((item) => item.id === params.id)
    if (!row) return problem(404, `no shopping list item ${String(params.id)}`)
    state.shopping = state.shopping.filter((item) => item !== row)
    return new HttpResponse(null, { status: 204 })
  }),
]

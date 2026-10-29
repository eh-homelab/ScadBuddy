import { http, HttpResponse } from 'msw'
import type { Filament, FilamentInput, FilamentPatch, OrderEstimate, PolicyUpdate, PriceBook, PricingPolicy, Quote, QuoteLine, SliceQuoteRequest } from '../../../api/types'
import { mockSettings, setMockSettings } from '../../handlers'
import { acceptOperation } from '../operations'
import { FilamentsRefused, event, itemOf, lock, notEnded, OPEN_FOR_EDITS, quotesOf, rederive, refused, SETTLED, withFilaments } from './orders'
import { BROWSER, fieldProblem, minorExponent, newId, now, problem, PROBLEMS, Refused, state } from './state'

/**
 * Orders phase 2 (#4250, spec 2026-10-18 §5; backend `api/orders.py`): the price book (the
 * policy as the stored `pricing_*` settings, so the Settings page and the book agree, and the
 * filament records in the shared `state.ts`), the slice quote, which answers 202 with an
 * operation the mock settles into a `slice` quote with one filament line per colour slot
 * (`operations.ts` answers its follow), and the rough estimate.
 */

const base = '/api/v1/orders'
const DECIMAL = /^\d+(\.\d+)?$/
/** What the mock's slice reads off its one plate: an hour, and these grams per slot. */
export const MOCK_SLICE = { seconds: 3600, grams: [12.5, 3.0, 2.0, 1.0] }

function policy(): PricingPolicy {
  const settings = mockSettings()
  return {
    machine_rate_per_hour: settings.pricing_machine_rate_per_hour,
    setup_fee: settings.pricing_setup_fee,
    labour_per_item: settings.pricing_labour_per_item,
    markup_percent: settings.pricing_markup_percent,
    rounding: settings.pricing_rounding,
  }
}

function book(): PriceBook {
  const currency = mockSettings().orders_currency
  return {
    currency,
    minor_exponent: minorExponent(currency),
    policy: policy(),
    rough_estimate_factor: mockSettings().rough_estimate_factor,
    filaments: state.filaments.filter((f) => !f.archived_at),
  }
}

/** Half up, once, at the line (§3.6). */
const toMinor = (major: number, exponent: number) => Math.round(major * 10 ** exponent)

function identity(f: Pick<Filament, 'material' | 'subtype' | 'brand' | 'colour'>) {
  return [f.material, f.subtype ?? '', f.brand ?? '', f.colour].map((part) => part.toUpperCase()).join('|')
}

/** `orders/pricing.py` over the mock's one plate. */
function priced(colours: { slot: number; filament_id?: string | null }[], currency: string): Omit<Quote, 'id' | 'order_item_id' | 'created_at' | 'accepted' | 'quantity' | 'total_minor' | 'created_by'> {
  const exponent = minorExponent(currency)
  const rules = policy()
  const lines: QuoteLine[] = []
  const filaments = colours.map((colour, index) => {
    const record = state.filaments.find((f) => f.id === colour.filament_id)
    const grams = MOCK_SLICE.grams[index] ?? 1
    return { slot: colour.slot, grams, record }
  })
  for (const { grams, record } of filaments) {
    if (!record) continue
    const amount = toMinor((grams * Number(record.cost_per_kg)) / 1000, exponent)
    const name = [record.material, record.subtype, record.colour_name].filter(Boolean).join(' ')
    lines.push({ kind: 'filament', label: `${name}, ${grams} g at ${record.cost_per_kg} per kg`, quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  if (Number(rules.machine_rate_per_hour)) {
    const amount = toMinor((MOCK_SLICE.seconds / 3600) * Number(rules.machine_rate_per_hour), exponent)
    lines.push({ kind: 'machine_time', label: `Machine time, 1 h at ${rules.machine_rate_per_hour} per hour`, quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  const cost = lines.reduce((sum, line) => sum + line.amount_minor, 0)
  if (Number(rules.setup_fee)) {
    const amount = toMinor(Number(rules.setup_fee), exponent)
    lines.push({ kind: 'setup', label: 'Setup, plate 1', quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  if (Number(rules.labour_per_item)) {
    const amount = toMinor(Number(rules.labour_per_item), exponent)
    lines.push({ kind: 'labour', label: 'Labour', quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  if (Number(rules.markup_percent)) {
    const amount = Math.round((lines.reduce((sum, line) => sum + line.amount_minor, 0) * Number(rules.markup_percent)) / 100)
    lines.push({ kind: 'markup', label: `Markup, ${rules.markup_percent} %`, quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  let price = lines.reduce((sum, line) => sum + line.amount_minor, 0)
  if (rules.rounding === 'to_major') {
    const whole = Math.ceil(price / 10 ** exponent) * 10 ** exponent
    lines.push({ kind: 'rounding', label: 'Rounded up to a whole unit', quantity: 1, unit_minor: whole - price, amount_minor: whole - price })
    price = whole
  }
  const sha = 'b'.repeat(64)
  return {
    source: 'slice',
    sliced_sha256: sha,
    sliced_blob_key: `quote-${sha}`,
    plates: [
      {
        index: 1,
        print_seconds: MOCK_SLICE.seconds,
        filaments: filaments.map(({ slot, grams }) => ({ slot, used_g: String(grams) })),
        blob_key: `quote-${sha}`,
        sha256: sha,
      },
    ],
    lines,
    unit_cost_minor: cost,
    unit_price_minor: price,
  }
}

export const handlers = [
  http.get(`${base}/price-book`, () => HttpResponse.json(book())),
  http.put(`${base}/price-book`, async ({ request }) => {
    const body = (await request.json()) as Partial<PolicyUpdate> & Record<string, unknown>
    const allowed = ['machine_rate_per_hour', 'setup_fee', 'labour_per_item', 'markup_percent', 'rounding', 'rough_estimate_factor']
    for (const key of Object.keys(body)) if (!allowed.includes(key)) return fieldProblem(request, key, 'Extra inputs are not permitted')
    for (const key of allowed.slice(0, 4)) {
      const value = body[key]
      if (value !== undefined && !(typeof value === 'string' && DECIMAL.test(value.trim()))) return fieldProblem(request, key, `${key} must be at least 0`)
    }
    const factor = body.rough_estimate_factor
    if (factor !== undefined && factor !== null && !(DECIMAL.test(factor) && Number(factor) > 0)) {
      return fieldProblem(request, 'rough_estimate_factor', 'rough_estimate_factor must be more than 0')
    }
    setMockSettings({
      ...mockSettings(),
      pricing_machine_rate_per_hour: body.machine_rate_per_hour ?? '0',
      pricing_setup_fee: body.setup_fee ?? '0',
      pricing_labour_per_item: body.labour_per_item ?? '0',
      pricing_markup_percent: body.markup_percent ?? '0',
      pricing_rounding: body.rounding ?? 'to_minor_unit',
      ...(factor ? { rough_estimate_factor: factor } : {}),
    })
    return HttpResponse.json(book())
  }),
  http.post(`${base}/price-book/filaments`, async ({ request }) => {
    const body = (await request.json()) as FilamentInput
    if (!body.material) return fieldProblem(request, 'material', 'Field required')
    if (!/^#[0-9a-fA-F]{6}$/.test(body.colour ?? '')) return fieldProblem(request, 'colour', "String should match pattern '^#[0-9a-fA-F]{6}$'")
    if (!DECIMAL.test(body.cost_per_kg ?? '')) return fieldProblem(request, 'cost_per_kg', 'Value error, the value must be at least 0')
    const made: Filament = {
      id: newId('f'),
      material: body.material,
      subtype: body.subtype ?? null,
      brand: body.brand ?? null,
      colour_name: body.colour_name ?? null,
      colour: body.colour,
      cost_per_kg: body.cost_per_kg,
      density_g_cm3: body.density_g_cm3 ?? null,
      archived_at: null,
      created_at: now(),
      updated_at: now(),
    }
    const taken = state.filaments.find((f) => identity(f) === identity(made))
    if (taken) {
      return problem(request, 409, `a filament with that material, subtype, brand and colour exists: ${taken.id}`, PROBLEMS + 'filament_exists', { filament_id: taken.id })
    }
    state.filaments.push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.patch(`${base}/price-book/filaments/:id`, async ({ request, params }) => {
    const body = (await request.json()) as FilamentPatch
    const found = state.filaments.find((f) => f.id === params.id)
    if (!found) return problem(request, 404, `no filament '${String(params.id)}'`)
    if (body.cost_per_kg != null && !DECIMAL.test(body.cost_per_kg)) return fieldProblem(request, 'cost_per_kg', 'Value error, the value must be at least 0')
    const next = { ...found, ...Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined)), updated_at: now() } as Filament
    const taken = state.filaments.find((f) => f.id !== found.id && identity(f) === identity(next))
    if (taken) {
      return problem(request, 409, `a filament with that material, subtype, brand and colour exists: ${taken.id}`, PROBLEMS + 'filament_exists', { filament_id: taken.id })
    }
    Object.assign(found, next)
    return HttpResponse.json(found)
  }),
  http.post(`${base}/price-book/filaments/:id/archive`, ({ request, params }) => {
    const found = state.filaments.find((f) => f.id === params.id)
    if (!found) return problem(request, 404, `no filament '${String(params.id)}'`)
    found.archived_at ??= now()
    found.updated_at = now()
    return HttpResponse.json(found)
  }),

  http.post(`${base}/:id/items/:item/quote`, async ({ request, params }) => {
    if (!request.headers.get('Idempotency-Key')) {
      return problem(request, 428, 'This request needs an Idempotency-Key header', 'https://scadbuddy.dev/problems/idempotency-key-required')
    }
    const body = (await request.json()) as SliceQuoteRequest
    try {
      const order = lock(String(params.id))
      const found = itemOf(order, String(params.item))
      // The check (Ruling 8): the rows alone, nothing written.
      if (!OPEN_FOR_EDITS.has(order.status)) {
        throw new Refused('order_not_open', `order ${order.number} is ${order.status}: an item is quoted while it is draft or quoted`)
      }
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      // `quote.py` `reference_of` (#4954): a custom item has nothing to slice.
      if (found.reference === null) {
        throw new Refused('item_custom', `item ${found.id} is a custom item: it has no model to render or slice, so its price is typed (PATCH the item's unit_price_minor)`, [found.id])
      }
      const { colours, changed } = withFilaments(found, body.filaments ?? {})
      const empty = colours.find((c) => !c.filament_id)
      if (empty) throw new FilamentsRefused(`colour slot ${empty.slot} has no filament to price it with`)
      const rules = policy()
      const named = colours.map((c) => state.filaments.find((f) => f.id === c.filament_id))
      const nothing =
        [rules.machine_rate_per_hour, rules.setup_fee, rules.labour_per_item].every((rate) => Number(rate) === 0) &&
        named.every((f) => Number(f?.cost_per_kg ?? 0) === 0)
      if (nothing) {
        return problem(request, 422, 'the price book prices nothing: every rate is 0 and every filament this item names costs 0', PROBLEMS + 'price_book', {
          errors: [{ loc: ['body', 'price_book'], msg: 'the price book prices nothing' }],
        })
      }
      // The run: the filaments first (a change drops the accepted quote), then the quote.
      notEnded(order)
      if (changed) {
        found.colours = colours
        for (const q of quotesOf(found.id)) q.accepted = false
        found.status = 'draft'
        event(order, 'item.updated', { item_id: found.id, repinned: true })
      }
      const made: Quote = {
        id: newId('q'),
        order_item_id: found.id,
        created_at: now(),
        accepted: false,
        quantity: found.quantity,
        created_by: BROWSER,
        ...priced(found.colours, order.currency),
        total_minor: 0,
      }
      made.total_minor = made.unit_price_minor * made.quantity
      state.quotes.push(made)
      if (found.status === 'draft') found.status = 'quoted'
      found.updated_at = now()
      event(order, 'quote.recorded', { item_id: found.id, quote_id: made.id })
      rederive(order)
      return acceptOperation('order_item_quote', `order_item:${found.id}`, made as unknown as Record<string, unknown>)
    } catch (error) {
      return refused(request, error)
    }
  }),

  http.get(`${base}/estimate`, ({ request }) => {
    const url = new URL(request.url)
    const outputId = url.searchParams.get('output_id')
    const filamentId = url.searchParams.get('filament_id')
    if (!outputId || !filamentId) return fieldProblem(request, outputId ? 'filament_id' : 'output_id', 'Field required')
    const record = state.filaments.find((f) => f.id === filamentId)
    if (!record) return problem(request, 404, `no filament '${filamentId}'`)
    if (!record.density_g_cm3) {
      return problem(request, 422, `filament ${filamentId} has no density, so there is no estimate`, PROBLEMS + 'filament_density')
    }
    const currency = mockSettings().orders_currency
    const exponent = minorExponent(currency)
    const factor = mockSettings().rough_estimate_factor
    // One closed part of 8 cm3: the mock measures no 3MF.
    const grams = Math.round(8 * Number(record.density_g_cm3) * Number(factor) * 100) / 100
    const answer: OrderEstimate = {
      estimate: true,
      grams: grams.toFixed(2),
      filament_minor: toMinor((grams * Number(record.cost_per_kg)) / 1000, exponent),
      currency,
      minor_exponent: exponent,
      factor,
      parts: [{ plate: 1, part: 1, name: 'Color 1', colour: '#F9D72C', volume_mm3: 8000 }],
      unknown_parts: 0,
    }
    return HttpResponse.json(answer)
  }),
]

import { http, HttpResponse } from 'msw'
import type { ArchiveQuoteRequest, Filament, FilamentInput, FilamentPatch, OrderEstimate, PolicyUpdate, PriceBook, PricingPolicy, Quote, QuoteLine, SliceQuoteRequest, SpoolImportRow } from '../../../api/types'
import { mockSettings, setMockSettings } from '../../handlers'
import { SPOOL_INVENTORY } from '../spoolInventory'
import { acceptOperation } from '../operations'
import { FilamentsRefused, event, itemOf, lock, notEnded, quotesOf, rederive, requoteOpen, refused, SETTLED, TAKES_ITEMS, withFilaments } from './orders'
import { machineRateFor } from './machineRates'
import { pushRevision } from './revisions'
import { pastPrint } from './pastPrints'
import { retax } from './money'
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

function book(includeArchived = false): PriceBook {
  const currency = mockSettings().orders_currency
  return {
    currency,
    minor_exponent: minorExponent(currency),
    policy: policy(),
    rough_estimate_factor: mockSettings().rough_estimate_factor,
    filaments: state.filaments.filter((f) => includeArchived || !f.archived_at),
    spool_import: Boolean(mockSettings().bambuddy_url),
  }
}

/** `orders/spool_import.py` over the spool inventory's mock (#4253 §8.3). */
function importSpools(): SpoolImportRow[] {
  const groups = new Map<string, typeof SPOOL_INVENTORY>()
  for (const spool of SPOOL_INVENTORY) {
    if (spool.archived) continue
    const key = [spool.material, spool.subtype ?? '', spool.brand ?? '', spool.rgba ? spool.rgba.slice(0, 6) : `name:${spool.color_name ?? ''}`].map((p) => p.trim().toUpperCase()).join('|')
    groups.set(key, [...(groups.get(key) ?? []), spool])
  }
  return [...groups.keys()].sort().flatMap((key): SpoolImportRow[] => {
    const spools = [...(groups.get(key) ?? [])].sort((a, b) => a.id - b.id)
    const latest = spools.at(-1)
    if (!latest) return []
    const colour = latest.rgba && /^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(latest.rgba) ? `#${latest.rgba.slice(0, 6).toUpperCase()}` : null
    const priced = spools.filter((s) => s.cost_per_kg != null)
    const prices = [...new Set(priced.map((s) => s.cost_per_kg as number))]
    const row: SpoolImportRow = {
      action: 'skipped',
      material: latest.material,
      subtype: latest.subtype ?? null,
      brand: latest.brand ?? null,
      colour_name: latest.color_name ?? null,
      colour,
      spool_ids: spools.map((s) => s.id),
      min_cost_per_kg: prices.length > 1 ? String(Math.min(...prices)) : null,
      max_cost_per_kg: prices.length > 1 ? String(Math.max(...prices)) : null,
    }
    if (!colour) return [{ ...row, reason: 'no_colour' }]
    if (priced.length === 0) return [{ ...row, reason: 'no_price' }]
    const cost = String(priced.at(-1)?.cost_per_kg)
    const found = state.filaments.find((f) => !f.archived_at && identity(f) === identity({ material: row.material, subtype: row.subtype ?? null, brand: row.brand ?? null, colour }))
    if (!found) {
      const made: Filament = {
        id: newId('f'),
        material: row.material,
        subtype: row.subtype ?? null,
        brand: row.brand ?? null,
        colour_name: row.colour_name ?? null,
        colour,
        cost_per_kg: cost,
        density_g_cm3: null,
        archived_at: null,
        created_at: now(),
        updated_at: now(),
      }
      state.filaments.push(made)
      return [{ ...row, action: 'created', filament_id: made.id, cost_per_kg: cost }]
    }
    const previous = found.cost_per_kg
    const action = Number(previous) === Number(cost) ? 'unchanged' : 'updated'
    if (action === 'updated') Object.assign(found, { cost_per_kg: cost, updated_at: now() })
    return [{ ...row, action, filament_id: found.id, cost_per_kg: cost, previous_cost_per_kg: previous }]
  })
}

/** Half up, once, at the line (§3.6). */
const toMinor = (major: number, exponent: number) => Math.round(major * 10 ** exponent)

function identity(f: Pick<Filament, 'material' | 'subtype' | 'brand' | 'colour'>) {
  return [f.material, f.subtype ?? '', f.brand ?? '', f.colour].map((part) => part.toUpperCase()).join('|')
}

/**
 * `orders/pricing.py` over `plates`: each slot's grams at the filament of the item's colour
 * slot, the plates' seconds at the machine rate, a setup fee per plate. `support` prices a
 * slot that is no colour slot as its own `Support, …` filament line (#4625 §6.3), and
 * `machineRate` is the rate of the printer it sliced for, in place of the book's.
 */
export function pricePlates(
  plates: { index: number; print_seconds: number; filaments: { slot: number; used_g: number }[] }[],
  colours: { slot: number; filament_id?: string | null }[],
  currency: string,
  { support = {}, machineRate }: { support?: Record<string, string>; machineRate?: string } = {},
): Pick<Quote, 'lines' | 'unit_cost_minor' | 'unit_price_minor'> {
  const exponent = minorExponent(currency)
  const rules = machineRate === undefined ? policy() : { ...policy(), machine_rate_per_hour: machineRate }
  const lines: QuoteLine[] = []
  const grams = new Map<number, number>()
  for (const plate of plates) for (const f of plate.filaments) grams.set(f.slot, (grams.get(f.slot) ?? 0) + f.used_g)
  for (const [slot, used] of [...grams].sort((a, b) => a[0] - b[0])) {
    const colour = colours.find((c) => c.slot === slot)
    const record = state.filaments.find((f) => f.id === (colour ? colour.filament_id : support[String(slot)]))
    if (!record) continue
    const amount = toMinor((used * Number(record.cost_per_kg)) / 1000, exponent)
    const name = [record.material, record.subtype, record.colour_name].filter(Boolean).join(' ')
    const label = `${name}, ${used} g at ${record.cost_per_kg} per kg`
    lines.push({ kind: 'filament', label: colour ? label : `Support, ${label}`, quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  if (Number(rules.machine_rate_per_hour)) {
    const seconds = plates.reduce((sum, plate) => sum + plate.print_seconds, 0)
    const hours = Math.round((seconds / 3600) * 100) / 100
    const amount = toMinor((seconds / 3600) * Number(rules.machine_rate_per_hour), exponent)
    lines.push({ kind: 'machine_time', label: `Machine time, ${hours} h at ${rules.machine_rate_per_hour} per hour`, quantity: 1, unit_minor: amount, amount_minor: amount })
  }
  const cost = lines.reduce((sum, line) => sum + line.amount_minor, 0)
  if (Number(rules.setup_fee)) {
    const amount = toMinor(Number(rules.setup_fee), exponent)
    for (const plate of plates) lines.push({ kind: 'setup', label: `Setup, plate ${plate.index}`, quantity: 1, unit_minor: amount, amount_minor: amount })
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
  return { lines, unit_cost_minor: cost, unit_price_minor: price }
}

/** `orders/pricing.py` over the mock's one plate: each support slot uses the grams of
 * `MOCK_SLICE` at its own index, and the quote is priced at, and records, the machine rate of
 * the printer it names (#4625). */
function priced(
  colours: { slot: number; filament_id?: string | null }[],
  currency: string,
  request: Pick<SliceQuoteRequest, 'printer' | 'printer_id' | 'support_filaments'> = {},
): Omit<Quote, 'id' | 'order_item_id' | 'created_at' | 'accepted' | 'quantity' | 'total_minor' | 'created_by'> {
  const support = request.support_filaments ?? {}
  const filaments = [
    ...colours.map((colour, index) => ({ slot: colour.slot, used_g: MOCK_SLICE.grams[index] ?? 1 })),
    ...Object.keys(support).map((slot) => ({ slot: Number(slot), used_g: MOCK_SLICE.grams[Number(slot)] ?? 1 })),
  ]
  const rate = machineRateFor(request.printer, request.printer_id)
  const sha = 'b'.repeat(64)
  return {
    source: 'slice',
    sliced_sha256: sha,
    sliced_blob_key: `quote-${sha}`,
    plates: [
      {
        index: 1,
        print_seconds: MOCK_SLICE.seconds,
        filaments: filaments.map(({ slot, used_g }) => ({ slot, used_g: String(used_g) })),
        blob_key: `quote-${sha}`,
        sha256: sha,
      },
    ],
    ...pricePlates([{ index: 1, print_seconds: MOCK_SLICE.seconds, filaments }], colours, currency, {
      support,
      machineRate: rate?.source === 'profile' ? rate.rate_per_hour : undefined,
    }),
    printer: rate?.printer ?? null,
    machine_rate_breakdown: rate,
  }
}

/** A 1×1 grey PNG: every mocked plate image. */
const PLATE_PNG = Uint8Array.from(
  atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGO4BwAA6gDpRGJRjQAAAABJRU5ErkJggg=='),
  (c) => c.charCodeAt(0),
)

export const handlers = [
  http.get(`${base}/price-book`, ({ request }) => HttpResponse.json(book(new URL(request.url).searchParams.get('include_archived') === 'true'))),
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
  http.post(`${base}/price-book/import-spools`, ({ request }) => {
    if (!mockSettings().bambuddy_url) {
      return problem(request, 409, 'Bambuddy is not linked: set its URL in Settings to import spools', PROBLEMS + 'not_linked')
    }
    return HttpResponse.json({ rows: importSpools() })
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
    const taken = state.filaments.find((f) => !f.archived_at && identity(f) === identity(made))
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
    const taken = state.filaments.find((f) => f.id !== found.id && !f.archived_at && !next.archived_at && identity(f) === identity(next))
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
  http.post(`${base}/price-book/filaments/:id/unarchive`, ({ request, params }) => {
    const found = state.filaments.find((f) => f.id === params.id)
    if (!found) return problem(request, 404, `no filament '${String(params.id)}'`)
    const taken = state.filaments.find((f) => f.id !== found.id && !f.archived_at && identity(f) === identity(found))
    if (taken) {
      return problem(request, 409, `a filament with that material, subtype, brand and colour exists: ${taken.id}`, PROBLEMS + 'filament_exists', { filament_id: taken.id })
    }
    found.archived_at = null
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
      // #4673 PR 3 (`api/orders.py` `_slice_revision`): new inputs or a commit are a revision
      // of the item first, through the revise path (after acceptance for an accepted item).
      if ((body.inputs ?? null) !== null || (body.commit ?? null) !== null) {
        if (found.reference === null) throw new Refused('item_custom', `item ${found.id} has no design to slice with new inputs`, [found.id])
        const next = { ...found.reference, ...(body.commit ? { commit: body.commit } : {}), ...(body.inputs ? { inputs: body.inputs } : {}) }
        if (JSON.stringify(next) !== JSON.stringify(found.reference)) {
          notEnded(order)
          const after = TAKES_ITEMS.has(order.status) && quotesOf(found.id).some((q) => q.accepted) && !found.revised_after_acceptance
          pushRevision(order, found, next, found.colours, '', after)
        }
      }
      // The check (Ruling 8): the rows alone, nothing written.
      // `lifecycle.py` `requote_open` (#5349, #5350): past `quoted`, an item with no accepted
      // quote, or one revised after acceptance, is quoted too.
      if (!requoteOpen(order, found)) {
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
        // A slot's filament is the item's next revision (#5280), which drops the accepted quote.
        pushRevision(order, found, found.reference, colours, '')
        event(order, 'item.updated', { item_id: found.id, repinned: true })
      }
      const made: Quote = {
        id: newId('q'),
        order_item_id: found.id,
        revision_id: found.current_revision_id,
        created_at: now(),
        accepted: false,
        quantity: found.quantity,
        created_by: BROWSER,
        ...priced(found.colours, order.currency, body),
        // #4673 PR 1 (`quote.py` `print_setup`): the whole print setup, as recorded.
        choices: {
          printer_id: body.printer_id ?? null,
          printer: body.printer ?? null,
          path: body.path ?? null,
          choices: body.choices,
          prime_tower: body.prime_tower ?? null,
          supports: body.supports ?? null,
          print_sequence: body.print_sequence ?? null,
        },
        filament_plan: body.filament_plan as Quote['filament_plan'],
        total_minor: 0,
      }
      made.total_minor = made.unit_price_minor * made.quantity
      state.quotes.push(made)
      if (found.status === 'draft') found.status = 'quoted'
      found.updated_at = now()
      event(order, 'quote.recorded', { item_id: found.id, quote_id: made.id })
      retax(order.id)
      rederive(order)
      return acceptOperation('order_item_quote', `order_item:${found.id}`, made as unknown as Record<string, unknown>)
    } catch (error) {
      return refused(request, error)
    }
  }),

  // #4673 PR 4 — a slice quote's plate image, from its sliced file: one grey square here.
  http.get(`${base}/:id/items/:item/quotes/:quote/plates/:plate/thumbnail`, ({ request, params }) => {
    const quote = state.quotes.find((q) => q.id === params.quote && q.order_item_id === params.item)
    const plate = Number(params.plate)
    if (!quote?.sliced_blob_key || !(quote.plates as { index?: number }[]).some((p) => p.index === plate)) {
      return problem(request, 404, `quote ${String(params.quote)} has no image of plate ${plate}`)
    }
    return new HttpResponse(PLATE_PNG, { headers: { 'Content-Type': 'image/png' } })
  }),

  // #4777 — an `archive` quote (`orders/past_prints.py` `quote_from_archive`): every plate
  // archive of the past print's run, its grams per slot and actual seconds, no slice.
  http.post(`${base}/:id/items/:item/quotes/archive`, async ({ request, params }) => {
    const body = (await request.json()) as ArchiveQuoteRequest
    try {
      const order = lock(String(params.id))
      const found = itemOf(order, String(params.item))
      if (!requoteOpen(order, found)) {
        throw new Refused('order_not_open', `order ${order.number} is ${order.status}: an item is quoted while it is draft or quoted`)
      }
      if (SETTLED.has(found.status)) throw new Refused('item_not_open', `item ${found.id} is ${found.status}`)
      if (found.kind === 'custom') throw new Refused('item_custom', `item ${found.id} is a custom item: its price is its own field`, [found.id])
      const archiveId = body.archive_id ?? found.origin_archive_ids?.[0]
      if (archiveId == null) return fieldProblem(request, 'archive_id', `item ${found.id} was not taken from a past print: name its archive_id`)
      const picked = pastPrint(archiveId)
      // `past_prints.py` `_prints_item`: one the item was taken from, or one its output or file printed.
      const its =
        (found.origin_archive_ids ?? []).includes(archiveId) ||
        (picked?.output_id !== undefined && picked.output_id === found.output_id) ||
        (picked?.library_file_id !== undefined && picked.library_file_id === found.library_file?.id)
      if (!its) return fieldProblem(request, 'archive_id', `archive ${archiveId} is not a print of item ${found.id}`)
      if (!picked) return problem(request, 409, `Bambuddy has no archive ${archiveId} any more`, PROBLEMS + 'archive_gone', { archive_id: archiveId })
      const run = picked.run.map((id) => pastPrint(id))
      if (run.some((p) => !p)) return problem(request, 409, `archive ${archiveId}'s print run has plates with no archive`, PROBLEMS + 'archive_plates_missing', { archive_id: archiveId })
      const failed = run.find((p) => p?.status !== 'completed')
      if (failed) {
        return problem(request, 409, `archive ${failed.id} is ${failed.status ?? 'of unknown status'}: only a completed print is a quote source`, PROBLEMS + 'archive_not_succeeded', { archive_id: failed.id })
      }
      const used = run.flatMap((p) => p!.slots.filter((s) => s.used_g > 0).map((s) => String(s.slot_id - 1)))
      const filaments: Record<string, string> = { ...(body.filament_id ? Object.fromEntries(used.map((slot) => [slot, body.filament_id!])) : {}), ...(body.filaments ?? {}) }
      const plates = run.map((p) => ({
        index: p!.plate_id ?? 1,
        print_seconds: p!.actual_time_seconds ?? p!.print_time_seconds ?? 0,
        filaments: p!.slots.filter((s) => s.used_g > 0).map((s) => ({ slot: s.slot_id - 1, used_g: s.used_g })),
        archive_id: p!.id,
      }))
      notEnded(order)
      if (found.colours.length === 0) {
        // A library file item takes the slots the print used.
        const used = [...new Map(run.flatMap((p) => p!.slots).map((s) => [s.slot_id - 1, s.color])).entries()].sort((a, b) => a[0] - b[0])
        found.colours = used.map(([slot, colour]) => ({ slot, colour, filament_id: filaments[String(slot)] ?? null }))
      } else {
        found.colours = withFilaments(found, filaments).colours
      }
      const empty = found.colours.find((c) => !c.filament_id)
      if (empty) throw new FilamentsRefused(`colour slot ${empty.slot} has no filament to price it with`)
      const made: Quote = {
        id: newId('q'),
        order_item_id: found.id,
        revision_id: found.current_revision_id,
        created_at: now(),
        source: 'archive',
        accepted: false,
        sliced_sha256: null,
        sliced_blob_key: null,
        plates: plates.map((plate) => ({ ...plate, filaments: plate.filaments.map((f) => ({ slot: f.slot, used_g: String(f.used_g) })) })),
        quantity: found.quantity,
        created_by: BROWSER,
        ...pricePlates(plates, found.colours, order.currency),
        total_minor: 0,
      }
      made.total_minor = made.unit_price_minor * made.quantity
      state.quotes.push(made)
      if (found.status === 'draft') found.status = 'quoted'
      found.updated_at = now()
      event(order, 'quote.recorded', { item_id: found.id, quote_id: made.id })
      retax(order.id)
      rederive(order)
      return HttpResponse.json(made, { status: 201 })
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

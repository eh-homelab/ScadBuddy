import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import { DEFAULT_ORDERS_QUERY } from '../../../lib/ordersQuery'
import { resetMockState } from '../../handlers'
import { FILAMENT_PETG, FILAMENT_PLA, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, OUTPUT_REAGAN, PROBLEMS } from './state'

// #4250 — the price book, the slice quote and the estimate, answered as `api/orders.py`
// answers them, so the pages' paths are testable against the same shapes.

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)
const DIALOG = {
  printer_id: 1,
  filament_plan: { slots: [{ slot_id: 1, spool_id: 9 }, { slot_id: 2, spool_id: 5 }], force_colour_match: false },
  choices: { nozzles: [{ size: '0.4' as const, flow: 'standard' as const }], tier: 'standard' as const, bed_type: 'Textured PEI Plate' },
}

describe('price book mock (#4250)', () => {
  beforeEach(() => resetMockState())

  it('carries the minor unit on the book and on every order', async () => {
    const book = await api.priceBook()
    expect(book.currency).toBe('USD')
    expect(book.minor_exponent).toBe(2)
    expect(book.filaments.map((f) => f.id)).toEqual([FILAMENT_PLA, FILAMENT_PETG])
    expect((await api.order(ORDER_DRAFT)).order.minor_exponent).toBe(2)
    const page = await api.orders(DEFAULT_ORDERS_QUERY)
    expect(page.items.every((o) => o.minor_exponent === 2)).toBe(true)
  })

  it('sets the policy as the settings it is, and keeps the filaments', async () => {
    const saved = await api.setPriceBook({ machine_rate_per_hour: '1.5', setup_fee: '0.5', labour_per_item: '0', markup_percent: '15', rounding: 'to_major' })
    expect(saved.policy.markup_percent).toBe('15')
    expect((await api.getSettings()).pricing_markup_percent).toBe('15')
    const made = await api.createFilament({ material: 'ABS', colour: '#FFFFFF', cost_per_kg: '22' })
    expect((await failure(api.createFilament({ material: 'abs', colour: '#ffffff', cost_per_kg: '1' })))?.problem.type).toBe(
      `${PROBLEMS}filament_exists`,
    )
    expect((await api.updateFilament(made.id, { cost_per_kg: '24' })).cost_per_kg).toBe('24')
    await api.archiveFilament(made.id)
    expect((await api.priceBook()).filaments.map((f) => f.id)).not.toContain(made.id)
  })

  it('quotes from a slice: 202, then a slice quote with a filament line per slot', async () => {
    await api.setPriceBook({ machine_rate_per_hour: '1.5', setup_fee: '0', labour_per_item: '0', markup_percent: '0', rounding: 'to_minor_unit' })
    const quote = await api.quoteItem(ORDER_DRAFT, ITEM_DRAFT, { ...DIALOG, filaments: { '0': FILAMENT_PLA, '1': FILAMENT_PETG } })
    expect(quote.source).toBe('slice')
    expect(quote.lines.map((l) => l.kind)).toEqual(['filament', 'filament', 'machine_time'])
    const item = (await api.order(ORDER_DRAFT)).items.find((i) => i.id === ITEM_DRAFT)
    expect(item?.status).toBe('quoted')
    expect(item?.colours.map((c) => c.filament_id)).toEqual([FILAMENT_PLA, FILAMENT_PETG])
    expect(item?.quotes.map((q) => q.id)).toContain(quote.id)
  })

  it('refuses a quote the check refuses, writing nothing', async () => {
    const missing = await failure(api.quoteItem(ORDER_DRAFT, ITEM_DRAFT, { ...DIALOG, filaments: { '0': FILAMENT_PLA } }))
    expect(missing?.status).toBe(422)
    const free = await api.createFilament({ material: 'PLA', colour: '#FFFFFF', cost_per_kg: '0' })
    const nothing = await failure(api.quoteItem(ORDER_DRAFT, ITEM_DRAFT, { ...DIALOG, filaments: { '0': free.id, '1': free.id } }))
    expect(nothing?.status).toBe(422)
    expect(nothing?.problem.type).toBe(`${PROBLEMS}price_book`)
    const accepted = await failure(api.quoteItem(ORDER_ACCEPTED, 'x', { ...DIALOG, filaments: {} }))
    expect(accepted?.status).toBe(404)
    const item = (await api.order(ORDER_DRAFT)).items.find((i) => i.id === ITEM_DRAFT)
    expect(item?.quotes).toEqual([])
    expect(item?.colours.map((c) => c.filament_id)).toEqual([null, null])
  })

  it('estimates from an output and a filament, and records nothing', async () => {
    const estimate = await api.orderEstimate(OUTPUT_REAGAN, FILAMENT_PLA)
    expect(estimate.estimate).toBe(true)
    expect(estimate.minor_exponent).toBe(2)
    expect(Number(estimate.grams)).toBeGreaterThan(0)
    expect((await api.order(ORDER_DRAFT)).items.find((i) => i.id === ITEM_DRAFT)?.quotes).toEqual([])
  })

  it('sets an item\'s filaments, which drops its accepted quote only when they change', async () => {
    const item = await api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { filaments: { '0': FILAMENT_PLA } })
    expect(item.colours[0]?.filament_id).toBe(FILAMENT_PLA)
    const refused = await failure(api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { filaments: { '5': FILAMENT_PLA } }))
    expect(refused?.status).toBe(422)
  })
})

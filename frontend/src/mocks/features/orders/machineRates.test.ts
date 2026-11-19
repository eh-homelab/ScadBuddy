import { beforeEach, describe, expect, it } from 'vitest'
import { api, type ApiError } from '../../../api/client'
import { resetMockState } from '../../handlers'
import { importBambuddyEvent, setBambuddyLogAnswers, setEventsReadAt } from './machineRates'
import { FILAMENT_PETG, FILAMENT_PLA, ITEM_DRAFT, ORDER_DRAFT } from './state'

// #4625 — a printer's machine rate, answered as `api/machine_rates.py` answers it, and a
// slice quote priced at the rate of the printer it names, with its support slots.

const LINKED = '00000000-0000-4000-8000-000000000001'
const GARAGE = '00000000-0000-4000-8000-0000000000aa'
const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)
const DIALOG = {
  printer_id: 1,
  filament_plan: { slots: [{ slot_id: 1, spool_id: 9 }, { slot_id: 2, spool_id: 5 }], force_colour_match: false, ids: 'sb' as const },
  choices: { nozzles: [{ size: '0.4' as const, flow: 'standard' as const }], tier: 'standard' as const, bed_type: 'Textured PEI Plate' },
}

describe('machine rate mock (#4625)', () => {
  beforeEach(() => resetMockState())

  it('is the book rate with no inputs, and the sum of its parts with them', async () => {
    const none = await api.machineRate(GARAGE)
    expect(none.inputs).toBeNull()
    expect(none.breakdown.source).toBe('price_book')

    const set = await api.setMachineRate(GARAGE, {
      profile: { purchase_price: '2400', life_hours: '5000', watts: '200', kwh_price: '0.15', labour_rate_per_hour: '20' },
      wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000' }],
      maintenance: [{ task: 'Lubricate rails', minutes: '15', consumables: '1', interval_hours: '50' }],
    })
    const rate = set.breakdown
    expect([rate.depreciation_per_hour, rate.power_per_hour, rate.wear_per_hour, rate.maintenance_per_hour]).toEqual(['0.48', '0.03', '0.035', '0.12'])
    expect(rate.rate_per_hour).toBe('0.665')
    expect(rate.intervals).toBe('typed')
    expect((await api.machineRate(GARAGE)).inputs?.wear_parts).toHaveLength(1)

    await api.deleteMachineRate(GARAGE)
    expect((await api.machineRate(GARAGE)).breakdown.source).toBe('price_book')
    expect((await failure(api.deleteMachineRate(GARAGE)))?.status).toBe(404)
  })

  it("takes a linked printer's tasks and intervals from Bambuddy, ignoring an interval sent", async () => {
    const view = await api.setMachineRate(LINKED, {
      profile: { labour_rate_per_hour: '30' },
      wear_parts: [],
      maintenance: [{ task: 'Lubricate rails', minutes: '20', consumables: '2', interval_hours: '999' }],
    })
    expect(view.breakdown.intervals).toBe('bambuddy')
    const parts = Object.fromEntries((view.breakdown.maintenance ?? []).map((m) => [m.task, m]))
    expect(parts['Lubricate rails']).toMatchObject({ interval_hours: '50', counted: true, per_hour: '0.24' })
    expect(parts['Check belts']).toMatchObject({ counted: false, why: 'calendar' })
    expect(view.inputs?.maintenance?.[0]?.interval_hours).toBe('50')
  })

  it('prices a slice quote at the rate of the printer it names, with a support line', async () => {
    await api.setMachineRate(GARAGE, { profile: { watts: '1000', kwh_price: '2' }, wear_parts: [], maintenance: [] })
    const quote = await api.quoteItem(ORDER_DRAFT, ITEM_DRAFT, {
      ...DIALOG,
      printer: GARAGE,
      filaments: { '0': FILAMENT_PLA, '1': FILAMENT_PETG },
      support_filaments: { '2': FILAMENT_PETG },
    })
    expect(quote.printer).toBe(GARAGE)
    expect(quote.machine_rate_breakdown?.rate_per_hour).toBe('2')
    expect(quote.lines.find((l) => l.kind === 'machine_time')?.label).toMatch(/at 2 per hour/)
    expect(quote.lines.filter((l) => l.label.startsWith('Support, '))).toHaveLength(1)
  })

  it('prices a quote naming only a Bambuddy printer at its linked registry printer, as for_quote does', async () => {
    await api.setMachineRate(LINKED, { profile: { watts: '1000', kwh_price: '3' }, wear_parts: [], maintenance: [] })
    const quote = await api.quoteItem(ORDER_DRAFT, ITEM_DRAFT, { ...DIALOG, filaments: { '0': FILAMENT_PLA, '1': FILAMENT_PETG } })
    expect(quote.printer).toBe(LINKED)
    expect(quote.machine_rate_breakdown?.rate_per_hour).toBe('3')
  })
})

// #6665 — "Read from Bambuddy" and every event, paged.
describe('machine rate actuals mock (#6665)', () => {
  beforeEach(() => resetMockState())

  it('reads the log of a linked printer with a profile, and refuses as the route does', async () => {
    expect((await failure(api.importMachineRate(GARAGE)))?.problem.type).toMatch(/\/not_linked$/)
    expect((await failure(api.importMachineRate(LINKED)))?.problem.type).toMatch(/\/no_machine_rate$/)
    await api.setMachineRate(LINKED, { profile: {}, wear_parts: [], maintenance: [] })
    setEventsReadAt(LINKED, '2026-11-01T10:00:00Z')
    setBambuddyLogAnswers(false)
    const down = await failure(api.importMachineRate(LINKED))
    expect([down?.status, down?.problem.type]).toEqual([503, 'https://scadbuddy.dev/problems/machine-rate/log_unavailable'])
    expect((await api.machineRate(LINKED)).actuals.events_read_at).toBe('2026-11-01T10:00:00Z')
    setBambuddyLogAnswers(true)
    const read = await api.importMachineRate(LINKED)
    expect(Date.parse(read.actuals.events_read_at!)).toBeGreaterThan(Date.parse('2026-11-01T10:00:00Z'))
  })

  it('pages back through every event of a row, newest first', async () => {
    for (let i = 0; i < 55; i++) importBambuddyEvent(LINKED, 'Clean build plate', String(i), `2026-10-01T${String(i % 24).padStart(2, '0')}:${String(i).padStart(2, '0')}:00Z`)
    importBambuddyEvent(LINKED, 'Lubricate rails', '1')
    const first = await api.machineRateEvents(LINKED, { kind: 'task', name: 'Clean build plate' })
    expect(first.events).toHaveLength(50)
    expect(first.next).toBeTruthy()
    const second = await api.machineRateEvents(LINKED, { kind: 'task', name: 'Clean build plate', cursor: first.next })
    expect(second.events).toHaveLength(5)
    expect(second.next).toBeNull()
    const all = [...first.events, ...second.events]
    expect(new Set(all.map((e) => e.id)).size).toBe(55)
    expect(all.map((e) => e.at)).toEqual([...all.map((e) => e.at)].sort().reverse())
  })
})

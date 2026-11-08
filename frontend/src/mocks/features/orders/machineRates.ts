import { HttpResponse, http } from 'msw'
import type { MachineRateBreakdown, MachineRateInputs, MachineRateView, MaintenancePart, PrinterRow } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { printersState } from '../printers'

/**
 * #4625 (order operations spec 2026-11-07 §6) — a registry printer's machine rate
 * (`backend/scadbuddy/api/machine_rates.py`, `orders/machine_rate.py` `compute`): its inputs
 * and the rate they give, each part per hour rounded to 0.0001 and the rate their sum. A
 * printer linked to Bambuddy takes its tasks and intervals from `BAMBUDDY_TASKS` (the mock
 * Bambuddy always answers, `intervals` `bambuddy`); an interval sent for such a task is
 * ignored. A printer with no profile is the price book's `machine_rate_per_hour`.
 */

/** Bambuddy's maintenance tracker for the linked printer, as `GET /maintenance/printers/{id}` lists it. */
export const BAMBUDDY_TASKS: { task: string; interval_hours: number; interval_type: 'hours' | 'days'; enabled: boolean }[] = [
  { task: 'Lubricate rails', interval_hours: 50, interval_type: 'hours', enabled: true },
  { task: 'Clean build plate', interval_hours: 25, interval_type: 'hours', enabled: true },
  { task: 'Check belts', interval_hours: 30, interval_type: 'days', enabled: true },
]

let inputs = new Map<string, MachineRateInputs>()

export function reset(): void {
  inputs = new Map()
}

/** The inputs kept for `printer`, as the store keeps them. */
export function machineRateInputs(printer: string): MachineRateInputs | undefined {
  return inputs.get(printer)
}

const places = (n: number) => String(Number(n.toFixed(4)))
const num = (text: string | null | undefined) => Number(text ?? 0)

function compute(row: PrinterRow, kept: MachineRateInputs | undefined): MachineRateBreakdown {
  const settings = mockSettings()
  const currency = settings.orders_currency
  if (!kept) {
    return {
      printer: row.id,
      printer_name: row.name,
      source: 'price_book',
      rate_per_hour: settings.pricing_machine_rate_per_hour,
      currency,
      intervals: 'none',
      purchase_price: '0',
      watts: '0',
      kwh_price: '0',
      depreciation_per_hour: '0',
      power_per_hour: '0',
      wear_per_hour: '0',
      maintenance_per_hour: '0',
    }
  }
  const profile = kept.profile
  const labour = profile.labour_rate_per_hour ?? settings.pricing_labour_rate_per_hour ?? '0'
  const life = profile.life_hours ?? null
  const depreciation = life && num(life) > 0 ? num(profile.purchase_price) / num(life) : 0
  const power = (num(profile.watts) / 1000) * num(profile.kwh_price)
  const wear = kept.wear_parts.map((part) => ({ ...part, per_hour: places(num(part.cost) / num(part.life_hours)) }))
  const linked = row.bambuddy_printer_id != null
  const costs = new Map(kept.maintenance.map((cost) => [cost.task, cost]))
  const part = (task: string, interval: number | null, type: 'hours' | 'days', why: MaintenancePart['why']): MaintenancePart => {
    const cost = costs.get(task)
    costs.delete(task)
    const minutes = cost?.minutes ?? '0'
    const consumables = cost?.consumables ?? '0'
    const counted = why == null && interval != null && interval > 0
    const perHour = counted ? ((num(minutes) * num(labour)) / 60 + num(consumables)) / (interval as number) : 0
    return {
      task,
      interval_hours: interval == null ? null : String(interval),
      interval_type: type,
      minutes,
      consumables,
      per_hour: places(perHour),
      counted,
      why: counted ? null : (why ?? 'no_interval'),
    }
  }
  const maintenance = linked
    ? [
        ...BAMBUDDY_TASKS.map((t) => part(t.task, t.interval_hours, t.interval_type, !t.enabled ? 'disabled' : t.interval_type === 'days' ? 'calendar' : null)),
        ...[...costs.keys()].map((task) => part(task, null, 'hours', 'not_in_bambuddy')),
      ]
    : kept.maintenance.map((cost) => part(cost.task, cost.interval_hours ? num(cost.interval_hours) : null, 'hours', null))
  const parts = {
    depreciation_per_hour: places(depreciation),
    power_per_hour: places(power),
    wear_per_hour: places(wear.reduce((sum, w) => sum + num(w.per_hour), 0)),
    maintenance_per_hour: places(maintenance.reduce((sum, m) => sum + num(m.per_hour), 0)),
  }
  return {
    printer: row.id,
    printer_name: row.name,
    source: 'profile',
    rate_per_hour: places(Object.values(parts).reduce((sum, p) => sum + num(p), 0)),
    currency,
    intervals: linked ? 'bambuddy' : 'typed',
    intervals_read_at: linked ? new Date().toISOString() : null,
    purchase_price: profile.purchase_price ?? '0',
    life_hours: life,
    watts: profile.watts ?? '0',
    kwh_price: profile.kwh_price ?? '0',
    labour_rate_per_hour: profile.labour_rate_per_hour ?? null,
    wear,
    maintenance,
    ...parts,
  }
}

/**
 * The rate a slice quote is priced at, for the printer it names, as `MachineRates.for_quote`
 * resolves it: the registry printer named, else the one linked to the Bambuddy printer named,
 * else the configured default printer. Null for none (the book's rate prices it), and for a
 * named row gone with no Bambuddy printer beside it.
 */
export function machineRateFor(printer: string | null | undefined, printerId?: number | null): MachineRateBreakdown | null {
  const rows = printersState()
  const byBambuddy = (id: number | null | undefined) => (id == null ? undefined : rows.find((p) => p.bambuddy_printer_id === id))
  let row: PrinterRow | undefined
  if (printer != null) {
    row = rows.find((p) => p.id === printer)
    if (!row && printerId == null) return null
  }
  if (!row && printerId != null) row = byBambuddy(printerId)
  else if (!row) {
    const settings = mockSettings()
    row = settings.printer_id != null ? byBambuddy(settings.printer_id) : rows.find((p) => p.id === settings.default_printer)
  }
  return row ? compute(row, inputs.get(row.id)) : null
}

function view(row: PrinterRow): MachineRateView {
  return {
    printer: row.id,
    printer_name: row.name,
    bambuddy_printer_id: row.bambuddy_printer_id ?? null,
    inputs: (inputs.get(row.id) ?? null) as MachineRateView['inputs'],
    breakdown: compute(row, inputs.get(row.id)),
  }
}

const gone = (id: string) => HttpResponse.json({ detail: `no printer '${id}'` }, { status: 404 })

export const handlers = [
  http.get('/api/v1/printers/:id/machine-rate', ({ params }) => {
    const row = printersState().find((p) => p.id === params.id)
    return row ? HttpResponse.json(view(row)) : gone(String(params.id))
  }),
  http.put('/api/v1/printers/:id/machine-rate', async ({ params, request }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    const body = (await request.json()) as MachineRateInputs
    const read = new Map(BAMBUDDY_TASKS.map((t) => [t.task, String(t.interval_hours)]))
    inputs.set(row.id, {
      profile: { purchase_price: '0', watts: '0', kwh_price: '0', life_hours: null, labour_rate_per_hour: null, ...body.profile },
      wear_parts: body.wear_parts ?? [],
      // A linked printer's task keeps the interval Bambuddy gave it: what is sent is ignored.
      maintenance: (body.maintenance ?? []).map((cost) => ({
        minutes: '0',
        consumables: '0',
        ...cost,
        interval_hours: row.bambuddy_printer_id != null && read.has(cost.task) ? read.get(cost.task) : (cost.interval_hours ?? null),
      })),
    })
    return HttpResponse.json(view(row))
  }),
  http.delete('/api/v1/printers/:id/machine-rate', ({ params }) => {
    const id = String(params.id)
    if (!inputs.delete(id)) return HttpResponse.json({ detail: `printer '${id}' has no machine rate inputs` }, { status: 404 })
    return new HttpResponse(null, { status: 204 })
  }),
]

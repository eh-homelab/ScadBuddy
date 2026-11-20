import { HttpResponse, http } from 'msw'
import type {
  LanCount,
  MachineRateBreakdown,
  MachineRateEvent,
  MachineRateEventCreate,
  MachineRateEventPatch,
  MachineRateInputs,
  MachineRateView,
  MaintenancePart,
  PrinterRow,
  RowActuals,
  SkippedGap,
} from '../../../api/types'
import { mockSettings } from '../../handlers'
import { printersState } from '../printers'

/**
 * #4625 (order operations spec 2026-11-07 §6) — a registry printer's machine rate
 * (`backend/scadbuddy/api/machine_rates.py`, `orders/machine_rate.py` `compute`): its inputs
 * and the rate they give, each part per hour rounded to 0.0001 and the rate their sum. A
 * printer linked to Bambuddy takes its tasks and intervals from `BAMBUDDY_TASKS` (the mock
 * Bambuddy always answers, `intervals` `bambuddy`); an interval sent for such a task is
 * ignored. A printer with no profile is the price book's `machine_rate_per_hour`.
 *
 * #5774 (spec 2026-11-17 §3, §5): the events logged on a printer (maintenance done, parts
 * replaced), each row's actual over the last N usable gaps (`task_actual`, `part_actual`),
 * its confidence, `priced_by`, and the printer's counter: Bambuddy's (`setBambuddyHours`,
 * null when Bambuddy does not answer), ScadBuddy's own LAN count (`setLanCount`), or none.
 *
 * #6665: the view and "Read from Bambuddy" (`POST …/import`) read a linked printer's log and
 * stamp `events_read_at`; `setBambuddyLogAnswers(false)` makes that read fail (503
 * `log_unavailable`), keeping the stamp. The events list pages by its cursor.
 * Job cost does not read a rate saved here for the two fixture printers in `costs.ts`
 * `TODAYS_PRINTER_RATES`: that table wins, so saving their rate leaves job cost unchanged.
 */

/** Bambuddy's maintenance tracker for the linked printer, as `GET /maintenance/printers/{id}` lists it. */
export const BAMBUDDY_TASKS: { task: string; interval_hours: number; interval_type: 'hours' | 'days'; enabled: boolean }[] = [
  { task: 'Lubricate rails', interval_hours: 50, interval_type: 'hours', enabled: true },
  { task: 'Clean build plate', interval_hours: 25, interval_type: 'hours', enabled: true },
  { task: 'Check belts', interval_hours: 30, interval_type: 'days', enabled: true },
]

/** Bambuddy's counter for its printer 1, as the maintenance overview's `total_print_hours` gives it. */
const BAMBUDDY_HOURS = '495.3'

type LanSeed = { run_hours: string; unseen_hours?: string; started_at?: string }

let inputs = new Map<string, MachineRateInputs>()
let events = new Map<string, MachineRateEvent[]>()
let lanCounts = new Map<string, LanSeed>()
let bambuddyHours: string | null = BAMBUDDY_HOURS
let nextEvent = 1
/** #6665: when each printer's Bambuddy log was last read in full (`events_read_at`). */
let readAt = new Map<string, string>()
let logAnswers = true

export function reset(): void {
  inputs = new Map()
  events = new Map()
  lanCounts = new Map()
  bambuddyHours = BAMBUDDY_HOURS
  nextEvent = 1
  readAt = new Map()
  logAnswers = true
}

/** #6665: whether Bambuddy answers the maintenance log read (`maintenance_history`). */
export function setBambuddyLogAnswers(answers: boolean): void {
  logAnswers = answers
}

/** #6665: `events_read_at` for `printer`, as a full read of its log at `at` leaves it. */
export function setEventsReadAt(printer: string, at: string | null): void {
  if (at == null) readAt.delete(printer)
  else readAt.set(printer, at)
}

/** A linked printer with a profile reads its log with the view (§3.7); a read that fails keeps `events_read_at`. */
function readLog(row: PrinterRow): 'read' | 'unlinked' | 'no_profile' | 'unanswered' {
  if (row.bambuddy_printer_id == null) return 'unlinked'
  if (!inputs.has(row.id)) return 'no_profile'
  if (!logAnswers) return 'unanswered'
  readAt.set(row.id, new Date().toISOString())
  return 'read'
}

/** The inputs kept for `printer`, as the store keeps them. */
export function machineRateInputs(printer: string): MachineRateInputs | undefined {
  return inputs.get(printer)
}

/** Every event kept for `printer`, in the order written. */
export function machineRateEvents(printer: string): MachineRateEvent[] {
  return events.get(printer) ?? []
}

/** Bambuddy's counter now; null: Bambuddy does not answer. */
export function setBambuddyHours(hours: string | null): void {
  bambuddyHours = hours
}

/** ScadBuddy's own LAN count for `printer` (`printer_run_hours`), as if its watch had counted it. */
export function setLanCount(printer: string, seed: LanSeed): void {
  lanCounts.set(printer, seed)
}

/** A task event as the Bambuddy import (#5774 PR 3) writes it: origin `bambuddy`, no minutes. */
export function importBambuddyEvent(printer: string, task: string, hoursAt: string, at = '2026-11-15T03:59:38Z'): MachineRateEvent {
  const historyId = nextEvent
  const event = write(printer, {
    kind: 'task',
    name: task,
    at,
    hours_at: hoursAt,
    hours_source: 'bambuddy',
    origin: 'bambuddy',
    bambuddy_history_id: historyId,
    source: `bambuddy:${historyId}`,
    created_by: 'bambuddy',
  })
  return event
}

const places = (n: number) => String(Number(n.toFixed(4)))
const tenths = (n: number) => String(Number((Math.round(n * 10 + 1e-9) / 10).toFixed(1)))
const num = (text: string | null | undefined) => Number(text ?? 0)

function write(printer: string, fields: Partial<MachineRateEvent> & Pick<MachineRateEvent, 'kind' | 'name' | 'origin' | 'source'>): MachineRateEvent {
  const now = new Date().toISOString()
  const event: MachineRateEvent = {
    id: `evt-${String(nextEvent++).padStart(4, '0')}`,
    at: now,
    hours_at: null,
    hours_source: null,
    minutes: null,
    consumables: null,
    cost: null,
    hours_used: null,
    notes: null,
    bambuddy_history_id: null,
    created_at: now,
    created_by: 'local',
    ...fields,
  }
  events.set(printer, [...machineRateEvents(printer), event])
  return event
}

function lanCount(row: PrinterRow): LanCount | null {
  const seed = lanCounts.get(row.id)
  if (!seed) return null
  const offset = inputs.get(row.id)?.profile.lan_hours_offset ?? '0'
  return {
    hours: tenths(num(seed.run_hours) + num(offset)),
    run_hours: seed.run_hours,
    offset_hours: tenths(num(offset)),
    unseen_hours: seed.unseen_hours ?? '0',
    counted_to: new Date().toISOString(),
    started_at: seed.started_at ?? '2026-11-20T09:00:00Z',
  }
}

const sourceOf = (row: PrinterRow) => (row.bambuddy_printer_id != null ? 'bambuddy' : row.lan_host != null ? 'lan_watch' : 'typed')

/** The reading an event gets now (`counter_reading`). */
function reading(row: PrinterRow): string | null {
  const source = sourceOf(row)
  if (source === 'bambuddy') return bambuddyHours
  if (source === 'lan_watch') return lanCount(row)?.hours ?? null
  return null
}

type Actual = { have: number; cost: number; hours: number; partlyTyped: boolean; window: string[]; skipped: SkippedGap[] }

function gapProblem(newer: MachineRateEvent, older: MachineRateEvent, hours: number): SkippedGap['reason'] | null {
  if (newer.hours_source !== older.hours_source) return 'counter_changed'
  if (hours <= 0) return 'not_positive'
  return null
}

const byKey = (keys: (e: MachineRateEvent) => (string | number)[]) => (a: MachineRateEvent, b: MachineRateEvent) => {
  const [x, y] = [keys(a), keys(b)]
  for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i]! < y[i]! ? -1 : 1
  return 0
}

/** `task_actual`: the last `need` usable gaps between two events with a reading. */
function taskActual(list: MachineRateEvent[], minutes: number, consumables: number, labour: number, need: number): Actual {
  const read = list.filter((e) => e.hours_at != null).sort(byKey((e) => [num(e.hours_at), e.at, e.id]))
  const actual: Actual = { have: 0, cost: 0, hours: 0, partlyTyped: false, window: [], skipped: [] }
  for (let index = read.length - 1; index >= 0; index--) {
    const newer = read[index]!
    actual.window.push(newer.id)
    if (actual.have >= need || index === 0) break
    const older = read[index - 1]!
    const gap = Number((num(newer.hours_at) - num(older.hours_at)).toFixed(1))
    const problem = gapProblem(newer, older, gap)
    if (problem) {
      actual.skipped.push({ event: newer.id, previous: older.id, reason: problem })
      continue
    }
    if (newer.minutes == null || newer.consumables == null) actual.partlyTyped = true
    actual.cost += ((newer.minutes == null ? minutes : num(newer.minutes)) * labour) / 60 + (newer.consumables == null ? consumables : num(newer.consumables))
    actual.hours += gap
    actual.have += 1
  }
  return actual
}

/** `part_actual`: the last `need` replacements with a usable `hours_used`. */
function partActual(list: MachineRateEvent[], need: number): Actual {
  const ordered = [...list].sort(byKey((e) => [e.at, num(e.hours_at), e.id]))
  const actual: Actual = { have: 0, cost: 0, hours: 0, partlyTyped: false, window: [], skipped: [] }
  for (let index = ordered.length - 1; index >= 0; index--) {
    const newer = ordered[index]!
    actual.window.push(newer.id)
    let used = newer.hours_used == null ? null : num(newer.hours_used)
    let base: MachineRateEvent | null = null
    if (used == null) {
      base = index > 0 ? ordered[index - 1]! : null
      if (!base || base.hours_at == null || newer.hours_at == null) continue
      used = Number((num(newer.hours_at) - num(base.hours_at)).toFixed(1))
      const problem = gapProblem(newer, base, used)
      if (problem) {
        actual.skipped.push({ event: newer.id, previous: base.id, reason: problem })
        continue
      }
    }
    actual.cost += num(newer.cost)
    actual.hours += used
    actual.have += 1
    if (actual.have >= need) {
      if (base) actual.window.push(base.id)
      break
    }
  }
  return actual
}

const confidence = (a: Actual, need: number) => Math.floor((100 * Math.min(a.have, need)) / need)
const why = (a: Actual, need: number) => (a.have >= need ? null : a.have === 0 ? ('no_events' as const) : ('too_few' as const))
const perHour = (a: Actual) => (a.have ? places(a.cost / a.hours) : null)

type Computed = { breakdown: MachineRateBreakdown; rows: RowActuals[] }

function compute(row: PrinterRow, kept: MachineRateInputs | undefined): Computed {
  const settings = mockSettings()
  const currency = settings.orders_currency
  if (!kept) {
    return {
      rows: [],
      breakdown: {
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
        actuals: false,
      },
    }
  }
  const profile = kept.profile
  const need = profile.actuals_window ?? 3
  const logged = machineRateEvents(row.id)
  const named = (kind: 'task' | 'part', name: string) => logged.filter((e) => e.kind === kind && e.name === name)
  const listed = (a: Actual) => a.window.map((id) => logged.find((e) => e.id === id)!).filter(Boolean)
  const rows: RowActuals[] = []
  const labour = profile.labour_rate_per_hour ?? settings.pricing_labour_rate_per_hour ?? '0'
  const life = profile.life_hours ?? null
  const depreciation = life && num(life) > 0 ? num(profile.purchase_price) / num(life) : 0
  const power = (num(profile.watts) / 1000) * num(profile.kwh_price)
  const wear = kept.wear_parts.map((part) => {
    const estimate = places(num(part.cost) / num(part.life_hours))
    const actual = partActual(named('part', part.name), need)
    const pricedBy = part.priced_by ?? 'estimate'
    const basis = pricedBy === 'actual' && actual.have >= need ? ('actual' as const) : ('estimate' as const)
    const actualHoursUsed = actual.have ? tenths(actual.hours / actual.have) : null
    rows.push({
      kind: 'part',
      name: part.name,
      events: listed(actual),
      actual_per_hour: perHour(actual),
      actual_hours_used: actualHoursUsed,
      have: actual.have,
      need,
      confidence: confidence(actual, need),
      partly_typed: false,
      skipped_gaps: actual.skipped,
    })
    return {
      name: part.name,
      cost: part.cost,
      life_hours: part.life_hours,
      per_hour: basis === 'actual' ? perHour(actual)! : estimate,
      priced_by: pricedBy,
      basis,
      estimate_per_hour: estimate,
      actual_per_hour: perHour(actual),
      have: actual.have,
      need,
      confidence: confidence(actual, need),
      actual_why: why(actual, need),
      skipped_gaps: actual.skipped,
      actual_hours_used: actualHoursUsed,
      partly_typed: false,
    }
  })
  const linked = row.bambuddy_printer_id != null
  const costs = new Map(kept.maintenance.map((cost) => [cost.task, cost]))
  const part = (task: string, interval: number | null, type: 'hours' | 'days', reason: MaintenancePart['why']): MaintenancePart => {
    const cost = costs.get(task)
    costs.delete(task)
    const minutes = cost?.minutes ?? '0'
    const consumables = cost?.consumables ?? '0'
    const counted = reason == null && interval != null && interval > 0
    const estimate = counted ? places(((num(minutes) * num(labour)) / 60 + num(consumables)) / (interval as number)) : null
    const actual = taskActual(named('task', task), num(minutes), num(consumables), num(labour), need)
    const pricedBy = cost?.priced_by ?? 'estimate'
    const basis = pricedBy === 'actual' && counted && actual.have >= need ? ('actual' as const) : ('estimate' as const)
    const interval_ = actual.have ? tenths(actual.hours / actual.have) : null
    const occurrence = actual.have ? places(actual.cost / actual.have) : null
    rows.push({
      kind: 'task',
      name: task,
      events: listed(actual),
      actual_per_hour: perHour(actual),
      actual_interval_hours: interval_,
      actual_cost_per_occurrence: occurrence,
      have: actual.have,
      need,
      confidence: confidence(actual, need),
      partly_typed: actual.partlyTyped,
      skipped_gaps: actual.skipped,
    })
    return {
      task,
      interval_hours: interval == null ? null : String(interval),
      interval_type: type,
      minutes,
      consumables,
      per_hour: (basis === 'actual' ? perHour(actual) : estimate) ?? '0',
      counted,
      why: counted ? null : (reason ?? 'no_interval'),
      priced_by: pricedBy,
      basis,
      estimate_per_hour: estimate,
      actual_per_hour: perHour(actual),
      have: actual.have,
      need,
      confidence: confidence(actual, need),
      partly_typed: actual.partlyTyped,
      actual_why: why(actual, need),
      skipped_gaps: actual.skipped,
      actual_interval_hours: interval_,
      actual_cost_per_occurrence: occurrence,
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
  const trusted = [...maintenance.filter((m) => m.priced_by === 'actual' && m.counted), ...wear.filter((w) => w.priced_by === 'actual')].map((r) => r.confidence ?? 0)
  return {
    rows,
    breakdown: {
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
      actuals: [...wear, ...maintenance].some((r) => r.basis === 'actual'),
      actuals_window: need,
      confidence: trusted.length ? Math.min(...trusted) : null,
    },
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
  return row ? compute(row, inputs.get(row.id)).breakdown : null
}

function view(row: PrinterRow): MachineRateView {
  const kept = inputs.get(row.id)
  const { breakdown, rows } = compute(row, kept)
  const names = { task: new Set(kept?.maintenance.map((m) => m.task)), part: new Set(kept?.wear_parts.map((w) => w.name)) }
  for (const m of breakdown.maintenance ?? []) names.task.add(m.task)
  const orphans = new Map<string, { kind: 'task' | 'part'; name: string; events: number }>()
  for (const e of machineRateEvents(row.id)) {
    if (names[e.kind].has(e.name)) continue
    const key = `${e.kind}:${e.name}`
    orphans.set(key, { kind: e.kind, name: e.name, events: (orphans.get(key)?.events ?? 0) + 1 })
  }
  return {
    printer: row.id,
    printer_name: row.name,
    bambuddy_printer_id: row.bambuddy_printer_id ?? null,
    inputs: (kept ?? null) as MachineRateView['inputs'],
    breakdown,
    // Read from the mock Bambuddy, which always answers: also before the first save.
    bambuddy_tasks: row.bambuddy_printer_id != null ? BAMBUDDY_TASKS.map((t) => t.task) : [],
    actuals: {
      window: kept?.profile.actuals_window ?? 3,
      rows,
      orphaned: [...orphans.values()].sort((a, b) => a.name.localeCompare(b.name)),
      events_read_at: readAt.get(row.id) ?? null,
    },
    counter: { source: sourceOf(row), hours: reading(row), lan_watch: lanCount(row) },
  }
}

const gone = (id: string) => HttpResponse.json({ detail: `no printer '${id}'` }, { status: 404 })
const PROBLEM = 'https://scadbuddy.dev/problems/machine-rate/'
const refuse = (status: number, code: string, detail: string) =>
  HttpResponse.json({ type: PROBLEM + code, title: code, status, detail, code }, { status, headers: { 'Content-Type': 'application/problem+json' } })

export const handlers = [
  http.get('/api/v1/printers/:id/machine-rate', ({ params }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    readLog(row)
    return HttpResponse.json(view(row))
  }),
  // #6665: "Read from Bambuddy" (`api/machine_rates.py` `import_machine_rate_events`).
  http.post('/api/v1/printers/:id/machine-rate/import', ({ params }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    const read = readLog(row)
    if (read === 'unlinked') return refuse(409, 'not_linked', 'The printer is not linked to Bambuddy: it has no log to read.')
    if (read === 'no_profile') return refuse(409, 'no_machine_rate', 'The printer has no machine rate inputs yet: set them, then read its log.')
    if (read === 'unanswered') return refuse(503, 'log_unavailable', 'Bambuddy did not answer. The rate uses the events last read.')
    return HttpResponse.json(view(row))
  }),
  http.put('/api/v1/printers/:id/machine-rate', async ({ params, request }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    const body = (await request.json()) as MachineRateInputs
    const read = new Map(BAMBUDDY_TASKS.map((t) => [t.task, String(t.interval_hours)]))
    // A rename moves the old name's events (`MachineRateStore.replace`).
    const renames = [
      ...(body.wear_parts ?? []).map((w) => ['part', w.renamed_from, w.name] as const),
      ...(body.maintenance ?? []).map((m) => ['task', m.renamed_from, m.task] as const),
    ]
    for (const [kind, from, to] of renames) {
      if (!from || from === to) continue
      events.set(
        row.id,
        machineRateEvents(row.id).map((e) => (e.kind === kind && e.name === from ? { ...e, name: to } : e)),
      )
    }
    inputs.set(row.id, {
      profile: { purchase_price: '0', watts: '0', kwh_price: '0', life_hours: null, labour_rate_per_hour: null, actuals_window: 3, lan_hours_offset: '0', ...body.profile },
      wear_parts: (body.wear_parts ?? []).map(({ renamed_from: _, ...w }) => ({ ...w, priced_by: w.priced_by ?? 'estimate' })),
      // A linked printer's task keeps the interval Bambuddy gave it: what is sent is ignored.
      maintenance: (body.maintenance ?? []).map(({ renamed_from: _, ...cost }) => ({
        minutes: '0',
        consumables: '0',
        ...cost,
        priced_by: cost.priced_by ?? 'estimate',
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
  http.get('/api/v1/printers/:id/machine-rate/events', ({ params, request }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    const url = new URL(request.url)
    const kind = url.searchParams.get('kind')
    const name = url.searchParams.get('name')
    // Newest first by time, then id, a page at a time (`MachineRateStore.list_events`). The ids are
    // zero-padded (`evt-0001`), so comparing them as text orders them as numbers.
    const order = (e: MachineRateEvent) => `${e.at}|${e.id}`
    const cursor = url.searchParams.get('cursor')
    const limit = Number(url.searchParams.get('limit') ?? 50)
    const list = machineRateEvents(row.id)
      .filter((e) => (!kind || e.kind === kind) && (!name || e.name === name))
      .filter((e) => !cursor || order(e) < cursor)
      .sort((a, b) => (order(a) < order(b) ? 1 : order(a) > order(b) ? -1 : 0))
    const page = list.slice(0, limit)
    return HttpResponse.json({ events: page, next: list.length > limit ? order(page[page.length - 1]!) : null })
  }),
  http.post('/api/v1/printers/:id/machine-rate/events', async ({ params, request }) => {
    const row = printersState().find((p) => p.id === params.id)
    if (!row) return gone(String(params.id))
    const body = (await request.json()) as MachineRateEventCreate
    const first = body.source ? machineRateEvents(row.id).find((e) => e.source === body.source && e.kind === body.kind) : undefined
    if (first) return HttpResponse.json(first, { status: 200 })
    const source = sourceOf(row)
    if (body.kind === 'task' && source === 'bambuddy') return refuse(409, 'maintenance_from_bambuddy', 'Mark tasks done in Bambuddy; ScadBuddy reads them.')
    const kept = inputs.get(row.id)
    const names = body.kind === 'task' ? kept?.maintenance.map((m) => m.task) : kept?.wear_parts.map((w) => w.name)
    if (!names?.includes(body.name)) return refuse(422, `unknown_${body.kind}`, `the printer has no ${body.kind === 'task' ? 'maintenance task' : 'wear part'} named '${body.name}'`)
    let hoursAt = body.hours_at ?? null
    let recorded: MachineRateEvent['hours_source'] = source
    if (hoursAt == null) {
      const now = reading(row)
      if (now != null) hoursAt = now
      else if (source !== 'typed') return refuse(503, 'counter_unavailable', "The printer's counter cannot be read now. Try again, or enter the reading.")
      else if (body.kind === 'task' || body.hours_used == null)
        return refuse(422, 'counter_reading_required', 'The printer has no counter: enter the reading (or, for a part, the hours it ran).')
      else recorded = null
    }
    const event = write(row.id, {
      kind: body.kind,
      name: body.name,
      at: body.at ?? new Date().toISOString(),
      hours_at: hoursAt == null ? null : tenths(num(hoursAt)),
      hours_source: recorded,
      minutes: body.minutes ?? null,
      consumables: body.consumables ?? null,
      cost: body.cost ?? null,
      hours_used: body.hours_used ?? null,
      notes: body.notes ?? null,
      origin: 'scadbuddy',
      source: body.source ?? `mock:${nextEvent}`,
    })
    return HttpResponse.json(event, { status: 201 })
  }),
  http.patch('/api/v1/printers/:id/machine-rate/events/:event', async ({ params, request }) => {
    const id = String(params.id)
    const list = machineRateEvents(id)
    const was = list.find((e) => e.id === params.event)
    if (!was) return HttpResponse.json({ detail: `no event '${String(params.event)}'` }, { status: 404 })
    const body = (await request.json()) as MachineRateEventPatch
    if (was.origin === 'bambuddy' && 'hours_at' in body && body.hours_at == null) return refuse(422, 'imported_event', "An imported event's counter reading stays as read.")
    const next = { ...was, ...body, hours_at: body.hours_at === undefined ? was.hours_at : body.hours_at }
    events.set(id, list.map((e) => (e.id === was.id ? next : e)))
    return HttpResponse.json(next)
  }),
  http.delete('/api/v1/printers/:id/machine-rate/events/:event', ({ params }) => {
    const id = String(params.id)
    const list = machineRateEvents(id)
    const was = list.find((e) => e.id === params.event)
    if (!was) return HttpResponse.json({ detail: `no event '${String(params.event)}'` }, { status: 404 })
    if (was.origin === 'bambuddy') return refuse(409, 'imported_event', 'An event read from Bambuddy cannot be deleted: the next read would bring it back.')
    events.set(id, list.filter((e) => e.id !== was.id))
    return new HttpResponse(null, { status: 204 })
  }),
]

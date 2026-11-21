import { useState, type ReactNode } from 'react'
import { api, newRequestId } from '../../api/client'
import type { MachineRateBreakdown, MachineRateInputs, MachineRateView, MaintenancePart, PrinterRow, RowActuals, WearPartRate } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { DataTable, type Column } from '../records/DataTable'
import { refusalText } from './refusal'
import { BambuddyRead, EventList, FigurePair, LogEventForm, PrintHours, type Kind, type PricedBy } from './MachineRateActuals'
import { AMOUNT, decimal, placesText, TENTHS } from './machineRateNumbers'

type Profile = { purchase_price: string; life_hours: string; watts: string; kwh_price: string; labour_rate_per_hour: string; actuals_window: string; lan_hours_offset: string }
/**
 * `saved`: the row's name as stored, so a rename moves its events (`renamed_from`); null for a new row.
 * `key`: the row's React key, its stored name or a draft id (#6665), so an open event list stays with
 * its row when a row above it is removed.
 */
type Wear = { name: string; cost: string; life_hours: string; priced_by: PricedBy; saved: string | null; key: string }
type Task = { task: string; minutes: string; consumables: string; interval_hours: string; priced_by: PricedBy; saved: string | null; key: string }
type Draft = { profile: Profile; wear: Wear[]; maintenance: Task[] }

const DEFAULT_WINDOW = 3
/** A new row's key; `newRequestId`, not `crypto.randomUUID`: plain http and Bambuddy's iframe have no secure context. */
const draftKey = () => `new:${newRequestId()}`
const EMPTY_PROFILE: Profile = { purchase_price: '', life_hours: '', watts: '', kwh_price: '', labour_rate_per_hour: '', actuals_window: String(DEFAULT_WINDOW), lan_hours_offset: '' }

function draftOf(view: MachineRateView): Draft {
  const inputs = view.inputs
  if (!inputs) return { profile: EMPTY_PROFILE, wear: [], maintenance: [] }
  const p = inputs.profile
  return {
    profile: {
      purchase_price: p.purchase_price ?? '',
      life_hours: p.life_hours ?? '',
      watts: p.watts ?? '',
      kwh_price: p.kwh_price ?? '',
      labour_rate_per_hour: p.labour_rate_per_hour ?? '',
      actuals_window: String(p.actuals_window ?? DEFAULT_WINDOW),
      lan_hours_offset: p.lan_hours_offset ?? '',
    },
    wear: (inputs.wear_parts ?? []).map((w) => ({ name: w.name, cost: w.cost, life_hours: w.life_hours, priced_by: w.priced_by ?? 'estimate', saved: w.name, key: `saved:${w.name}` })),
    maintenance: (inputs.maintenance ?? []).map((m) => ({
      task: m.task,
      minutes: m.minutes,
      consumables: m.consumables,
      interval_hours: m.interval_hours ?? '',
      priced_by: m.priced_by ?? 'estimate',
      saved: m.task,
      key: `saved:${m.task}`,
    })),
  }
}

/** The stored name a row had, when the row now has another one that no other row holds. */
function renamedFrom(saved: string | null, name: string, names: Set<string>): string | undefined {
  return saved && saved !== name && !names.has(saved) ? saved : undefined
}

/** The body a draft makes, or what to fix first. An empty optional field is left out (its default) or null. */
function bodyOf(draft: Draft, linked: boolean): MachineRateInputs | string {
  const p = draft.profile
  const number = (text: string, what: string, places: number) => (text.trim() && !decimal(text, places) ? `Enter ${what} as a number ${placesText(places)}, or leave it empty.` : null)
  const problem =
    number(p.purchase_price, 'the purchase price', AMOUNT) ??
    number(p.life_hours, 'the life in hours', TENTHS) ??
    number(p.watts, 'the watts', TENTHS) ??
    number(p.kwh_price, 'the price of a kWh', AMOUNT) ??
    number(p.labour_rate_per_hour, 'the labour rate', AMOUNT) ??
    number(p.lan_hours_offset, 'the offset', TENTHS)
  if (problem) return problem
  const actualsWindow = p.actuals_window.trim()
  if (!/^\d+$/.test(actualsWindow) || Number(actualsWindow) < 1 || Number(actualsWindow) > 20) return 'Enter the actuals window as a whole number from 1 to 20.'
  const optional = (text: string) => (text.trim() ? text.trim() : undefined)
  for (const w of draft.wear) {
    if (!w.name.trim()) return 'Name each wear part, such as Nozzle.'
    if (!decimal(w.cost, AMOUNT) || !decimal(w.life_hours, TENTHS) || Number(w.life_hours) <= 0)
      return `Enter ${w.name}'s cost (at most 4 decimal places) and its life in print-hours (more than 0, at most 1 decimal place).`
  }
  for (const m of draft.maintenance) {
    if (!m.task.trim()) return 'Name each maintenance task, such as Lubricate rails.'
    const bad =
      number(m.minutes, `${m.task}'s minutes`, TENTHS) ?? number(m.consumables, `${m.task}'s consumables`, AMOUNT) ?? (linked ? null : number(m.interval_hours, `${m.task}'s interval`, TENTHS))
    if (bad) return bad
  }
  const partNames = new Set(draft.wear.map((w) => w.name.trim()))
  const taskNames = new Set(draft.maintenance.map((m) => m.task.trim()))
  return {
    profile: {
      purchase_price: optional(p.purchase_price),
      life_hours: optional(p.life_hours) ?? null,
      watts: optional(p.watts),
      kwh_price: optional(p.kwh_price),
      labour_rate_per_hour: optional(p.labour_rate_per_hour) ?? null,
      actuals_window: Number(actualsWindow),
      // Empty is no offset; left out, the backend would keep the stored one.
      lan_hours_offset: p.lan_hours_offset.trim() || '0',
    },
    wear_parts: draft.wear.map((w) => ({
      name: w.name.trim(),
      cost: w.cost.trim(),
      life_hours: w.life_hours.trim(),
      priced_by: w.priced_by,
      renamed_from: renamedFrom(w.saved, w.name.trim(), partNames),
    })),
    maintenance: draft.maintenance.map((m) => ({
      task: m.task.trim(),
      minutes: optional(m.minutes),
      consumables: optional(m.consumables),
      priced_by: m.priced_by,
      renamed_from: renamedFrom(m.saved, m.task.trim(), taskNames),
      // A linked printer's interval is Bambuddy's: none is sent.
      interval_hours: linked ? null : (optional(m.interval_hours) ?? null),
    })),
  }
}

const WHY: Record<NonNullable<MaintenancePart['why']>, string> = {
  calendar: 'a calendar task, in days: not counted',
  disabled: 'disabled in Bambuddy: not counted',
  no_interval: 'no interval: not counted',
  not_in_bambuddy: 'not a task Bambuddy tracks: not counted',
  not_read: 'not read from Bambuddy yet: not counted',
}

const INTERVALS: Record<MachineRateBreakdown['intervals'], string> = {
  bambuddy: "Maintenance intervals read from Bambuddy now.",
  stored: 'Bambuddy did not answer: the maintenance intervals last read price it.',
  typed: 'Maintenance intervals as typed here.',
  none: '',
}

/** Which figure priced a row, and how much logged history backs it (§3.4): a row set to the estimate carries no confidence. */
function basisText(row: Pick<WearPartRate, 'basis' | 'priced_by' | 'confidence'>): string {
  if (row.basis === 'actual') return `actual, ${row.confidence ?? 0} % confidence`
  return row.priced_by === 'actual' ? `estimate, ${row.confidence ?? 0} % confidence` : 'estimate'
}

/** Each part of the sum, as the rate is computed now. */
function Breakdown({ rate }: { rate: MachineRateBreakdown }) {
  if (rate.source === 'price_book') {
    return (
      <p className="text-xs text-muted-foreground" data-testid="machine-rate-book">
        No inputs yet: priced at the price book&rsquo;s machine rate, {rate.rate_per_hour} {rate.currency} per hour.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-1 text-xs" data-testid="machine-rate-breakdown">
      <p>
        <span className="font-medium">
          {rate.rate_per_hour} {rate.currency} per hour
        </span>
        {INTERVALS[rate.intervals] && <span className="text-muted-foreground"> · {INTERVALS[rate.intervals]}</span>}
      </p>
      {rate.actuals && <p className="text-muted-foreground">Some parts priced from actuals; lowest confidence {rate.confidence ?? 0} %</p>}
      <ul className="ml-3 flex flex-col text-muted-foreground" aria-label="Rate parts">
        <li>
          Depreciation {rate.depreciation_per_hour} per hour{rate.life_hours ? ` (${rate.purchase_price} over ${rate.life_hours} h)` : ' (no life set: none)'}
        </li>
        <li>
          Power {rate.power_per_hour} per hour ({rate.watts} W at {rate.kwh_price} per kWh)
        </li>
        <li>Wear parts {rate.wear_per_hour} per hour</li>
        {(rate.wear ?? []).map((w) => (
          <li key={w.name} className="ml-3">
            {w.basis === 'actual' ? `${w.name}: ${w.per_hour} per hour from logged replacements` : `${w.name}: ${w.cost} over ${w.life_hours} h = ${w.per_hour}`} ({basisText(w)})
          </li>
        ))}
        <li>Maintenance {rate.maintenance_per_hour} per hour</li>
        {(rate.maintenance ?? []).map((m) => (
          <li key={m.task} className="ml-3">
            {!m.counted ? (
              `${m.task}${m.interval_hours ? `, every ${m.interval_hours} ${m.interval_type === 'days' ? 'days' : 'h'}` : ''}: ${WHY[m.why ?? 'no_interval']}`
            ) : m.basis === 'actual' ? (
              `${m.task}: ${m.per_hour} per hour from logged maintenance (${basisText(m)})`
            ) : (
              `${m.task}${m.interval_hours ? `, every ${m.interval_hours} ${m.interval_type === 'days' ? 'days' : 'h'}` : ''}: ${m.per_hour} (${basisText(m)})`
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** One printer's inputs, edited whole and saved with PUT; Remove drops them (the book's rate again). */
function Editor({ printer, onClose }: { printer: PrinterRow; onClose: () => void }) {
  const linked = printer.bambuddy_printer_id != null
  const view = useAsync(() => api.machineRate(printer.id), [printer.id])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // #5774: the row whose log form is open, as `kind:stored name`.
  const [logging, setLogging] = useState<string | null>(null)
  const current = draft ?? (view.data ? draftOf(view.data) : null)
  // Bambuddy's own task names, from a read of Bambuddy itself, also before the first save.
  const bambuddyTasks = view.data?.bambuddy_tasks ?? []
  const actuals = new Map((view.data?.actuals.rows ?? []).map((row) => [`${row.kind}:${row.name}`, row]))
  const actualsWindow = view.data?.actuals.window ?? DEFAULT_WINDOW
  const wearRates = new Map((view.data?.breakdown.wear ?? []).map((w) => [w.name, w]))
  const taskRates = new Map((view.data?.breakdown.maintenance ?? []).map((m) => [m.task, m]))
  const logged = () => {
    setLogging(null)
    view.refresh()
  }

  function edit(change: (was: Draft) => Draft) {
    if (current) setDraft(change(current))
  }

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setDraft(null)
      view.refresh()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  function save() {
    if (!current) return
    const body = bodyOf(current, linked)
    if (typeof body === 'string') return setError(body)
    void act(() => api.setMachineRate(printer.id, body))
  }

  /**
   * #5774 — a stored row's figure pair, its events and its log form; a row not saved yet
   * has none. An event logged or changed reads the rate again and keeps the unsaved edits.
   */
  function rowActuals(
    kind: Kind,
    saved: string | null,
    pricedBy: PricedBy,
    setPricedBy: (next: PricedBy) => void,
    figures: { estimate?: string | null; notCounted?: string | null } | undefined,
    log: { canLog: boolean; form: (close: () => void) => ReactNode },
  ) {
    if (!saved || !figures || !view.data) return <p className="text-xs text-muted-foreground">Save to see its estimate and actual.</p>
    const key = `${kind}:${saved}`
    const row: RowActuals | undefined = actuals.get(key)
    return (
      <div className="flex flex-col gap-2">
        <FigurePair name={saved} kind={kind} estimate={figures.estimate} row={row} pricedBy={pricedBy} need={actualsWindow} notCounted={figures.notCounted} onPricedBy={setPricedBy} />
        <EventList
          printerId={printer.id}
          kind={kind}
          name={saved}
          row={row}
          linked={linked}
          onChanged={() => view.refresh()}
          action={
            log.canLog &&
            logging !== key && (
              <Button type="button" size="xs" variant="outline" onClick={() => setLogging(key)}>
                {kind === 'part' ? 'Log replacement' : 'Log done'}
              </Button>
            )
          }
        />
        {logging === key && log.form(() => setLogging(null))}
      </div>
    )
  }

  const profileField = (name: keyof Profile, label: string, placeholder: string) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`machine-${name}`}>{label}</Label>
      <Input
        id={`machine-${name}`}
        className="w-28"
        inputMode="decimal"
        placeholder={placeholder}
        value={current?.profile[name] ?? ''}
        onChange={(event) => edit((was) => ({ ...was, profile: { ...was.profile, [name]: event.target.value } }))}
      />
    </div>
  )

  return (
    <section className="flex flex-col gap-3 rounded-md border p-3" aria-label={`Machine rate of ${printer.name}`}>
      <div className="flex items-center justify-between gap-2">
        <h5 className="text-sm font-medium">{printer.name}</h5>
        <Button size="xs" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {view.error && (
        <p role="alert" className="text-[13px] text-warn">
          The machine rate could not be read: {view.error.message}
        </p>
      )}
      {view.data && <Breakdown rate={view.data.breakdown} />}
      {current && (
        <form
          className="flex flex-col gap-3"
          aria-label={`Machine rate inputs of ${printer.name}`}
          onSubmit={(event) => {
            event.preventDefault()
            save()
          }}
        >
          <fieldset className="flex flex-wrap items-end gap-2">
            <legend className="mb-1 text-xs font-medium">The printer</legend>
            {profileField('purchase_price', 'Purchase price', '1440')}
            {profileField('life_hours', 'Life (h)', '5000')}
            {profileField('watts', 'Watts', '200')}
            {profileField('kwh_price', 'Price of a kWh', '0.15')}
            {profileField('labour_rate_per_hour', 'Labour per hour', 'the shop’s')}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Label htmlFor="machine-actuals-window">Actuals window (events)</Label>
            <Input
              id="machine-actuals-window"
              className="w-16"
              inputMode="numeric"
              value={current.profile.actuals_window}
              onChange={(event) => edit((was) => ({ ...was, profile: { ...was.profile, actuals_window: event.target.value } }))}
            />
            <span className="text-muted-foreground">An actual prices once this many gaps or replacements are logged; until then the estimate prices.</span>
          </div>
          {linked && view.data && (
            <BambuddyRead
              printerId={printer.id}
              readAt={view.data.actuals.events_read_at}
              stale={view.data.breakdown.intervals === 'stored'}
              // The answer is the view: shown in place, the unsaved edits kept.
              onRead={(next) => view.setData(next, { supersede: true })}
              onUnanswered={() => view.refresh()}
            />
          )}
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-xs font-medium">Wear parts</legend>
            {current.wear.length > 0 && (
              <div className="flex flex-col divide-y rounded-md border">
                {current.wear.map((w, index) => (
                  <div key={w.key} className="flex flex-col gap-2 p-2" role="group" aria-label={`Wear part ${index + 1}`}>
                    <div className="flex flex-wrap items-end gap-2">
                      <Input aria-label="Part" className="w-40" placeholder="Nozzle" value={w.name} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, name: e.target.value } : x)) }))} />
                      <Input aria-label="Cost" className="w-24" inputMode="decimal" placeholder="35" value={w.cost} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, cost: e.target.value } : x)) }))} />
                      <Input aria-label="Life (h)" className="w-24" inputMode="decimal" placeholder="1000" value={w.life_hours} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, life_hours: e.target.value } : x)) }))} />
                      <Button type="button" size="xs" variant="ghost" aria-label={`Remove wear part ${w.name || index + 1}`} onClick={() => edit((was) => ({ ...was, wear: was.wear.filter((_, i) => i !== index) }))}>
                        Remove
                      </Button>
                    </div>
                    {rowActuals(
                      'part',
                      w.saved,
                      w.priced_by,
                      (next) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, priced_by: next } : x)) })),
                      w.saved && wearRates.has(w.saved) ? { estimate: wearRates.get(w.saved)?.estimate_per_hour } : undefined,
                      {
                        canLog: true,
                        form: (close) => <LogEventForm printerId={printer.id} kind="part" name={w.saved!} counter={view.data!.counter} cost={wearRates.get(w.saved!)?.cost} onLogged={logged} onCancel={close} />,
                      },
                    )}
                  </div>
                ))}
              </div>
            )}
            <Button type="button" size="xs" variant="outline" className="self-start" onClick={() => edit((was) => ({ ...was, wear: [...was.wear, { name: '', cost: '', life_hours: '', priced_by: 'estimate', saved: null, key: draftKey() }] }))}>
              Add wear part
            </Button>
          </fieldset>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-xs font-medium">Maintenance tasks</legend>
            <p className="text-xs text-muted-foreground">
              {linked
                ? "The time and consumables each of Bambuddy's tasks takes. Its interval is Bambuddy's: name the task as Bambuddy does. Mark tasks done in Bambuddy; ScadBuddy reads them."
                : 'The time and consumables each task takes, and how many print-hours apart it comes. Log each time you do one.'}
            </p>
            {linked && bambuddyTasks.length > 0 && (
              <datalist id={`bambuddy-tasks-${printer.id}`}>
                {bambuddyTasks.map((task) => (
                  <option key={task} value={task} />
                ))}
              </datalist>
            )}
            {current.maintenance.length > 0 && (
              <div className="flex flex-col divide-y rounded-md border">
                {current.maintenance.map((m, index) => {
                  const rate = m.saved ? taskRates.get(m.saved) : undefined
                  return (
                    <div key={m.key} className="flex flex-col gap-2 p-2" role="group" aria-label={`Maintenance task ${index + 1}`}>
                      <div className="flex flex-wrap items-end gap-2">
                        <Input
                          aria-label="Task"
                          className="w-48"
                          placeholder="Lubricate rails"
                          list={linked ? `bambuddy-tasks-${printer.id}` : undefined}
                          value={m.task}
                          onChange={(e) => edit((was) => ({ ...was, maintenance: was.maintenance.map((x, i) => (i === index ? { ...x, task: e.target.value } : x)) }))}
                        />
                        <Input aria-label="Minutes" className="w-20" inputMode="decimal" placeholder="20" value={m.minutes} onChange={(e) => edit((was) => ({ ...was, maintenance: was.maintenance.map((x, i) => (i === index ? { ...x, minutes: e.target.value } : x)) }))} />
                        <Input aria-label="Consumables" className="w-24" inputMode="decimal" placeholder="2" value={m.consumables} onChange={(e) => edit((was) => ({ ...was, maintenance: was.maintenance.map((x, i) => (i === index ? { ...x, consumables: e.target.value } : x)) }))} />
                        {!linked && (
                          <Input aria-label="Every (h)" className="w-24" inputMode="decimal" placeholder="50" value={m.interval_hours} onChange={(e) => edit((was) => ({ ...was, maintenance: was.maintenance.map((x, i) => (i === index ? { ...x, interval_hours: e.target.value } : x)) }))} />
                        )}
                        <Button type="button" size="xs" variant="ghost" aria-label={`Remove task ${m.task || index + 1}`} onClick={() => edit((was) => ({ ...was, maintenance: was.maintenance.filter((_, i) => i !== index) }))}>
                          Remove
                        </Button>
                      </div>
                      {rowActuals(
                        'task',
                        m.saved,
                        m.priced_by,
                        (next) => edit((was) => ({ ...was, maintenance: was.maintenance.map((x, i) => (i === index ? { ...x, priced_by: next } : x)) })),
                        rate ? { estimate: rate.estimate_per_hour, notCounted: rate.counted ? null : WHY[rate.why ?? 'no_interval'].replace(/: not counted$/, '') } : undefined,
                        {
                          // A linked printer's tasks are marked done in Bambuddy (§3.1).
                          canLog: !linked,
                          form: (close) => (
                            <LogEventForm
                              printerId={printer.id}
                              kind="task"
                              name={m.saved!}
                              counter={view.data!.counter}
                              typedMinutes={rate?.minutes}
                              typedConsumables={rate?.consumables}
                              onLogged={logged}
                              onCancel={close}
                            />
                          ),
                        },
                      )}
                    </div>
                  )
                })}
              </div>
            )}
            <Button
              type="button"
              size="xs"
              variant="outline"
              className="self-start"
              onClick={() => edit((was) => ({ ...was, maintenance: [...was.maintenance, { task: '', minutes: '', consumables: '', interval_hours: '', priced_by: 'estimate', saved: null, key: draftKey() }] }))}
            >
              Add task
            </Button>
          </fieldset>
          {view.data && (
            <PrintHours counter={view.data.counter} offset={current.profile.lan_hours_offset} onOffset={(next) => edit((was) => ({ ...was, profile: { ...was.profile, lan_hours_offset: next } }))} />
          )}
          {view.data && (view.data.actuals.orphaned ?? []).length > 0 && (
            <p data-testid="orphaned" className="text-xs text-muted-foreground">
              Events kept for rows removed:{' '}
              {(view.data.actuals.orphaned ?? [])
                .map((o) => `${o.name} (${o.events} ${o.kind === 'part' ? (o.events === 1 ? 'replacement' : 'replacements') : o.events === 1 ? 'time done' : 'times done'})`)
                .join(', ')}
              . Add a row with the same name to use them again.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm" disabled={busy}>
              Save machine rate
            </Button>
            {view.data?.inputs && (
              <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void act(() => api.deleteMachineRate(printer.id))}>
                Remove inputs
              </Button>
            )}
          </div>
          {error && (
            <p role="alert" className="text-[13px] text-warn">
              {error}
            </p>
          )}
        </form>
      )}
    </section>
  )
}

/**
 * #4625 (order operations spec 2026-11-07 §6) — under the Settings page's Orders section,
 * each registry printer's machine rate: what a print-hour costs on it, from its maintenance,
 * wear parts, power and depreciation, with each part of the sum. A slice quote is priced at
 * the rate of the printer it sliced for, and keeps it. A printer with no inputs is priced
 * at the price book's machine rate. Bambuddy's virtual printers are not print targets
 * (#4747), so they are not listed.
 */
export function MachineRates() {
  const printers = useAsync(() => api.listPrinters(), [])
  const [open, setOpen] = useState<string | null>(null)
  const rows = (printers.data ?? []).filter((p) => p.kind === 'physical')
  const chosen = rows.find((p) => p.id === open)

  const columns: Column<PrinterRow>[] = [
    { key: 'printer', header: 'Printer', className: 'break-words', cell: (p) => p.name },
    { key: 'link', header: 'Intervals', cell: (p) => (p.bambuddy_printer_id != null ? 'From Bambuddy' : 'Typed') },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      className: 'whitespace-nowrap text-right',
      cell: (p) => (
        <Button size="xs" variant="outline" onClick={() => setOpen(p.id)} aria-label={`Machine rate of ${p.name}`}>
          Machine rate
        </Button>
      ),
    },
  ]

  return (
    <section className="flex flex-col gap-2" aria-label="Machine rates">
      <h4 className="text-sm font-medium">Machine rate per printer</h4>
      <p className="text-xs text-muted-foreground">
        What a print-hour costs on each printer: its maintenance, wear parts, power and depreciation. A slice quote is priced at the rate of the printer it sliced for, and keeps that rate. A printer with no inputs is priced at the price book&rsquo;s machine rate.
      </p>
      {printers.error && (
        <p role="alert" className="text-[13px] text-warn">
          The printers could not be read: {printers.error.message}
        </p>
      )}
      <DataTable label="Machine rates" columns={columns} rows={rows} rowKey={(p) => p.id} empty="No printers yet. Add one under Printers." />
      {chosen && <Editor key={chosen.id} printer={chosen} onClose={() => setOpen(null)} />}
    </section>
  )
}

import { useState } from 'react'
import { api } from '../../api/client'
import type { MachineRateBreakdown, MachineRateInputs, MachineRateView, MaintenancePart, PrinterRow } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { DataTable, type Column } from '../records/DataTable'
import { refusalText } from './refusal'

// As `orders/machine_rate.py` takes them: hours, watts and minutes to 0.1, money to 0.0001.
const TENTHS = 1
const AMOUNT = 4
const decimal = (text: string, places: number) => new RegExp(`^\\d+(\\.\\d{1,${places}})?$`).test(text.trim())
const placesText = (places: number) => (places === TENTHS ? 'with at most 1 decimal place, such as 12.5' : 'with at most 4 decimal places, such as 12.5')

type Profile = { purchase_price: string; life_hours: string; watts: string; kwh_price: string; labour_rate_per_hour: string }
type Wear = { name: string; cost: string; life_hours: string }
type Task = { task: string; minutes: string; consumables: string; interval_hours: string }
type Draft = { profile: Profile; wear: Wear[]; maintenance: Task[] }

const EMPTY_PROFILE: Profile = { purchase_price: '', life_hours: '', watts: '', kwh_price: '', labour_rate_per_hour: '' }

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
    },
    wear: (inputs.wear_parts ?? []).map((w) => ({ name: w.name, cost: w.cost, life_hours: w.life_hours })),
    maintenance: (inputs.maintenance ?? []).map((m) => ({ task: m.task, minutes: m.minutes, consumables: m.consumables, interval_hours: m.interval_hours ?? '' })),
  }
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
    number(p.labour_rate_per_hour, 'the labour rate', AMOUNT)
  if (problem) return problem
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
  return {
    profile: {
      purchase_price: optional(p.purchase_price),
      life_hours: optional(p.life_hours) ?? null,
      watts: optional(p.watts),
      kwh_price: optional(p.kwh_price),
      labour_rate_per_hour: optional(p.labour_rate_per_hour) ?? null,
    },
    wear_parts: draft.wear.map((w) => ({ name: w.name.trim(), cost: w.cost.trim(), life_hours: w.life_hours.trim() })),
    maintenance: draft.maintenance.map((m) => ({
      task: m.task.trim(),
      minutes: optional(m.minutes),
      consumables: optional(m.consumables),
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
            {w.name}: {w.cost} over {w.life_hours} h = {w.per_hour}
          </li>
        ))}
        <li>Maintenance {rate.maintenance_per_hour} per hour</li>
        {(rate.maintenance ?? []).map((m) => (
          <li key={m.task} className="ml-3">
            {m.task}
            {m.interval_hours ? `, every ${m.interval_hours} ${m.interval_type === 'days' ? 'days' : 'h'}` : ''}: {m.counted ? m.per_hour : WHY[m.why ?? 'no_interval']}
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
  const current = draft ?? (view.data ? draftOf(view.data) : null)
  // Bambuddy's own task names, from a read of Bambuddy itself, also before the first save.
  const bambuddyTasks = view.data?.bambuddy_tasks ?? []

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
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-xs font-medium">Wear parts</legend>
            {current.wear.map((w, index) => (
              <div key={index} className="flex flex-wrap items-end gap-2" role="group" aria-label={`Wear part ${index + 1}`}>
                <Input aria-label="Part" className="w-40" placeholder="Nozzle" value={w.name} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, name: e.target.value } : x)) }))} />
                <Input aria-label="Cost" className="w-24" inputMode="decimal" placeholder="35" value={w.cost} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, cost: e.target.value } : x)) }))} />
                <Input aria-label="Life (h)" className="w-24" inputMode="decimal" placeholder="1000" value={w.life_hours} onChange={(e) => edit((was) => ({ ...was, wear: was.wear.map((x, i) => (i === index ? { ...x, life_hours: e.target.value } : x)) }))} />
                <Button type="button" size="xs" variant="ghost" aria-label={`Remove wear part ${w.name || index + 1}`} onClick={() => edit((was) => ({ ...was, wear: was.wear.filter((_, i) => i !== index) }))}>
                  Remove
                </Button>
              </div>
            ))}
            <Button type="button" size="xs" variant="outline" className="self-start" onClick={() => edit((was) => ({ ...was, wear: [...was.wear, { name: '', cost: '', life_hours: '' }] }))}>
              Add wear part
            </Button>
          </fieldset>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-xs font-medium">Maintenance tasks</legend>
            <p className="text-xs text-muted-foreground">
              {linked
                ? "The time and consumables each of Bambuddy's tasks takes. Its interval is Bambuddy's: name the task as Bambuddy does."
                : 'The time and consumables each task takes, and how many print-hours apart it comes.'}
            </p>
            {linked && bambuddyTasks.length > 0 && (
              <datalist id={`bambuddy-tasks-${printer.id}`}>
                {bambuddyTasks.map((task) => (
                  <option key={task} value={task} />
                ))}
              </datalist>
            )}
            {current.maintenance.map((m, index) => (
              <div key={index} className="flex flex-wrap items-end gap-2" role="group" aria-label={`Maintenance task ${index + 1}`}>
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
            ))}
            <Button
              type="button"
              size="xs"
              variant="outline"
              className="self-start"
              onClick={() => edit((was) => ({ ...was, maintenance: [...was.maintenance, { task: '', minutes: '', consumables: '', interval_hours: '' }] }))}
            >
              Add task
            </Button>
          </fieldset>
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

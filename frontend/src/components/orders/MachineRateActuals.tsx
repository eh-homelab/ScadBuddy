import { Fragment, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { ApiError, api, newRequestId } from '../../api/client'
import type { CounterView, MachineRateEvent, MachineRateEventPatch, MachineRateView, RowActuals } from '../../api/types'
import { formatFullTime, formatLocalTime, timeAgo } from '../../lib/format'
import { toast } from '../../lib/toast'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Spinner } from '@/components/ui/spinner'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { AMOUNT, decimal, placesText, TENTHS } from './machineRateNumbers'
import { refusalText } from './refusal'
import { useNow } from './useNow'

/**
 * #5774 (machine-rate actuals spec 2026-11-17 §9) — the parts of a printer's machine-rate
 * editor that show what really happened: each row's figure pair (the estimate and the
 * actual per hour, the one that prices in full weight) with its toggle and confidence line,
 * the logged events, the form that logs one, and the print-hours line.
 */


export type Kind = 'task' | 'part'
export type PricedBy = 'estimate' | 'actual'

/** What a row's "k of N" counts. */
const UNIT: Record<Kind, string> = { part: 'replacements', task: 'gaps' }
const COUNTER_NAME: Record<NonNullable<MachineRateEvent['hours_source']>, string> = { bambuddy: 'Bambuddy', lan_watch: 'ScadBuddy count', typed: 'typed' }
const SKIPPED: Record<NonNullable<RowActuals['skipped_gaps']>[number]['reason'], string> = { counter_changed: 'counter changed', not_positive: 'no hours between' }

const day = (iso: string) => iso.slice(0, 10)

/** Enter in a text field runs `action`, not the editor's own submit (these sit inside its form). */
function enterSubmits(event: KeyboardEvent<HTMLElement>, action: () => unknown) {
  if (event.key !== 'Enter' || !(event.target instanceof HTMLInputElement)) return
  event.preventDefault()
  void action()
}
const hoursText = (hours: number) => String(Number(hours.toFixed(1)))

/** How much logged history backs a row's actual, and whether it is in use (§3.4). */
function confidenceText(row: RowActuals | undefined, kind: Kind, pricedBy: PricedBy, need: number, notCounted?: string | null): string {
  const have = row?.have ?? 0
  const confidence = row?.confidence ?? 0
  let text = `${confidence} % confidence: ${have} of ${row?.need ?? need} ${UNIT[kind]} logged`
  if (row && have > 0) {
    text += kind === 'part' ? `, ${row.actual_hours_used} h each on average` : `, done every ${row.actual_interval_hours} h on average`
    if (row.partly_typed) text += '; some use the typed minutes or consumables'
  }
  if (notCounted) return `${text}.`
  if (pricedBy === 'estimate') return `${text}. The estimate is chosen.`
  return have >= (row?.need ?? need) ? `${text}. The actual prices.` : `Estimate prices, ${text}`
}

/**
 * A row's estimate and actual per hour side by side, the toggle between them (`priced_by`),
 * and under them the confidence line. The figure that prices is in full weight, the other
 * muted, never hidden. A task not counted prices by neither.
 */
export function FigurePair({
  name,
  kind,
  estimate,
  row,
  pricedBy,
  need,
  notCounted,
  onPricedBy,
}: {
  name: string
  kind: Kind
  estimate: string | null | undefined
  row: RowActuals | undefined
  pricedBy: PricedBy
  need: number
  notCounted?: string | null
  onPricedBy: (next: PricedBy) => void
}) {
  const ready = row != null && row.have >= row.need
  const prices: PricedBy | null = notCounted ? null : pricedBy === 'actual' && ready ? 'actual' : 'estimate'
  const figure = (which: PricedBy) =>
    `text-sm tabular-nums ${prices === which ? 'font-medium text-foreground' : 'text-muted-foreground'}`
  return (
    <div className="flex flex-col gap-1">
      <div className="grid grid-cols-2 items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]">
        <p data-testid="figure-estimate" data-prices={String(prices === 'estimate')} className={`${figure('estimate')} sm:col-start-1 sm:row-start-1`}>
          Estimate {estimate ? `${estimate}/h` : 'none'}
          {prices === 'estimate' && <span className="sr-only">, prices the rate</span>}
        </p>
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          spacing={0}
          value={pricedBy}
          onValueChange={(value) => value && onPricedBy(value as PricedBy)}
          aria-label={`Which figure prices ${name}`}
          className="col-span-2 row-start-2 w-full sm:col-span-1 sm:col-start-2 sm:row-start-1 sm:w-auto"
        >
          <ToggleGroupItem value="estimate" className="flex-1 sm:flex-none">
            Estimate
          </ToggleGroupItem>
          <ToggleGroupItem value="actual" className="flex-1 sm:flex-none">
            Actual
          </ToggleGroupItem>
        </ToggleGroup>
        <p data-testid="figure-actual" data-prices={String(prices === 'actual')} className={`${figure('actual')} text-right sm:col-start-3 sm:row-start-1`}>
          {row?.actual_per_hour ? `Actual ${row.actual_per_hour}/h` : 'No events yet'}
          {prices === 'actual' && <span className="sr-only">, prices the rate</span>}
        </p>
      </div>
      <p data-testid="confidence" className="text-xs text-muted-foreground sm:text-right">
        {notCounted && `Not counted (${notCounted}), so neither figure prices. `}
        {confidenceText(row, kind, pricedBy, need, notCounted)}
      </p>
    </div>
  )
}

const TASK_EMPTY = { linked: 'None read from Bambuddy yet. Mark it done in Bambuddy; ScadBuddy reads it.', here: 'Not logged yet. Log it when you do this task.' }

/**
 * A row's events in its window, newest first, opened in place: when, the counter and its
 * source, the gap or hours used (or why it was skipped), the cost or minutes, and where it
 * came from. A ScadBuddy event can be edited or deleted (after a confirm); one read from
 * Bambuddy takes its minutes and consumables only. "Show older events" pages back through
 * every event past the window (#6665), listed under a line that says the rate does not use them.
 */
export function EventList({
  printerId,
  kind,
  name,
  row,
  linked,
  onChanged,
  action,
}: {
  printerId: string
  kind: Kind
  name: string
  row: RowActuals | undefined
  linked: boolean
  onChanged: () => void
  /** Beside the list's toggle: the button that logs one. */
  action?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  // #6665: every event, paged back past the window (`GET …/machine-rate/events`).
  const [older, setOlder] = useState<{ events: MachineRateEvent[]; next: string | null } | null>(null)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [olderError, setOlderError] = useState<string | null>(null)
  const events = row?.events ?? []
  // An event logged or read from Bambuddy changes the window: the pages read before it may miss it.
  const windowKey = events.map((event) => event.id).join(',')
  const [pagedFor, setPagedFor] = useState(windowKey)
  // Each reset of the pages starts a new generation; a page read for an older one is dropped.
  const [generation, setGeneration] = useState(0)
  const latest = useRef(generation)
  useEffect(() => {
    latest.current = generation
  }, [generation])
  if (pagedFor !== windowKey) {
    setPagedFor(windowKey)
    setOlder(null)
    setGeneration((g) => g + 1)
  }
  const inWindow = new Set(events.map((event) => event.id))
  const past = (older?.events ?? []).filter((event) => !inWindow.has(event.id))
  const shown = [...events, ...past]
  const skipped = new Map((row?.skipped_gaps ?? []).map((gap) => [gap.event, gap.reason]))
  const title = kind === 'part' ? 'Replacements' : 'Done'
  const listId = `events-${useId()}`
  const changed = () => {
    setOlder(null)
    setGeneration((g) => g + 1)
    onChanged()
  }

  async function loadOlder() {
    const asked = generation
    setLoadingOlder(true)
    setOlderError(null)
    try {
      const page = await api.machineRateEvents(printerId, { kind, name, cursor: older?.next })
      if (latest.current !== asked) return
      setOlder((was) => ({ events: [...(was?.events ?? []), ...page.events], next: page.next ?? null }))
    } catch (err) {
      setOlderError(refusalText(err))
    } finally {
      setLoadingOlder(false)
    }
  }

  const more = older == null || older.next != null
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="xs" variant="ghost" aria-expanded={open} aria-controls={open ? listId : undefined} onClick={() => setOpen(!open)}>
          {title} ({events.length})
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        </Button>
        {action}
      </div>
      {open &&
        (shown.length === 0 ? (
          <p id={listId} className="text-xs text-muted-foreground">
            {kind === 'part' ? 'No replacements logged. Log one when you change this part.' : linked ? TASK_EMPTY.linked : TASK_EMPTY.here}
          </p>
        ) : (
          <ul id={listId} aria-label={`${title}: ${name}`} className="flex flex-col divide-y rounded-md border text-xs">
            {shown.map((event, index) => (
              <Fragment key={event.id}>
                {index === events.length && (
                  <li role="separator" className="bg-muted/40 px-2 py-1 text-muted-foreground">
                    Older events, not in the rate (it uses the last {row?.need ?? events.length} {UNIT[kind]})
                  </li>
                )}
                <EventItem printerId={printerId} event={event} before={shown[index + 1]} skipped={skipped.get(event.id)} onChanged={changed} />
              </Fragment>
            ))}
          </ul>
        ))}
      {open && (shown.length > 0 || older != null) && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {more ? (
            <Button type="button" size="xs" variant="ghost" disabled={loadingOlder} onClick={() => void loadOlder()}>
              {older == null ? 'Show older events' : 'Show more'}
            </Button>
          ) : (
            <span className="text-muted-foreground">That is every event.</span>
          )}
          {olderError && (
            <span role="alert" className="text-warn">
              The older events could not be read: {olderError}
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function gapText(event: MachineRateEvent, before: MachineRateEvent | undefined, skipped: string | undefined): string | null {
  if (skipped) return `Skipped: ${SKIPPED[skipped as keyof typeof SKIPPED]}`
  if (event.kind === 'part' && event.hours_used != null) return `ran ${event.hours_used} h (typed)`
  if (!before || event.hours_at == null || before.hours_at == null) return null
  const hours = hoursText(Number(event.hours_at) - Number(before.hours_at))
  return event.kind === 'part' ? `ran ${hours} h` : `${hours} h since the one before`
}

function costText(event: MachineRateEvent): string {
  if (event.kind === 'part') return `cost ${event.cost}`
  if (event.minutes == null && event.consumables == null) return 'typed minutes and consumables'
  return `${event.minutes == null ? 'typed' : event.minutes} min, ${event.consumables == null ? 'typed' : event.consumables} consumables`
}

function EventItem({
  printerId,
  event,
  before,
  skipped,
  onChanged,
}: {
  printerId: string
  event: MachineRateEvent
  before: MachineRateEvent | undefined
  skipped: string | undefined
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const imported = event.origin === 'bambuddy'
  const counter = event.hours_at == null ? 'no reading' : `${event.hours_at} h, ${event.hours_source ? COUNTER_NAME[event.hours_source] : 'typed'}`
  const gap = gapText(event, before, skipped)

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteMachineRateEvent(printerId, event.id)
      setConfirming(false)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <li className="flex flex-col gap-1 p-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <p className="flex flex-wrap gap-x-3">
          <span className="font-medium">{day(event.at)}</span>
          <span className="tabular-nums">{counter}</span>
          {gap && <span className={skipped ? 'text-warn' : 'tabular-nums'}>{gap}</span>}
        </p>
        {!editing && (
          <div className="flex gap-1">
            {imported ? (
              event.kind === 'task' && (
                <Button type="button" size="xs" variant="ghost" onClick={() => setEditing(true)}>
                  {event.minutes == null && event.consumables == null ? 'Add minutes and consumables' : 'Edit minutes and consumables'}
                </Button>
              )
            ) : (
              <>
                <Button type="button" size="xs" variant="ghost" onClick={() => setEditing(true)}>
                  Edit
                </Button>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    setError(null)
                    setConfirming(true)
                  }}
                >
                  Delete
                </Button>
              </>
            )}
          </div>
        )}
      </div>
      <p className="flex flex-wrap gap-x-3 text-muted-foreground">
        <span>{costText(event)}</span>
        <span>{imported ? 'from Bambuddy' : 'logged here'}</span>
        {event.notes && <span className="break-words">{event.notes}</span>}
      </p>
      {editing && (
        <EventEdit
          printerId={printerId}
          event={event}
          onDone={(changed) => {
            setEditing(false)
            if (changed) onChanged()
          }}
        />
      )}
      {error && !confirming && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {/* #6665: a delete asks first, as the other deletes on the Orders pages do. */}
      <AlertDialog open={confirming} onOpenChange={(next) => !next && !busy && setConfirming(false)}>
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{event.kind === 'part' ? `Delete this replacement of ${event.name}?` : `Delete this time ${event.name} was done?`}</AlertDialogTitle>
            <AlertDialogDescription>
              Logged on {day(event.at)}. The rate stops counting it, and it cannot be brought back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <Button variant="destructive" disabled={busy} onClick={() => void remove()}>
              {busy && <Spinner />}
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </li>
  )
}

type EditField = 'hours_at' | 'hours_used' | 'cost' | 'minutes' | 'consumables' | 'notes'
const EDIT_LABEL: Record<EditField, string> = {
  hours_at: 'Counter reading (h)',
  hours_used: 'Hours used (h)',
  cost: 'Cost',
  minutes: 'Minutes',
  consumables: 'Consumables',
  notes: 'Notes',
}
const EDIT_PLACES: Partial<Record<EditField, number>> = { hours_at: TENTHS, hours_used: TENTHS, minutes: TENTHS, cost: AMOUNT, consumables: AMOUNT }

/** The fields an event can be corrected in: only its minutes and consumables when read from Bambuddy. */
function EventEdit({ printerId, event, onDone }: { printerId: string; event: MachineRateEvent; onDone: (changed: boolean) => void }) {
  const fields: EditField[] =
    event.origin === 'bambuddy' ? ['minutes', 'consumables'] : event.kind === 'part' ? ['hours_at', 'hours_used', 'cost', 'notes'] : ['hours_at', 'minutes', 'consumables', 'notes']
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(fields.map((f) => [f, event[f] ?? ''])))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    const body: MachineRateEventPatch = {}
    for (const field of fields) {
      const text = (values[field] ?? '').trim()
      if (text === (event[field] ?? '')) continue
      const places = EDIT_PLACES[field]
      if (text && places != null && !decimal(text, places)) return setError(`Enter the ${EDIT_LABEL[field].replace(' (h)', '').toLowerCase()} as a number ${placesText(places)}.`)
      if (field === 'cost' && !text) return setError('Enter the cost of the new part.')
      body[field] = text || null
    }
    if (Object.keys(body).length === 0) return onDone(false)
    setBusy(true)
    setError(null)
    try {
      await api.patchMachineRateEvent(printerId, event.id, body)
      onDone(true)
    } catch (err) {
      setError(refusalText(err))
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2 pt-1" onKeyDown={(e) => enterSubmits(e, save)}>
      <div className="flex flex-wrap items-end gap-2">
        {fields.map((field) => (
          <div key={field} className="flex flex-col gap-1">
            <Label htmlFor={`event-${event.id}-${field}`}>{EDIT_LABEL[field]}</Label>
            <Input
              id={`event-${event.id}-${field}`}
              className={field === 'notes' ? 'w-56' : 'w-24'}
              inputMode={field === 'notes' ? undefined : 'decimal'}
              placeholder={field === 'minutes' || field === 'consumables' ? 'typed' : undefined}
              value={values[field]}
              onChange={(e) => setValues({ ...values, [field]: e.target.value })}
            />
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <Button type="button" size="xs" disabled={busy} onClick={() => void save()}>
          Save
        </Button>
        <Button type="button" size="xs" variant="ghost" onClick={() => onDone(false)}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

const localDay = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`

/** Where the counter reading in the log form came from, or why there is none. */
function readingHint(counter: CounterView): string {
  if (counter.hours != null) return counter.source === 'bambuddy' ? 'from Bambuddy' : 'from ScadBuddy’s count'
  if (counter.source === 'bambuddy') return 'Bambuddy did not answer: enter the reading, or try again.'
  if (counter.source === 'lan_watch') return 'ScadBuddy has not counted this printer yet: enter the reading.'
  return 'This printer has no counter: enter the reading.'
}

const NO_BAMBUDDY_READING = 'Bambuddy did not answer, so there is no counter reading. Try again, or enter the reading.'

/**
 * Logs a replaced part or a task done. The counter reading is filled from the printer's
 * counter and left out of the request unless the person changes it, so the backend reads
 * the counter itself and records its source; a changed one is a correction. Only a printer
 * with no counter shows it empty and required. One writer key per form, so a retry writes once.
 */
export function LogEventForm({
  printerId,
  kind,
  name,
  counter,
  cost,
  typedMinutes,
  typedConsumables,
  onLogged,
  onCancel,
}: {
  printerId: string
  kind: Kind
  name: string
  counter: CounterView
  cost?: string
  typedMinutes?: string
  typedConsumables?: string
  onLogged: () => void
  onCancel: () => void
}) {
  const filled = counter.hours ?? ''
  // getRandomValues, not randomUUID: plain http on the LAN and Bambuddy's iframe have no secure context.
  const [source] = useState(() => newRequestId())
  const [date, setDate] = useState(() => localDay())
  const [reading, setReading] = useState(filled)
  const [usedOpen, setUsedOpen] = useState(false)
  const [used, setUsed] = useState('')
  const [money, setMoney] = useState(cost ?? '')
  const [minutes, setMinutes] = useState('')
  const [consumables, setConsumables] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const formId = useId()
  const id = (field: string) => `log-${formId}-${field}`
  const action = kind === 'part' ? 'Log replacement' : 'Log done'

  async function submit() {
    const hoursAt = reading.trim()
    const hoursUsed = usedOpen ? used.trim() : ''
    if (date && date > localDay()) return setError('Enter a date no later than today.')
    if (hoursAt && !decimal(hoursAt, TENTHS)) return setError(`Enter the counter reading as a number ${placesText(TENTHS)}.`)
    if (hoursUsed && (!decimal(hoursUsed, TENTHS) || Number(hoursUsed) <= 0)) return setError(`Enter the hours used as a number above 0 ${placesText(TENTHS)}.`)
    if (!hoursAt && counter.source === 'typed' && !hoursUsed)
      return setError(kind === 'part' ? 'Enter the counter reading, or the hours the old part ran.' : 'Enter the counter reading: this printer has no counter.')
    if (kind === 'part' && !decimal(money, AMOUNT)) return setError(`Enter what the new part cost, as a number ${placesText(AMOUNT)}.`)
    if (minutes.trim() && !decimal(minutes, TENTHS)) return setError(`Enter the minutes as a number ${placesText(TENTHS)}.`)
    if (consumables.trim() && !decimal(consumables, AMOUNT)) return setError(`Enter the consumables as a number ${placesText(AMOUNT)}.`)
    setBusy(true)
    setError(null)
    try {
      await api.addMachineRateEvent(printerId, {
        kind,
        name,
        source,
        // Today is now; another day is that day's noon, so its date stays the same in every zone.
        at: date && date !== localDay() ? new Date(`${date}T12:00:00`).toISOString() : undefined,
        hours_at: hoursAt && hoursAt !== filled ? hoursAt : undefined,
        hours_used: hoursUsed || undefined,
        cost: kind === 'part' ? money.trim() : undefined,
        minutes: kind === 'task' ? minutes.trim() || undefined : undefined,
        consumables: kind === 'task' ? consumables.trim() || undefined : undefined,
        notes: notes.trim() || undefined,
      })
      toast({ title: kind === 'part' ? 'Replacement logged' : 'Maintenance logged', detail: name })
      onLogged()
    } catch (err) {
      const code = err instanceof ApiError ? (err.problem.type ?? '').split('/').pop() : null
      setError(code === 'counter_unavailable' && counter.source === 'bambuddy' ? NO_BAMBUDDY_READING : refusalText(err))
      setBusy(false)
    }
  }

  const field = (key: string, label: string, input: ReactNode, hint?: string) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id(key)}>{label}</Label>
      {input}
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  )

  return (
    // Not a <form>: it sits inside the editor's form, and forms do not nest. Enter in a
    // field logs the event here instead of saving the editor.
    <div
      role="form"
      aria-label={kind === 'part' ? `Log replacement of ${name}` : `Log ${name} done`}
      className="flex flex-col gap-2 rounded-md bg-muted/40 p-2"
      onKeyDown={(event) => enterSubmits(event, submit)}
    >
      <div className="flex flex-wrap items-start gap-2">
        {field('date', 'Date', <Input id={id('date')} type="date" className="w-40" value={date} onChange={(e) => setDate(e.target.value)} />)}
        {field(
          'reading',
          'Counter reading (h)',
          <Input id={id('reading')} className="w-28" inputMode="decimal" value={reading} onChange={(e) => setReading(e.target.value)} />,
          // A late-dated event with the counter left as filled would log its gap short.
          counter.hours != null && date && date !== localDay() && reading.trim() === filled ? `${readingHint(counter)}. This is the reading now: change it to the reading on that day.` : readingHint(counter),
        )}
        {kind === 'part' && field('cost', 'Cost', <Input id={id('cost')} className="w-24" inputMode="decimal" value={money} onChange={(e) => setMoney(e.target.value)} />)}
        {kind === 'task' && field('minutes', 'Minutes', <Input id={id('minutes')} className="w-20" inputMode="decimal" placeholder={typedMinutes} value={minutes} onChange={(e) => setMinutes(e.target.value)} />)}
        {kind === 'task' &&
          field('consumables', 'Consumables', <Input id={id('consumables')} className="w-24" inputMode="decimal" placeholder={typedConsumables} value={consumables} onChange={(e) => setConsumables(e.target.value)} />)}
        {field('notes', 'Notes', <Input id={id('notes')} className="w-56" value={notes} onChange={(e) => setNotes(e.target.value)} />)}
      </div>
      {kind === 'task' && <p className="text-xs text-muted-foreground">Leave the minutes or consumables empty to use the task&rsquo;s typed figures.</p>}
      {kind === 'part' && (
        <div className="flex flex-col gap-1">
          <Button type="button" size="xs" variant="ghost" className="self-start" aria-expanded={usedOpen} onClick={() => setUsedOpen(!usedOpen)}>
            Hours used instead
          </Button>
          {usedOpen &&
            field(
              'used',
              'Hours used (h)',
              <Input id={id('used')} className="w-28" inputMode="decimal" value={used} onChange={(e) => setUsed(e.target.value)} />,
              'For a part fitted before the counter started: the hours the old part ran.',
            )}
        </div>
      )}
      <div className="flex gap-2">
        <Button type="button" size="xs" disabled={busy} onClick={() => void submit()}>
          {action}
        </Button>
        <Button type="button" size="xs" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/**
 * The printer's print-hour counter (§3.2): Bambuddy's on a linked printer (with
 * ScadBuddy's own count beside it as a check when it has one), ScadBuddy's own count with
 * its offset on a printer only the LAN reaches, or none.
 */
export function PrintHours({ counter, offset, onOffset }: { counter: CounterView; offset: string; onOffset: (next: string) => void }) {
  const lan = counter.lan_watch
  return (
    <div data-testid="print-hours" className="flex flex-col gap-1 text-xs">
      <p className="text-xs font-medium">Print-hours</p>
      {counter.source === 'bambuddy' && (
        <p className="flex flex-wrap gap-x-3">
          {counter.hours != null ? <span className="tabular-nums">{counter.hours} h from Bambuddy</span> : <span className="text-warn">Bambuddy did not answer, so there is no counter reading now.</span>}
          {lan && <span className="text-muted-foreground tabular-nums">ScadBuddy&rsquo;s count: {lan.hours} h, a check only</span>}
        </p>
      )}
      {counter.source === 'lan_watch' && (
        <>
          <p className="tabular-nums">
            {lan ? `${lan.hours} h, counted by ScadBuddy since ${day(lan.started_at)}` : 'ScadBuddy starts counting with the printer’s first report.'}
          </p>
          {lan && Number(lan.unseen_hours) > 0 && <p className="text-muted-foreground">{lan.unseen_hours} h not seen (the printer was offline and its state changed)</p>}
          <div className="flex flex-wrap items-center gap-2">
            <Label htmlFor="machine-lan-offset">Offset (h)</Label>
            <Input id="machine-lan-offset" className="w-24" inputMode="decimal" placeholder="0.0" value={offset} onChange={(e) => onOffset(e.target.value)} />
            <span className="text-muted-foreground">the hours the printer ran before ScadBuddy watched it</span>
          </div>
        </>
      )}
      {counter.source === 'typed' && <p className="text-muted-foreground">No counter: enter the reading when you log an event.</p>}
    </div>
  )
}

/** §9: a failed read names what the rate uses now. */
function unansweredText(readAt: string | null | undefined, now: number): string {
  return readAt
    ? `Bambuddy did not answer. The rate uses the events last read at ${formatLocalTime(Date.parse(readAt), now)}.`
    : 'Bambuddy did not answer. No maintenance has been read from it yet, so the rate uses only the events logged here.'
}

const READ_REFUSALS: Record<string, string> = {
  not_linked: 'This printer is no longer linked to Bambuddy, so there is no maintenance log to read.',
  no_machine_rate: 'Save the machine rate first, then read its maintenance from Bambuddy.',
}

/** `unanswered` keeps the view's read time when the press started (the server's clock): a later one means Bambuddy answered since. */
type ReadFailure = { kind: 'unanswered'; seen: string | null | undefined } | { kind: 'refused'; text: string }

/**
 * #6665 (spec §5, §9) — "Read from Bambuddy" beside the window field on a linked printer:
 * reads the printer's maintenance log now and shows the answer's view (`onRead`), and says
 * when it was last read. A read that fails says why in plain words; one Bambuddy did not
 * answer names the read the rate still uses. `stale`: the page's own read of Bambuddy failed.
 */
export function BambuddyRead({
  printerId,
  readAt,
  stale,
  onRead,
  onUnanswered,
}: {
  printerId: string
  readAt: string | null | undefined
  stale: boolean
  onRead: (view: MachineRateView) => void
  /** Bambuddy did not answer: read the view again for the stored events and their time. */
  onUnanswered: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<ReadFailure | null>(null)
  const now = useNow(true, 30_000)

  async function read() {
    const seen = readAt
    setBusy(true)
    setFailure(null)
    try {
      onRead(await api.importMachineRate(printerId))
      toast({ title: 'Maintenance read from Bambuddy' })
    } catch (err) {
      const code = err instanceof ApiError ? ((err.problem.type ?? '').split('/').pop() ?? '') : ''
      if (code === 'log_unavailable') {
        setFailure({ kind: 'unanswered', seen })
        onUnanswered()
      } else setFailure({ kind: 'refused', text: READ_REFUSALS[code] ?? refusalText(err) })
    } finally {
      setBusy(false)
    }
  }

  const readMs = readAt ? Date.parse(readAt) : NaN
  // The view read after a failed import reads the log too: a read time later than the one seen
  // when the press started means Bambuddy answered it, so the alert goes. The line stays, not as
  // an alert, while the view's own task read fell back to stored intervals. Both times are the
  // server's, so the browser's clock never decides it.
  const seenMs = failure?.kind === 'unanswered' && failure.seen ? Date.parse(failure.seen) : NaN
  const unanswered = failure?.kind === 'unanswered' && (Number.isNaN(readMs) || (!Number.isNaN(seenMs) && readMs <= seenMs))
  return (
    <div className="flex flex-col gap-1 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="xs" variant="outline" disabled={busy} onClick={() => void read()}>
          {busy && <Spinner />}
          Read from Bambuddy
        </Button>
        <span data-testid="events-read" className="text-muted-foreground">
          {readAt && !Number.isNaN(readMs) ? (
            <>
              Last read{' '}
              <time dateTime={readAt} title={formatFullTime(readMs)}>
                {now - readMs < 60_000 ? 'just now' : timeAgo(readAt, now)}
              </time>
            </>
          ) : (
            'Not read from Bambuddy yet'
          )}
        </span>
      </div>
      {failure?.kind === 'refused' && (
        <p role="alert" className="text-[13px] text-warn">
          {failure.text}
        </p>
      )}
      {/* The view's own task read fell back to stored intervals: say so whatever the button's last read did. */}
      {(unanswered || stale) && (
        <p role={unanswered ? 'alert' : undefined} className="text-[13px] text-warn">
          {unansweredText(readAt, now)}
        </p>
      )}
    </div>
  )
}

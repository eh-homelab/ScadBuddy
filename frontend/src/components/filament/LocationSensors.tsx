import { ArchiveIcon, BellIcon, BellOffIcon, ChartSplineIcon, PencilIcon, PlusIcon } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { api, ApiError } from '../../api/client'
import { useAsync } from '../../lib/useAsync'
import {
  archiveSensor,
  createSensor,
  listHaEntities,
  listSensorReadings,
  patchSensor,
  type HaEntity,
  type Sensor,
  type SensorNotification,
  type SensorPatch,
  type SensorReading,
} from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'
import { SensorChart } from './SensorChart'

interface Props {
  locationId: string
  locationName: string
  sensors: readonly Sensor[]
  /** A place the user keeps can take a Home Assistant sensor; a printer's AMS cannot. */
  canAdd: boolean
  /** Called after any change, so the place's list loads again. */
  onChanged: () => void
}

const KINDS = { humidity: 'Humidity', temperature: 'Temperature' } as const
const HISTORY_MS = 24 * 3_600_000
const message = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

const withUnit = (value: number, unit: string | null) => (unit ? `${value} ${unit}` : String(value))

/** Whether the reading is over `alert_above`; an alerting sensor that is not is under `alert_below`. */
const overLimit = (sensor: Sensor) => sensor.last_value != null && sensor.alert_above != null && sensor.last_value > sensor.alert_above

function limitsText(sensor: Sensor): string | null {
  const parts = [
    sensor.alert_above != null && `above ${withUnit(sensor.alert_above, sensor.unit)}`,
    sensor.alert_below != null && `below ${withUnit(sensor.alert_below, sensor.unit)}`,
  ].filter(Boolean)
  return parts.length ? `Alert ${parts.join(', ')}` : null
}

/** A limit field: blank clears it; anything else must be a number. */
function limit(text: string): number | null | undefined {
  if (text.trim() === '') return null
  const value = Number(text)
  return Number.isFinite(value) ? value : undefined
}

interface Draft {
  name: string
  unit: string
  above: string
  below: string
  notify: boolean
}

const draftOf = (sensor?: Sensor): Draft => ({
  name: sensor?.name ?? '',
  unit: sensor?.unit ?? '',
  above: sensor?.alert_above == null ? '' : String(sensor.alert_above),
  below: sensor?.alert_below == null ? '' : String(sensor.alert_below),
  notify: sensor?.notify ?? false,
})

const shortTime = (at: string) => new Date(at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

/** #6646 — what a failed notification's `error` code means, and what fixes it. */
const NOTIFY_FAILURE: Record<NonNullable<SensorNotification['error']>, string> = {
  unauthorized: "Bambuddy refused ScadBuddy's API key. Check the key in Settings.",
  scope: "ScadBuddy's API key lacks a permission. Give the key the Send notifications permission in Bambuddy.",
  not_found: 'Bambuddy has no route for messages from connected apps. Bambuddy must be 1.3.1 or later.',
  no_channel: 'No Bambuddy channel takes messages from connected apps. Turn that on for a channel in Bambuddy.',
  rate_limited: 'Bambuddy refused it for sending too many messages in a minute.',
  unavailable: 'Bambuddy could not be reached.',
  rejected: 'Bambuddy refused the message.',
  not_configured: 'Bambuddy is not connected. Connect it in Settings.',
  internal: 'ScadBuddy could not send it.',
  cancelled: 'Notify was turned off, or the sensor or its place was archived, before it went out.',
  superseded: 'A newer change of the sensor went out instead.',
  interrupted: 'The send stopped before Bambuddy answered, so it may have gone out. It is not sent again.',
}

/** The sensor's newest notification: sent, waiting to go, or failed and why. */
function LastNotification({ notification, connected }: { notification: SensorNotification; connected: boolean | undefined }) {
  const what = notification.kind === 'started' ? 'alert started' : 'alert ended'
  if (notification.state === 'failed')
    return (
      <p className="flex items-start gap-1.5 text-warn">
        <BellOffIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
        <span>
          {notification.error === 'interrupted' ? 'Notification may not have been sent' : 'Notification not sent'} ({what}, <time dateTime={notification.read_at}>{shortTime(notification.read_at)}</time>).{' '}
          {notification.error ? NOTIFY_FAILURE[notification.error] : null}
        </span>
      </p>
    )
  if (notification.state === 'sent' && notification.sent_at)
    return (
      <p className="flex items-start gap-1.5 text-muted-foreground">
        <BellIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
        <span>
          Notified through Bambuddy ({what}) <time dateTime={notification.sent_at}>{shortTime(notification.sent_at)}</time>
          {notification.channels != null && `, to ${notification.channels} ${notification.channels === 1 ? 'channel' : 'channels'}`}.
        </span>
      </p>
    )
  return (
    <p className="flex items-start gap-1.5 text-muted-foreground">
      <BellIcon aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>
        {notification.state === 'sending' ? 'Notification sent to Bambuddy, no answer yet' : 'Notification waiting'} ({what},{' '}
        <time dateTime={notification.read_at}>{shortTime(notification.read_at)}</time>).
        {notification.state === 'queued' && connected === true && ' It goes out through Bambuddy within about a minute.'}
        {notification.state === 'queued' && connected === false && ' It goes out once Bambuddy is connected in Settings.'}
      </span>
    </p>
  )
}

function LimitFields({ draft, set }: { draft: Draft; set: (next: Draft) => void }) {
  return (
    <>
      <label className="flex flex-col gap-1">
        Name
        <Input value={draft.name} required onChange={(event) => set({ ...draft, name: event.target.value })} />
      </label>
      <label className="flex flex-col gap-1">
        Unit
        <Input value={draft.unit} onChange={(event) => set({ ...draft, unit: event.target.value })} />
      </label>
      <label className="flex flex-col gap-1">
        Alert above
        <Input inputMode="decimal" value={draft.above} onChange={(event) => set({ ...draft, above: event.target.value })} />
      </label>
      <label className="flex flex-col gap-1">
        Alert below
        <Input inputMode="decimal" value={draft.below} onChange={(event) => set({ ...draft, below: event.target.value })} />
      </label>
      <label className="flex items-center gap-2 sm:col-span-2">
        <Checkbox checked={draft.notify} onCheckedChange={(checked) => set({ ...draft, notify: checked === true })} />
        Notify me through Bambuddy when it starts and stops alerting
      </label>
    </>
  )
}

function History({ sensor }: { sensor: Sensor }) {
  const [readings, setReadings] = useState<SensorReading[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let current = true
    listSensorReadings(sensor.id, { since: new Date(Date.now() - HISTORY_MS).toISOString(), limit: 500 }).then(
      (rows) => current && setReadings(rows),
      () => current && setError('The history could not be loaded. Try again.'),
    )
    return () => {
      current = false
    }
  }, [sensor.id, sensor.last_at])
  if (error)
    return (
      <p role="alert" className="text-[12px] text-warn">
        {error}
      </p>
    )
  if (!readings) return <p className="text-[12px] text-muted-foreground">Loading the history…</p>
  return (
    <div className="flex flex-col gap-1">
      <SensorChart readings={readings} unit={sensor.unit} label={sensor.name} alertAbove={sensor.alert_above} alertBelow={sensor.alert_below} />
      <p className="text-[12px] text-muted-foreground">Last 24 hours</p>
    </div>
  )
}

function EditSensor({ sensor, onDone }: { sensor: Sensor; onDone: (saved: boolean) => void }) {
  const [draft, setDraft] = useState(() => draftOf(sensor))
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function save(event: FormEvent) {
    event.preventDefault()
    const above = limit(draft.above)
    const below = limit(draft.below)
    if (above === undefined || below === undefined) return setError('Enter each limit as a number, or leave it empty.')
    const body: SensorPatch = { name: draft.name.trim(), unit: draft.unit.trim() || null, alert_above: above, alert_below: below, notify: draft.notify }
    setSaving(true)
    setError(null)
    try {
      await patchSensor(sensor.id, body)
      onDone(true)
    } catch (cause) {
      setError(message(cause, 'The sensor could not be saved. Try again.'))
      setSaving(false)
    }
  }

  return (
    <form aria-label={`Change ${sensor.name}`} onSubmit={(event) => void save(event)} className="grid gap-3 rounded-md border bg-muted/20 p-3 sm:grid-cols-2">
      <LimitFields draft={draft} set={setDraft} />
      {error && (
        <p role="alert" className="text-warn sm:col-span-2">
          {error}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" size="sm" disabled={saving}>
          Save sensor
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => onDone(false)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

function AddSensor({ locationId, locationName, onDone }: { locationId: string; locationName: string; onDone: (saved: boolean) => void }) {
  const [search, setSearch] = useState('')
  const [entities, setEntities] = useState<HaEntity[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [entity, setEntity] = useState<HaEntity | null>(null)
  const [kind, setKind] = useState<Sensor['kind']>('humidity')
  const [draft, setDraft] = useState(draftOf())
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let current = true
    const timer = setTimeout(() => {
      listHaEntities(search.trim()).then(
        (rows) => {
          if (!current) return
          setEntities(rows)
          setListError(null)
        },
        (cause: unknown) => {
          if (!current) return
          setListError(
            cause instanceof ApiError && cause.status === 409
              ? 'Home Assistant sensors need Bambuddy. Connect Bambuddy in Settings first.'
              : 'The Home Assistant sensors could not be loaded. Try again.',
          )
        },
      )
    }, search ? 250 : 0)
    return () => {
      current = false
      clearTimeout(timer)
    }
  }, [search])

  function choose(next: HaEntity) {
    setEntity(next)
    if (next.device_class === 'humidity' || next.device_class === 'temperature') setKind(next.device_class)
    setDraft({ ...draft, name: next.friendly_name || next.entity_id, unit: next.unit_of_measurement ?? '' })
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!entity) return setError('Choose a Home Assistant sensor first.')
    const above = limit(draft.above)
    const below = limit(draft.below)
    if (above === undefined || below === undefined) return setError('Enter each limit as a number, or leave it empty.')
    setSaving(true)
    setError(null)
    try {
      await createSensor({
        location_id: locationId,
        source: 'home_assistant',
        kind,
        entity_id: entity.entity_id,
        name: draft.name.trim(),
        unit: draft.unit.trim() || null,
        alert_above: above,
        alert_below: below,
        notify: draft.notify,
      })
      onDone(true)
    } catch (cause) {
      setError(message(cause, 'The sensor could not be added. Try again.'))
      setSaving(false)
    }
  }

  return (
    <form aria-label={`Add a sensor to ${locationName}`} onSubmit={(event) => void save(event)} className="flex flex-col gap-3 rounded-md border bg-muted/20 p-3">
      <label className="flex flex-col gap-1">
        Search Home Assistant
        <Input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="garage humidity" />
      </label>
      {listError && (
        <p role="alert" className="text-warn">
          {listError}
        </p>
      )}
      {!entities && !listError && <p className="text-muted-foreground">Loading the Home Assistant sensors…</p>}
      {entities && entities.length === 0 && <p className="text-muted-foreground">No sensor matches.</p>}
      {entities && entities.length > 0 && (
        <ul aria-label="Home Assistant sensors" className="flex max-h-40 flex-col overflow-y-auto rounded-md border bg-background">
          {entities.map((one) => (
            <li key={one.entity_id}>
              <button
                type="button"
                aria-pressed={entity?.entity_id === one.entity_id}
                onClick={() => choose(one)}
                className={cn(
                  'flex w-full items-baseline gap-2 px-2.5 py-1.5 text-left hover:bg-muted focus-visible:bg-muted focus-visible:outline-none',
                  entity?.entity_id === one.entity_id && 'bg-muted font-medium',
                )}
              >
                <span>{one.friendly_name || one.entity_id}</span>
                <span className="truncate text-[12px] text-muted-foreground">{one.entity_id}</span>
                <span className="ml-auto text-[12px] tabular-nums text-muted-foreground">{one.state ?? 'no state'}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {entity && (
        <div className="grid gap-3 sm:grid-cols-2">
          <LimitFields draft={draft} set={setDraft} />
          <label className="flex flex-col gap-1">
            Kind
            <Select value={kind} onValueChange={(next) => setKind(next as Sensor['kind'])}>
              <SelectTrigger aria-label="Kind" className="w-full text-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {Object.entries(KINDS).map(([value, title]) => (
                  <SelectItem key={value} value={value}>
                    {title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </div>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving || !entity}>
          Save sensor
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => onDone(false)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/**
 * #6268 — a place's humidity and temperature sensors: the value and its unit, whether it is
 * over a limit or cannot be reached, a day's history on request, and for a Home Assistant
 * sensor a form to add, change and archive it. With `notify` on, ScadBuddy sends a notification
 * through Bambuddy when the sensor starts and stops alerting (#6646); the newest one shows here.
 */
export function LocationSensors({ locationId, locationName, sensors, canAdd, onChanged }: Props) {
  const [history, setHistory] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [archiving, setArchiving] = useState<string | null>(null)
  // A queued notice goes out only through a connected Bambuddy; until the read lands, it promises nothing.
  const settings = useAsync(() => api.getSettings(), [])
  const connected = settings.data === undefined ? undefined : Boolean(settings.data.bambuddy_url)

  async function archive(sensor: Sensor) {
    if (archiving) return
    setArchiving(sensor.id)
    setError(null)
    try {
      await archiveSensor(sensor.id)
      onChanged()
    } catch (cause) {
      setError(message(cause, 'The sensor could not be archived. Try again.'))
    } finally {
      setArchiving(null)
    }
  }

  if (sensors.length === 0 && !canAdd) return null
  return (
    <div className="flex flex-col gap-2 pb-1.5 pl-4 text-[12px]">
      {sensors.length > 0 && (
        <ul aria-label={`Sensors in ${locationName}`} className="flex flex-col gap-1">
          {sensors.map((sensor) => {
            const hasValue = sensor.last_value != null
            const limits = limitsText(sensor)
            const open = history === sensor.id
            return (
              <li
                key={sensor.id}
                aria-label={sensor.name}
                data-sensor={sensor.id}
                className={cn('flex flex-col gap-1.5 border-l-2 py-1 pl-2.5', sensor.alerting ? 'border-warn' : 'border-border')}
              >
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                  <span className={cn('min-w-14 text-sm font-medium tabular-nums', sensor.alerting && 'text-warn', !sensor.reachable && 'text-muted-foreground')}>
                    {hasValue ? withUnit(sensor.last_value!, sensor.unit) : 'No reading'}
                  </span>
                  <span className="text-[13px]">{sensor.name}</span>
                  <span className="text-muted-foreground">{KINDS[sensor.kind]}</span>
                  {sensor.alerting && <span className="font-medium text-warn">{overLimit(sensor) ? 'Above the limit' : 'Below the limit'}</span>}
                  {!sensor.reachable && <span className="text-warn">Not reachable</span>}
                  {limits && <span className="text-muted-foreground">{limits}</span>}
                  {sensor.notify && <span className="text-muted-foreground">Notifies through Bambuddy</span>}
                  {sensor.source === 'ams' && <span className="text-muted-foreground">Made by ScadBuddy from the printer</span>}
                  {sensor.last_at && (
                    <time dateTime={sensor.last_at} className="text-muted-foreground">
                      Read {shortTime(sensor.last_at)}
                    </time>
                  )}
                  <span className="ml-auto flex gap-1">
                    <Button
                      type="button"
                      size="xs"
                      variant="ghost"
                      aria-expanded={open}
                      aria-label={`${open ? 'Hide' : 'Show'} history of ${sensor.name}`}
                      onClick={() => setHistory(open ? null : sensor.id)}
                    >
                      <ChartSplineIcon /> History
                    </Button>
                    <Button type="button" size="xs" variant="ghost" aria-label={`Edit ${sensor.name}`} onClick={() => setEditing(editing === sensor.id ? null : sensor.id)}>
                      <PencilIcon /> Edit
                    </Button>
                    {sensor.source === 'home_assistant' && (
                      <Button type="button" size="xs" variant="ghost" aria-label={`Archive ${sensor.name}`} disabled={archiving !== null} onClick={() => void archive(sensor)}>
                        <ArchiveIcon /> Archive
                      </Button>
                    )}
                  </span>
                </div>
                {sensor.last_notification && <LastNotification notification={sensor.last_notification} connected={connected} />}
                {open && <History sensor={sensor} />}
                {editing === sensor.id && (
                  <EditSensor
                    sensor={sensor}
                    onDone={(saved) => {
                      setEditing(null)
                      if (saved) onChanged()
                    }}
                  />
                )}
              </li>
            )
          })}
        </ul>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
      {canAdd && !adding && (
        <div>
          <Button type="button" size="xs" variant="ghost" aria-label={`Add a sensor to ${locationName}`} onClick={() => setAdding(true)}>
            <PlusIcon /> Add a sensor
          </Button>
        </div>
      )}
      {adding && (
        <AddSensor
          locationId={locationId}
          locationName={locationName}
          onDone={(saved) => {
            setAdding(false)
            if (saved) onChanged()
          }}
        />
      )}
    </div>
  )
}

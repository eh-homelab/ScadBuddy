import { ChevronLeftIcon } from 'lucide-react'
import { useId, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { AttachmentsPanel } from '../../components/entities/AttachmentsPanel'
import { LinksPanel } from '../../components/entities/LinksPanel'
import { PropertiesPanel } from '../../components/entities/PropertiesPanel'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { filamentLinks } from '../../lib/filamentLinks'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { inventoryApi, type SettableStatus, type Unit } from './api'
import { dayText, lifeText, minorText, SETTABLE_STATUSES, STATUS_LABELS } from './format'
import { PlaceSelect, PlacesError } from './PlaceSelect'
import { placeName, usePlaces, type Place } from './places'
import { StillWorking } from './StillWorking'
import { activityHref, failed, usePress } from './usePress'

/**
 * #7067 — one unit at `/inventory/units/:id`: a nozzle, a build plate, a tool. Its status and
 * the change of it (leaving stock writes a move at the unit's cost; coming back a return), its
 * place, its cost, and its properties, files and links (entity `unit:<id>`).
 */
export function UnitPage() {
  const { id = '' } = useParams()
  const money = useMoney()
  const unit = useAsync(() => inventoryApi.unit(id), [id])
  const itemId = unit.data?.item_id
  const item = useAsync(() => (itemId ? inventoryApi.item(itemId) : Promise.resolve(null)), [itemId])
  const places = usePlaces()

  if (!unit.data) {
    const missing = unit.error instanceof ApiError && unit.error.status === 404
    return (
      <Shell itemId={null}>
        <h1 className="text-lg font-semibold tracking-tight">Unit</h1>
        {unit.error ? (
          <p role="alert" className="mt-2 text-[13px] text-warn">
            {missing ? 'No such unit.' : `The unit could not be loaded: ${unit.error.message}`}
          </p>
        ) : (
          <p className="mt-2 text-[13px] text-muted-foreground">Loading the unit…</p>
        )}
      </Shell>
    )
  }

  const shown = unit.data
  const name = shown.serial || shown.name || 'Unit'
  return (
    <Shell itemId={shown.item_id}>
      <h1 className="min-w-0 break-words text-lg font-semibold tracking-tight">{item.data ? `${item.data.name} ${name}` : name}</h1>
      {item.data && (
        <p className="text-[13px] text-muted-foreground">
          A unit of{' '}
          <Link to={inventoryPaths.item(item.data.id)} className="underline underline-offset-4">
            {item.data.name}
          </Link>
          {shown.name && shown.serial ? `, named ${shown.name}` : ''}
        </p>
      )}
      <dl className="my-5 grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4">
        <div>
          <dt className="text-muted-foreground">Status</dt>
          <dd data-testid="unit-status">{STATUS_LABELS[shown.status]}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Cost</dt>
          <dd className="tabular-nums">{minorText(shown.cost_minor, money, shown.cost_unknown)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Received</dt>
          <dd>{dayText(shown.received_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Place</dt>
          <dd>
            {shown.location_id ? (
              <Link to={filamentLinks.location(shown.location_id)} className="underline underline-offset-4">
                {placeName(places.data, shown.location_id)}
              </Link>
            ) : (
              'none'
            )}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Service life</dt>
          <dd>{lifeText(shown.life_hours, shown.life_months)}</dd>
        </div>
        {shown.in_service_at && (
          <div>
            <dt className="text-muted-foreground">In service since</dt>
            <dd>{dayText(shown.in_service_at)}</dd>
          </div>
        )}
        {shown.retired_at && (
          <div>
            <dt className="text-muted-foreground">Out of use since</dt>
            <dd>{dayText(shown.retired_at)}</dd>
          </div>
        )}
      </dl>
      <div className="flex flex-col gap-4">
        <PlacesError error={places.error} />
        <DetailsForm key={shown.id} unit={shown} places={places.data ?? []} onChanged={(next) => unit.setData(next, { supersede: true })} />
        <StatusForm unit={shown} onChanged={(next) => unit.setData(next, { supersede: true })} />
      </div>
      <section aria-label="Files, links and properties" className="mt-8 flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Files, links and properties</h2>
        <PropertiesPanel entity={`unit:${shown.id}`} />
        <div className="grid gap-6 lg:grid-cols-2">
          <AttachmentsPanel entity={`unit:${shown.id}`} />
          <LinksPanel entity={`unit:${shown.id}`} />
        </div>
      </section>
    </Shell>
  )
}

/** R-P2-38 — a unit's name, serial and place after it was received (`PATCH /inventory/units/{id}`). */
function DetailsForm({ unit, places, onChanged }: { unit: Unit; places: Place[]; onChanged: (unit: Unit) => void }) {
  const id = useId()
  const [name, setName] = useState(unit.name)
  const [serial, setSerial] = useState(unit.serial ?? '')
  const [place, setPlace] = useState<string | null>(unit.location_id)
  const { busy, running, press } = usePress()
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSaved(false)
    try {
      onChanged(await press((o) => inventoryApi.patchUnit(unit.id, { name: name.trim(), serial: serial.trim() || null, location_id: place }, o)))
      setSaved(true)
    } catch (cause) {
      setError(failed(cause, 'The unit could not be saved. Try again.'))
    }
  }

  return (
    <form aria-label="Unit details" onSubmit={(event) => void submit(event)} className="flex flex-col gap-2 rounded-md border p-3 text-[13px]">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name</Label>
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} className="w-48" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-serial`}>Serial</Label>
          <Input id={`${id}-serial`} value={serial} onChange={(e) => setSerial(e.target.value)} className="w-40" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-place`}>Place</Label>
          <PlaceSelect id={`${id}-place`} label="Place" places={places} value={place} onChange={setPlace} />
        </div>
        <Button type="submit" size="sm" disabled={busy}>
          Save unit
        </Button>
        {saved && (
          <span role="status" className="text-muted-foreground">
            Saved.
          </span>
        )}
      </div>
      <p className="text-muted-foreground">A serial is unique within the item. No stock moves.</p>
      {running && <StillWorking id={running.id} href={activityHref(running.id)} />}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function StatusForm({ unit, onChanged }: { unit: Unit; onChanged: (unit: Unit) => void }) {
  const id = useId()
  const [status, setStatus] = useState<SettableStatus | null>(unit.status === 'ended' ? null : unit.status)
  const [note, setNote] = useState('')
  const { busy, running, press } = usePress()
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!status) return
    setError(null)
    try {
      onChanged(await press((o) => inventoryApi.setUnitStatus(unit.id, { status, ...(note.trim() ? { note: note.trim() } : {}) }, o), status))
      setNote('')
    } catch (cause) {
      setError(failed(cause, 'The status could not be changed. Try again.'))
    }
  }

  return (
    <form aria-label="Change status" onSubmit={(event) => void submit(event)} className="flex flex-col gap-2 rounded-md border p-3 text-[13px]">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-status`}>New status</Label>
          <Select value={status ?? undefined} onValueChange={(next) => setStatus(next as SettableStatus)}>
            <SelectTrigger id={`${id}-status`} aria-label="New status" className="w-40">
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper">
              {SETTABLE_STATUSES.map((value) => (
                <SelectItem key={value} value={value}>
                  {STATUS_LABELS[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-note`}>Note</Label>
          <Input id={`${id}-note`} value={note} onChange={(e) => setNote(e.target.value)} className="w-64" />
        </div>
        <Button type="submit" size="sm" disabled={busy || !status || status === unit.status}>
          Change status
        </Button>
      </div>
      <p className="text-muted-foreground">Leaving stock takes the unit out at its cost; coming back to stock puts it back.</p>
      {running && <StillWorking id={running.id} href={activityHref(running.id)} />}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function Shell({ itemId, children }: { itemId: string | null; children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <Link to={itemId ? inventoryPaths.item(itemId) : inventoryPaths.list} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> {itemId ? 'Item' : 'Inventory'}
        </Link>
        {children}
      </div>
    </div>
  )
}

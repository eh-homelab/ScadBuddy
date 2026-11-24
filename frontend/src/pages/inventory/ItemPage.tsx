import { ArchiveIcon, ArchiveRestoreIcon, ChevronLeftIcon } from 'lucide-react'
import { useId, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router'
import { ApiError } from '../../api/client'
import { AttachmentsPanel } from '../../components/entities/AttachmentsPanel'
import { LinksPanel } from '../../components/entities/LinksPanel'
import { PropertiesPanel } from '../../components/entities/PropertiesPanel'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { useHashTarget } from '../../lib/useHashTarget'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { inventoryApi, type Item, type ItemKind } from './api'
import { dec, lifeText, qtyText, valueText } from './format'
import { ItemOffers } from './ItemOffers'
import { LotStock, MovesTable, UnitStock } from './ItemStock'

const failed = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** The id an anchor names, without its prefix, or null. */
function anchored(hashId: string | null, prefix: string): string | null {
  return hashId?.startsWith(prefix) ? hashId.slice(prefix.length) : null
}

/**
 * #7067 — one item at `/inventory/items/:id`: its kind path, its offers, its stock by its
 * kind's tracking (lots, units, or none for a kind that is not counted), its moves, and its
 * properties, files and links (entity `item:<id>`). `#lot-<id>` and `#offer-<id>` mark a row.
 */
export function ItemPage() {
  const { id = '' } = useParams()
  const money = useMoney()
  const [version, setVersion] = useState(0)
  const item = useAsync(() => inventoryApi.item(id), [id])
  const kinds = useAsync(() => inventoryApi.kinds(true), [])
  const offers = useAsync(() => inventoryApi.itemOffers(id), [id])
  const counted = !!item.data && item.data.tracking !== 'none' && item.data.tracking !== 'filament'
  const moves = useAsync(() => (counted ? inventoryApi.moves({ item_id: id, limit: 50 }) : Promise.resolve([])), [id, counted])
  const byId = useMemo(() => new Map((kinds.data ?? []).map((k) => [k.id, k])), [kinds.data])
  const hashId = useHashTarget(!!item.data && !!offers.data)
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const current = item.data

  /** After a change: everything is read again in the background, the page staying up meanwhile. */
  function changed() {
    item.refresh()
    offers.refresh()
    moves.refresh()
    setVersion((v) => v + 1)
  }

  async function archive(next: boolean) {
    setError(null)
    try {
      await (next ? inventoryApi.archiveItem(id) : inventoryApi.restoreItem(id))
      changed()
    } catch (cause) {
      setError(failed(cause, 'The item could not be changed. Try again.'))
    }
  }

  if (!current) {
    const missing = item.error instanceof ApiError && item.error.status === 404
    return (
      <Shell>
        <h1 className="text-lg font-semibold tracking-tight">Item</h1>
        {item.error ? (
          <p role="alert" className="mt-2 text-[13px] text-warn">
            {missing ? 'No such item. It may have been merged or deleted.' : `The item could not be loaded: ${item.error.message}`}
          </p>
        ) : (
          <p className="mt-2 text-[13px] text-muted-foreground">Loading the item…</p>
        )}
      </Shell>
    )
  }

  const kind = byId.get(current.kind_id)
  return (
    <Shell>
      <nav aria-label="Kind" className="flex flex-wrap items-center gap-x-1 text-[13px] text-muted-foreground">
        {current.kind_path.map((step, at) => (
          <span key={step} className="flex items-center gap-1">
            {at > 0 && <span aria-hidden="true">/</span>}
            <Link to={inventoryPaths.kind(step)} className="hover:underline">
              {byId.get(step)?.name ?? step}
            </Link>
          </span>
        ))}
      </nav>
      <header className="mt-1 mb-4 flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="min-w-0 break-words text-lg font-semibold tracking-tight">{current.name}</h1>
            {current.effective_saleable && <Badge variant="secondary">For sale</Badge>}
            {current.low && <Badge variant="destructive">Low</Badge>}
            {current.archived_at && <Badge variant="outline">Archived</Badge>}
          </div>
          {current.manufacturer && <p className="text-[13px] text-muted-foreground">{current.manufacturer}</p>}
        </div>
        <Button size="sm" variant="outline" aria-expanded={editing} onClick={() => setEditing((open) => !open)}>
          Edit details
        </Button>
        <Button size="sm" variant="outline" onClick={() => void archive(!current.archived_at)}>
          {current.archived_at ? <ArchiveRestoreIcon /> : <ArchiveIcon />}
          {current.archived_at ? 'Restore' : 'Archive'}
        </Button>
      </header>
      {error && (
        <p role="alert" className="mb-3 text-[13px] text-warn">
          {error}
        </p>
      )}
      {editing && (
        <DetailsForm
          item={current}
          kind={kind}
          onDone={() => {
            setEditing(false)
            changed()
          }}
          onCancel={() => setEditing(false)}
        />
      )}
      <dl className="mb-6 grid grid-cols-2 gap-x-6 gap-y-2 text-[13px] sm:grid-cols-4">
        {current.tracking === 'none' ? (
          <div className="col-span-full">
            <dt className="text-muted-foreground">Stock</dt>
            <dd>Not counted: a {kind?.name ?? current.kind_id} item keeps its offers and prices, never stock.</dd>
          </div>
        ) : (
          <>
            <div>
              <dt className="text-muted-foreground">On hand</dt>
              <dd data-testid="item-on-hand" className={`tabular-nums ${current.low || Number(dec(current.on_hand)) < 0 ? 'text-warn' : ''}`}>
                {qtyText(current.on_hand, current.uom)}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Value</dt>
              <dd className="tabular-nums">{valueText(current.stock_value_minor, current.cost_unknown, money)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Low at</dt>
              <dd className="tabular-nums">{Number(dec(current.low_stock)) > 0 ? qtyText(current.low_stock, current.uom) : 'not set'}</dd>
            </div>
          </>
        )}
        {(current.life_hours !== null || current.life_months !== null) && (
          <div>
            <dt className="text-muted-foreground">Service life</dt>
            <dd>{lifeText(current.life_hours, current.life_months)}</dd>
          </div>
        )}
        {current.compatible_with.length > 0 && (
          <div>
            <dt className="text-muted-foreground">Fits</dt>
            <dd>{current.compatible_with.join(', ')}</dd>
          </div>
        )}
        {current.note && (
          <div className="col-span-full">
            <dt className="text-muted-foreground">Note</dt>
            <dd className="whitespace-pre-wrap">{current.note}</dd>
          </div>
        )}
      </dl>

      <div className="flex flex-col gap-8">
        {offers.data && <ItemOffers item={current} offers={offers.data} money={money} named={anchored(hashId, 'offer-')} onChanged={changed} />}
        {current.tracking === 'lot' && offers.data && <LotStock item={current} offers={offers.data} money={money} named={anchored(hashId, 'lot-')} version={version} onChanged={changed} />}
        {current.tracking === 'unit' && <UnitStock item={current} money={money} version={version} onChanged={changed} />}
        {counted && moves.data && <MovesTable moves={moves.data} uom={current.uom} money={money} />}
        <section aria-label="Files, links and properties" className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Files, links and properties</h2>
          <PropertiesPanel entity={`item:${current.id}`} />
          <div className="grid gap-6 lg:grid-cols-2">
            <AttachmentsPanel entity={`item:${current.id}`} />
            <LinksPanel entity={`item:${current.id}`} />
          </div>
        </section>
      </div>
    </Shell>
  )
}

/** "As its kind" stands for `saleable` null: the item takes its kind's. */
const SALEABLE = { kind: 'As its kind', yes: 'For sale', no: 'Not for sale' } as const
type Saleable = keyof typeof SALEABLE

function DetailsForm({ item, kind, onDone, onCancel }: { item: Item; kind: ItemKind | undefined; onDone: () => void; onCancel: () => void }) {
  const id = useId()
  const [name, setName] = useState(item.name)
  const [manufacturer, setManufacturer] = useState(item.manufacturer)
  const [lowStock, setLowStock] = useState(dec(item.low_stock))
  const [saleable, setSaleable] = useState<Saleable>(item.saleable === null ? 'kind' : item.saleable ? 'yes' : 'no')
  const [note, setNote] = useState(item.note)
  const [lifeHours, setLifeHours] = useState(item.life_hours ?? '')
  const [lifeMonths, setLifeMonths] = useState(item.life_months === null ? '' : String(item.life_months))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return setError('Give the item a name.')
    if (!/^\d+(\.\d+)?$/.test(lowStock.trim())) return setError('Low stock is a number, 0 or more.')
    const hours = lifeHours.trim()
    if (hours && !(/^\d+(\.\d{1,2})?$/.test(hours) && Number(hours) > 0)) return setError('The life in print hours is a number above 0, such as 500, or empty.')
    const months = lifeMonths.trim()
    if (months && !(/^\d+$/.test(months) && Number(months) > 0 && Number(months) <= 1200)) return setError('The life in months is a whole number from 1 to 1200, or empty.')
    setBusy(true)
    setError(null)
    try {
      await inventoryApi.patchItem(item.id, {
        name: name.trim(),
        manufacturer: manufacturer.trim(),
        low_stock: lowStock.trim(),
        saleable: saleable === 'kind' ? null : saleable === 'yes',
        note,
        life_hours: hours || null,
        life_months: months ? Number(months) : null,
      })
      onDone()
    } catch (cause) {
      setError(failed(cause, 'The item could not be saved. Try again.'))
      setBusy(false)
    }
  }

  return (
    <form aria-label="Item details" onSubmit={(event) => void submit(event)} className="mb-6 flex flex-col gap-3 rounded-md border p-3 text-[13px]">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name</Label>
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} className="w-64" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-maker`}>Manufacturer</Label>
          <Input id={`${id}-maker`} value={manufacturer} onChange={(e) => setManufacturer(e.target.value)} className="w-48" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-low`}>Low stock at</Label>
          <Input id={`${id}-low`} inputMode="decimal" value={lowStock} onChange={(e) => setLowStock(e.target.value)} className="w-24" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-sale`}>Sold to customers</Label>
          <Select value={saleable} onValueChange={(next) => setSaleable(next as Saleable)}>
            <SelectTrigger id={`${id}-sale`} aria-label="Sold to customers" className="w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent position="popper">
              {(Object.keys(SALEABLE) as Saleable[]).map((value) => (
                <SelectItem key={value} value={value}>
                  {value === 'kind' ? `${SALEABLE.kind} (${kind?.saleable ? 'for sale' : 'not for sale'})` : SALEABLE[value]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-hours`}>Life in print hours</Label>
          <Input id={`${id}-hours`} inputMode="decimal" value={lifeHours} onChange={(e) => setLifeHours(e.target.value)} className="w-24" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-months`}>Life in months</Label>
          <Input id={`${id}-months`} inputMode="numeric" value={lifeMonths} onChange={(e) => setLifeMonths(e.target.value)} className="w-24" />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-note`}>Note</Label>
        <textarea id={`${id}-note`} rows={2} value={note} onChange={(e) => setNote(e.target.value)} className="w-full max-w-xl rounded-md border bg-transparent px-2 py-1" />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          Save details
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <Link to={inventoryPaths.list} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Inventory
        </Link>
        {children}
      </div>
    </div>
  )
}

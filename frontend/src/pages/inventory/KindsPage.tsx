import { ChevronLeftIcon, PlusIcon } from 'lucide-react'
import { useId, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { PropertyKeyTable } from '../../components/entities/PropertyKeyTable'
import { namedTwice, rowsOf, schemaOf, type KeyRow } from '../../components/entities/propertyKeys'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { inventoryApi, type ItemKind, type KindPatch, type Tracking } from './api'
import { TRACKING_LABELS } from './format'
import { kindTree, type KindNode } from './kindTree'
import { StillWorking } from './StillWorking'
import { activityHref, failed, usePress } from './usePress'

const NONE = '__none'
type LifeBasis = ItemKind['life_basis']
const LIFE_LABELS: Record<LifeBasis, string> = { none: 'No service life', hours: 'In print hours', months: 'In months' }
/** Filament items wait for their own phase (#7068), so a new kind is never one. */
const NEW_TRACKING: Tracking[] = ['lot', 'unit', 'none']

/**
 * #7067 — `/inventory/kinds`: the item kinds as a tree. A kind is a row, never a list in code
 * (spec §4): add one, change its name, parent, unit, service life and whether its items are
 * sold, archive one with no items, and type its items' properties as a key table. The
 * server's refusal is shown as it says it.
 */
export function KindsPage() {
  const [showArchived, setShowArchived] = useState(false)
  const kinds = useAsync(() => inventoryApi.kinds(showArchived), [showArchived])
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const archiving = usePress()
  const all = kinds.data ?? []

  async function archive(kind: ItemKind) {
    setError(null)
    try {
      await archiving.press((o) => (kind.archived_at ? inventoryApi.restoreKind(kind.id, o) : inventoryApi.archiveKind(kind.id, o)), `${kind.archived_at ? 'restore' : 'archive'}:${kind.id}`)
      kinds.refresh()
    } catch (cause) {
      setError(failed(cause, 'The kind could not be changed. Try again.'))
    }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6 text-[13px]">
        <Link to={inventoryPaths.list} className="mb-3 inline-flex items-center gap-1 text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Inventory
        </Link>
        <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Item kinds</h1>
            <p className="mt-0.5 text-muted-foreground">What kinds of things the shop keeps, how each is counted, and the properties its items take. A kind's items take its parent's properties too.</p>
          </div>
          <Button size="sm" aria-expanded={adding} onClick={() => setAdding((open) => !open)}>
            <PlusIcon /> Add kind
          </Button>
        </div>
        {adding && (
          <AddKindForm
            kinds={all}
            onDone={() => {
              setAdding(false)
              kinds.refresh()
            }}
            onCancel={() => setAdding(false)}
          />
        )}
        <label className="mb-2 flex items-center gap-2 text-muted-foreground">
          <Checkbox checked={showArchived} aria-label="Show archived kinds" onCheckedChange={(next) => setShowArchived(next === true)} />
          Show archived
        </label>
        {archiving.running && <StillWorking id={archiving.running.id} href={activityHref(archiving.running.id)} />}
        {error && (
          <p role="alert" className="mb-2 text-warn">
            {error}
          </p>
        )}
        {kinds.error && (
          <p role="alert" className="text-warn">
            The kinds could not be loaded: {kinds.error.message}
          </p>
        )}
        {kinds.data && (
          <ul aria-label="Item kinds" className="rounded-md border p-2">
            {kindTree(all).map((node) => (
              <Node key={node.kind.id} node={node} kinds={all} editing={editing} onEdit={setEditing} onArchive={(kind) => void archive(kind)} onSaved={kinds.refresh} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

interface NodeProps {
node: KindNode
kinds: ItemKind[]
editing: string | null
onEdit: (id: string | null) => void
onArchive: (kind: ItemKind) => void
onSaved: () => void
}

/** One kind in the tree, its edit form under it when open, then its children. */
function Node({ node, kinds, editing, onEdit, onArchive, onSaved }: NodeProps) {
  const { kind } = node
  return (
    <li className="py-0.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1 hover:bg-muted/40">
        <Link to={inventoryPaths.kind(kind.id)} className="font-medium underline-offset-4 hover:underline">
          {kind.name}
        </Link>
        <span className="text-muted-foreground">{TRACKING_LABELS[kind.tracking]}</span>
        {kind.uom !== 'each' && <span className="text-muted-foreground">in {kind.uom}</span>}
        {kind.saleable && <span className="text-muted-foreground">for sale</span>}
        <span className="text-muted-foreground tabular-nums">
          {kind.item_count} {kind.item_count === 1 ? 'item' : 'items'}
        </span>
        {kind.archived_at && <span className="rounded-full bg-muted px-2 text-[12px] text-muted-foreground">Archived</span>}
        <span className="ml-auto flex gap-1">
          <Button size="xs" variant="ghost" aria-label={`Edit ${kind.name}`} aria-expanded={editing === kind.id} onClick={() => onEdit(editing === kind.id ? null : kind.id)}>
            Edit
          </Button>
          <Button size="xs" variant="ghost" aria-label={`${kind.archived_at ? 'Restore' : 'Archive'} ${kind.name}`} onClick={() => onArchive(kind)}>
            {kind.archived_at ? 'Restore' : 'Archive'}
          </Button>
        </span>
      </div>
      {editing === kind.id && (
        <EditKindForm
          kind={kind}
          kinds={kinds}
          onDone={() => {
            onEdit(null)
            onSaved()
          }}
          onCancel={() => onEdit(null)}
        />
      )}
      {node.children.length > 0 && (
        <ul aria-label={`Kinds under ${kind.name}`} className="ml-4 border-l pl-2">
          {node.children.map((child) => (
            <Node key={child.kind.id} node={child} kinds={kinds} editing={editing} onEdit={onEdit} onArchive={onArchive} onSaved={onSaved} />
          ))}
        </ul>
      )}
    </li>
  )
}

/** The kinds a kind may sit under: none of itself and its descendants, nor an archived one. */
function parentChoices(kinds: ItemKind[], own: string | null): ItemKind[] {
  const below = new Set(own ? [own] : [])
  for (let grew = true; grew; ) {
    grew = false
    for (const k of kinds) if (k.parent_id && below.has(k.parent_id) && !below.has(k.id)) grew = !!below.add(k.id)
  }
  return kinds.filter((k) => !below.has(k.id) && !k.archived_at).sort((a, b) => a.name.localeCompare(b.name))
}

function ParentSelect({ id, kinds, own, value, onChange }: { id: string; kinds: ItemKind[]; own: string | null; value: string | null; onChange: (value: string | null) => void }) {
  return (
    <Select value={value ?? NONE} onValueChange={(next) => onChange(next === NONE ? null : next)}>
      <SelectTrigger id={id} aria-label="Parent" className="w-44">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        <SelectItem value={NONE}>None (a root kind)</SelectItem>
        {parentChoices(kinds, own).map((k) => (
          <SelectItem key={k.id} value={k.id}>
            {k.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function LifeSelect({ id, value, onChange }: { id: string; value: LifeBasis; onChange: (value: LifeBasis) => void }) {
  return (
    <Select value={value} onValueChange={(next) => onChange(next as LifeBasis)}>
      <SelectTrigger id={id} aria-label="Service life" className="w-40">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        {(Object.keys(LIFE_LABELS) as LifeBasis[]).map((value) => (
          <SelectItem key={value} value={value}>
            {LIFE_LABELS[value]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function TrackingSelect({ id, value, onChange, disabled }: { id: string; value: Tracking; onChange: (value: Tracking) => void; disabled?: boolean }) {
  return (
    <Select value={value} onValueChange={(next) => onChange(next as Tracking)} disabled={disabled}>
      <SelectTrigger id={id} aria-label="Counted in" className="w-36">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        {(value === 'filament' ? (['filament'] as Tracking[]) : NEW_TRACKING).map((t) => (
          <SelectItem key={t} value={t}>
            {TRACKING_LABELS[t]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function FormError({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="text-warn">
      {error}
    </p>
  ) : null
}

function AddKindForm({ kinds, onDone, onCancel }: { kinds: ItemKind[]; onDone: () => void; onCancel: () => void }) {
  const id = useId()
  const [kindId, setKindId] = useState('')
  const [name, setName] = useState('')
  const [parent, setParent] = useState<string | null>(null)
  const [tracking, setTracking] = useState<Tracking | null>(null)
  const [uom, setUom] = useState('each')
  const [life, setLife] = useState<LifeBasis>('none')
  const [saleable, setSaleable] = useState(false)
  const { busy, running, press } = usePress()
  const [error, setError] = useState<string | null>(null)
  // Until the person picks one, a new kind is counted as its parent is.
  const parentTracking = kinds.find((k) => k.id === parent)?.tracking
  const counted: Tracking = tracking ?? (parentTracking && parentTracking !== 'filament' ? parentTracking : 'lot')

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!/^[a-z][a-z0-9_]{0,39}$/.test(kindId.trim())) return setError('The id is lower-case letters, digits and _, starting with a letter, such as magnet.')
    if (!name.trim()) return setError('Give the kind a name.')
    setError(null)
    try {
      const body = { id: kindId.trim(), name: name.trim(), parent_id: parent, tracking: counted, uom: uom.trim() || 'each', life_basis: life, saleable }
      await press((o) => inventoryApi.createKind(body, o), body)
      onDone()
    } catch (cause) {
      setError(failed(cause, 'The kind could not be added. Try again.'))
    }
  }

  return (
    <form aria-label="Add kind" onSubmit={(event) => void submit(event)} className="mb-4 flex flex-col gap-3 rounded-md border p-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-id`}>Id</Label>
          <Input id={`${id}-id`} value={kindId} onChange={(e) => setKindId(e.target.value)} className="w-36" placeholder="magnet" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name</Label>
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} className="w-44" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-parent`}>Parent</Label>
          <ParentSelect id={`${id}-parent`} kinds={kinds} own={null} value={parent} onChange={setParent} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-tracking`}>Counted in</Label>
          <TrackingSelect id={`${id}-tracking`} value={counted} onChange={setTracking} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-uom`}>Unit</Label>
          <Input id={`${id}-uom`} value={uom} onChange={(e) => setUom(e.target.value)} className="w-20" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-life`}>Service life</Label>
          <LifeSelect id={`${id}-life`} value={life} onChange={setLife} />
        </div>
        <label className="flex items-center gap-2 pb-1.5">
          <Checkbox checked={saleable} aria-label="Its items are for sale" onCheckedChange={(next) => setSaleable(next === true)} />
          Its items are for sale
        </label>
      </div>
      <p className="text-muted-foreground">Lots keep a quantity at a cost (hooks, desiccant); units are counted one by one (a nozzle, a tool); a kind that is not counted keeps offers and prices only (a service).</p>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          Add kind
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {running && <StillWorking id={running.id} href={activityHref(running.id)} />}
      <FormError error={error} />
    </form>
  )
}

function EditKindForm({ kind, kinds, onDone, onCancel }: { kind: ItemKind; kinds: ItemKind[]; onDone: () => void; onCancel: () => void }) {
  const id = useId()
  const [name, setName] = useState(kind.name)
  const [parent, setParent] = useState<string | null>(kind.parent_id)
  const [tracking, setTracking] = useState<Tracking>(kind.tracking)
  const [uom, setUom] = useState(kind.uom)
  const [life, setLife] = useState<LifeBasis>(kind.life_basis)
  const [saleable, setSaleable] = useState(kind.saleable)
  const [rows, setRows] = useState<KeyRow[]>(() => rowsOf(kind.attribute_schema))
  const { busy, running, press } = usePress()
  const [error, setError] = useState<string | null>(null)
  // A built-in kind, or one with items, keeps how it is counted (409 `kind_in_use`).
  const trackingFixed = kind.builtin || kind.item_count > 0 || kind.tracking === 'filament'

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!name.trim()) return setError('Give the kind a name.')
    if (namedTwice(rows)) return setError('Each key is named once.')
    const body: KindPatch = { name: name.trim(), parent_id: parent, uom: uom.trim() || 'each', life_basis: life, saleable, attribute_schema: schemaOf(kind.attribute_schema, rows) }
    if (tracking !== kind.tracking) body.tracking = tracking
    setError(null)
    try {
      await press((o) => inventoryApi.patchKind(kind.id, body, o), body)
      onDone()
    } catch (cause) {
      setError(failed(cause, 'The kind could not be saved. Try again.'))
    }
  }

  return (
    <form aria-label={`Edit ${kind.name}`} onSubmit={(event) => void submit(event)} className="my-2 ml-2 flex flex-col gap-3 rounded-md border p-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-name`}>Name</Label>
          <Input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} className="w-44" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-parent`}>Parent</Label>
          <ParentSelect id={`${id}-parent`} kinds={kinds} own={kind.id} value={parent} onChange={setParent} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-tracking`}>Counted in</Label>
          <TrackingSelect id={`${id}-tracking`} value={tracking} onChange={setTracking} disabled={trackingFixed} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-uom`}>Unit</Label>
          <Input id={`${id}-uom`} value={uom} onChange={(e) => setUom(e.target.value)} className="w-20" />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-life`}>Service life</Label>
          <LifeSelect id={`${id}-life`} value={life} onChange={setLife} />
        </div>
        <label className="flex items-center gap-2 pb-1.5">
          <Checkbox checked={saleable} aria-label="Its items are for sale" onCheckedChange={(next) => setSaleable(next === true)} />
          Its items are for sale
        </label>
      </div>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 font-medium">Properties of its items</legend>
        <p className="text-muted-foreground">Each key says what an item's property holds. It types a value; any other key is still kept.</p>
        <PropertyKeyTable label={`Keys of ${kind.name}`} rows={rows} onChange={setRows} />
      </fieldset>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          Save kind
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {running && <StillWorking id={running.id} href={activityHref(running.id)} />}
      <FormError error={error} />
    </form>
  )
}

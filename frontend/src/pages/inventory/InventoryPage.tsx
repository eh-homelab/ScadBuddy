import { PlusIcon } from 'lucide-react'
import { useId, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { PartRules } from '../../components/orders/PartRules'
import { DataTable, type Column } from '../../components/records/DataTable'
import { inventoryPaths } from '../../lib/inventoryLinks'
import { useAsync } from '../../lib/useAsync'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { failed } from './usePress'
import { inventoryApi, type Item, type ItemKind } from './api'
import { dec, qtyText, valueText } from './format'
import { kindTree, type KindNode } from './kindTree'

/**
 * #7067 — `/inventory`: every item the shop buys or uses that is not filament, with its
 * stock. The kind tree filters by a kind and the kinds below it; the search, "Low stock
 * only" and "Show archived" are in the address too, so a link (`inventoryPaths.kind`) opens the same view.
 */
export function InventoryPage() {
  const [params, setParams] = useSearchParams()
  const kind = params.get('kind')
  const q = params.get('q') ?? ''
  const low = params.get('low') === 'true'
  const archived = params.get('archived') === 'true'
  const money = useMoney()
  const kinds = useAsync(() => inventoryApi.kinds(), [])
  const items = useAsync(() => inventoryApi.items({ kind, q, low, archived }), [kind, q, low, archived])
  const [adding, setAdding] = useState(false)
  const byId = useMemo(() => new Map((kinds.data ?? []).map((k) => [k.id, k])), [kinds.data])

  function set(name: string, value: string | null) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    setParams(next, { replace: name === 'q' })
  }

  const columns: Column<Item>[] = [
    {
      key: 'name',
      header: 'Item',
      className: 'break-words',
      cell: (item) => (
        <span className="flex flex-wrap items-center gap-1.5">
          <Link to={inventoryPaths.item(item.id)} className="font-medium underline-offset-4 hover:underline">
            {item.name}
          </Link>
          {item.low && <Badge variant="destructive">Low</Badge>}
          {item.effective_saleable && <Badge variant="secondary">For sale</Badge>}
          {item.archived_at && <Badge variant="outline">Archived</Badge>}
        </span>
      ),
    },
    { key: 'kind', header: 'Kind', className: 'text-muted-foreground', cell: (item) => byId.get(item.kind_id)?.name ?? item.kind_id },
    {
      key: 'on_hand',
      header: 'On hand',
      className: 'sb-num',
      cell: (item) =>
        item.tracking === 'none' ? (
          <span className="text-muted-foreground">not counted</span>
        ) : (
          <span className={item.low || Number(dec(item.on_hand)) < 0 ? 'text-warn' : undefined}>{qtyText(item.on_hand, item.uom)}</span>
        ),
    },
    { key: 'low', header: 'Low at', className: 'sb-num text-muted-foreground', cell: (item) => (item.tracking !== 'none' && Number(dec(item.low_stock)) > 0 ? dec(item.low_stock) : '—') },
    { key: 'value', header: 'Value', className: 'sb-num', cell: (item) => (item.tracking === 'none' ? '—' : valueText(item.stock_value_minor, item.cost_unknown, money)) },
    {
      key: 'offers',
      header: 'Offers',
      className: 'text-muted-foreground',
      cell: (item) => (item.offer_count === 0 ? 'no supplier' : `${item.offer_count} ${item.offer_count === 1 ? 'offer' : 'offers'}`),
    },
  ]

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Inventory</h1>
            <p className="mt-0.5 text-[13px] text-muted-foreground">Parts, consumables, equipment and services: what you buy for the shop besides filament, and what is on the shelf.</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to={inventoryPaths.kinds}>Kinds</Link>
            </Button>
            <Button size="sm" onClick={() => setAdding((open) => !open)} aria-expanded={adding}>
              <PlusIcon /> New item
            </Button>
          </div>
        </div>
        <div className="grid gap-6 md:grid-cols-[13rem_minmax(0,1fr)]">
          <nav aria-label="Kinds" className="text-[13px]">
            <Link to={inventoryPaths.list} className={treeLink(!kind)} aria-current={kind ? undefined : 'page'}>
              All items
            </Link>
            {kinds.error && (
              <p role="alert" className="mt-2 text-warn">
                The kinds could not be loaded: {kinds.error.message}
              </p>
            )}
            {kinds.data && <KindLinks nodes={kindTree(kinds.data.filter((k) => k.tracking !== 'filament'))} current={kind} />}
          </nav>
          <div className="flex min-w-0 flex-col gap-3">
            {adding && kinds.data && <NewItemForm kinds={kinds.data} kind={kind} onCancel={() => setAdding(false)} />}
            <div className="flex flex-wrap items-center gap-3">
              <Input type="search" aria-label="Search items" placeholder="Search by name, maker or property" className="h-8 w-72 max-w-full" value={q} onChange={(e) => set('q', e.target.value)} />
              <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
                <Checkbox checked={low} aria-label="Low stock only" onCheckedChange={(next) => set('low', next === true ? 'true' : null)} />
                Low stock only
              </label>
              <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
                <Checkbox checked={archived} aria-label="Show archived" onCheckedChange={(next) => set('archived', next === true ? 'true' : null)} />
                Show archived
              </label>
            </div>
            {items.error ? (
              <p role="alert" className="text-[13px] text-warn">
                The items could not be loaded: {items.error.message}
              </p>
            ) : items.loading && !items.data ? (
              <div role="status" aria-label="Loading items" className="flex flex-col gap-2">
                {Array.from({ length: 3 }, (_, i) => (
                  <Skeleton key={i} className="h-9 w-full" />
                ))}
              </div>
            ) : (
              <DataTable label="Items" columns={columns} rows={items.data ?? []} rowKey={(item) => item.id} empty={q || low || kind || archived ? 'No item matches.' : 'No items yet. Add the hooks, nozzles or boxes you buy.'} />
            )}
            {/* #5543 — moved from the parts page (#7067): the parts each unit of a model uses,
                under the Part kind and the kinds below it only (R-P2-39). */}
            {kind && underPart(kind, byId) && <PartRules />}
          </div>
        </div>
      </div>
    </div>
  )
}

/** Whether the kind is `part` or below it (R-P2-39). */
function underPart(kindId: string, byId: Map<string, ItemKind>): boolean {
  const seen = new Set<string>()
  for (let at: string | null | undefined = kindId; at && !seen.has(at); at = byId.get(at)?.parent_id) {
    if (at === 'part') return true
    seen.add(at)
  }
  return false
}

const treeLink = (active: boolean) => cn('block truncate rounded-md px-2 py-1 hover:bg-muted', active ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground')

function KindLinks({ nodes, current, depth = 0 }: { nodes: KindNode[]; current: string | null; depth?: number }) {
  return (
    <ul className={depth > 0 ? 'ml-3 border-l pl-1' : undefined}>
      {nodes.map((node) => (
        <li key={node.kind.id}>
          <Link to={inventoryPaths.kind(node.kind.id)} className={treeLink(current === node.kind.id)} aria-current={current === node.kind.id ? 'page' : undefined}>
            {node.kind.name}
          </Link>
          {node.children.length > 0 && <KindLinks nodes={node.children} current={current} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  )
}

/** A new item of a kind; its page opens when it is made. Filament waits for its own phase (#7068). */
function NewItemForm({ kinds, kind, onCancel }: { kinds: ItemKind[]; kind: string | null; onCancel: () => void }) {
  const id = useId()
  const navigate = useNavigate()
  const choices = kinds.filter((k) => k.tracking !== 'filament' && !k.archived_at).sort((a, b) => a.name.localeCompare(b.name))
  const [kindId, setKindId] = useState(choices.some((k) => k.id === kind) ? kind! : '')
  const [name, setName] = useState('')
  const [manufacturer, setManufacturer] = useState('')
  const [lowStock, setLowStock] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!kindId) return setError('Pick the kind of item.')
    if (!name.trim()) return setError('Give the item a name.')
    const low = lowStock.trim()
    if (low && !/^\d+(\.\d+)?$/.test(low)) return setError('Low stock is a number, such as 10.')
    setBusy(true)
    setError(null)
    try {
      const made = await inventoryApi.createItem({ kind_id: kindId, name: name.trim(), ...(manufacturer.trim() ? { manufacturer: manufacturer.trim() } : {}), ...(low ? { low_stock: low } : {}) })
      void navigate(inventoryPaths.item(made.id))
    } catch (cause) {
      setError(failed(cause, 'The item could not be added. Try again.'))
      setBusy(false)
    }
  }

  return (
    <form aria-label="New item" onSubmit={(event) => void submit(event)} className="flex flex-col gap-3 rounded-md border p-3 text-[13px]">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-kind`}>Kind</Label>
          <Select value={kindId} onValueChange={setKindId}>
            <SelectTrigger id={`${id}-kind`} aria-label="Kind" className="w-48">
              <SelectValue placeholder="Pick a kind" />
            </SelectTrigger>
            <SelectContent position="popper">
              {choices.map((k) => (
                <SelectItem key={k.id} value={k.id}>
                  {k.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
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
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          Add item
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

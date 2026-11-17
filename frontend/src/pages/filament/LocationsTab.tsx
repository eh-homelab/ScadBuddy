import { PlusIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import {
  listCatalog,
  listLocations,
  listSpools,
  upsertLocation,
  type LocationCreate,
  type LocationNode,
  type LocationPatch,
  type Spool,
} from '../../api/filament'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LocationTree } from '../../components/filament/LocationTree'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'

interface Loaded {
  nodes: LocationNode[]
  kinds: { id: string; name: string }[]
  spools: Spool[]
}

interface Draft {
  /** The place being changed; `null` for a new one. */
  id: string | null
  name: string
  kind_id: string
  parent_id: string
  identifier: string
  capacity: string
  note: string
}

const TOP = '__top'
const message = (cause: unknown, fallback: string) => (cause instanceof ApiError ? cause.detail : fallback)

/** Every place, depth first, with its path ("Workshop / Shelf A"); a printer's tray is not a place to put a place in. */
function flatten(nodes: LocationNode[], trail: string[] = []): { node: LocationNode; path: string }[] {
  return nodes.flatMap((node) => {
    const path = [...trail, node.name]
    return [...(node.printer_id == null ? [{ node, path: path.join(' / ') }] : []), ...flatten(node.children ?? [], path)]
  })
}

function find(nodes: LocationNode[], id: string): LocationNode | undefined {
  for (const node of nodes) {
    const found = node.id === id ? node : find(node.children ?? [], id)
    if (found) return found
  }
  return undefined
}

const draftOf = (node: LocationNode | undefined, kindId: string): Draft => ({
  id: node?.id ?? null,
  name: node?.name ?? '',
  kind_id: node?.kind_id ?? kindId,
  parent_id: node?.parent_id ?? '',
  identifier: node?.identifier ?? '',
  capacity: node?.capacity == null ? '' : String(node.capacity),
  note: node?.note ?? '',
})

/** #6262 — the Locations tab: the tree of places spools are kept in, and the form that adds or changes one. */
export function LocationsTab() {
  const [params, setParams] = useSearchParams()
  const archived = params.get('archived') === 'true'
  const [data, setData] = useState<Loaded | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const [saving, setSaving] = useState(false)

  /** Each load takes a number; only the newest one's answer is used. */
  const latest = useRef(0)
  const load = useCallback(async () => {
    const mine = ++latest.current
    try {
      const [nodes, kinds, spools] = await Promise.all([
        listLocations({ includeArchived: archived }),
        listCatalog('location-kinds'),
        listSpools({ includeArchived: false }),
      ])
      if (mine !== latest.current) return
      setData({ nodes, kinds, spools })
      setError(null)
    } catch (cause) {
      if (mine === latest.current) setError(message(cause, 'The places could not be loaded. Try again.'))
    }
  }, [archived])

  useEffect(() => {
    void load()
  }, [load])

  const kinds = useMemo(() => new Map((data?.kinds ?? []).map((kind) => [kind.id, kind.name])), [data])
  const spoolsAt = useMemo(() => {
    const at = new Map<string, Spool[]>()
    for (const spool of data?.spools ?? []) {
      if (spool.location_id) at.set(spool.location_id, [...(at.get(spool.location_id) ?? []), spool])
    }
    return at
  }, [data])
  const places = useMemo(() => flatten(data?.nodes ?? []), [data])

  function open(node?: LocationNode) {
    setFormError(null)
    setNotice('')
    setDraft(draftOf(node, data?.kinds[0]?.id ?? ''))
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft || !data) return
    const was = draft.id ? find(data.nodes, draft.id) : undefined
    if (draft.id && !was) {
      // The list was reloaded without this place (an archived one): a post would add a copy.
      setDraft(null)
      setError('That place is no longer in the list. Show archived places to change it.')
      return
    }
    if (!draft.kind_id) {
      setFormError('Choose a kind.')
      return
    }
    const capacity = draft.capacity.trim() === '' ? null : Number(draft.capacity)
    const values = {
      name: draft.name.trim(),
      kind_id: draft.kind_id,
      parent_id: draft.parent_id || null,
      identifier: draft.identifier.trim() || null,
      capacity,
      note: draft.note.trim() || null,
    }
    setSaving(true)
    setFormError(null)
    try {
      if (was) {
        // Only what changed: a patch that names a field the place keeps still counts as an edit.
        const changed: LocationPatch = {}
        for (const key of Object.keys(values) as (keyof typeof values)[]) {
          if (values[key] !== (was[key] ?? null)) Object.assign(changed, { [key]: values[key] })
        }
        if (Object.keys(changed).length > 0) await upsertLocation(changed, was.id)
      } else {
        const body: LocationCreate = { name: values.name, kind_id: values.kind_id, sort_order: 0 }
        for (const key of ['parent_id', 'identifier', 'capacity', 'note'] as const) {
          if (values[key] !== null) Object.assign(body, { [key]: values[key] })
        }
        await upsertLocation(body)
      }
      setDraft(null)
      setNotice('Saved')
      await load()
    } catch (cause) {
      setFormError(message(cause, 'The place could not be saved. Try again.'))
    } finally {
      setSaving(false)
    }
  }

  async function archive(node: LocationNode, value: boolean) {
    setNotice('')
    setError(null)
    try {
      await upsertLocation({ archived: value }, node.id)
      setNotice(value ? 'Archived' : 'Restored')
      await load()
    } catch (cause) {
      setError(message(cause, 'The place could not be changed. Try again.'))
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <Checkbox
            checked={archived}
            aria-label="Show archived places"
            onCheckedChange={(on) => {
              const next = new URLSearchParams(params)
              if (on === true) next.set('archived', 'true')
              else next.delete('archived')
              setParams(next, { replace: true })
            }}
          />
          Show archived
        </label>
        <Button type="button" size="sm" disabled={!data} onClick={() => open()}>
          <PlusIcon /> Add a place
        </Button>
      </div>
      <p role="status" className="min-h-5 text-[13px] text-muted-foreground">
        {notice}
      </p>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!data && !error && <p className="text-[13px] text-muted-foreground">Loading places…</p>}
      {draft && data && (
        <form aria-label="Place" onSubmit={(event) => void save(event)} className="grid max-w-2xl gap-3 rounded-md border p-3 text-[13px] sm:grid-cols-2">
          <label className="flex flex-col gap-1">
            Name
            <Input value={draft.name} required onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
          </label>
          <label className="flex flex-col gap-1">
            Kind
            <Select value={draft.kind_id} onValueChange={(next) => setDraft({ ...draft, kind_id: next })}>
              <SelectTrigger aria-label="Kind" aria-required className="w-full text-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {data.kinds.map((kind) => (
                  <SelectItem key={kind.id} value={kind.id}>
                    {kind.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label className="flex flex-col gap-1">
            Inside
            <Select value={draft.parent_id || TOP} onValueChange={(next) => setDraft({ ...draft, parent_id: next === TOP ? '' : next })}>
              <SelectTrigger aria-label="Inside" className="w-full text-[13px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TOP}>Nowhere (top level)</SelectItem>
                {places
                  .filter(({ node }) => node.id !== draft.id)
                  .map(({ node, path }) => (
                    <SelectItem key={node.id} value={node.id}>
                      {path}
                    </SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </label>
          <label className="flex flex-col gap-1">
            Label
            <Input value={draft.identifier} onChange={(event) => setDraft({ ...draft, identifier: event.target.value })} />
          </label>
          <label className="flex flex-col gap-1">
            Capacity (spools)
            <Input
              type="number"
              min={1}
              inputMode="numeric"
              value={draft.capacity}
              onChange={(event) => setDraft({ ...draft, capacity: event.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1">
            Note
            <Input value={draft.note} onChange={(event) => setDraft({ ...draft, note: event.target.value })} />
          </label>
          {formError && (
            <p role="alert" className="text-warn sm:col-span-2">
              {formError}
            </p>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <Button type="submit" size="sm" disabled={saving}>
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </div>
        </form>
      )}
      {data && <LocationTree nodes={data.nodes} kinds={kinds} spoolsAt={spoolsAt} onEdit={open} onArchive={(node, value) => void archive(node, value)} />}
    </div>
  )
}

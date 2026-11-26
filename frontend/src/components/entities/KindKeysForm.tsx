import { useState, type FormEvent } from 'react'
import { listCatalog, listSpools, listTypes } from '../../api/filament'
import { failure } from '../../lib/mediaFiles'
import { useAsync } from '../../lib/useAsync'
import { CATALOGS } from '../filament/catalogs'
import { REQUIRED_KINDS, entityApi, hasValue, type PropertyValue } from './api'
import { PropertyKeyTable } from './PropertyKeyTable'
import { requiredOf } from './definedProperties'
import { namedTwice, rowsOf, schemaOf } from './propertyKeys'
import { Button } from '@/components/ui/button'

type Props = { kind: string; onSaved: () => void; onCancel: () => void }

/**
 * #7656 — the property keys of an entity kind (a spool type, a catalog), which every entity
 * of the kind is offered and whose values they type. A key is never refused for not being
 * one of them, and removing one keeps the values stored under it (spec §6.3a). An item's
 * keys are its item kind's, edited on `/inventory/kinds`.
 */
export function KindKeysForm({ kind, onSaved, onCancel }: Props) {
  const schema = useAsync(() => entityApi.propertySchema(kind), [kind])
  if (schema.error)
    return (
      <p role="alert" className="text-[13px] text-warn">
        The keys could not be loaded: {failure(schema.error)}
      </p>
    )
  // A saved schema is whole, so the table starts only from the one read.
  if (!schema.data || schema.loading) return <p className="text-[13px] text-muted-foreground">Loading the keys…</p>
  return <Keys kind={kind} own={schema.data.schema} onSaved={onSaved} onCancel={onCancel} />
}

/** The unarchived rows of a kind whose rows are checked (#7703), by id: what its own list route answers. */
async function liveRows(kind: string): Promise<string[]> {
  const catalog = CATALOGS.find((spec) => spec.entity === kind)
  const rows: { id: string; archived_at?: string | null }[] =
    kind === 'spool' ? await listSpools() : kind === 'spool_type' ? await listTypes() : catalog ? await listCatalog(catalog.kind) : []
  return rows.filter((row) => !row.archived_at).map((row) => row.id)
}

/** Each key's count of unarchived rows that have no value for it. */
async function lacking(kind: string, keys: string[]): Promise<Map<string, number>> {
  const [ids, properties] = await Promise.all([liveRows(kind), entityApi.kindProperties(kind)])
  const values = (id: string): Record<string, PropertyValue> => properties[id] ?? {}
  return new Map(keys.map((key) => [key, ids.filter((id) => !hasValue(values(id)[key])).length]))
}

function Keys({ kind, own, onSaved, onCancel }: Props & { own: Record<string, unknown> }) {
  const [rows, setRows] = useState(() => rowsOf(own))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requirable = REQUIRED_KINDS.has(kind)
  // A key ticked required here and not before: rows without it read incomplete once saved.
  const wasRequired = new Set(requiredOf(own))
  const newlyRequired = [...new Set(rows.filter((row) => row.required && row.key.trim() && !wasRequired.has(row.key.trim())).map((row) => row.key.trim()))]
  const counts = useAsync(() => (newlyRequired.length ? lacking(kind, newlyRequired) : Promise.resolve(new Map<string, number>())), [kind, newlyRequired.join('\u001f')])

  async function save(event: FormEvent) {
    event.preventDefault()
    if (namedTwice(rows)) return setError('Each key is named once.')
    setBusy(true)
    setError(null)
    try {
      await entityApi.setPropertySchema(kind, schemaOf(own, rows))
      onSaved()
    } catch (caught) {
      setError(failure(caught))
      setBusy(false)
    }
  }

  return (
    <form noValidate aria-label={`Keys of ${kind}`} onSubmit={(event) => void save(event)} className="flex flex-col gap-2 overflow-x-auto rounded-md border p-2.5 text-[13px]">
      <p className="text-muted-foreground">These keys are for every entity of this kind. They type a value; any other key is still kept, and removing a key here keeps the values stored under it.</p>
      <PropertyKeyTable label={`Property keys of ${kind}`} rows={rows} onChange={setRows} requirable={requirable} />
      {newlyRequired.length > 0 && (
        <ul aria-label="Rows that lack a newly required key" className="text-xs text-muted-foreground">
          {newlyRequired.map((key) => {
            const n = counts.data?.get(key)
            return <li key={key}>{n === undefined ? `Counting the rows that lack ${key}…` : `${n} ${n === 1 ? 'row lacks' : 'rows lack'} ${key}`}</li>
          })}
          <li>A row without a required key reads incomplete, and its next save asks for it.</li>
        </ul>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="xs" disabled={busy}>
          Save keys
        </Button>
        <Button type="button" size="xs" variant="ghost" disabled={busy} onClick={onCancel}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

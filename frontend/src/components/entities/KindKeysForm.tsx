import { useState, type FormEvent } from 'react'
import { failure } from '../../lib/mediaFiles'
import { useAsync } from '../../lib/useAsync'
import { entityApi } from './api'
import { PropertyKeyTable } from './PropertyKeyTable'
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

function Keys({ kind, own, onSaved, onCancel }: Props & { own: Record<string, unknown> }) {
  const [rows, setRows] = useState(() => rowsOf(own))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
      <PropertyKeyTable label={`Property keys of ${kind}`} rows={rows} onChange={setRows} />
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

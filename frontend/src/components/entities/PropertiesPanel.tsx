import { memo, useId, useState, type FormEvent } from 'react'
import { PencilIcon, Trash2Icon } from 'lucide-react'
import { failure } from '../../lib/mediaFiles'
import { useAsync } from '../../lib/useAsync'
import { entityApi, entityParts, type EntityProperty, type KindPropertySchema, type PropertyValue } from './api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from '@/components/ui/spinner'

type ValueType = 'text' | 'number' | 'yesno' | 'list'
const TYPE_LABELS: Record<ValueType, string> = { text: 'Text', number: 'Number', yesno: 'Yes/no', list: 'List' }
const TYPES = Object.keys(TYPE_LABELS) as ValueType[]

/** What the schema says of a key: its JSON Schema `type` and its description. */
type KeyDoc = { type?: string; description?: string }

function keyDocs(schema: KindPropertySchema | undefined): Record<string, KeyDoc> {
  const properties = schema?.schema.properties
  return properties && typeof properties === 'object' ? (properties as Record<string, KeyDoc>) : {}
}

/** The picker's type for a schema's `type`; a key the schema does not type is text. */
function typeOfSchema(doc: KeyDoc | undefined): ValueType {
  switch (doc?.type) {
    case 'number':
    case 'integer':
      return 'number'
    case 'boolean':
      return 'yesno'
    case 'array':
      return 'list'
    default:
      return 'text'
  }
}

function typeOfValue(value: PropertyValue): ValueType {
  if (Array.isArray(value)) return 'list'
  if (typeof value === 'number') return 'number'
  if (typeof value === 'boolean') return 'yesno'
  return 'text'
}

type Draft = { key: string; type: ValueType; text: string; yes: boolean }

function draftOf(property: EntityProperty): Draft {
  const { value } = property
  return {
    key: property.key,
    type: typeOfValue(value),
    text: Array.isArray(value) ? value.join(', ') : typeof value === 'boolean' ? '' : String(value),
    yes: value === true,
  }
}

/** The draft's value, or why it is not one. A list is split at commas. */
function valueOf(draft: Draft): { value: PropertyValue } | { error: string } {
  switch (draft.type) {
    case 'number': {
      const number = Number(draft.text.trim())
      return draft.text.trim() !== '' && Number.isFinite(number) ? { value: number } : { error: 'Enter a number, such as 1.8.' }
    }
    case 'yesno':
      return { value: draft.yes }
    case 'list':
      return { value: draft.text.split(',').map((item) => item.trim()).filter((item) => item !== '') }
    default:
      return { value: draft.text.trim() }
  }
}

/**
 * #7026 — the key-value properties of any entity (`entity` is `kind:ref`): a GTIN, an MPN, a
 * lot number. Each row shows the key, its value (a list as chips) and what the kind's schema
 * says of the key. A key is edited in place; renaming one is one PATCH that removes the old
 * key and sets the new. The server decides what a key and a value may be (`bad_property_value`).
 */
export const PropertiesPanel = memo(function PropertiesPanel({ entity, title = 'Properties' }: { entity: string; title?: string }) {
  const { kind } = entityParts(entity)
  const list = useAsync(() => entityApi.properties(entity), [entity])
  const schema = useAsync(() => entityApi.propertySchema(kind), [kind])
  const properties = list.data ?? []
  const docs = keyDocs(schema.data)
  const suggestions = [...new Set([...Object.keys(docs), ...(schema.data?.keys_in_use ?? []).map((row) => row.key)])].sort()
  const keysId = useId()

  /** Sends one PATCH and shows its answer; throws its failure for the caller to show. */
  async function patch(set: Record<string, PropertyValue | null>) {
    list.setData(await entityApi.setProperties(entity, set), { supersede: true })
  }

  return (
    <section aria-label={title} className="flex flex-col gap-3">
      <h3 className="text-[13px] font-medium">{title}</h3>
      <datalist id={keysId}>
        {suggestions.map((key) => (
          <option key={key} value={key} />
        ))}
      </datalist>
      {list.error ? (
        <p role="alert" className="text-[13px] text-warn">
          The properties could not be loaded: {failure(list.error)}
        </p>
      ) : list.loading && !list.data ? (
        <p className="text-[13px] text-muted-foreground">Loading the properties…</p>
      ) : properties.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No properties yet.</p>
      ) : (
        <div className="overflow-hidden rounded-md border">
          <table className="w-full text-[13px]">
            <tbody className="divide-y">
              {properties.map((property) => (
                <PropertyRow key={property.key} property={property} doc={docs[property.key]} docs={docs} keysId={keysId} onPatch={patch} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AddProperty docs={docs} keysId={keysId} onPatch={patch} />
    </section>
  )
})

function TypePicker({ value, onChange, label }: { value: ValueType; onChange: (type: ValueType) => void; label: string }) {
  return (
    <Select value={value} onValueChange={(type) => onChange(type as ValueType)}>
      <SelectTrigger size="sm" aria-label={label} className="w-28">
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper">
        {TYPES.map((type) => (
          <SelectItem key={type} value={type}>
            {TYPE_LABELS[type]}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function ValueField({ draft, onChange, label }: { draft: Draft; onChange: (draft: Draft) => void; label: string }) {
  if (draft.type === 'yesno') {
    return (
      <Select value={draft.yes ? 'yes' : 'no'} onValueChange={(yes) => onChange({ ...draft, yes: yes === 'yes' })}>
        <SelectTrigger size="sm" aria-label={label} className="w-24">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper">
          <SelectItem value="yes">Yes</SelectItem>
          <SelectItem value="no">No</SelectItem>
        </SelectContent>
      </Select>
    )
  }
  return (
    <Input
      value={draft.text}
      onChange={(event) => onChange({ ...draft, text: event.target.value })}
      aria-label={label}
      inputMode={draft.type === 'number' ? 'decimal' : undefined}
      placeholder={draft.type === 'list' ? 'One, two, three' : draft.type === 'number' ? '0' : 'Value'}
      className="h-8 min-w-0 flex-[2_1_12rem]"
    />
  )
}

function Chips({ value }: { value: PropertyValue }) {
  if (Array.isArray(value)) {
    return value.length === 0 ? (
      <span className="text-muted-foreground">Empty list</span>
    ) : (
      <ul className="flex flex-wrap gap-1">
        {value.map((item, at) => (
          <li key={at} className="rounded-sm bg-muted px-1.5 py-px tabular-nums">
            {item}
          </li>
        ))}
      </ul>
    )
  }
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>
  return <span className="break-all tabular-nums">{String(value)}</span>
}

function PropertyRow({
  property,
  doc,
  docs,
  keysId,
  onPatch,
}: {
  property: EntityProperty
  doc: KeyDoc | undefined
  docs: Record<string, KeyDoc>
  keysId: string
  onPatch: (set: Record<string, PropertyValue | null>) => Promise<void>
}) {
  const { key } = property
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Draft>(() => draftOf(property))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function act(set: Record<string, PropertyValue | null>, done?: () => void) {
    setBusy(true)
    setError(null)
    try {
      await onPatch(set)
      done?.()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  function save(event: FormEvent) {
    event.preventDefault()
    const parsed = valueOf(draft)
    if ('error' in parsed) return setError(parsed.error)
    const next = draft.key.trim()
    void act(next === key ? { [key]: parsed.value } : { [key]: null, [next]: parsed.value }, () => setEditing(false))
  }

  const alert = error && (
    <p role="alert" className="text-xs text-warn">
      {error}
    </p>
  )

  if (editing) {
    return (
      <tr>
        <td colSpan={3} className="p-2">
          <form noValidate onSubmit={save} className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-2">
              <Input
                value={draft.key}
                onChange={(event) => setDraft({ ...draft, key: event.target.value })}
                aria-label={`Key of ${key}`}
                list={keysId}
                spellCheck={false}
                className="h-8 min-w-0 flex-[1_1_9rem] font-mono text-[12px]"
              />
              <TypePicker value={draft.type} onChange={(type) => setDraft({ ...draft, type })} label={`Type of ${key}`} />
              <ValueField draft={draft} onChange={setDraft} label={`Value of ${key}`} />
            </div>
            {docs[draft.key.trim()]?.description && <p className="text-[12px] text-muted-foreground">{docs[draft.key.trim()]!.description}</p>}
            <div className="flex gap-1">
              <Button size="xs" type="submit" disabled={busy || draft.key.trim() === ''}>
                {busy && <Spinner />}
                Save
              </Button>
              <Button
                size="xs"
                type="button"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setEditing(false)
                  setError(null)
                }}
              >
                Cancel
              </Button>
            </div>
            {alert}
          </form>
        </td>
      </tr>
    )
  }

  return (
    <tr className="align-top">
      <th scope="row" className="w-2/5 px-2.5 py-1.5 text-left font-normal">
        <span className="font-mono text-[12px] break-all">{key}</span>
        {doc?.description && <span className="block text-[12px] text-muted-foreground">{doc.description}</span>}
      </th>
      <td className="px-2.5 py-1.5">
        <Chips value={property.value} />
        {alert}
      </td>
      <td className="w-px py-1 pr-1 whitespace-nowrap">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Edit ${key}`}
          title="Edit"
          disabled={busy}
          onClick={() => {
            setDraft(draftOf(property))
            setError(null)
            setEditing(true)
          }}
        >
          <PencilIcon aria-hidden="true" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Remove ${key}`}
          title="Remove"
          disabled={busy}
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          onClick={() => void act({ [key]: null })}
        >
          {busy ? <Spinner /> : <Trash2Icon aria-hidden="true" />}
        </Button>
      </td>
    </tr>
  )
}

const EMPTY: Draft = { key: '', type: 'text', text: '', yes: true }

function AddProperty({ docs, keysId, onPatch }: { docs: Record<string, KeyDoc>; keysId: string; onPatch: (set: Record<string, PropertyValue | null>) => Promise<void> }) {
  const [draft, setDraft] = useState<Draft>(EMPTY)
  // Until the person picks a type, a key the schema knows sets it.
  const [typePicked, setTypePicked] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const doc = docs[draft.key.trim()]

  async function add(event: FormEvent) {
    event.preventDefault()
    const parsed = valueOf(draft)
    if ('error' in parsed) return setError(parsed.error)
    setBusy(true)
    setError(null)
    try {
      await onPatch({ [draft.key.trim()]: parsed.value })
      setDraft(EMPTY)
      setTypePicked(false)
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form noValidate onSubmit={(event) => void add(event)} aria-label="Add a property" className="flex flex-col gap-2 rounded-md border border-dashed p-2.5">
      <div className="flex flex-wrap gap-2">
        <Input
          value={draft.key}
          onChange={(event) => {
            const key = event.target.value
            setDraft({ ...draft, key, type: typePicked ? draft.type : typeOfSchema(docs[key.trim()]) })
          }}
          aria-label="New property key"
          list={keysId}
          spellCheck={false}
          placeholder="key, such as gtin"
          className="h-8 min-w-0 flex-[1_1_9rem] font-mono text-[12px]"
        />
        <TypePicker
          value={draft.type}
          onChange={(type) => {
            setTypePicked(true)
            setDraft({ ...draft, type })
          }}
          label="New property type"
        />
        <ValueField draft={draft} onChange={setDraft} label="New property value" />
        <Button type="submit" size="sm" disabled={busy || draft.key.trim() === ''}>
          {busy && <Spinner />}
          Add property
        </Button>
      </div>
      {doc?.description && <p className="text-[12px] text-muted-foreground">{doc.description}</p>}
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </form>
  )
}

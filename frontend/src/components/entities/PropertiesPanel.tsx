import { memo, useId, useState, type FormEvent } from 'react'
import { PencilIcon, PlusIcon, Trash2Icon, XIcon } from 'lucide-react'
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

type PropertySet = Record<string, PropertyValue | null>
type Patch = (set: PropertySet) => Promise<void>

/** What the schema says of a key: its JSON Schema `type` (one, or a union) and its description. */
type KeyDoc = { type?: string | string[]; description?: string }

function keyDocs(schema: KindPropertySchema | undefined): Record<string, KeyDoc> {
  const properties = schema?.schema.properties
  return properties && typeof properties === 'object' ? (properties as Record<string, KeyDoc>) : {}
}

/** The picker's type for a schema's `type`: a union's first member that is not `null`; an untyped key is text. */
function typeOfSchema(doc: KeyDoc | undefined): ValueType {
  const type = Array.isArray(doc?.type) ? doc.type.find((member) => member !== 'null') : doc?.type
  switch (type) {
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

/** A value being edited. A list is edited item by item, so an item may hold a comma. */
type Draft = { type: ValueType; text: string; yes: boolean; items: string[] }

function draftOf(value: PropertyValue): Draft {
  return {
    type: typeOfValue(value),
    text: Array.isArray(value) || typeof value === 'boolean' ? '' : String(value),
    yes: value === true,
    items: Array.isArray(value) ? [...value] : [''],
  }
}

/** A type change carries what it can: text to a one-item list, a one-item list back to text. */
function retyped(draft: Draft, type: ValueType): Draft {
  if (type === 'list' && draft.type !== 'list' && draft.text !== '') return { ...draft, type, items: [draft.text] }
  if (draft.type === 'list' && type !== 'list' && draft.items.length === 1) return { ...draft, type, text: draft.items[0]! }
  return { ...draft, type }
}

/** The draft's value, or why it is not one. A string is sent as typed: only the key is trimmed. */
function valueOf(draft: Draft): { value: PropertyValue } | { error: string } {
  switch (draft.type) {
    case 'number': {
      const number = Number(draft.text.trim())
      return draft.text.trim() !== '' && Number.isFinite(number) ? { value: number } : { error: 'Enter a number, such as 1.8.' }
    }
    case 'yesno':
      return { value: draft.yes }
    case 'list':
      return { value: [...draft.items] }
    default:
      return { value: draft.text }
  }
}

/**
 * One PATCH at a time, its failure kept to show, and a write over a key the entity already
 * has held until the person confirms it.
 */
function usePatch(onPatch: Patch) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [held, setHeld] = useState<{ set: PropertySet; replaces: string; done?: () => void } | null>(null)

  async function run(set: PropertySet, done?: () => void) {
    setBusy(true)
    setError(null)
    setHeld(null)
    try {
      await onPatch(set)
      done?.()
    } catch (caught) {
      setError(failure(caught))
    } finally {
      setBusy(false)
    }
  }

  return {
    busy,
    error,
    held,
    fail: (message: string | null) => setError(message),
    /** Sends `set`; with `replaces`, only after the person confirms it. */
    send(set: PropertySet, options: { replaces?: string; done?: () => void } = {}) {
      if (!options.replaces) return void run(set, options.done)
      setError(null)
      setHeld({ set, replaces: options.replaces, done: options.done })
    },
    confirm() {
      if (held) void run(held.set, held.done)
    },
    cancel: () => setHeld(null),
  }
}

function PatchFeedback({ patch }: { patch: ReturnType<typeof usePatch> }) {
  return (
    <>
      {patch.held && (
        <div role="group" aria-label="Replace a property" className="flex flex-wrap items-center gap-2 text-xs">
          <span>
            This replaces <span className="font-mono">{patch.held.replaces}</span>&rsquo;s value.
          </span>
          <Button size="xs" type="button" variant="destructive" disabled={patch.busy} onClick={patch.confirm}>
            Replace
          </Button>
          <Button size="xs" type="button" variant="ghost" disabled={patch.busy} onClick={patch.cancel}>
            Keep it
          </Button>
        </div>
      )}
      {patch.error && (
        <p role="alert" className="text-xs text-warn">
          {patch.error}
        </p>
      )}
    </>
  )
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
  const keys = new Set(properties.map((property) => property.key))
  const docs = keyDocs(schema.data)
  const suggestions = [...new Set([...Object.keys(docs), ...(schema.data?.keys_in_use ?? []).map((row) => row.key)])].sort()
  const keysId = useId()

  async function patch(set: PropertySet) {
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
                <PropertyRow key={property.key} property={property} doc={docs[property.key]} docs={docs} keys={keys} keysId={keysId} onPatch={patch} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <AddProperty docs={docs} keys={keys} keysId={keysId} onPatch={patch} />
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
  if (draft.type === 'list') {
    const setItem = (at: number, item: string) => onChange({ ...draft, items: draft.items.map((was, i) => (i === at ? item : was)) })
    return (
      <div role="group" aria-label={label} className="flex min-w-0 flex-[2_1_12rem] flex-col gap-1">
        {draft.items.map((item, at) => (
          <div key={at} className="flex gap-1">
            <Input value={item} onChange={(event) => setItem(at, event.target.value)} aria-label={`${label}, item ${at + 1}`} className="h-8 min-w-0 flex-1" />
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Remove item ${at + 1}`}
              title="Remove item"
              onClick={() => onChange({ ...draft, items: draft.items.filter((_, i) => i !== at) })}
            >
              <XIcon aria-hidden="true" />
            </Button>
          </div>
        ))}
        <Button type="button" variant="ghost" size="xs" className="self-start" onClick={() => onChange({ ...draft, items: [...draft.items, ''] })}>
          <PlusIcon aria-hidden="true" />
          Add item
        </Button>
      </div>
    )
  }
  return (
    <Input
      value={draft.text}
      onChange={(event) => onChange({ ...draft, text: event.target.value })}
      aria-label={label}
      inputMode={draft.type === 'number' ? 'decimal' : undefined}
      placeholder={draft.type === 'number' ? '0' : 'Value'}
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
          <li key={at} className="rounded-sm bg-muted px-1.5 py-px whitespace-pre-wrap tabular-nums">
            {item}
          </li>
        ))}
      </ul>
    )
  }
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>
  return <span className="break-all whitespace-pre-wrap tabular-nums">{String(value)}</span>
}

function PropertyRow({
  property,
  doc,
  docs,
  keys,
  keysId,
  onPatch,
}: {
  property: EntityProperty
  doc: KeyDoc | undefined
  docs: Record<string, KeyDoc>
  keys: ReadonlySet<string>
  keysId: string
  onPatch: Patch
}) {
  const { key } = property
  const [editing, setEditing] = useState(false)
  const [newKey, setNewKey] = useState(key)
  const [draft, setDraft] = useState<Draft>(() => draftOf(property.value))
  // An untouched value is sent as stored, so a rename alone never changes it.
  const [touched, setTouched] = useState(false)
  const patch = usePatch(onPatch)
  const close = () => setEditing(false)

  function save(event: FormEvent) {
    event.preventDefault()
    const parsed = touched ? valueOf(draft) : { value: property.value }
    if ('error' in parsed) return patch.fail(parsed.error)
    const next = newKey.trim()
    if (next === key) return patch.send({ [key]: parsed.value }, { done: close })
    patch.send({ [key]: null, [next]: parsed.value }, { replaces: keys.has(next) ? next : undefined, done: close })
  }

  const editDraft = (next: Draft) => {
    setTouched(true)
    setDraft(next)
  }

  if (editing) {
    return (
      <tr>
        <td colSpan={3} className="p-2">
          <form noValidate onSubmit={save} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-start gap-2">
              <Input
                value={newKey}
                onChange={(event) => setNewKey(event.target.value)}
                aria-label={`Key of ${key}`}
                list={keysId}
                spellCheck={false}
                className="h-8 min-w-0 flex-[1_1_9rem] font-mono text-[12px]"
              />
              <TypePicker value={draft.type} onChange={(type) => editDraft(retyped(draft, type))} label={`Type of ${key}`} />
              <ValueField draft={draft} onChange={editDraft} label={`Value of ${key}`} />
            </div>
            {docs[newKey.trim()]?.description && <p className="text-[12px] text-muted-foreground">{docs[newKey.trim()]!.description}</p>}
            <div className="flex gap-1">
              <Button size="xs" type="submit" disabled={patch.busy || newKey.trim() === ''}>
                {patch.busy && <Spinner />}
                Save
              </Button>
              <Button
                size="xs"
                type="button"
                variant="ghost"
                disabled={patch.busy}
                onClick={() => {
                  close()
                  patch.cancel()
                  patch.fail(null)
                }}
              >
                Cancel
              </Button>
            </div>
            <PatchFeedback patch={patch} />
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
        <PatchFeedback patch={patch} />
      </td>
      <td className="w-px py-1 pr-1 whitespace-nowrap">
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Edit ${key}`}
          title="Edit"
          disabled={patch.busy}
          onClick={() => {
            setNewKey(key)
            setDraft(draftOf(property.value))
            setTouched(false)
            patch.fail(null)
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
          disabled={patch.busy}
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          onClick={() => patch.send({ [key]: null })}
        >
          {patch.busy ? <Spinner /> : <Trash2Icon aria-hidden="true" />}
        </Button>
      </td>
    </tr>
  )
}

const EMPTY: Draft = { type: 'text', text: '', yes: true, items: [''] }

function AddProperty({ docs, keys, keysId, onPatch }: { docs: Record<string, KeyDoc>; keys: ReadonlySet<string>; keysId: string; onPatch: Patch }) {
  const [key, setKey] = useState('')
  const [draft, setDraft] = useState<Draft>(EMPTY)
  // Until the person picks a type, a key the schema knows sets it.
  const [typePicked, setTypePicked] = useState(false)
  const patch = usePatch(onPatch)
  const doc = docs[key.trim()]

  function add(event: FormEvent) {
    event.preventDefault()
    const parsed = valueOf(draft)
    if ('error' in parsed) return patch.fail(parsed.error)
    const name = key.trim()
    patch.send(
      { [name]: parsed.value },
      {
        replaces: keys.has(name) ? name : undefined,
        done: () => {
          setKey('')
          setDraft(EMPTY)
          setTypePicked(false)
        },
      },
    )
  }

  return (
    <form noValidate onSubmit={add} aria-label="Add a property" className="flex flex-col gap-2 rounded-md border border-dashed p-2.5">
      <div className="flex flex-wrap items-start gap-2">
        <Input
          value={key}
          onChange={(event) => {
            setKey(event.target.value)
            if (!typePicked) setDraft(retyped(draft, typeOfSchema(docs[event.target.value.trim()])))
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
            setDraft(retyped(draft, type))
          }}
          label="New property type"
        />
        <ValueField draft={draft} onChange={setDraft} label="New property value" />
        <Button type="submit" size="sm" disabled={patch.busy || key.trim() === ''}>
          {patch.busy && <Spinner />}
          Add property
        </Button>
      </div>
      {doc?.description && <p className="text-[12px] text-muted-foreground">{doc.description}</p>}
      <PatchFeedback patch={patch} />
    </form>
  )
}

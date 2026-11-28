import { useMemo, useState } from 'react'
import { copyPreset, editPreset, type PresetDetail, type PresetEdit, type PresetEdited } from '../../api/filamentPresets'
import { FilamentSearchBox } from '../../components/filament/FilamentSearchBox'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { writeFailure, type WriteFailure } from './presetWrites'

type Doc = Record<string, unknown>

/** A value the edit route takes: a text, or a list of texts. Anything else is shown but not edited. */
const editable = (value: unknown): value is string | string[] => typeof value === 'string' || (Array.isArray(value) && value.every((item) => typeof item === 'string'))

const toText = (value: string | string[]) => (Array.isArray(value) ? value.join('\n') : value)

/** Text back to a value: a list stays a list, and a new text with more than one line becomes one. No text at all is the empty list, a valid value. */
function fromText(was: unknown, text: string): string | string[] {
  if (Array.isArray(was) && text === '') return []
  if (!Array.isArray(was) && !text.includes('\n')) return text
  return text.split('\n').map((line) => line.replace(/\r$/, ''))
}

const hasEmptyItem = (value: string | string[]) => Array.isArray(value) && value.some((item) => item === '')

/** One key for each opened form. A retry after a 503 sends it again, so the server runs the write once. */
const useFormKey = () => useState(() => crypto.randomUUID())[0]

function Failure({ failure }: { failure: WriteFailure }) {
  return (
    <p role="alert" className="text-[13px] text-warn">
      {failure.text}
    </p>
  )
}

/** A manual copy of any preset, under a name the person gives. It opens the copy when it is made. */
export function CopyForm({ preset, onMade, onClose, onOff }: { preset: PresetDetail; onMade: (copy: PresetDetail) => void; onClose: () => void; onOff: () => void }) {
  const key = useFormKey()
  const [name, setName] = useState(`${preset.name} copy`)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<WriteFailure | null>(null)

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setFailure(null)
    try {
      onMade(await copyPreset(preset.id, name.trim(), key))
    } catch (cause) {
      const next = writeFailure(cause)
      if (next.off) onOff()
      else setFailure(next)
      setBusy(false)
    }
  }

  return (
    <form aria-label="Make a copy" onSubmit={submit} className="flex flex-col gap-2 rounded-md border p-3">
      <p className="text-[13px] text-muted-foreground">The copy is a manual preset with the same settings. You can edit it.</p>
      <div className="flex flex-wrap items-center gap-2">
        <Input aria-label="Name of the copy" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} className="w-full sm:w-96" />
        <Button type="submit" size="sm" disabled={busy || name.trim() === ''}>
          Create copy
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {failure && <Failure failure={failure} />}
    </form>
  )
}

interface Added {
  id: number
  key: string
  value: string
}

/** The edit of a manual preset: the name, the value of each key, keys to remove and keys to add. */
export function EditForm({
  preset,
  onSaved,
  onMoved,
  onClose,
  onOff,
}: {
  preset: PresetDetail
  onSaved: (edited: PresetEdited) => void
  onMoved: (failure: WriteFailure) => void
  onClose: () => void
  onOff: () => void
}) {
  const key = useFormKey()
  const document = useMemo<Doc>(() => preset.document ?? {}, [preset.document])
  const entries = useMemo(() => Object.entries(document).sort(([a], [b]) => a.localeCompare(b)), [document])
  const [name, setName] = useState(preset.name)
  const [values, setValues] = useState<Record<string, string>>({})
  const [removed, setRemoved] = useState<Set<string>>(new Set())
  const [added, setAdded] = useState<Added[]>([{ id: 0, key: '', value: '' }])
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<WriteFailure | null>(null)

  const body = useMemo<PresetEdit>(() => {
    const set: Record<string, string | string[]> = {}
    for (const [field, text] of Object.entries(values)) {
      const was = document[field]
      if (removed.has(field) || !editable(was) || text === toText(was)) continue
      set[field] = fromText(was, text)
    }
    for (const row of added) {
      const field = row.key.trim()
      if (field && row.value !== '') set[field] = fromText(undefined, row.value)
    }
    const edit: PresetEdit = { expected_version: preset.version }
    if (name.trim() !== preset.name) edit.name = name.trim()
    if (Object.keys(set).length > 0) edit.set = set
    if (removed.size > 0) edit.unset = [...removed].sort()
    return edit
  }, [values, removed, added, name, document, preset.name, preset.version])

  const nothing = body.name === undefined && !body.set && !body.unset
  const emptyItems = Object.entries(body.set ?? {}).filter(([, value]) => hasEmptyItem(value)).map(([field]) => field)
  const needle = filter.trim().toLowerCase()
  const shown = needle ? entries.filter(([field]) => field.toLowerCase().includes(needle)) : entries

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setFailure(null)
    try {
      onSaved(await editPreset(preset.id, body, key))
    } catch (cause) {
      const next = writeFailure(cause)
      if (next.off) onOff()
      else if (next.moved) onMoved(next)
      else setFailure(next)
      setBusy(false)
    }
  }

  const toggle = (field: string, on: boolean) =>
    setRemoved((was) => {
      const next = new Set(was)
      if (on) next.add(field)
      else next.delete(field)
      return next
    })

  const setAddedRow = (id: number, patch: Partial<Added>) => setAdded((was) => was.map((row) => (row.id === id ? { ...row, ...patch } : row)))

  return (
    <form aria-label="Edit preset" onSubmit={submit} className="flex flex-col gap-3 rounded-md border p-3">
      <p className="text-[13px] text-muted-foreground">A saved edit makes a new version. Put one item on each line to set a list.</p>
      <label className="flex flex-col gap-1 text-[13px]">
        <span className="font-medium">Name</span>
        <Input aria-label="Name" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} className="w-full sm:w-96" />
      </label>
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-[13px] font-medium">Settings</span>
          <FilamentSearchBox label="Filter keys to edit" placeholder="Filter keys  /" value={filter} onChange={setFilter} className="w-full sm:w-72" />
        </div>
        <div className="max-h-96 overflow-y-auto rounded-md border">
          <table aria-label="Settings to edit" className="w-full text-left text-[13px]">
            <tbody>
              {shown.map(([field, value]) => {
                const gone = removed.has(field)
                return (
                  <tr key={field} className="border-t align-top first:border-t-0">
                    <th scope="row" className="w-1/3 px-2 py-1.5 font-mono font-normal break-all">
                      {field}
                    </th>
                    <td className="px-2 py-1.5">
                      {editable(value) ? (
                        <Textarea
                          aria-label={`Value of ${field}`}
                          value={values[field] ?? toText(value)}
                          disabled={gone}
                          rows={1}
                          onChange={(event) => setValues((was) => ({ ...was, [field]: event.target.value }))}
                          className="min-h-8 py-1 font-mono text-[13px]"
                        />
                      ) : (
                        <span className="text-muted-foreground">This value is not text. Edit it in Bambu Studio.</span>
                      )}
                    </td>
                    <td className="px-2 py-1.5">
                      <label className="flex items-center gap-1.5 whitespace-nowrap">
                        <Checkbox aria-label={`Remove ${field}`} checked={gone} onCheckedChange={(on) => toggle(field, on === true)} />
                        Remove
                      </label>
                    </td>
                  </tr>
                )
              })}
              {shown.length === 0 && (
                <tr>
                  <td className="px-2 py-3 text-muted-foreground">{entries.length === 0 ? 'The document has no keys.' : `No key matches “${filter}”.`}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-[13px] font-medium">Add a setting</span>
        {added.map((row) => (
          <div key={row.id} className="flex flex-wrap items-start gap-2">
            <Input aria-label="New key" placeholder="Key" value={row.key} onChange={(event) => setAddedRow(row.id, { key: event.target.value })} className="w-full font-mono sm:w-64" />
            <Textarea aria-label="New value" placeholder="Value" value={row.value} rows={1} onChange={(event) => setAddedRow(row.id, { value: event.target.value })} className="min-h-8 w-full py-1 font-mono text-[13px] sm:w-64" />
          </div>
        ))}
        <div>
          <Button type="button" variant="outline" size="sm" onClick={() => setAdded((was) => [...was, { id: was.length, key: '', value: '' }])}>
            Add another
          </Button>
        </div>
      </div>
      {emptyItems.map((field) => (
        <p key={field} role="alert" className="text-[13px] text-warn">
          {field}: a list item is empty. Fill it in or delete the line.
        </p>
      ))}
      {failure && <Failure failure={failure} />}
      <div className="flex items-center gap-2">
        <Button type="submit" size="sm" disabled={busy || nothing || name.trim() === '' || emptyItems.length > 0}>
          Save edit
        </Button>
        <Button type="button" variant="outline" size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

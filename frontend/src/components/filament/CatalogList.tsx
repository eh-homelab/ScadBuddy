import { ArchiveIcon, ArchiveRestoreIcon, ArrowDownIcon, ArrowUpIcon, PencilIcon, PlusIcon } from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listCatalog, upsertCatalog, type CatalogInput, type CatalogKind } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { TD, TH } from './formClasses'
import { RgbaColourField } from './RgbaColourField'
import { parseRgba } from '../../lib/rgba'
import { RgbaSwatch } from './RgbaSwatch'
import { CATALOGS, type Field } from './catalogs'

type Row = { id: string; name: string; sort_order: number; archived_at: string | null } & Record<string, unknown>
type Value = string | boolean | null
type Draft = { id: string | null; name: string; values: Record<string, Value> }

const NONE = '__none'
const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The change could not be saved. Try again.')

/**
 * The route takes any object and checks it against the kind's model, so the body is built
 * from the field list; this is the one place it is typed as the kind's input.
 */
const write = (kind: CatalogKind, body: Record<string, unknown>, id?: string) =>
  upsertCatalog<CatalogKind>(kind, body as CatalogInput<CatalogKind>, id)

const draftOf = (fields: Field[], row?: Row): Draft => ({
  id: row?.id ?? null,
  name: row?.name ?? '',
  values: Object.fromEntries(
    fields.map((field) => {
      const have = row?.[field.name]
      return [field.name, field.kind === 'bool' ? have === true : Array.isArray(have) ? have.join(', ') : have == null ? null : typeof have === 'string' ? have : String(have)]
    }),
  ),
})

/** A field's value as the body sends it: `null` for an empty box, a number for a whole number. */
function sent(field: Field, value: Value): unknown {
  if (field.kind === 'bool' || field.kind === 'rgba' || field.kind === 'ref') return value === '' ? null : value
  const textual = typeof value === 'string' ? value.trim() : ''
  if (textual === '') return null
  if (field.kind === 'rgbalist') {
    const colours = textual.split(/[\s,]+/).filter(Boolean).map((one) => {
      const parsed = parseRgba(one)
      if (parsed === null) throw new Error(`${field.label}: "${one}" is not a colour. Use #RRGGBB or #RRGGBBAA, separated by commas.`)
      return parsed
    })
    if (colours.length > 16) throw new Error(`${field.label}: at most 16 colours.`)
    // Commas alone name no colour: the field is cleared.
    return colours.length === 0 ? null : colours
  }
  return field.kind === 'int' ? Number(textual) : textual
}

/** What a row shows besides its name: its set values, in words. */
function details(fields: Field[], row: Row, names: ReadonlyMap<string, string>): string {
  const parts: string[] = []
  for (const field of fields) {
    const have = row[field.name]
    if (have == null || have === '' || have === false) continue
    if (field.kind === 'bool') parts.push(field.label)
    else if (field.kind === 'ref') parts.push(`${field.label}: ${names.get(String(have)) ?? ''}`)
    else if (field.kind === 'rgba') parts.push(String(have))
    else if (Array.isArray(have)) parts.push(`${field.label}: ${have.join(' ')}`)
    else parts.push(`${field.label}: ${String(have)}`)
  }
  return parts.join(', ')
}

/**
 * #6262 — one catalog as a dense table: add, rename, re-order (`sort_order`), archive and
 * restore a row, and edit its value columns. Archived rows show only with "Show archived"
 * (`?archived=true`). Colours use `RgbaColourField`; an insert type's Bambuddy catalog id
 * is read only (the pull sets it).
 */
export function CatalogList({ kind }: { kind: CatalogKind }) {
  const spec = CATALOGS.find((one) => one.kind === kind)!
  const [params, setParams] = useSearchParams()
  const archived = params.get('archived') === 'true'
  const [rows, setRows] = useState<Row[] | null>(null)
  const [names, setNames] = useState<Map<string, string>>(new Map())
  const [options, setOptions] = useState<Partial<Record<CatalogKind, Row[]>>>({})
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState('')
  const [addError, setAddError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const refs = [...new Set(spec.fields.flatMap((field) => (field.ref ? [field.ref] : [])))]
  const needsMore = spec.fields.some((field) => field.required)

  const load = useCallback(async (): Promise<boolean> => {
    try {
      const [mine, ...others] = await Promise.all([listCatalog(kind, { includeArchived: archived }), ...refs.map((to) => listCatalog(to, { includeArchived: true }))])
      setRows(mine as Row[])
      setOptions(Object.fromEntries(refs.map((to, at) => [to, others[at] as Row[]])))
      setNames(new Map(others.flat().map((row) => [row.id, row.archived_at ? `${row.name} (archived)` : row.name])))
      setError(null)
      return true
    } catch (cause) {
      setError(message(cause))
      return false
    }
    // `refs` is the kind's own constant list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, archived])

  useEffect(() => {
    void load()
  }, [load])

  async function run(job: () => Promise<unknown>) {
    setError(null)
    try {
      await job()
      await load()
    } catch (cause) {
      setError(message(cause))
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault()
    setAddError(null)
    try {
      await write(kind, { name: adding.trim() })
      setAdding('')
      await load()
    } catch (cause) {
      setAddError(message(cause))
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!draft) return
    const was = draft.id ? rows?.find((row) => row.id === draft.id) : undefined
    setSaving(true)
    setFormError(null)
    try {
      const body: Record<string, unknown> = {}
      if (!was || draft.name.trim() !== was.name) body.name = draft.name.trim()
      for (const field of spec.fields) {
        const value = sent(field, draft.values[field.name] ?? null)
        const before = was ? sent(field, draftOf(spec.fields, was).values[field.name] ?? null) : null
        if (was ? JSON.stringify(value) !== JSON.stringify(before) : value !== null && value !== false) body[field.name] = value
      }
      if (was) {
        if (Object.keys(body).length > 0) await write(kind, body, was.id)
      } else {
        await write(kind, body)
      }
      setDraft(null)
      await load()
    } catch (cause) {
      setFormError(cause instanceof Error && !(cause instanceof ApiError) ? cause.message : message(cause))
    } finally {
      setSaving(false)
    }
  }

  const table = useRef<HTMLTableElement>(null)
  const refocus = useRef<{ name: string; by: -1 | 1 } | null>(null)

  // After a re-order the moved row's button keeps the focus: the one it was pressed on, or
  // the other when the row has reached an end and that one is disabled.
  useEffect(() => {
    const goal = refocus.current
    if (!goal || !rows) return
    refocus.current = null
    const row = [...(table.current?.querySelectorAll<HTMLElement>('tr[data-name]') ?? [])].find((one) => one.dataset.name === goal.name)
    const buttons = [...(row?.querySelectorAll<HTMLButtonElement>('button[data-move]') ?? [])]
    const same = buttons.find((one) => one.dataset.move === String(goal.by))
    const target = same && !same.disabled ? same : buttons.find((one) => !one.disabled)
    target?.focus()
  }, [rows])

  async function move(at: number, by: -1 | 1) {
    const order = [...(rows ?? [])]
    const moved = order[at]!
    order.splice(at, 1)
    order.splice(at + by, 0, moved)
    setError(null)
    let failure: string | null = null
    refocus.current = { name: moved.name, by }
    for (const [index, row] of order.entries()) {
      if (row.sort_order === index) continue
      try {
        await write(kind, { sort_order: index }, row.id)
      } catch (cause) {
        failure = `Could not move ${row.name}: ${message(cause)}`
        break
      }
    }
    // Whatever was written stays: show the order the server holds now.
    // A failed reload shows no new order, so no row is waiting for the focus.
    if (!(await load())) refocus.current = null
    if (failure) setError(failure)
  }

  const form = draft && (
    <form aria-label={draft.id ? `Edit ${draft.name}` : `New ${spec.title.toLowerCase()} row`} onSubmit={(event) => void save(event)} className="grid gap-3 rounded-md border bg-muted/20 p-3 text-[13px] sm:grid-cols-2">
      <label className="flex flex-col gap-1">
        Name
        <Input value={draft.name} required onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
      </label>
      {spec.fields.map((field) => {
        const value = draft.values[field.name] ?? null
        const set = (next: Value) => setDraft({ ...draft, values: { ...draft.values, [field.name]: next } })
        if (field.kind === 'bool')
          return (
            <label key={field.name} className="flex items-center gap-2 self-end pb-1.5">
              <Checkbox checked={value === true} aria-label={field.label} onCheckedChange={(on) => set(on === true)} />
              {field.label}
            </label>
          )
        if (field.kind === 'rgba')
          return (
            <div key={field.name} className="flex flex-col gap-1 sm:col-span-2">
              {field.label}
              <RgbaColourField value={typeof value === 'string' ? value : null} onChange={set} />
            </div>
          )
        if (field.kind === 'ref')
          return (
            <label key={field.name} className="flex flex-col gap-1">
              {field.label}
              <Select value={typeof value === 'string' && value ? value : NONE} onValueChange={(next) => set(next === NONE ? null : next)}>
                <SelectTrigger aria-label={field.label} className="w-full text-[13px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None</SelectItem>
                  {(options[field.ref!] ?? []).filter((row) => !row.archived_at || row.id === value).map((row) => (
                    <SelectItem key={row.id} value={row.id}>
                      {row.archived_at ? `${row.name} (archived)` : row.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </label>
          )
        return (
          <label key={field.name} className="flex flex-col gap-1">
            {field.label}
            <Input
              type={field.kind === 'text' || field.kind === 'rgbalist' ? 'text' : 'number'}
              step={field.kind === 'decimal' ? 'any' : undefined}
              placeholder={field.kind === 'rgbalist' ? '#RRGGBB, #RRGGBBAA' : undefined}
              required={field.required}
              value={typeof value === 'string' ? value : ''}
              onChange={(event) => set(event.target.value)}
            />
          </label>
        )
      })}
      {kind === 'insert-types' && draft.id && (
        <div className="flex flex-col gap-1">
          <span>Bambuddy catalog id</span>
          <span className="tabular-nums text-muted-foreground">
            {String(rows?.find((row) => row.id === draft.id)?.bb_catalog_id ?? 'Not linked')}
          </span>
        </div>
      )}
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
  )

  return (
    <section aria-label={`${spec.title} catalog`} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
          <Checkbox
            checked={archived}
            aria-label="Show archived"
            onCheckedChange={(on) => {
              const next = new URLSearchParams(params)
              if (on === true) next.set('archived', 'true')
              else next.delete('archived')
              setParams(next, { replace: true })
            }}
          />
          Show archived
        </label>
        {needsMore ? (
          <Button type="button" size="sm" onClick={() => { setFormError(null); setDraft(draftOf(spec.fields)) }}>
            <PlusIcon /> Add
          </Button>
        ) : (
          <form onSubmit={(event) => void add(event)} className="flex items-center gap-2">
            <Input aria-label="New name" placeholder="New name" value={adding} className="w-48 text-[13px]" onChange={(event) => setAdding(event.target.value)} />
            <Button type="submit" size="sm" disabled={adding.trim() === ''}>
              <PlusIcon /> Add
            </Button>
          </form>
        )}
      </div>
      {addError && (
        <p role="alert" className="text-[13px] text-warn">
          {addError}
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!draft?.id && form}
      {!rows && !error && <p className="text-[13px] text-muted-foreground">Loading {spec.title.toLowerCase()}…</p>}
      {rows && (
        <div className="overflow-x-auto rounded-md border">
          <table ref={table} aria-label={spec.title} className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className={TH}>Name</th>
                <th scope="col" className={TH}>Values</th>
                <th scope="col" className={`${TH} text-right`}>Order</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.flatMap((row, at) => {
                const gone = row.archived_at != null
                return [
                  <tr key={row.id} data-name={row.name} className="border-t">
                    <td className={TD}>
                      <span className="inline-flex items-center gap-2">
                        {typeof row.rgba === 'string' && <RgbaSwatch rgba={row.rgba} size="md" />}
                        <span className={gone ? 'text-muted-foreground line-through' : 'font-medium'}>{row.name}</span>
                        {gone && <span className="text-[12px] text-muted-foreground">Archived</span>}
                      </span>
                    </td>
                    <td className={`${TD} text-muted-foreground`}>{details(spec.fields, row, names)}</td>
                    <td className={`${TD} text-right tabular-nums`}>{row.sort_order}</td>
                    <td className={`${TD} whitespace-nowrap text-right`}>
                      <Button type="button" size="xs" variant="ghost" data-move="-1" aria-label={`Move ${row.name} up`} disabled={at === 0} onClick={() => void move(at, -1)}>
                        <ArrowUpIcon />
                      </Button>
                      <Button type="button" size="xs" variant="ghost" data-move="1" aria-label={`Move ${row.name} down`} disabled={at === rows.length - 1} onClick={() => void move(at, 1)}>
                        <ArrowDownIcon />
                      </Button>
                      <Button type="button" size="xs" variant="ghost" aria-label={`Edit ${row.name}`} onClick={() => { setFormError(null); setDraft(draftOf(spec.fields, row)) }}>
                        <PencilIcon /> Edit
                      </Button>
                      <Button
                        type="button"
                        size="xs"
                        variant="ghost"
                        aria-label={`${gone ? 'Restore' : 'Archive'} ${row.name}`}
                        onClick={() => void run(() => write(kind, { archived: !gone }, row.id))}
                      >
                        {gone ? <ArchiveRestoreIcon /> : <ArchiveIcon />} {gone ? 'Restore' : 'Archive'}
                      </Button>
                    </td>
                  </tr>,
                  ...(draft?.id === row.id
                    ? [
                        <tr key={`${row.id}-edit`} className="border-t">
                          <td colSpan={4} className="p-2">
                            {form}
                          </td>
                        </tr>,
                      ]
                    : []),
                ]
              })}
              {rows.length === 0 && (
                <tr className="border-t">
                  <td colSpan={4} className="px-2 py-3 text-muted-foreground">
                    Nothing here yet. Add the first {spec.title.toLowerCase().replace(/s$/, '')}.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

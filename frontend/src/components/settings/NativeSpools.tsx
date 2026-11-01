import { useState } from 'react'
import { api } from '../../api/client'
import type { NativeSpool, NativeSpoolCreate } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { DataTable, type Column } from '../records/DataTable'
import { refusalText } from '../orders/refusal'

type Draft = {
  material: string
  colour: string
  brand: string
  name: string
  label_weight_g: string
  remaining_g: string
  tray_uuid: string
  tag_uid: string
  preset: string
}

const EMPTY: Draft = { material: '', colour: '', brand: '', name: '', label_weight_g: '', remaining_g: '', tray_uuid: '', tag_uid: '', preset: '' }
const GRAMS = /^\d+(\.\d+)?$/
const WHOLE = /^\d+$/
const TAG = /^[0-9A-Fa-f:\- ]*$/

function draftOf(spool: NativeSpool): Draft {
  const text = (value: string | number | null | undefined) => (value === null || value === undefined ? '' : String(value))
  return {
    material: text(spool.material),
    colour: text(spool.colour),
    brand: text(spool.brand),
    name: text(spool.name),
    label_weight_g: text(spool.label_weight_g),
    remaining_g: text(spool.remaining_g),
    tray_uuid: text(spool.tray_uuid),
    tag_uid: text(spool.tag_uid),
    preset: text(spool.preset),
  }
}

/** The body a draft makes: every field sent, an empty one as null (which clears it on an edit). */
function bodyOf(draft: Draft): NativeSpoolCreate | string {
  const t = (text: string) => text.trim()
  if (t(draft.colour) && !/^#[0-9a-fA-F]{6}$/.test(t(draft.colour))) return 'Enter the colour as #RRGGBB, or leave it empty.'
  if (t(draft.label_weight_g) && !(WHOLE.test(t(draft.label_weight_g)) && Number(t(draft.label_weight_g)) > 0))
    return 'Enter the label weight in whole grams, such as 1000, or leave it empty.'
  if (t(draft.remaining_g) && !GRAMS.test(t(draft.remaining_g))) return 'Enter the grams left, such as 812.5, or leave it empty.'
  if (!TAG.test(draft.tray_uuid) || !TAG.test(draft.tag_uid)) return 'A tag is hex digits, as the printer reports it.'
  const optional = (text: string) => (t(text) ? t(text) : null)
  const number = (text: string) => (t(text) ? Number(t(text)) : null)
  return {
    material: optional(draft.material),
    colour: optional(draft.colour),
    brand: optional(draft.brand),
    name: optional(draft.name),
    label_weight_g: number(draft.label_weight_g),
    remaining_g: number(draft.remaining_g),
    tray_uuid: optional(draft.tray_uuid),
    tag_uid: optional(draft.tag_uid),
    preset: optional(draft.preset),
  }
}

/** `Bambu PLA Red`, or "Unnamed spool" for a record with none of them. */
function nativeSpoolLabel(spool: Pick<NativeSpool, 'brand' | 'material' | 'name'>): string {
  return [spool.brand, spool.material, spool.name].filter(Boolean).join(' ') || 'Unnamed spool'
}

function grams(spool: NativeSpool): string {
  if (spool.remaining_g === null || spool.remaining_g === undefined) return '—'
  const left = `${Math.round(spool.remaining_g * 10) / 10} g`
  return spool.label_weight_g ? `${left} of ${spool.label_weight_g} g` : left
}

/**
 * #4643 — ScadBuddy's own spools, the native spool provider's records: what the print
 * dialog shows for a tray beside (and ahead of) Bambuddy's inventory, and what a native
 * print's filament is taken off when it finishes. A spool used up is archived, which keeps
 * its record; only an archived one can be deleted.
 */
export function NativeSpools() {
  const [showArchived, setShowArchived] = useState(false)
  const list = useAsync(() => api.nativeSpools(showArchived), [showArchived])
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const spools = list.data ?? []

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setDraft(EMPTY)
      setEditing(null)
      list.refresh()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  function save() {
    const body = bodyOf(draft)
    if (typeof body === 'string') {
      setError(body)
      return
    }
    void act(() => (editing ? api.updateNativeSpool(editing, body) : api.createNativeSpool(body)))
  }

  const field = (name: keyof Draft, label: string, extra: { width?: string; placeholder?: string; inputMode?: 'decimal' | 'numeric' } = {}) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`native-spool-${name}`}>{label}</Label>
      <Input
        id={`native-spool-${name}`}
        className={extra.width ?? 'w-28'}
        placeholder={extra.placeholder}
        inputMode={extra.inputMode}
        value={draft[name]}
        onChange={(event) => setDraft((was) => ({ ...was, [name]: event.target.value }))}
      />
    </div>
  )

  const columns: Column<NativeSpool>[] = [
    {
      key: 'spool',
      header: 'Spool',
      className: 'break-words',
      cell: (s) => (
        <span className="inline-flex items-center gap-2">
          {s.colour ? (
            <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm border" style={{ background: s.colour }} />
          ) : (
            <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm border border-dashed" />
          )}
          {nativeSpoolLabel(s)}
          {s.archived_at && <span className="text-xs text-muted-foreground">Archived</span>}
        </span>
      ),
    },
    { key: 'left', header: 'Left', className: 'sb-num', cell: grams },
    { key: 'tag', header: 'Tag', className: 'font-mono text-xs', cell: (s) => (s.tray_uuid || s.tag_uid ? 'RFID' : 'Untagged') },
    { key: 'preset', header: 'Preset', className: 'font-mono text-xs', cell: (s) => s.preset ?? '—' },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      className: 'whitespace-nowrap text-right',
      cell: (s) =>
        s.archived_at ? (
          <span className="inline-flex gap-1">
            <Button size="xs" variant="outline" disabled={busy} onClick={() => void act(() => api.updateNativeSpool(s.id, { archived: false }))} aria-label={`Unarchive ${nativeSpoolLabel(s)}`}>
              Unarchive
            </Button>
            <Button size="xs" variant="ghost" disabled={busy} onClick={() => void act(() => api.deleteNativeSpool(s.id))} aria-label={`Delete ${nativeSpoolLabel(s)}`}>
              Delete
            </Button>
          </span>
        ) : (
          <span className="inline-flex gap-1">
            <Button
              size="xs"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setEditing(s.id)
                setDraft(draftOf(s))
                setError(null)
              }}
            >
              Edit
            </Button>
            <Button size="xs" variant="ghost" disabled={busy} onClick={() => void act(() => api.updateNativeSpool(s.id, { archived: true }))} aria-label={`Archive ${nativeSpoolLabel(s)}`}>
              Archive
            </Button>
          </span>
        ),
    },
  ]

  return (
    <section className="flex flex-col gap-2" aria-label="ScadBuddy's spools">
      <p className="text-xs text-muted-foreground">
        Spools ScadBuddy keeps itself, with no Bambuddy needed. The print dialog shows each tray's spool from here first, found by its RFID tag or by linking the tray, and a print sent over the LAN takes what it used off the spool when it finishes.
      </p>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <Checkbox checked={showArchived} onCheckedChange={(checked) => setShowArchived(checked === true)} aria-label="Show archived" />
        Show archived
      </label>
      {list.error && (
        <p role="alert" className="text-[13px] text-warn">
          ScadBuddy's spools could not be read: {list.error.message}
        </p>
      )}
      <DataTable
        label="ScadBuddy's spools"
        columns={columns}
        rows={spools}
        rowKey={(s) => s.id}
        rowTestId={(s) => `native-spool-${s.id}`}
        empty="No spools yet. Add the ones on your shelf."
      />
      <form
        className="flex flex-wrap items-end gap-2"
        aria-label={editing ? 'Edit spool' : 'Add a spool'}
        onSubmit={(event) => {
          event.preventDefault()
          save()
        }}
      >
        {field('material', 'Material', { placeholder: 'PLA' })}
        {field('colour', 'Colour', { width: 'w-24', placeholder: '#FF0000' })}
        {field('brand', 'Brand', { placeholder: 'Bambu' })}
        {field('name', 'Name', { width: 'w-36', placeholder: 'Jade White' })}
        {field('label_weight_g', 'Label weight g', { width: 'w-24', placeholder: '1000', inputMode: 'numeric' })}
        {field('remaining_g', 'Grams left', { width: 'w-24', placeholder: '812.5', inputMode: 'decimal' })}
        {field('preset', 'Preset', { width: 'w-24', placeholder: 'GFA00' })}
        {field('tray_uuid', 'Tray UUID', { width: 'w-72' })}
        {field('tag_uid', 'Tag UID', { width: 'w-44' })}
        <Button type="submit" size="sm" disabled={busy}>
          {editing ? 'Save spool' : 'Add spool'}
        </Button>
        {editing && (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setEditing(null)
              setDraft(EMPTY)
            }}
          >
            Cancel
          </Button>
        )}
      </form>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}

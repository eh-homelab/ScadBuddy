import { useState } from 'react'
import { api } from '../../api/client'
import type { Filament, FilamentInput } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { DataTable, type Column } from '../records/DataTable'
import { refusalText } from './refusal'

type Draft = {
  material: string
  subtype: string
  brand: string
  colour_name: string
  colour: string
  cost_per_kg: string
  density_g_cm3: string
}

const EMPTY: Draft = { material: '', subtype: '', brand: '', colour_name: '', colour: '#FFFFFF', cost_per_kg: '', density_g_cm3: '' }
const DECIMAL = /^\d+(\.\d+)?$/

function draftOf(filament: Filament): Draft {
  return {
    material: filament.material,
    subtype: filament.subtype ?? '',
    brand: filament.brand ?? '',
    colour_name: filament.colour_name ?? '',
    colour: filament.colour,
    cost_per_kg: filament.cost_per_kg,
    density_g_cm3: filament.density_g_cm3 ?? '',
  }
}

/** The body a draft makes: an empty optional field is null, the decimals as typed. */
function bodyOf(draft: Draft): FilamentInput | string {
  if (!draft.material.trim()) return 'Enter the material, such as PLA.'
  if (!/^#[0-9a-fA-F]{6}$/.test(draft.colour.trim())) return 'Enter the colour as #RRGGBB.'
  if (!DECIMAL.test(draft.cost_per_kg.trim())) return 'Enter the cost per kilogram, such as 25.50.'
  if (draft.density_g_cm3.trim() && !DECIMAL.test(draft.density_g_cm3.trim())) return 'Enter the density in g/cm³, such as 1.24, or leave it empty.'
  const optional = (text: string) => (text.trim() ? text.trim() : null)
  return {
    material: draft.material.trim(),
    subtype: optional(draft.subtype),
    brand: optional(draft.brand),
    colour_name: optional(draft.colour_name),
    colour: draft.colour.trim(),
    cost_per_kg: draft.cost_per_kg.trim(),
    density_g_cm3: optional(draft.density_g_cm3),
  }
}

/** `PLA Basic`, with the colour's name when it has one. */
export function filamentLabel(filament: Pick<Filament, 'material' | 'subtype' | 'colour_name' | 'brand'>): string {
  return [filament.brand, filament.material, filament.subtype, filament.colour_name].filter(Boolean).join(' ')
}

/**
 * #4250 — the price book's filaments (spec 2026-10-18 §5.1, plan Ruling 4), under the
 * Settings page's Orders section: what a kilogram of each costs, and its density for the
 * rough estimate. A record is never deleted, only archived: a quote priced with it keeps
 * its lines, and an item may still name it.
 */
export function FilamentsEditor() {
  const book = useAsync(() => api.priceBook(), [])
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const filaments = book.data?.filaments ?? []

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setDraft(EMPTY)
      setEditing(null)
      book.refresh()
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
    void act(() => (editing ? api.updateFilament(editing, body) : api.createFilament(body)))
  }

  const field = (name: keyof Draft, label: string, extra: { width?: string; placeholder?: string; inputMode?: 'decimal' } = {}) => (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`filament-${name}`}>{label}</Label>
      <Input
        id={`filament-${name}`}
        className={extra.width ?? 'w-28'}
        placeholder={extra.placeholder}
        inputMode={extra.inputMode}
        value={draft[name]}
        onChange={(event) => setDraft((was) => ({ ...was, [name]: event.target.value }))}
      />
    </div>
  )

  const columns: Column<Filament>[] = [
    {
      key: 'filament',
      header: 'Filament',
      className: 'break-words',
      cell: (f) => (
        <span className="inline-flex items-center gap-2">
          <span aria-hidden="true" className="inline-block size-3 shrink-0 rounded-sm border" style={{ background: f.colour }} />
          {filamentLabel(f)}
        </span>
      ),
    },
    { key: 'colour', header: 'Colour', className: 'font-mono text-xs', cell: (f) => f.colour },
    { key: 'cost', header: 'Cost per kg', className: 'sb-num', cell: (f) => f.cost_per_kg },
    { key: 'density', header: 'Density', className: 'sb-num', cell: (f) => (f.density_g_cm3 ? `${f.density_g_cm3} g/cm³` : '—') },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      className: 'whitespace-nowrap text-right',
      cell: (f) => (
        <span className="inline-flex gap-1">
          <Button
            size="xs"
            variant="outline"
            disabled={busy}
            onClick={() => {
              setEditing(f.id)
              setDraft(draftOf(f))
              setError(null)
            }}
          >
            Edit
          </Button>
          <Button size="xs" variant="ghost" disabled={busy} onClick={() => void act(() => api.archiveFilament(f.id))} aria-label={`Archive ${filamentLabel(f)}`}>
            Archive
          </Button>
        </span>
      ),
    },
  ]

  return (
    <section className="flex flex-col gap-2" aria-label="Filaments">
      <h3 className="text-sm font-medium">Filaments</h3>
      <p className="text-xs text-muted-foreground">
        What a kilogram of each filament costs, in {book.data?.currency ?? 'the orders currency'}. A slice quote prices each colour at the filament it names; archiving one hides it from new quotes.
      </p>
      {book.error && (
        <p role="alert" className="text-[13px] text-warn">
          The price book could not be read: {book.error.message}
        </p>
      )}
      <DataTable
        label="Filaments"
        columns={columns}
        rows={filaments}
        rowKey={(f) => f.id}
        rowTestId={(f) => `filament-${f.id}`}
        empty="No filaments yet. Add the ones you price prints with."
      />
      <form
        className="flex flex-wrap items-end gap-2"
        aria-label={editing ? 'Edit filament' : 'Add a filament'}
        onSubmit={(event) => {
          event.preventDefault()
          save()
        }}
      >
        {field('material', 'Material', { placeholder: 'PLA' })}
        {field('subtype', 'Subtype', { placeholder: 'Basic' })}
        {field('brand', 'Brand', { placeholder: 'Bambu' })}
        {field('colour_name', 'Colour name', { width: 'w-36', placeholder: 'Jade White' })}
        {field('colour', 'Colour', { width: 'w-24', placeholder: '#FFFFFF' })}
        {field('cost_per_kg', 'Cost per kg', { width: 'w-24', placeholder: '25.50', inputMode: 'decimal' })}
        {field('density_g_cm3', 'Density g/cm³', { width: 'w-24', placeholder: '1.24', inputMode: 'decimal' })}
        <Button type="submit" size="sm" disabled={busy}>
          {editing ? 'Save filament' : 'Add filament'}
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

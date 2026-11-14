import { useState } from 'react'
import { api } from '../../api/client'
import type { DensityFillRow, Filament, FilamentInput, SpoolImportRow } from '../../api/types'
import { useAsync } from '../../lib/useAsync'
import { displayHex, swatchStyle } from '../../lib/filamentSwatch'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
    colour: displayHex(filament.colour, filament.alpha),
    cost_per_kg: filament.cost_per_kg,
    density_g_cm3: filament.density_g_cm3 ?? '',
  }
}

/** The body a draft makes: an empty optional field is null, the decimals as typed. */
function bodyOf(draft: Draft): FilamentInput | string {
  if (!draft.material.trim()) return 'Enter the material, such as PLA.'
  const colour = draft.colour.trim()
  if (!/^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(colour)) return 'Enter the colour as #RRGGBB, or #RRGGBBAA for a translucent filament.'
  if (!DECIMAL.test(draft.cost_per_kg.trim())) return 'Enter the cost per kilogram, such as 25.50.'
  if (draft.density_g_cm3.trim() && !DECIMAL.test(draft.density_g_cm3.trim())) return 'Enter the density in g/cm³, such as 1.24, or leave it empty.'
  const optional = (text: string) => (text.trim() ? text.trim() : null)
  return {
    material: draft.material.trim(),
    subtype: optional(draft.subtype),
    brand: optional(draft.brand),
    colour_name: optional(draft.colour_name),
    // #6209: `colour` stays #RRGGBB for the render; the alpha byte goes beside it.
    colour: colour.slice(0, 7),
    alpha: colour.length === 9 ? parseInt(colour.slice(7), 16) : 255,
    cost_per_kg: draft.cost_per_kg.trim(),
    density_g_cm3: optional(draft.density_g_cm3),
  }
}

/** `PLA Basic`, with the colour's name when it has one. */
export function filamentLabel(filament: { material: string; subtype?: string | null; colour_name?: string | null; brand?: string | null }): string {
  return [filament.brand, filament.material, filament.subtype, filament.colour_name].filter(Boolean).join(' ')
}

const ACTIONS: Record<SpoolImportRow['action'], string> = { created: 'added', updated: 'updated', unchanged: 'unchanged', skipped: 'skipped' }
const REASONS: Record<NonNullable<SpoolImportRow['reason']>, string> = {
  no_colour: 'its spools have only a colour name, no colour',
  no_price: 'none of its spools has a price',
  too_long: 'a name is longer than a filament record takes (100 characters)',
  colour_taken: 'another filament of it has its colour, set by hand',
}

/** One line of an import's answer: what happened to a filament, and the spread of its spools' prices. */
function importLine(row: SpoolImportRow): string {
  const name = filamentLabel(row) || row.material
  if (row.action === 'skipped') return `${name}: skipped, ${row.reason ? REASONS[row.reason] : 'nothing to import'}`
  const price = row.action === 'updated' ? `${row.previous_cost_per_kg} → ${row.cost_per_kg}` : row.cost_per_kg
  const spread = row.min_cost_per_kg && row.max_cost_per_kg ? ` (spools priced ${row.min_cost_per_kg} to ${row.max_cost_per_kg}; the newest spool's price)` : ''
  const density = row.density_profile ? `; density ${row.density_g_cm3} g/cm³ from ${row.density_profile}` : ''
  return `${name}: ${ACTIONS[row.action]} at ${price} per kg${spread}${density}`
}

/**
 * Where a record's density came from (#6136): the preset Bambuddy linked it to, or a
 * person. An empty one says that an estimate of it is refused, never a bare dash.
 */
function densityText(f: Pick<Filament, 'density_g_cm3' | 'density_profile'>): { text: string; missing: boolean } {
  if (f.density_g_cm3) return { text: f.density_profile ? `${f.density_g_cm3} g/cm³, from ${f.density_profile}` : `${f.density_g_cm3} g/cm³`, missing: false }
  return { text: 'Not set: no estimate', missing: true }
}

function fillLine(row: DensityFillRow): string {
  const name = filamentLabel(row) || row.material
  return row.density_g_cm3 ? `${name}: filled, ${row.density_g_cm3} g/cm³ from ${row.density_profile}` : `${name}: no source (Bambuddy links no preset or density to it); enter its density`
}

/**
 * #4250 — the price book's filaments (spec 2026-10-18 §5.1, plan Ruling 4), under the
 * Settings page's Orders section: what a kilogram of each costs, and its density for the
 * rough estimate. A record is never deleted, only archived: a quote priced with it keeps
 * its lines, and an item may still name it. "Show archived" lists the archived ones, each
 * with an Unarchive that brings the same record back (#4977). With Bambuddy linked
 * (`spool_import`), "Import from spools" makes or updates one record per filament of its
 * spool inventory, on click only: a quote never reads Bambuddy (#4253, spec §8.3).
 * Each density says where it came from; "Fill densities" writes into every empty one
 * the density Bambuddy's link to the slicer preset gives (#6136).
 */
export function FilamentsEditor() {
  const [showArchived, setShowArchived] = useState(false)
  const book = useAsync(() => api.priceBook(showArchived), [showArchived])
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imported, setImported] = useState<SpoolImportRow[] | null>(null)
  const [filled, setFilled] = useState<DensityFillRow[] | null>(null)
  const filaments = book.data?.filaments ?? []
  const emptyDensities = filaments.some((f) => !f.archived_at && !f.density_g_cm3)

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

  // Not `act`: a draft being typed stays.
  async function importSpools() {
    setBusy(true)
    setError(null)
    try {
      setImported((await api.importSpools()).rows)
      book.refresh()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  async function fillDensities() {
    setBusy(true)
    setError(null)
    try {
      setFilled((await api.fillDensities()).rows)
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
          <span aria-hidden="true" data-testid="filament-swatch" className="inline-block size-3 shrink-0 rounded-sm border" style={swatchStyle(f)} />
          {filamentLabel(f)}
          {f.archived_at && <span className="text-xs text-muted-foreground">Archived</span>}
        </span>
      ),
    },
    {
      key: 'colour',
      header: 'Colour',
      className: 'text-xs',
      cell: (f) => (
        <span className="inline-flex flex-col">
          <span className="font-mono">{displayHex(f.colour, f.alpha)}</span>
          {(f.effect || f.colour_source === 'manual') && (
            <span className="text-muted-foreground">{[f.effect, f.colour_source === 'manual' && 'set by hand'].filter(Boolean).join(', ')}</span>
          )}
        </span>
      ),
    },
    { key: 'cost', header: 'Cost per kg', className: 'sb-num', cell: (f) => f.cost_per_kg },
    {
      key: 'density',
      header: 'Density',
      cell: (f) => {
        const { text, missing } = densityText(f)
        return <span className={missing ? 'text-xs text-warn' : 'sb-num'}>{text}</span>
      },
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      className: 'whitespace-nowrap text-right',
      cell: (f) =>
        f.archived_at ? (
          <Button size="xs" variant="outline" disabled={busy} onClick={() => void act(() => api.unarchiveFilament(f.id))} aria-label={`Unarchive ${filamentLabel(f)}`}>
            Unarchive
          </Button>
        ) : (
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
            {f.colour_source === 'manual' && book.data?.spool_import && (
              <Button
                size="xs"
                variant="ghost"
                disabled={busy}
                onClick={() => void act(() => api.updateFilament(f.id, { colour_source: 'import' }))}
                aria-label={`Use the spool colour for ${filamentLabel(f)}`}
                title="The next Import from spools sets its colour from the spools"
              >
                Use spool colour
              </Button>
            )}
            <Button size="xs" variant="ghost" disabled={busy} onClick={() => void act(() => api.archiveFilament(f.id))} aria-label={`Archive ${filamentLabel(f)}`}>
              Archive
            </Button>
          </span>
        ),
    },
  ]

  return (
    <section className="flex flex-col gap-2" aria-label="Filaments">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium">Filaments</h4>
        <span className="inline-flex gap-1">
          {emptyDensities && book.data?.spool_import && (
            <Button size="xs" variant="outline" disabled={busy} onClick={() => void fillDensities()}>
              Fill densities
            </Button>
          )}
          {book.data?.spool_import && (
            <Button size="xs" variant="outline" disabled={busy} onClick={() => void importSpools()}>
              Import from spools
            </Button>
          )}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">
        What a kilogram of each filament costs, in {book.data?.currency ?? 'the orders currency'}. A slice quote prices each colour at the filament it names; archiving one hides it from new quotes until it is unarchived.
      </p>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <Checkbox checked={showArchived} onCheckedChange={(checked) => setShowArchived(checked === true)} aria-label="Show archived" />
        Show archived
      </label>
      {book.error && (
        <p role="alert" className="text-[13px] text-warn">
          The price book could not be read: {book.error.message}
        </p>
      )}
      {filled && (
        <div role="status" aria-label="Density fill" className="text-xs text-muted-foreground">
          {filled.length === 0 ? (
            <p>Every filament has a density.</p>
          ) : (
            <ul className="list-disc pl-4">
              {filled.map((row) => (
                <li key={row.filament_id}>{fillLine(row)}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {imported && (
        <div role="status" aria-label="Spool import" className="text-xs text-muted-foreground">
          {imported.length === 0 ? (
            <p>Bambuddy has no spools to import.</p>
          ) : (
            <ul className="list-disc pl-4">
              {imported.map((row) => (
                <li key={row.spool_ids.join(',')}>{importLine(row)}</li>
              ))}
            </ul>
          )}
        </div>
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
        {field('colour', 'Colour', { width: 'w-28', placeholder: '#FFFFFF' })}
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

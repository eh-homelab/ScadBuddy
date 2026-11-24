import { useEffect, useState } from 'react'
import { ApiError } from '../../api/client'
import { bulkUpdateSpools, listCatalog, type CatalogKind, type SpoolPatch, type SpoolType } from '../../api/filament'
import { parseRgba } from '../../lib/rgba'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

type Kind = 'pick' | 'text' | 'whole' | 'decimal' | 'rgba' | 'colours' | 'yesno' | 'known'
type Name = Exclude<keyof SpoolPatch, 'archived'>

interface Field {
  name: Name
  label: string
  kind: Kind
  /** A pick's list: a catalog, or the spool types. */
  from?: CatalogKind | 'types'
}

/**
 * #7151 (R-7151-4) — every field a spool's PATCH edits but `archived`, which the bar's
 * Archive and Restore do. In the spool page's order.
 */
const BULK_FIELDS: readonly Field[] = [
  { name: 'type_id', label: 'Type', kind: 'pick', from: 'types' },
  { name: 'material_id', label: 'Material', kind: 'pick', from: 'materials' },
  { name: 'subtype_id', label: 'Subtype', kind: 'pick', from: 'subtypes' },
  { name: 'brand_id', label: 'Brand', kind: 'pick', from: 'brands' },
  { name: 'category_id', label: 'Category', kind: 'pick', from: 'categories' },
  { name: 'effect_id', label: 'Effect', kind: 'pick', from: 'effects' },
  { name: 'colour_name', label: 'Colour name', kind: 'text' },
  { name: 'rgba', label: 'Colour', kind: 'rgba' },
  { name: 'extra_colours', label: 'Extra colours', kind: 'colours' },
  { name: 'insert_type_id', label: 'Spool', kind: 'pick', from: 'insert-types' },
  { name: 'label_weight_g', label: 'Label weight (g)', kind: 'whole' },
  { name: 'diameter_mm', label: 'Diameter (mm)', kind: 'decimal' },
  { name: 'density_g_cm3', label: 'Density (g/cm³)', kind: 'decimal' },
  { name: 'nozzle_temp_min', label: 'Nozzle min (°C)', kind: 'whole' },
  { name: 'nozzle_temp_max', label: 'Nozzle max (°C)', kind: 'whole' },
  { name: 'bed_temp_min', label: 'Bed min (°C)', kind: 'whole' },
  { name: 'bed_temp_max', label: 'Bed max (°C)', kind: 'whole' },
  { name: 'drying_temp_c', label: 'Drying temperature (°C)', kind: 'whole' },
  { name: 'drying_hours', label: 'Drying hours', kind: 'decimal' },
  { name: 'low_stock_pct', label: 'Low stock (%)', kind: 'whole' },
  { name: 'cost_per_kg', label: 'Cost per kg', kind: 'decimal' },
  { name: 'weight_locked', label: 'Weight locked', kind: 'yesno' },
  { name: 'added_full', label: 'Added full', kind: 'known' },
  { name: 'note', label: 'Note', kind: 'text' },
]

/** A pick's "no value": the override cleared, so the spool takes its type's. */
const CLEAR = '__clear'
const INTEGER = /^\d+$/
const DECIMAL = /^\d+(\.\d+)?$/

type Options = Partial<Record<CatalogKind | 'types', { id: string; name: string }[]>>

/** The patch the ticked fields make (an empty value clears an override), or why it cannot be sent. */
function patchOf(ticked: ReadonlySet<Name>, values: Partial<Record<Name, string>>): SpoolPatch | string {
  const patch: Record<string, unknown> = {}
  for (const field of BULK_FIELDS) {
    if (!ticked.has(field.name)) continue
    const raw = (values[field.name] ?? '').trim()
    const label = field.label.toLowerCase()
    switch (field.kind) {
      case 'pick':
        patch[field.name] = raw === '' || raw === CLEAR ? null : raw
        break
      case 'text':
        patch[field.name] = raw || null
        break
      case 'whole':
        if (raw !== '' && !INTEGER.test(raw)) return `Enter the ${label} as a whole number.`
        patch[field.name] = raw === '' ? null : Number(raw)
        break
      case 'decimal':
        if (raw !== '' && !DECIMAL.test(raw)) return `Enter the ${label} as a number, such as 1.75.`
        patch[field.name] = raw || null
        break
      case 'rgba': {
        const rgba = raw ? parseRgba(raw) : null
        if (raw && !rgba) return 'Enter the colour as #RRGGBB or #RRGGBBAA.'
        patch[field.name] = rgba
        break
      }
      case 'colours': {
        const parts = raw.split(',').map((one) => one.trim()).filter(Boolean)
        const colours = parts.map((one) => parseRgba(one))
        const bad = parts.find((_, at) => !colours[at])
        if (bad) return `"${bad}" is not a colour. Enter extra colours as #RRGGBB or #RRGGBBAA, separated by commas.`
        if (colours.length > 16) return 'Enter at most 16 extra colours.'
        patch[field.name] = colours.length ? colours : null
        break
      }
      case 'yesno':
        patch[field.name] = raw === 'yes'
        break
      case 'known':
        patch[field.name] = raw === '' || raw === CLEAR ? null : raw === 'yes'
        break
    }
  }
  return patch as SpoolPatch
}

interface Props {
  open: boolean
  /** The spools to change, in number order. */
  ids: string[]
  types: SpoolType[]
  onClose: () => void
  /** After the edit: what it did. */
  onDone: (report: string) => void
}

const spools = (count: number) => (count === 1 ? '1 spool' : `${count} spools`)

/**
 * #7151 — the bulk edit: tick a field to change it on every selected spool. A ticked
 * field left empty clears the spool's own value, so it takes its type's again. One
 * request, all or none: when one spool refuses, nothing changes and the reason shows here.
 */
export function BulkEditDialog({ open, ids, types, onClose, onDone }: Props) {
  const [ticked, setTicked] = useState<ReadonlySet<Name>>(() => new Set())
  const [values, setValues] = useState<Partial<Record<Name, string>>>({})
  const [options, setOptions] = useState<Options>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    setTicked(new Set())
    setValues({})
    setError(null)
    let live = true
    const kinds = ['materials', 'subtypes', 'brands', 'categories', 'effects', 'insert-types'] as const
    // A catalog that cannot be read leaves its pick empty: every other field still works.
    void Promise.allSettled(kinds.map((kind) => listCatalog(kind))).then((lists) => {
      if (!live) return
      const found: Options = {}
      lists.forEach((list, at) => {
        if (list.status === 'fulfilled') found[kinds[at]!] = list.value.map((row) => ({ id: row.id, name: row.name }))
      })
      // A subtype's name repeats across materials: show whose it is.
      const materials = new Map((found.materials ?? []).map((row) => [row.id, row.name]))
      const subtypes = lists[kinds.indexOf('subtypes')]
      if (subtypes?.status === 'fulfilled') {
        found.subtypes = subtypes.value.map((row) => {
          const material = row.material_id ? materials.get(row.material_id) : undefined
          return { id: row.id, name: material ? `${row.name} (${material})` : row.name }
        })
      }
      setOptions(found)
    })
    return () => {
      live = false
    }
  }, [open])

  const pickOptions = (field: Field) =>
    field.from === 'types' ? types.map((type) => ({ id: type.id, name: type.colour_name ? `${type.code} ${type.colour_name}` : type.code })) : (options[field.from!] ?? [])

  const tick = (name: Name, on: boolean) =>
    setTicked((current) => {
      const next = new Set(current)
      if (on) next.add(name)
      else next.delete(name)
      return next
    })

  const set = (name: Name, value: string) => setValues((current) => ({ ...current, [name]: value }))

  async function save() {
    const patch = patchOf(ticked, values)
    if (typeof patch === 'string') {
      setError(patch)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const done = await bulkUpdateSpools(ids, patch)
      const gone = done.not_found.length ? ` ${spools(done.not_found.length)} no longer ${done.not_found.length === 1 ? 'exists' : 'exist'}.` : ''
      const same = done.unchanged.length ? ` ${spools(done.unchanged.length)} already had these values.` : ''
      onDone(`${done.changed.length ? `Changed ${spools(done.changed.length)}.` : 'Changed no spool.'}${same}${gone}`)
      onClose()
    } catch (cause) {
      setError(`Nothing changed. ${cause instanceof ApiError ? cause.detail : 'The request failed. Try again.'}`)
    } finally {
      setBusy(false)
    }
  }

  const input = (field: Field) => {
    const disabled = !ticked.has(field.name)
    const value = values[field.name] ?? ''
    if (field.kind === 'pick' || field.kind === 'yesno' || field.kind === 'known') {
      const choices =
        field.kind === 'pick'
          ? [{ id: CLEAR, name: field.name === 'type_id' ? 'No type' : "The type's value" }, ...pickOptions(field)]
          : [...(field.kind === 'known' ? [{ id: CLEAR, name: 'Not known' }] : []), { id: 'yes', name: 'Yes' }, { id: 'no', name: 'No' }]
      return (
        <Select value={value || (field.kind === 'yesno' ? 'no' : CLEAR)} onValueChange={(next) => set(field.name, next)} disabled={disabled}>
          <SelectTrigger aria-label={field.label} className="h-8 w-full text-[13px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="max-h-72">
            {choices.map((choice) => (
              <SelectItem key={choice.id} value={choice.id}>
                {choice.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )
    }
    const hint = field.kind === 'rgba' ? '#RRGGBBAA' : field.kind === 'colours' ? '#RRGGBB, #RRGGBB' : "Empty: the type's value"
    return <Input aria-label={field.label} value={value} placeholder={hint} disabled={disabled} onChange={(event) => set(field.name, event.target.value)} className="h-8 text-[13px]" />
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit {spools(ids.length)}</DialogTitle>
          <DialogDescription>Tick a field to set it on every selected spool. A ticked field left empty clears the spool&apos;s own value, so it takes its type&apos;s.</DialogDescription>
        </DialogHeader>
        <div className="grid max-h-[60vh] grid-cols-1 gap-x-4 gap-y-2 overflow-y-auto pr-1 text-[13px] sm:grid-cols-2">
          {BULK_FIELDS.map((field) => (
            <div key={field.name} className={`flex flex-col gap-1 rounded-md px-1.5 py-1 ${ticked.has(field.name) ? 'bg-muted/60' : ''}`}>
              <label className="flex items-center gap-2">
                <Checkbox checked={ticked.has(field.name)} aria-label={`Change ${field.label}`} onCheckedChange={(on) => tick(field.name, on === true)} />
                <span className={ticked.has(field.name) ? 'font-medium' : 'text-muted-foreground'}>{field.label}</span>
              </label>
              {input(field)}
            </div>
          ))}
        </div>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="button" disabled={busy || ticked.size === 0} onClick={() => void save()}>
            Change {spools(ids.length)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

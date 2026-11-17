import { ArchiveIcon, ArchiveRestoreIcon, ChevronLeftIcon } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { api, ApiError } from '../../api/client'
import {
  getType,
  listCatalog,
  listSpools,
  setTypeKValues,
  setTypePresets,
  upsertType,
  type CatalogRows,
  type PresetInput,
  type Spool,
  type SpoolTypeCreate,
  type SpoolTypeDetail,
  type SpoolTypePatch,
  type TypeKValueInput,
} from '../../api/filament'
import { KValueTable } from '../../components/filament/KValueTable'
import { kValueRowsError, type KPrinter } from '../../components/filament/kValueRows'
import { PresetTable } from '../../components/filament/PresetTable'
import { presetRowsError } from '../../components/filament/presetRows'
import { RgbaColourField } from '../../components/filament/RgbaColourField'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { listBack } from '../../components/filament/spoolsQuery'
import { parseRgba } from '../../lib/rgba'
import { useLeaveGuard } from '../../lib/useLeaveGuard'

/** What a field holds while it is edited: text, `''` for none. */
interface Draft {
  code: string
  material_id: string
  subtype_id: string
  brand_id: string
  category_id: string
  effect_id: string
  insert_type_id: string
  rgba: string | null
  colour_name: string
  extra_colours: string
  diameter_mm: string
  label_weight_g: string
  density_g_cm3: string
  cost_per_kg: string
  low_stock_pct: string
  nozzle_temp_min: string
  nozzle_temp_max: string
  bed_temp_min: string
  bed_temp_max: string
  note: string
  presets: PresetInput[]
  k_values: TypeKValueInput[]
}

const BLANK: Draft = {
  code: '',
  material_id: '',
  subtype_id: '',
  brand_id: '',
  category_id: '',
  effect_id: '',
  insert_type_id: '',
  rgba: '#FFFFFFFF',
  colour_name: '',
  extra_colours: '',
  diameter_mm: '1.75',
  label_weight_g: '1000',
  density_g_cm3: '',
  cost_per_kg: '',
  low_stock_pct: '',
  nozzle_temp_min: '',
  nozzle_temp_max: '',
  bed_temp_min: '',
  bed_temp_max: '',
  note: '',
  presets: [],
  k_values: [],
}

const str = (value: string | number | null | undefined) => (value == null ? '' : String(value))

function draftOf(type: SpoolTypeDetail): Draft {
  return {
    code: type.code,
    material_id: type.material_id,
    subtype_id: str(type.subtype_id),
    brand_id: str(type.brand_id),
    category_id: str(type.category_id),
    effect_id: str(type.effect_id),
    insert_type_id: str(type.insert_type_id),
    rgba: type.rgba,
    colour_name: str(type.colour_name),
    extra_colours: (type.extra_colours ?? []).join(', '),
    diameter_mm: type.diameter_mm,
    label_weight_g: str(type.label_weight_g),
    density_g_cm3: str(type.density_g_cm3),
    cost_per_kg: str(type.cost_per_kg),
    low_stock_pct: str(type.low_stock_pct),
    nozzle_temp_min: str(type.nozzle_temp_min),
    nozzle_temp_max: str(type.nozzle_temp_max),
    bed_temp_min: str(type.bed_temp_min),
    bed_temp_max: str(type.bed_temp_max),
    note: str(type.note),
    presets: type.presets.map(({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name }) => ({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name })),
    k_values: type.k_values.map(({ type_id: _type, ...row }) => row),
  }
}

const INTEGER = /^\d+$/
const DECIMAL = /^\d+(\.\d+)?$/

/** The type's own columns a draft makes, or why it cannot be saved. */
function bodyOf(draft: Draft): SpoolTypeCreate | string {
  if (!draft.code.trim()) return 'Enter the type code, such as BL-PLA-BASIC-JADE-WHITE.'
  if (!draft.material_id) return 'Choose the material.'
  const rgba = draft.rgba ? parseRgba(draft.rgba) : null
  if (!rgba) return 'Choose the colour.'
  const whole = (label: string, value: string, required = false): number | null | string => {
    if (value.trim() === '') return required ? `Enter the ${label}.` : null
    return INTEGER.test(value.trim()) ? Number(value) : `Enter the ${label} as a whole number.`
  }
  const decimal = (label: string, value: string): string | null => {
    if (value.trim() === '') return null
    return DECIMAL.test(value.trim()) ? value.trim() : `Enter the ${label} as a number, such as 1.24.`
  }
  const extras: string[] = []
  for (const part of draft.extra_colours.split(',').map((one) => one.trim()).filter(Boolean)) {
    const one = parseRgba(part)
    if (!one) return `"${part}" is not a colour. Enter extra colours as #RRGGBB or #RRGGBBAA, separated by commas.`
    extras.push(one)
  }
  if (extras.length > 16) return 'Enter at most 16 extra colours.'
  const label = whole('label weight in grams', draft.label_weight_g, true)
  const fields = {
    label_weight_g: label,
    low_stock_pct: whole('low stock percent', draft.low_stock_pct),
    nozzle_temp_min: whole('minimum nozzle temperature', draft.nozzle_temp_min),
    nozzle_temp_max: whole('maximum nozzle temperature', draft.nozzle_temp_max),
    bed_temp_min: whole('minimum bed temperature', draft.bed_temp_min),
    bed_temp_max: whole('maximum bed temperature', draft.bed_temp_max),
  }
  for (const value of Object.values(fields)) if (typeof value === 'string') return value
  const diameter = decimal('filament diameter', draft.diameter_mm)
  const density = decimal('density', draft.density_g_cm3)
  const cost = decimal('cost per kg', draft.cost_per_kg)
  for (const value of [diameter, density, cost]) if (value !== null && !DECIMAL.test(value)) return value
  if (diameter === null) return 'Enter the filament diameter, such as 1.75.'
  return {
    code: draft.code.trim(),
    material_id: draft.material_id,
    subtype_id: draft.subtype_id || null,
    brand_id: draft.brand_id || null,
    category_id: draft.category_id || null,
    effect_id: draft.effect_id || null,
    insert_type_id: draft.insert_type_id || null,
    rgba,
    colour_name: draft.colour_name.trim() || null,
    extra_colours: extras.length > 0 ? extras : null,
    diameter_mm: diameter,
    density_g_cm3: density,
    cost_per_kg: cost,
    note: draft.note.trim() || null,
    ...(fields as { label_weight_g: number; low_stock_pct: number | null; nozzle_temp_min: number | null; nozzle_temp_max: number | null; bed_temp_min: number | null; bed_temp_max: number | null }),
  }
}

/** Only the fields whose value differs from `was`. */
function changed(next: SpoolTypeCreate, was: SpoolTypeCreate | string): SpoolTypePatch {
  if (typeof was === 'string') return next
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof SpoolTypeCreate)[]) {
    if (JSON.stringify(next[key]) !== JSON.stringify(was[key])) out[key] = next[key]
  }
  return out as SpoolTypePatch
}

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

const NONE = '__none__'

function Pick({ label, value, onChange, options, required = false }: { label: string; value: string; onChange: (id: string) => void; options: { id: string; name: string }[]; required?: boolean }) {
  return (
    <Field label={label} htmlFor={undefined}>
      <Select value={value === '' && !required ? NONE : value || undefined} onValueChange={(next) => onChange(next === NONE ? '' : next)}>
        <SelectTrigger aria-label={label} className="w-full">
          <SelectValue placeholder="Choose…" />
        </SelectTrigger>
        <SelectContent>
          {!required && <SelectItem value={NONE}>None</SelectItem>}
          {options.map((option) => (
            <SelectItem key={option.id} value={option.id}>
              {option.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </Field>
  )
}

function Field({ label, htmlFor, hint, children }: { label: string; htmlFor?: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Label htmlFor={htmlFor} className="text-[13px]">
        {label}
      </Label>
      {children}
      {hint && <p className="text-[12px] text-muted-foreground">{hint}</p>}
    </div>
  )
}

interface Lists {
  materials: CatalogRows['materials'][]
  subtypes: CatalogRows['subtypes'][]
  brands: CatalogRows['brands'][]
  categories: CatalogRows['categories'][]
  effects: CatalogRows['effects'][]
  insertTypes: CatalogRows['insert-types'][]
  printers: (KPrinter & { model: string | null })[]
  spools: Spool[]
}

/**
 * #6262 — one type, `/filament/types/<id>` or `/filament/types/new`: what a spool of it is
 * (material, colour, weights, temperatures, price), the slicer presets it prints with per
 * printer model and nozzle, its pressure-advance K values, and the spools of it. Saving
 * sends one PATCH of what changed (a POST for a new type) and, when they changed, the
 * presets' and the K values' PUTs. Unsaved edits hold every way out (`useLeaveGuard`).
 */
export function TypePage() {
  const { id = 'new' } = useParams()
  const location = useLocation()
  /** Set at once when a new type's POST lands: the page is then an edit of it, so a retry never POSTs again. */
  const [madeId, setMadeId] = useState<string | null>(null)
  const creating = id === 'new' && madeId === null
  const [notice, setNotice] = useState<string | null>(null)
  const [saved, setSaved] = useState<SpoolTypeDetail | null>(null)
  const [draft, setDraft] = useState<Draft>(BLANK)
  const [lists, setLists] = useState<Lists | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    setLoadError(null)
    setSaved(null)
    setDraft(BLANK)
    setMadeId(null)
    Promise.all([
      id === 'new' ? Promise.resolve(null) : getType(id),
      listCatalog('materials'),
      listCatalog('subtypes'),
      listCatalog('brands'),
      listCatalog('categories'),
      listCatalog('effects'),
      listCatalog('insert-types'),
      api.listPrinters(),
      listSpools({ includeArchived: true }),
    ])
      .then(([type, materials, subtypes, brands, categories, effects, insertTypes, printers, spools]) => {
        if (!live) return
        setLists({ materials, subtypes, brands, categories, effects, insertTypes, printers: printers.map((p) => ({ id: p.id, name: p.name, model: p.model ?? null })), spools })
        if (type) {
          setSaved(type)
          setDraft(draftOf(type))
        }
      })
      .catch((cause: unknown) => {
        if (live) setLoadError(cause instanceof ApiError && cause.status === 404 ? 'That type is gone. Pick another from the Types tab.' : `The type could not be loaded: ${message(cause)}`)
      })
    return () => {
      live = false
    }
  }, [id])

  const base = useMemo(() => (saved ? draftOf(saved) : BLANK), [saved])
  const dirty = JSON.stringify(draft) !== JSON.stringify(base)
  const guard = useLeaveGuard(dirty)
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setNotice(null)
    setDraft((was) => ({ ...was, [key]: value }))
  }
  /** A subtype belongs to one material: a new material drops one of another's. */
  const setMaterial = (materialId: string) => {
    setNotice(null)
    setDraft((was) => {
      const kept = lists?.subtypes.find((one) => one.id === was.subtype_id)
      return { ...was, material_id: materialId, subtype_id: kept?.material_id && kept.material_id !== materialId ? '' : was.subtype_id }
    })
  }

  async function save() {
    const body = bodyOf(draft)
    const refused = typeof body === 'string' ? body : (presetRowsError(draft.presets) ?? kValueRowsError(draft.k_values))
    if (refused) {
      setError(refused)
      return
    }
    if (typeof body === 'string') return
    setBusy(true)
    setError(null)
    setNotice(null)
    let typeId = madeId ?? id
    try {
      let presetsWere = base.presets
      let kWere = base.k_values
      if (creating) {
        const made = await upsertType(body)
        typeId = made.id
        // From here the page is an edit of this type: a later failure retries as one.
        setMadeId(made.id)
        setSaved({ ...made, presets: [], k_values: [] })
        presetsWere = []
        kWere = []
      } else {
        const patch = changed(body, bodyOf(base))
        if (Object.keys(patch).length > 0) await upsertType(patch, typeId)
      }
      if (JSON.stringify(draft.presets) !== JSON.stringify(presetsWere)) await setTypePresets(typeId, draft.presets)
      if (JSON.stringify(draft.k_values) !== JSON.stringify(kWere)) await setTypeKValues(typeId, draft.k_values)
      setNotice('Saved')
      if (id === 'new') {
        guard.leaveTo(`/filament/types/${typeId}`, { replace: true, state: location.state })
        return
      }
      const fresh = await getType(typeId)
      setSaved(fresh)
      setDraft(draftOf(fresh))
    } catch (cause) {
      setError(message(cause))
      // A write that did land is shown as it now is, unsaved edits kept.
      if (typeId !== 'new') await getType(typeId).then(setSaved, () => {})
    } finally {
      setBusy(false)
    }
  }

  async function archive(archived: boolean) {
    setBusy(true)
    setError(null)
    try {
      await upsertType({ archived }, madeId ?? id)
      const fresh = await getType(madeId ?? id)
      setSaved(fresh)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  if (loadError) {
    return (
      <Shell>
        <p role="alert" className="text-[13px] text-warn">
          {loadError}
        </p>
      </Shell>
    )
  }
  if (!lists || (!creating && !saved)) {
    return (
      <Shell>
        <p className="text-[13px] text-muted-foreground">Loading the type…</p>
      </Shell>
    )
  }

  const subtypes = lists.subtypes.filter((one) => !one.material_id || one.material_id === draft.material_id)
  const models = [...new Set(lists.printers.map((p) => p.model).filter((m): m is string => !!m))]
  const mine = lists.spools.filter((spool) => spool.type_id === (madeId ?? id))
  const text = (key: keyof Draft, label: string, extra: { hint?: string; width?: string; mode?: 'decimal' | 'numeric' } = {}) => (
    <Field label={label} htmlFor={`type-${key}`} hint={extra.hint}>
      <Input id={`type-${key}`} className={extra.width} inputMode={extra.mode} value={draft[key] as string} onChange={(e) => set(key, e.target.value as never)} />
    </Field>
  )

  return (
    <Shell>
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <RgbaSwatch rgba={draft.rgba ?? '#FFFFFFFF'} size="lg" />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight">{creating ? 'New type' : draft.code || saved?.code}</h1>
          <p className="text-[13px] text-muted-foreground">
            {creating ? 'A product your spools can be.' : saved?.archived_at ? 'Archived. Restore it to pick it for spools again.' : `${mine.length} ${mine.length === 1 ? 'spool' : 'spools'} of this type.`}
          </p>
        </div>
        {!creating && saved && (
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void archive(!saved.archived_at)}>
            {saved.archived_at ? <ArchiveRestoreIcon /> : <ArchiveIcon />}
            {saved.archived_at ? 'Restore' : 'Archive'}
          </Button>
        )}
        <Button type="button" variant="ghost" size="sm" disabled={busy || !dirty} onClick={() => { setDraft(base); setError(null) }}>
          Discard changes
        </Button>
        <Button type="button" size="sm" disabled={busy || (!dirty && !creating)} onClick={() => void save()}>
          Save type
        </Button>
      </header>
      <p role="status" className="mb-2 min-h-5 text-[13px] text-muted-foreground">
        {notice}
      </p>
      {error && (
        <p role="alert" className="mb-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[13px] text-warn">
          {error}
        </p>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-label="Identity" className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold tracking-tight">What it is</h2>
          {text('code', 'Code', { hint: 'One per type, in any case. Bambu Studio calls it the filament name.' })}
          <div className="grid grid-cols-2 gap-3">
            <Pick label="Material" required value={draft.material_id} onChange={setMaterial} options={lists.materials} />
            <Pick label="Subtype" value={draft.subtype_id} onChange={(next) => set('subtype_id', next)} options={subtypes} />
            <Pick label="Brand" value={draft.brand_id} onChange={(next) => set('brand_id', next)} options={lists.brands} />
            <Pick label="Category" value={draft.category_id} onChange={(next) => set('category_id', next)} options={lists.categories} />
            <Pick label="Effect" value={draft.effect_id} onChange={(next) => set('effect_id', next)} options={lists.effects} />
            <Pick label="Insert type" value={draft.insert_type_id} onChange={(next) => set('insert_type_id', next)} options={lists.insertTypes} />
          </div>
          <Field label="Colour">
            <RgbaColourField value={draft.rgba} onChange={(next) => set('rgba', next)} />
          </Field>
          {text('colour_name', 'Colour name')}
          {text('extra_colours', 'Extra colours', { hint: 'For multi-colour filament: #RRGGBB or #RRGGBBAA, separated by commas.' })}
        </section>

        <div className="flex flex-col gap-6">
          <section aria-label="Weights and price" className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold tracking-tight">Weights and price</h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {text('diameter_mm', 'Diameter (mm)', { mode: 'decimal' })}
              {text('label_weight_g', 'Label weight (g)', { mode: 'numeric' })}
              {text('density_g_cm3', 'Density (g/cm³)', { mode: 'decimal' })}
              {text('cost_per_kg', 'Cost per kg', { mode: 'decimal' })}
              {text('low_stock_pct', 'Low stock at (%)', { mode: 'numeric', hint: 'Of the label weight.' })}
            </div>
            <p className="text-[12px] text-muted-foreground">A priced type is a row of the price book, which prices quotes.</p>
          </section>
          <section aria-label="Temperatures" className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold tracking-tight">Temperatures</h2>
            <div className="grid grid-cols-2 gap-3">
              {text('nozzle_temp_min', 'Nozzle temperature, min (°C)', { mode: 'numeric' })}
              {text('nozzle_temp_max', 'Nozzle temperature, max (°C)', { mode: 'numeric' })}
              {text('bed_temp_min', 'Bed temperature, min (°C)', { mode: 'numeric' })}
              {text('bed_temp_max', 'Bed temperature, max (°C)', { mode: 'numeric' })}
            </div>
          </section>
          <Field label="Note" htmlFor="type-note">
            <Textarea id="type-note" rows={3} value={draft.note} onChange={(e) => set('note', e.target.value)} />
          </Field>
        </div>
      </div>

      <section aria-label="Slicer presets" className="mt-6 flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Slicer presets</h2>
        <p className="text-[12px] text-muted-foreground">The Bambu Studio filament preset to slice with, for each printer model and nozzle.</p>
        <PresetTable rows={draft.presets} onChange={(rows) => set('presets', rows)} models={models} />
      </section>

      <section aria-label="Pressure advance" className="mt-6 flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">K values</h2>
        <p className="text-[12px] text-muted-foreground">Pressure advance, by printer, extruder and nozzle.</p>
        <KValueTable rows={draft.k_values} onChange={(rows) => set('k_values', rows)} printers={lists.printers} />
      </section>

      {!creating && (
        <section aria-label="Spools" className="mt-6 flex flex-col gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Spools of this type</h2>
          <div className="overflow-x-auto rounded-md border">
            <table aria-label="Spools of this type" className="w-full text-left text-[13px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="w-7 px-1 py-1.5"><span className="sr-only">Colour chip</span></th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Spool</th>
                  <th scope="col" className="px-2 py-1.5 text-right font-medium">Grams left</th>
                  <th scope="col" className="px-2 py-1.5 font-medium">Place</th>
                </tr>
              </thead>
              <tbody>
                {mine.map((spool) => (
                  <tr key={spool.id} className="border-t">
                    <td className="px-1 py-1.5">
                      <RgbaSwatch rgba={spool.effective?.rgba ?? draft.rgba ?? '#FFFFFFFF'} size="md" gramsLeft={spool.grams_left} labelWeight={spool.effective?.label_weight_g} />
                    </td>
                    <td className="px-2 py-1.5">
                      <Link to={`/filament/spools/${spool.id}`} className="font-medium underline-offset-4 hover:underline">
                        #{spool.number}
                      </Link>
                      {spool.archived_at && <span className="ml-2 text-[12px] text-muted-foreground">Archived</span>}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{spool.grams_left ?? ''}</td>
                    <td className="px-2 py-1.5">{spool.names?.location ?? ''}</td>
                  </tr>
                ))}
                {mine.length === 0 && (
                  <tr className="border-t">
                    <td colSpan={4} className="px-2 py-3 text-muted-foreground">
                      No spool has this type yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <AlertDialog open={guard.pending !== null} onOpenChange={(next) => !next && guard.stay()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>Your changes to this type have not been saved, and will be lost. Save first to keep them.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Stay</AlertDialogCancel>
            <Button type="button" variant="destructive" onClick={guard.leave}>
              Leave without saving
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Shell>
  )
}

function Shell({ children }: { children: ReactNode }) {
  const location = useLocation()
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <Link to={`/filament/types${listBack(location.state)}`} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Types
        </Link>
        {children}
      </div>
    </div>
  )
}

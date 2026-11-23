import { ArchiveIcon, ArchiveRestoreIcon, ChevronLeftIcon, ExternalLinkIcon, ShoppingCartIcon, SlidersHorizontalIcon } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { AttachmentsPanel } from '../../components/entities/AttachmentsPanel'
import { LinksPanel } from '../../components/entities/LinksPanel'
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
import { addShopping } from '../../api/filamentPurchasing'
import { CostCalculator } from '../../components/filament/CostCalculator'
import { KValueTable } from '../../components/filament/KValueTable'
import { keyOf, kValueRowsError, type KPrinter } from '../../components/filament/kValueRows'
import { PresetTable } from '../../components/filament/PresetTable'
import { ProfileFetchPanel } from '../../components/filament/ProfileFetchPanel'
import { ProfileFillDialog } from '../../components/filament/ProfileFillDialog'
import { presetRowsError } from '../../components/filament/presetRows'
import { PricePanel } from '../../components/filament/purchasing/PricePanel'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { RgbaColourField } from '../../components/filament/RgbaColourField'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { listBack } from '../../components/filament/spoolsQuery'
import { EntityLink } from '../../components/filament/EntityLink'
import { filamentLinks } from '../../lib/filamentLinks'
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
  lead_time_days: string
  safety_margin_days: string
  nozzle_temp_min: string
  nozzle_temp_max: string
  bed_temp_min: string
  bed_temp_max: string
  /** The default of a spool's drying (#6720). */
  drying_temp_c: string
  drying_hours: string
  /** 3dfilamentprofiles.com's filament id (#6625). */
  tdfp_id: string
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
  lead_time_days: '',
  safety_margin_days: '',
  nozzle_temp_min: '',
  nozzle_temp_max: '',
  bed_temp_min: '',
  bed_temp_max: '',
  drying_temp_c: '',
  drying_hours: '',
  tdfp_id: '',
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
    lead_time_days: str(type.lead_time_days),
    safety_margin_days: str(type.safety_margin_days),
    nozzle_temp_min: str(type.nozzle_temp_min),
    nozzle_temp_max: str(type.nozzle_temp_max),
    bed_temp_min: str(type.bed_temp_min),
    bed_temp_max: str(type.bed_temp_max),
    drying_temp_c: str(type.drying_temp_c),
    drying_hours: str(type.drying_hours),
    tdfp_id: str(type.tdfp_id),
    note: str(type.note),
    presets: type.presets.map(({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name }) => ({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name })),
    // `source` is the server's to set (R-6714-19): a save never sends it.
    k_values: type.k_values.map(({ type_id: _type, source: _source, ...row }) => row),
  }
}

const INTEGER = /^\d+$/
/** `filament/models.py` `TdfpId`: a positive whole number below 2^31. */
const TDFP_MAX = 2 ** 31 - 1

/** 3dfilamentprofiles.com's page of a filament (#6625): an outside link, never on ScadBuddy's host. */
const tdfpUrl = (id: number) => `https://3dfilamentprofiles.com/filament/details/${id}`

/** The draft's 3DFP id, `null` for none, or `undefined` when it is not one. */
function tdfpOf(text: string): number | null | undefined {
  const value = text.trim()
  if (value === '') return null
  if (!INTEGER.test(value)) return undefined
  const id = Number(value)
  return id >= 1 && id <= TDFP_MAX ? id : undefined
}
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
    drying_temp_c: whole('drying temperature', draft.drying_temp_c),
    lead_time_days: whole('lead time in days', draft.lead_time_days),
    safety_margin_days: whole('safety margin in days', draft.safety_margin_days),
  }
  for (const value of Object.values(fields)) if (typeof value === 'string') return value
  // `filament/models.py` `Days`: 0 to 365, as the settings that stand in for them.
  for (const [label, value] of [['lead time', fields.lead_time_days], ['safety margin', fields.safety_margin_days]] as const) {
    if (typeof value === 'number' && value > 365) return `Enter the ${label} as at most 365 days.`
  }
  const tdfp = tdfpOf(draft.tdfp_id)
  if (tdfp === undefined) return `Enter the 3DFP id as a whole number from 1 to ${TDFP_MAX}.`
  const diameter = decimal('filament diameter', draft.diameter_mm)
  const density = decimal('density', draft.density_g_cm3)
  const cost = decimal('cost per kg', draft.cost_per_kg)
  const dryingHours = decimal('drying hours', draft.drying_hours)
  for (const value of [diameter, density, cost, dryingHours]) if (value !== null && !DECIMAL.test(value)) return value
  // `filament/models.py` `Temp` and `Hours`, as the columns hold them.
  if (typeof fields.drying_temp_c === 'number' && fields.drying_temp_c > 1000) return 'Enter the drying temperature as at most 1000 °C.'
  if (dryingHours !== null && Number(dryingHours) >= 1000) return 'Enter the drying hours as less than 1000.'
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
    drying_hours: dryingHours,
    tdfp_id: tdfp,
    note: draft.note.trim() || null,
    ...(fields as {
      label_weight_g: number
      low_stock_pct: number | null
      nozzle_temp_min: number | null
      nozzle_temp_max: number | null
      bed_temp_min: number | null
      bed_temp_max: number | null
      drying_temp_c: number | null
      lead_time_days: number | null
      safety_margin_days: number | null
    }),
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
 * printer model and nozzle, its pressure-advance K values, the supplier prices it has
 * (#6266; "Use as default cost" only fills the cost field, Save writes it), a cost calculator
 * from a purchase's price, tax, shipping and net weight (#7152, the same "Use as default cost"), an "Add to shopping
 * list" button (one pending spool, written at once), and the spools of it. Saving
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
  const [adding, setAdding] = useState(false)
  const [filling, setFilling] = useState(false)
  const money = useMoney()

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
        guard.leaveTo(filamentLinks.type(typeId), { replace: true, state: location.state })
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

  /**
   * #6625 — a 3DFP fetch ended: it set the type's presets (and K values) on the server.
   * Shown at once where this page holds no unsaved edit of them; an edited list is kept,
   * and Discard changes shows the fetched one.
   */
  function fetched() {
    const before = saved ? draftOf(saved) : BLANK
    const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
    const edited = !same(draft.presets, before.presets) || !same(draft.k_values, before.k_values)
    getType(madeId ?? id).then(
      (fresh) => {
        const after = draftOf(fresh)
        setSaved(fresh)
        setDraft((was) => ({
          ...was,
          presets: same(was.presets, before.presets) ? after.presets : was.presets,
          k_values: same(was.k_values, before.k_values) ? after.k_values : was.k_values,
        }))
        if (edited) setNotice('The fetch set new presets. Your unsaved edits to them are kept: discard changes to see the fetched ones.')
      },
      (cause: unknown) => setError(`The fetched presets could not be read: ${message(cause)}`),
    )
  }

  async function addToShopping() {
    if (adding) return
    setAdding(true)
    setError(null)
    setNotice(null)
    try {
      await addShopping({ type_id: madeId ?? id, quantity_spools: 1 })
      setNotice('Added to the shopping list.')
    } catch (cause) {
      setError(message(cause))
    } finally {
      setAdding(false)
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

  const draftTdfp = tdfpOf(draft.tdfp_id)
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
        {!creating && (
          <Button type="button" variant="outline" size="sm" disabled={busy || dirty} title={dirty ? 'Save or discard your changes first.' : undefined} onClick={() => setFilling(true)}>
            <SlidersHorizontalIcon />
            Fill from slicer profile
          </Button>
        )}
        {!creating && (
          <Button type="button" variant="outline" size="sm" disabled={busy || adding} onClick={() => void addToShopping()}>
            <ShoppingCartIcon />
            Add to shopping list
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
            <RgbaColourField value={draft.rgba} onChange={(next) => set('rgba', next)} catalog onExtraColours={(extras) => set('extra_colours', extras.join(', '))} />
          </Field>
          {text('colour_name', 'Colour name')}
          {text('extra_colours', 'Extra colours', { hint: 'For multi-colour filament: #RRGGBB or #RRGGBBAA, separated by commas.' })}
          <Field label="3DFP id" htmlFor="type-tdfp_id" hint="The number at the end of the filament's address on 3dfilamentprofiles.com. Fetch from 3DFP, under Slicer presets, uses it.">
            <div className="flex items-center gap-3">
              <Input id="type-tdfp_id" className="w-36" inputMode="numeric" value={draft.tdfp_id} onChange={(e) => set('tdfp_id', e.target.value)} />
              {typeof draftTdfp === 'number' && (
                <a href={tdfpUrl(draftTdfp)} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[13px] underline-offset-4 hover:underline">
                  Open on 3DFP
                  <ExternalLinkIcon aria-hidden className="size-3.5" />
                </a>
              )}
            </div>
          </Field>
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
            <CostCalculator
              labelWeight={draft.label_weight_g}
              onUseAsCost={(perKg) => {
                set('cost_per_kg', perKg)
                setNotice(`Cost per kg is now ${perKg}, from the calculator. Save the type to keep it.`)
              }}
            />
          </section>
          <section aria-label="Reordering" className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold tracking-tight">Reordering</h2>
            <div className="grid grid-cols-2 gap-3">
              {text('lead_time_days', 'Lead time (days)', { mode: 'numeric', hint: 'How long an order takes to arrive.' })}
              {text('safety_margin_days', 'Safety margin (days)', { mode: 'numeric', hint: 'Stock to keep on top of the lead time.' })}
            </div>
            <p className="text-[12px] text-muted-foreground">
              Empty, the type takes the defaults in Settings, Filament. The Metrics tab says when to reorder: the day it runs out, less both.
            </p>
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
          <section aria-label="Drying" className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold tracking-tight">Drying</h2>
            <div className="grid grid-cols-2 gap-3">
              {text('drying_temp_c', 'Drying temperature (°C)', { mode: 'numeric' })}
              {text('drying_hours', 'Drying time (h)', { mode: 'decimal' })}
            </div>
            <p className="text-[12px] text-muted-foreground">
              A spool of this type fills its drying form with these. Empty, it takes the subtype's or the material's.
            </p>
          </section>
          <Field label="Note" htmlFor="type-note">
            <Textarea id="type-note" rows={3} value={draft.note} onChange={(e) => set('note', e.target.value)} />
          </Field>
        </div>
      </div>

      {!creating && (
        <PricePanel
          typeId={madeId ?? id}
          currency={money.currency}
          exponent={money.exponent}
          onUseAsCost={(perKg, from) => {
            set('cost_per_kg', perKg)
            setNotice(`Cost per kg is now ${perKg}, from ${from}. Save the type to keep it.`)
          }}
        />
      )}

      <section aria-label="Slicer presets" className="mt-6 flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">Slicer presets</h2>
        <p className="text-[12px] text-muted-foreground">The Bambu Studio filament preset to slice with, for each printer model and nozzle.</p>
        <ProfileFetchPanel
          typeId={creating ? null : (madeId ?? id)}
          tdfpId={saved?.tdfp_id ?? null}
          idEdited={draft.tdfp_id.trim() !== base.tdfp_id}
          onEnded={fetched}
        />
        <PresetTable rows={draft.presets} onChange={(rows) => set('presets', rows)} models={models} />
      </section>

      <section aria-label="Pressure advance" className="mt-6 flex flex-col gap-2">
        <h2 className="text-sm font-semibold tracking-tight">K values</h2>
        <p className="text-[12px] text-muted-foreground">Pressure advance, by printer, extruder and nozzle.</p>
        <KValueTable rows={draft.k_values} onChange={(rows) => set('k_values', rows)} printers={lists.printers} saved={new Map((saved?.k_values ?? []).map((row) => [keyOf(row), row]))} />
      </section>

      {!creating && (
        <section aria-label="Files and links" className="mt-6 flex flex-col gap-2">
          <h2 className="text-sm font-semibold tracking-tight">Files and links</h2>
          <div className="grid gap-6 lg:grid-cols-2">
            <AttachmentsPanel entity={`spool_type:${madeId ?? id}`} />
            <LinksPanel entity={`spool_type:${madeId ?? id}`} />
          </div>
        </section>
      )}

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
                      <EntityLink to={filamentLinks.spool(spool.id)} className="font-medium">
                        #{spool.number}
                      </EntityLink>
                      {spool.archived_at && <span className="ml-2 text-[12px] text-muted-foreground">Archived</span>}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{spool.grams_left ?? ''}</td>
                    <td className="px-2 py-1.5">{spool.names?.location && spool.location_id ? <EntityLink to={filamentLinks.location(spool.location_id)}>{spool.names.location}</EntityLink> : ''}</td>
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

      {!creating && (
        <ProfileFillDialog
          typeId={madeId ?? id}
          open={filling}
          onClose={() => setFilling(false)}
          onApplied={(done) => {
            setFilling(false)
            const fields = Object.keys(done.filled).length
            const parts = [fields > 0 ? `${fields} ${fields === 1 ? 'field' : 'fields'}` : null, done.k_values.length > 0 ? `${done.k_values.length} K ${done.k_values.length === 1 ? 'value' : 'values'}` : null].filter(Boolean)
            void getType(madeId ?? id).then((fresh) => {
              setSaved(fresh)
              setDraft(draftOf(fresh))
              setNotice(`Filled ${parts.join(' and ')}${done.source ? ` from ${done.source.name}` : ''}.`)
            }, (cause: unknown) => setError(message(cause)))
          }}
        />
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
        <Link to={`${filamentLinks.types}${listBack(location.state)}`} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Types
        </Link>
        {children}
      </div>
    </div>
  )
}

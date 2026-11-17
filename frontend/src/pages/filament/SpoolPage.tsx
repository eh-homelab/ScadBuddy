import { ArchiveIcon, ArchiveRestoreIcon, ChevronLeftIcon, Trash2Icon } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import { api, ApiError } from '../../api/client'
import {
  addSpoolEvent,
  archiveSpool,
  createSpool,
  deleteSpool,
  getSpool,
  getType,
  listCatalog,
  listLocations,
  listSpoolEvents,
  listTypes,
  moveSpool,
  patchSpool,
  setSpoolKValues,
  setSpoolPresets,
  type CatalogRows,
  type KValueInput,
  type LocationNode,
  type PresetInput,
  type SpoolCreate,
  type SpoolDetail,
  type SpoolEvent,
  type SpoolPatch,
  type SpoolType,
  type SpoolTypeDetail,
} from '../../api/filament'
import { GramsBasis } from '../../components/filament/GramsBasis'
import { FirstTypePresets } from '../../components/filament/FirstTypePresets'
import { KValueTable } from '../../components/filament/KValueTable'
import { kValueRowsError, keyOf as kValueKey, type KPrinter } from '../../components/filament/kValueRows'
import { makeTypeFromSpool } from '../../components/filament/makeType'
import { OverrideField } from '../../components/filament/OverrideField'
import { PresetTable } from '../../components/filament/PresetTable'
import { keyOf as presetKey, presetRowsError } from '../../components/filament/presetRows'
import { RgbaColourField } from '../../components/filament/RgbaColourField'
import { RgbaSwatch } from '../../components/filament/RgbaSwatch'
import { HistoryList, Section, SectionIndex } from '../../components/filament/SpoolSections'
import { TagPanel } from '../../components/filament/TagPanel'
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { listBack } from '../../components/filament/spoolsQuery'
import { parseRgba } from '../../lib/rgba'
import { useLeaveGuard } from '../../lib/useLeaveGuard'

/** What a field holds while it is edited: text, `''` for none (the spool takes its type's value). */
interface Draft {
  type_id: string
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
  density_g_cm3: string
  cost_per_kg: string
  label_weight_g: string
  low_stock_pct: string
  nozzle_temp_min: string
  nozzle_temp_max: string
  bed_temp_min: string
  bed_temp_max: string
  weight_locked: boolean
  note: string
  location_id: string
  /** A new spool's first reading; an existing one is weighed instead. */
  grams_left: string
  /** The spool's own presets and K values; the type's are shown beside them. */
  presets: PresetInput[]
  k_values: KValueInput[]
}

const BLANK: Draft = {
  type_id: '',
  material_id: '',
  subtype_id: '',
  brand_id: '',
  category_id: '',
  effect_id: '',
  insert_type_id: '',
  rgba: null,
  colour_name: '',
  extra_colours: '',
  diameter_mm: '',
  density_g_cm3: '',
  cost_per_kg: '',
  label_weight_g: '',
  low_stock_pct: '',
  nozzle_temp_min: '',
  nozzle_temp_max: '',
  bed_temp_min: '',
  bed_temp_max: '',
  weight_locked: false,
  note: '',
  location_id: '',
  grams_left: '',
  presets: [],
  k_values: [],
}

const str = (value: string | number | null | undefined) => (value == null ? '' : String(value))

function draftOf(spool: SpoolDetail): Draft {
  return {
    type_id: str(spool.type_id),
    material_id: str(spool.material_id),
    subtype_id: str(spool.subtype_id),
    brand_id: str(spool.brand_id),
    category_id: str(spool.category_id),
    effect_id: str(spool.effect_id),
    insert_type_id: str(spool.insert_type_id),
    rgba: spool.rgba ?? null,
    colour_name: str(spool.colour_name),
    extra_colours: (spool.extra_colours ?? []).join(', '),
    diameter_mm: str(spool.diameter_mm),
    density_g_cm3: str(spool.density_g_cm3),
    cost_per_kg: str(spool.cost_per_kg),
    label_weight_g: str(spool.label_weight_g),
    low_stock_pct: str(spool.low_stock_pct),
    nozzle_temp_min: str(spool.nozzle_temp_min),
    nozzle_temp_max: str(spool.nozzle_temp_max),
    bed_temp_min: str(spool.bed_temp_min),
    bed_temp_max: str(spool.bed_temp_max),
    weight_locked: spool.weight_locked,
    note: str(spool.note),
    location_id: str(spool.location_id),
    grams_left: '',
    presets: spool.presets.filter((row) => row.overridden).map(({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name }) => ({ printer_model, nozzle_diameter, slicer_filament, slicer_filament_name })),
    k_values: spool.k_values
      .filter((row) => row.overridden)
      .map(({ overridden: _own, k_value, ...row }) => ({ ...row, k_value: String(k_value) })),
  }
}

const INTEGER = /^\d+$/
const DECIMAL = /^\d+(\.\d+)?$/

/** The spool's own columns a draft makes (`null`: the type's value), or why it cannot be saved. */
function bodyOf(draft: Draft): SpoolPatch | string {
  const whole = (label: string, value: string): number | null | string => {
    if (value.trim() === '') return null
    return INTEGER.test(value.trim()) ? Number(value) : `Enter the ${label} as a whole number.`
  }
  const decimal = (value: string): string | null | undefined => {
    if (value.trim() === '') return null
    return DECIMAL.test(value.trim()) ? value.trim() : undefined
  }
  const rgba = draft.rgba ? parseRgba(draft.rgba) : null
  if (draft.rgba && !rgba) return 'Choose the colour as #RRGGBB or #RRGGBBAA.'
  const extras: string[] = []
  for (const part of draft.extra_colours.split(',').map((one) => one.trim()).filter(Boolean)) {
    const one = parseRgba(part)
    if (!one) return `"${part}" is not a colour. Enter extra colours as #RRGGBB or #RRGGBBAA, separated by commas.`
    extras.push(one)
  }
  if (extras.length > 16) return 'Enter at most 16 extra colours.'
  const ints = {
    label_weight_g: whole('label weight in grams', draft.label_weight_g),
    low_stock_pct: whole('low stock percent', draft.low_stock_pct),
    nozzle_temp_min: whole('minimum nozzle temperature', draft.nozzle_temp_min),
    nozzle_temp_max: whole('maximum nozzle temperature', draft.nozzle_temp_max),
    bed_temp_min: whole('minimum bed temperature', draft.bed_temp_min),
    bed_temp_max: whole('maximum bed temperature', draft.bed_temp_max),
  }
  for (const value of Object.values(ints)) if (typeof value === 'string') return value
  const decimals = {
    diameter_mm: decimal(draft.diameter_mm),
    density_g_cm3: decimal(draft.density_g_cm3),
    cost_per_kg: decimal(draft.cost_per_kg),
  }
  if (decimals.diameter_mm === undefined) return 'Enter the filament diameter as a number, such as 1.75.'
  if (decimals.density_g_cm3 === undefined) return 'Enter the density as a number, such as 1.24.'
  if (decimals.cost_per_kg === undefined) return 'Enter the cost per kg as a number, such as 15.91.'
  if (!draft.type_id && !draft.material_id) return 'Choose a type. A spool with no type needs its material.'
  return {
    type_id: draft.type_id || null,
    material_id: draft.material_id || null,
    subtype_id: draft.subtype_id || null,
    brand_id: draft.brand_id || null,
    category_id: draft.category_id || null,
    effect_id: draft.effect_id || null,
    insert_type_id: draft.insert_type_id || null,
    rgba,
    colour_name: draft.colour_name.trim() || null,
    extra_colours: extras.length > 0 ? extras : null,
    weight_locked: draft.weight_locked,
    note: draft.note.trim() || null,
    ...(ints as Record<keyof typeof ints, number | null>),
    ...(decimals as Record<keyof typeof decimals, string | null>),
  }
}

/** Only the fields whose value differs from `was`. */
function changed(next: SpoolPatch, was: SpoolPatch | string): SpoolPatch {
  if (typeof was === 'string') return next
  const out: Record<string, unknown> = {}
  for (const key of Object.keys(next) as (keyof SpoolPatch)[]) {
    if (JSON.stringify(next[key]) !== JSON.stringify(was[key])) out[key] = next[key]
  }
  return out as SpoolPatch
}

/** A create body: the fields a person set, not the `null`s that mean the type's value. */
const created = (body: SpoolPatch, grams: string): SpoolCreate => {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(body)) if (value !== null && key !== 'archived') out[key] = value
  if (grams.trim() !== '') out.grams_left = Number(grams)
  return out as SpoolCreate
}

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : cause instanceof Error ? cause.message : 'The server could not be reached. Try again.')

const NONE = '__none__'

interface PickProps {
  name: string
  label: string
  value: string
  onChange: (id: string) => void
  options: { id: string; name: string }[]
  /** What the field is after the type's value is taken, for the "From type" entry. */
  effectiveName?: string
  overridden: boolean
  hasType: boolean
  onReset: () => void
  required?: boolean
  /** The id of the text that says what is wrong with the choice; the trigger is then invalid. */
  invalidBy?: string
}

/** A catalog choice that may override the type's: "From type (PLA)" is the cleared entry. */
function Pick({ name, label, value, onChange, options, effectiveName, overridden, hasType, onReset, required = false, invalidBy }: PickProps) {
  const noneLabel = hasType ? (effectiveName ? `From type (${effectiveName})` : 'From type') : 'None'
  return (
    <OverrideField name={name} label={label} value={value} overridden={overridden} hasType={hasType} onChange={onChange} onReset={onReset}>
      <Select value={value === '' && !required ? NONE : value || undefined} onValueChange={(next) => onChange(next === NONE ? '' : next)}>
        <SelectTrigger id={`spool-${name}`} aria-label={label} aria-invalid={invalidBy ? true : undefined} aria-describedby={invalidBy} className="w-full">
          <SelectValue placeholder="Choose…" />
        </SelectTrigger>
        <SelectContent>
          {!required && <SelectItem value={NONE}>{noneLabel}</SelectItem>}
          {options.map((option) => (
            <SelectItem key={option.id} value={option.id}>
              {option.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </OverrideField>
  )
}

interface Lists {
  materials: CatalogRows['materials'][]
  subtypes: CatalogRows['subtypes'][]
  brands: CatalogRows['brands'][]
  categories: CatalogRows['categories'][]
  effects: CatalogRows['effects'][]
  insertTypes: CatalogRows['insert-types'][]
  types: SpoolType[]
  places: { id: string; name: string }[]
  printers: (KPrinter & { model: string | null })[]
}

function flatten(nodes: LocationNode[], path: string[] = []): { id: string; name: string }[] {
  // A printer's tray is not a place to move a spool to: loading one is the tray's job.
  return nodes.flatMap((node) => [...(node.printer_id == null ? [{ id: node.id, name: [...path, node.name].join(' / ') }] : []), ...flatten(node.children ?? [], [...path, node.name])])
}

const grams = (value: number) => `${Number(value.toFixed(1))} g`

/**
 * #6262 — one spool, `/filament/spools/<id>` or `/filament/spools/new`, in spec §10's
 * order: Identity, Weight, Colour and cost, Printers, Storage, History. A field the spool
 * may override shows its type's value as the placeholder; one it sets carries "Set on
 * this spool" and "Reset to type" (a PATCH of `null` for that field, at once). Saving
 * sends one PATCH of what changed (a POST for a new spool), a move through the move route
 * when the place changed, and the presets' and K values' PUTs when they changed; a created
 * spool keeps its id, so a retry is an edit. Weighings and dryings are ledger events and
 * land at once. Unsaved edits hold every way out (`useLeaveGuard`).
 */
export function SpoolPage() {
  const { id = 'new' } = useParams()
  const location = useLocation()
  /** The list's filters this page was opened from: back and a delete return to them. */
  const listPath = `/filament/spools${listBack(location.state)}`
  /** Set at once when a new spool's POST lands: the page is then an edit of it. */
  const [madeId, setMadeId] = useState<string | null>(null)
  const creating = id === 'new' && madeId === null
  const spoolKey = madeId ?? id
  const [notice, setNotice] = useState<string | null>(null)
  const [saved, setSaved] = useState<SpoolDetail | null>(null)
  const [draft, setDraft] = useState<Draft>(BLANK)
  const [lists, setLists] = useState<Lists | null>(null)
  const [events, setEvents] = useState<SpoolEvent[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [reading, setReading] = useState('')
  const [dryTemp, setDryTemp] = useState('')
  const [dryHours, setDryHours] = useState('')
  /** The type the form holds: its own presets and K values say which keys "Reset to type" can reset. */
  const [typeDetail, setTypeDetail] = useState<SpoolTypeDetail | null>(null)
  const [typeError, setTypeError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setLoadError(null)
    setSaved(null)
    setDraft(BLANK)
    setMadeId(null)
    setEvents(null)
    Promise.all([
      id === 'new' ? Promise.resolve(null) : getSpool(id),
      listCatalog('materials'),
      listCatalog('subtypes'),
      listCatalog('brands'),
      listCatalog('categories'),
      listCatalog('effects'),
      listCatalog('insert-types'),
      listTypes({ includeArchived: true }),
      listLocations(),
      api.listPrinters(),
    ])
      .then(([spool, materials, subtypes, brands, categories, effects, insertTypes, types, places, printers]) => {
        if (!live) return
        setLists({ materials, subtypes, brands, categories, effects, insertTypes, types, places: flatten(places), printers: printers.map((p) => ({ id: p.id, name: p.name, model: p.model ?? null })) })
        if (spool) {
          setSaved(spool)
          setDraft(draftOf(spool))
        }
      })
      .catch((cause: unknown) => {
        if (live) setLoadError(cause instanceof ApiError && cause.status === 404 ? 'That spool is gone. Pick another from the Spools tab.' : `The spool could not be loaded: ${message(cause)}`)
      })
    if (id !== 'new') listSpoolEvents(id).then((rows) => live && setEvents(rows), () => live && setEvents([]))
    return () => {
      live = false
    }
  }, [id])

  const base = useMemo(() => (saved ? draftOf(saved) : BLANK), [saved])
  const dirty = JSON.stringify(draft) !== JSON.stringify(base)
  const presetsDirty = JSON.stringify(draft.presets) !== JSON.stringify(base.presets)
  const typeId = draft.type_id
  useEffect(() => {
    let live = true
    setTypeDetail(null)
    setTypeError(null)
    if (typeId !== '') getType(typeId).then((type) => live && setTypeDetail(type), (cause: unknown) => live && setTypeError(message(cause)))
    return () => {
      live = false
    }
  }, [typeId])
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

  /** Reads the spool and its ledger again; `keepDraft` leaves unsaved edits as they are. */
  async function reload(keepDraft: boolean) {
    const [fresh, ledger] = await Promise.all([getSpool(spoolKey), listSpoolEvents(spoolKey)])
    setSaved(fresh)
    setEvents(ledger)
    if (!keepDraft) setDraft(draftOf(fresh))
    return fresh
  }

  /** Runs a write on its own: the error is shown, the busy flag held. */
  async function act(work: () => Promise<void>, done?: string) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await work()
      if (done) setNotice(done)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    const body = bodyOf(draft)
    const refused = typeof body === 'string' ? body : (presetRowsError(draft.presets) ?? kValueRowsError(draft.k_values) ?? (draft.grams_left.trim() !== '' && !DECIMAL.test(draft.grams_left.trim()) ? 'Enter the grams left as a number, such as 640.' : null))
    if (refused) {
      setError(refused)
      return
    }
    if (typeof body === 'string') return
    setBusy(true)
    setError(null)
    setNotice(null)
    let spoolId = spoolKey
    const firstType = !creating && saved !== null && !saved.type_id && draft.type_id !== ''
    try {
      let presetsWere = base.presets
      let kWere = base.k_values
      let placeWas = base.location_id
      if (creating) {
        const made = await createSpool(created(body, draft.grams_left))
        spoolId = made.id
        // From here the page is an edit of this spool: a later failure retries as one.
        setMadeId(made.id)
        setSaved({ ...made, effective: made.effective ?? {}, overridden: made.overridden ?? [], names: made.names ?? {}, low_stock: made.low_stock ?? false, presets: [], k_values: [], tag: null, last_tag: null, basis: { anchor: null, deltas: [] }, in_use: false })
        presetsWere = []
        kWere = []
        placeWas = ''
      } else {
        const patch = changed(body, bodyOf(base))
        if (Object.keys(patch).length > 0) await patchSpool(spoolId, patch)
      }
      if (draft.location_id !== placeWas) await moveSpool(spoolId, draft.location_id || null)
      // The first type drops the carried-over presets: the form's copy of them is not written back.
      if (!firstType && JSON.stringify(draft.presets) !== JSON.stringify(presetsWere)) await setSpoolPresets(spoolId, draft.presets)
      if (JSON.stringify(draft.k_values) !== JSON.stringify(kWere)) await setSpoolKValues(spoolId, draft.k_values)
      setNotice('Saved')
      if (id === 'new') {
        guard.leaveTo(`/filament/spools/${spoolId}`, { replace: true, state: location.state })
        return
      }
      const [fresh, ledger] = await Promise.all([getSpool(spoolId), listSpoolEvents(spoolId)])
      setSaved(fresh)
      setEvents(ledger)
      setDraft(draftOf(fresh))
    } catch (cause) {
      setError(message(cause))
      // A write that did land is shown as it now is, unsaved edits kept.
      if (spoolId !== 'new') await Promise.all([getSpool(spoolId).then(setSaved, () => {}), listSpoolEvents(spoolId).then(setEvents, () => {})])
    } finally {
      setBusy(false)
    }
  }

  /** "Reset to type": `null` for the one field, at once; other unsaved edits stay. */
  const reset = (name: keyof Draft & keyof SpoolPatch) =>
    act(async () => {
      await patchSpool(spoolKey, { [name]: null })
      const fresh = await reload(true)
      setDraft((was) => ({ ...was, [name]: draftOf(fresh)[name] }))
    })

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
        <p className="text-[13px] text-muted-foreground">Loading the spool…</p>
      </Shell>
    )
  }

  const hasType = draft.type_id !== ''
  const typeRow = lists.types.find((one) => one.id === draft.type_id)
  const sameType = saved !== null && draft.type_id === str(saved.type_id)
  const eff = (name: keyof NonNullable<SpoolDetail['effective']>): string => {
    const value: unknown = sameType ? saved.effective[name] : typeRow?.[name as keyof SpoolType]
    return value == null ? '' : Array.isArray(value) ? value.join(', ') : String(value)
  }
  /** The marks are the saved spool's: picking another type in the form does not unset what the spool holds. */
  const own = (name: string) => saved?.overridden.includes(name) ?? false
  const material = lists.materials.find((one) => one.id === (draft.material_id || eff('material_id')))
  const unknownMaterial = !hasType && material?.name === 'Unknown'
  const subtypes = lists.subtypes.filter((one) => !one.material_id || one.material_id === (draft.material_id || eff('material_id')))
  const types = lists.types.filter((one) => !one.archived_at || one.id === draft.type_id)
  const empty = lists.insertTypes.find((one) => one.id === (draft.insert_type_id || eff('insert_type_id')))
  const models = [...new Set(lists.printers.map((p) => p.model).filter((m): m is string => !!m))]
  const effective = saved?.effective
  /** A spool with no type has chosen its first one in the form, not saved it yet. */
  const firstTypeChosen = !creating && saved !== null && !saved.type_id && hasType
  /** A spool with no type, whose presets the first type would drop: the type cannot be chosen over unsaved preset edits. */
  const typeLocked = !creating && saved !== null && !saved.type_id && presetsDirty
  const typePresets = typeDetail?.presets ?? []
  const typeKValues = typeDetail?.k_values ?? []
  const presetTypeKeys = new Set(typePresets.map(presetKey))
  const kValueTypeKeys = new Set(typeKValues.map(kValueKey))
  /** The type's rows as a spool shows them while it holds none of its own: "From type". */
  const typeAsPresets = typePresets.map(({ type_id: _t, ...row }) => ({ ...row, overridden: false, migrated: false }))
  const typeAsKValues = typeKValues.map(({ type_id: _t, ...row }) => ({ ...row, overridden: false }))
  const savedPresets = saved?.presets ?? []

  const field = (key: 'colour_name' | 'diameter_mm' | 'density_g_cm3' | 'cost_per_kg' | 'label_weight_g' | 'low_stock_pct' | 'nozzle_temp_min' | 'nozzle_temp_max' | 'bed_temp_min' | 'bed_temp_max' | 'extra_colours', label: string, extra: { hint?: string; mode?: 'decimal' | 'numeric' } = {}) => (
    <OverrideField name={key} label={label} value={draft[key]} effective={eff(key)} overridden={own(key)} hasType={hasType} inputMode={extra.mode} hint={extra.hint} onChange={(next) => set(key, next)} onReset={() => void reset(key)} />
  )
  const pick = (key: 'material_id' | 'subtype_id' | 'brand_id' | 'category_id' | 'effect_id' | 'insert_type_id', label: string, options: { id: string; name: string }[], extra: { required?: boolean; invalidBy?: string; onChange?: (id: string) => void } = {}) => (
    <Pick
      name={key}
      label={label}
      value={draft[key]}
      options={options}
      effectiveName={options.find((one) => one.id === eff(key))?.name}
      overridden={own(key)}
      hasType={hasType}
      onChange={extra.onChange ?? ((next) => set(key, next))}
      onReset={() => void reset(key)}
      required={extra.required}
      invalidBy={extra.invalidBy}
    />
  )

  const leftNow = saved?.grams_left ?? null
  const scale = Number(reading)
  const filament = empty && reading.trim() !== '' && Number.isFinite(scale) ? scale - empty.empty_weight_g : scale
  const readingOk = reading.trim() !== '' && DECIMAL.test(reading.trim()) && filament >= 0

  return (
    <Shell>
      <header className="mb-5 flex flex-wrap items-center gap-3">
        <RgbaSwatch rgba={draft.rgba ?? (eff('rgba') || '#FFFFFFFF')} size="lg" gramsLeft={leftNow} labelWeight={effective?.label_weight_g ?? null} />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold tracking-tight">{creating ? 'New spool' : `Spool #${saved?.number}`}</h1>
          <p className="text-[13px] text-muted-foreground">
            {creating
              ? 'Choose its type, or its material if it has none yet.'
              : [typeRow?.code ?? saved?.names.material, leftNow != null ? grams(leftNow) : null, saved?.low_stock ? 'Low stock' : null, saved?.archived_at ? 'Archived' : null].filter(Boolean).join(', ')}
          </p>
        </div>
        {!creating && saved?.archived_at && (
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void act(async () => { await archiveSpool(spoolKey, false); await reload(true) })}>
            <ArchiveRestoreIcon /> Restore
          </Button>
        )}
        <Button type="button" variant="ghost" size="sm" disabled={busy || !dirty} onClick={() => { setDraft(base); setError(null) }}>
          Discard changes
        </Button>
        <Button type="button" size="sm" disabled={busy || (!dirty && !creating)} onClick={() => void save()}>
          Save spool
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

      <div className="grid gap-6 lg:grid-cols-[11rem_minmax(0,1fr)]">
        <SectionIndex />
        <div className="flex min-w-0 flex-col gap-8">
          <Section id="identity" title="Identity">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="spool-number" className="text-[13px]">
                  Number
                </Label>
                <Input id="spool-number" readOnly value={creating ? 'Assigned when saved' : `#${saved?.number}`} className="tabular-nums" />
              </div>
              <div className="flex min-w-0 flex-col gap-1">
                <Label htmlFor="spool-type" className="text-[13px]">
                  Type
                </Label>
                <Select value={draft.type_id === '' ? NONE : draft.type_id} disabled={typeLocked} onValueChange={(next) => set('type_id', next === NONE ? '' : next)}>
                  <SelectTrigger id="spool-type" aria-label="Type" aria-describedby={typeLocked ? 'spool-type-locked' : undefined} className="w-full">
                    <SelectValue placeholder="Choose a type…" />
                  </SelectTrigger>
                  <SelectContent>
                    {!saved?.type_id && <SelectItem value={NONE}>No type yet</SelectItem>}
                    {types.map((one) => (
                      <SelectItem key={one.id} value={one.id}>
                        {one.code}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {typeLocked && (
                  <p id="spool-type-locked" className="text-[12px] text-muted-foreground">
                    Save or discard your preset changes before choosing a type
                  </p>
                )}
                {!hasType && saved && (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      size="xs"
                      variant="outline"
                      disabled={busy || dirty}
                      onClick={() => void act(async () => { await makeTypeFromSpool(saved); await reload(false) }, 'Type created')}
                    >
                      Create type from this spool
                    </Button>
                    {dirty && <span className="text-[12px] text-muted-foreground">Save the spool first.</span>}
                  </div>
                )}
              </div>
            </div>
            {unknownMaterial && (
              <p id="spool-material-prompt" className="text-[13px] font-medium text-destructive">
                Choose a material
              </p>
            )}
            <div className="grid gap-3 sm:grid-cols-2">
              {pick('material_id', 'Material', lists.materials, { required: !hasType, invalidBy: unknownMaterial ? 'spool-material-prompt' : undefined, onChange: setMaterial })}
              {pick('subtype_id', 'Subtype', subtypes)}
              {pick('brand_id', 'Brand', lists.brands)}
              {pick('category_id', 'Category', lists.categories)}
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="spool-note" className="text-[13px]">
                Note
              </Label>
              <Textarea id="spool-note" rows={2} value={draft.note} onChange={(e) => set('note', e.target.value)} />
            </div>
            <div className="flex flex-col gap-2">
              <h3 className="text-[13px] font-medium">Tag</h3>
              {saved && !creating ? <TagPanel spool={saved} onChanged={() => void reload(true).catch((cause) => setError(message(cause)))} /> : <p className="text-[13px] text-muted-foreground">Save the spool first, then bind its tag.</p>}
            </div>
            {!creating && saved && (
              <div className="flex flex-wrap items-center gap-2 border-t pt-3">
                {!saved.archived_at && (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void act(async () => { await archiveSpool(spoolKey, true); await reload(true) })}>
                    <ArchiveIcon /> Archive
                  </Button>
                )}
                {saved.in_use ? (
                  <p className="text-[13px] text-muted-foreground">It has prints, so job costs keep their source.</p>
                ) : (
                  <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setConfirmDelete(true)}>
                    <Trash2Icon /> Delete
                  </Button>
                )}
              </div>
            )}
          </Section>

          <Section id="weight" title="Weight">
            <div className="grid gap-3 sm:grid-cols-2">
              {field('label_weight_g', 'Label weight (g)', { mode: 'numeric', hint: 'The filament on a full spool.' })}
              {pick('insert_type_id', 'Spool', lists.insertTypes)}
              {field('diameter_mm', 'Diameter (mm)', { mode: 'decimal' })}
              {field('density_g_cm3', 'Density (g/cm³)', { mode: 'decimal' })}
              {field('low_stock_pct', 'Low stock at (%)', { mode: 'numeric', hint: 'Of the label weight.' })}
            </div>
            <p className="text-[13px] text-muted-foreground">
              {empty ? `Empty spool ${empty.empty_weight_g} g (${empty.name}).` : 'Empty spool weight not known. Choose its spool.'}
            </p>
            {creating ? (
              <div className="max-w-56">
                <Label htmlFor="spool-grams-left" className="text-[13px]">
                  Grams left
                </Label>
                <Input id="spool-grams-left" inputMode="decimal" className="tabular-nums" value={draft.grams_left} onChange={(e) => set('grams_left', e.target.value)} />
              </div>
            ) : (
              <div className="grid gap-4 rounded-md border p-3 md:grid-cols-2">
                <div className="flex flex-col gap-2">
                  <p className="text-2xl font-semibold tracking-tight tabular-nums">{leftNow == null ? 'Not known' : grams(leftNow)}</p>
                  <p className="text-[13px] text-muted-foreground">
                    {saved?.last_weighed_g != null && saved.last_weighed_at
                      ? `Measured ${grams(saved.last_weighed_g)} on ${new Date(saved.last_weighed_at).toLocaleDateString(undefined, { dateStyle: 'medium' })}.`
                      : 'Never weighed.'}
                  </p>
                  {saved && <GramsBasis basis={saved.basis} gramsLeft={leftNow} />}
                </div>
                <form
                  className="flex flex-col gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (readingOk) void act(async () => { await addSpoolEvent(spoolKey, { kind: 'weighed', grams: filament, source: 'scale' }); setReading(''); await reload(true) }, 'Weighed')
                  }}
                >
                  <Label htmlFor="spool-reading" className="text-[13px]">
                    {empty ? 'Scale reading, with the spool (g)' : 'Filament left (g)'}
                  </Label>
                  <Input id="spool-reading" inputMode="decimal" className="tabular-nums" value={reading} onChange={(e) => setReading(e.target.value)} />
                  {empty && readingOk && <p className="text-[12px] text-muted-foreground tabular-nums">{grams(filament)} of filament.</p>}
                  {empty && reading.trim() !== '' && DECIMAL.test(reading.trim()) && filament < 0 && <p className="text-[12px] text-warn">That is less than the empty spool.</p>}
                  <div>
                    <Button type="submit" size="sm" variant="outline" disabled={busy || !readingOk}>
                      Record weighing
                    </Button>
                  </div>
                </form>
              </div>
            )}
            <Label className="flex items-start gap-2 text-[13px] font-normal">
              <Checkbox className="mt-0.5" checked={draft.weight_locked} onCheckedChange={(next) => set('weight_locked', next === true)} />
              <span>
                Lock the weight
                <span className="block text-[12px] text-muted-foreground">A Bambuddy sync never changes the grams left of a locked spool.</span>
              </span>
            </Label>
          </Section>

          <Section id="colour-and-cost" title="Colour and cost">
            <OverrideField name="rgba" label="Colour" group value={draft.rgba ?? ''} overridden={own('rgba')} hasType={hasType} onChange={() => {}} onReset={() => void reset('rgba')}>
              <RgbaColourField value={draft.rgba} onChange={(next) => set('rgba', next)} inherited={hasType ? eff('rgba') || null : null} />
            </OverrideField>
            <div className="grid gap-3 sm:grid-cols-2">
              {field('colour_name', 'Colour name')}
              {pick('effect_id', 'Effect', lists.effects)}
              {field('extra_colours', 'Extra colours', { hint: 'For multi-colour filament: #RRGGBB or #RRGGBBAA, separated by commas.' })}
              {field('cost_per_kg', 'Cost per kg', { mode: 'decimal' })}
            </div>
          </Section>

          <Section id="printers" title="Printers">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {field('nozzle_temp_min', 'Nozzle temperature, min (°C)', { mode: 'numeric' })}
              {field('nozzle_temp_max', 'Nozzle temperature, max (°C)', { mode: 'numeric' })}
              {field('bed_temp_min', 'Bed temperature, min (°C)', { mode: 'numeric' })}
              {field('bed_temp_max', 'Bed temperature, max (°C)', { mode: 'numeric' })}
            </div>
            <div className="flex flex-col gap-2">
              {typeError && (
                <p role="alert" className="text-[13px] text-warn">
                  The type&apos;s presets and K values could not be loaded: {typeError}
                </p>
              )}
              <h3 className="text-[13px] font-medium">Slicer presets</h3>
              <p className="text-[12px] text-muted-foreground">The Bambu Studio filament preset to slice with, for each printer model and nozzle. A row you do not set takes the type&apos;s.</p>
              {firstTypeChosen ? (
                <FirstTypePresets own={savedPresets} typeRows={typeAsPresets} />
              ) : (
                <PresetTable rows={draft.presets} onChange={(rows) => set('presets', rows)} models={models} inherited={hasType ? (sameType ? saved.presets : typeAsPresets) : undefined} typeKeys={typeDetail ? presetTypeKeys : undefined} />
              )}
            </div>
            <div className="flex flex-col gap-2">
              <h3 className="text-[13px] font-medium">K values</h3>
              <p className="text-[12px] text-muted-foreground">Pressure advance, by printer, extruder and nozzle.</p>
              <KValueTable rows={draft.k_values} onChange={(rows) => set('k_values', rows)} printers={lists.printers} inherited={hasType ? (sameType ? saved.k_values : typeAsKValues) : undefined} typeKeys={typeDetail ? kValueTypeKeys : undefined} />
            </div>
          </Section>

          <Section id="storage" title="Storage">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex min-w-0 flex-col gap-1">
                <Label htmlFor="spool-location" className="text-[13px]">
                  Location
                </Label>
                <Select value={draft.location_id === '' ? NONE : draft.location_id} onValueChange={(next) => set('location_id', next === NONE ? '' : next)}>
                  <SelectTrigger id="spool-location" aria-label="Location" className="w-full">
                    <SelectValue placeholder="Choose a place…" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NONE}>Nowhere</SelectItem>
                    {draft.location_id !== '' && !lists.places.some((place) => place.id === draft.location_id) && (
                      <SelectItem value={draft.location_id} disabled>
                        {saved?.names?.location ?? 'A printer tray'}
                      </SelectItem>
                    )}
                    {lists.places.map((place) => (
                      <SelectItem key={place.id} value={place.id}>
                        {place.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-[12px] text-muted-foreground">Saved through a move, which the history records.</p>
              </div>
              {!creating && (
                <form
                  className="flex flex-col gap-1"
                  onSubmit={(event) => {
                    event.preventDefault()
                    if (dryTemp.trim() !== '' && !INTEGER.test(dryTemp.trim())) return setError('Enter the drying temperature as a whole number of degrees.')
                    if (dryHours.trim() !== '' && !DECIMAL.test(dryHours.trim())) return setError('Enter the drying hours as a number, such as 8.')
                    const temp = dryTemp.trim() === '' ? null : Number(dryTemp)
                    const hours = dryHours.trim() === '' ? null : dryHours.trim()
                    void act(async () => { await addSpoolEvent(spoolKey, { kind: 'dried', temp_c: temp, hours }); setDryTemp(''); setDryHours(''); await reload(true) }, 'Drying recorded')
                  }}
                >
                  <p className="text-[13px]">
                    Last dried:{' '}
                    {saved?.last_dried_at
                      ? `${new Date(saved.last_dried_at).toLocaleDateString(undefined, { dateStyle: 'medium' })}${saved.last_dried_temp_c != null ? `, ${saved.last_dried_temp_c} °C` : ''}${saved.last_dried_hours != null ? ` for ${saved.last_dried_hours} h` : ''}`
                      : 'never'}
                  </p>
                  <div className="flex flex-wrap items-end gap-2">
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="spool-dry-temp" className="text-[12px]">
                        Drying temperature (°C)
                      </Label>
                      <Input id="spool-dry-temp" inputMode="numeric" className="w-24 tabular-nums" value={dryTemp} onChange={(e) => setDryTemp(e.target.value)} />
                    </div>
                    <div className="flex flex-col gap-1">
                      <Label htmlFor="spool-dry-hours" className="text-[12px]">
                        Hours
                      </Label>
                      <Input id="spool-dry-hours" inputMode="decimal" className="w-20 tabular-nums" value={dryHours} onChange={(e) => setDryHours(e.target.value)} />
                    </div>
                    <Button type="submit" size="sm" variant="outline" disabled={busy}>
                      Record drying
                    </Button>
                  </div>
                </form>
              )}
            </div>
          </Section>

          <Section id="history" title="History">
            {creating ? <p className="text-[13px] text-muted-foreground">The history starts when the spool is saved.</p> : <HistoryList events={events} />}
          </Section>
        </div>
      </div>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete spool #{saved?.number}?</AlertDialogTitle>
            <AlertDialogDescription>It never printed, so nothing keeps its cost. Its history goes with it. This cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep it</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  try {
                    await deleteSpool(spoolKey)
                  } catch (cause) {
                    // The refusal is shown on the page, not behind the dialog.
                    setConfirmDelete(false)
                    throw cause
                  }
                  setConfirmDelete(false)
                  guard.leaveTo(listPath)
                })
              }
            >
              Delete spool
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={guard.pending !== null} onOpenChange={(next) => !next && guard.stay()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Leave without saving?</AlertDialogTitle>
            <AlertDialogDescription>Your changes to this spool have not been saved, and will be lost. Save first to keep them.</AlertDialogDescription>
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
        <Link to={`/filament/spools${listBack(location.state)}`} className="mb-3 inline-flex items-center gap-1 text-[13px] text-muted-foreground hover:text-foreground">
          <ChevronLeftIcon className="size-3.5" /> Spools
        </Link>
        {children}
      </div>
    </div>
  )
}

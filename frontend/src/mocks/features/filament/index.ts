import { HttpResponse, http } from 'msw'
import type { components } from '../../../api/schema'
import type { FilamentInventorySpool, FilamentInventoryView, FilamentSpool } from '../../../api/types'
import { parseRgba } from '../../../lib/rgba'
// A module cycle (`orders/priceBook.ts` imports `inventoryRows` from here): use it only inside functions.
import { linkTypeFilament } from '../orders/priceBook'
import { H2C, seed, type Catalogs, type Seed, type SpoolRow } from './seed'

type S = components['schemas']

/**
 * #6262 — every `/api/v1/filament/*` route (`backend/scadbuddy/api/filament.py`) over the
 * in-memory seed (`seed.ts`), with `reset`. It works out what the backend works out: a
 * spool's effective values (its own override, else its type's), `overridden`, `names`,
 * `low_stock`, the tray a spool is in (its place's printer tray), the tag history, the
 * ledger's pages and the summary. It refuses what the routes refuse, with the same
 * status and text (`REFUSED`), and a priced type writes its price book row
 * (`orders/priceBook.ts` `linkTypeFilament`). `GET /filament/inventory` (Task 9) is
 * served here too, from the same spools: one route, one module.
 */

const BASE = '/api/v1/filament'

let state: Seed = seed()

export function reset() {
  state = seed()
  imports.clear()
}

/** The default low-stock percentage (`filament/settings.py`, F3). */
const LOW_STOCK_PCT = 20

const now = () => new Date().toISOString()

const TITLES = { 404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable Content', 428: 'Precondition Required' } as const

/** A refusal as `core/problems.py` `problem_response` writes it: every error here. */
const problem = (status: keyof typeof TITLES, detail: string) =>
  HttpResponse.json(
    { type: 'about:blank', title: TITLES[status], status, detail },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

/**
 * The fields each write body takes: every body model of `filament/models.py` extends
 * `_In` (`extra="forbid"`), so a key outside them is a 422. Read from the models' fields.
 */
const FIELDS = {
  preset: ['printer_model', 'nozzle_diameter', 'slicer_filament', 'slicer_filament_name'],
  kValue: ['printer_id', 'extruder', 'nozzle_diameter', 'nozzle_type', 'k_value', 'name', 'cali_idx', 'setting_id'],
  typeCreate: ['code', 'material_id', 'subtype_id', 'brand_id', 'category_id', 'effect_id', 'insert_type_id', 'rgba', 'colour_name', 'extra_colours', 'diameter_mm', 'density_g_cm3', 'cost_per_kg', 'label_weight_g', 'low_stock_pct', 'nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'note'],
  spoolCreate: ['type_id', 'brand_id', 'material_id', 'subtype_id', 'category_id', 'effect_id', 'colour_name', 'rgba', 'extra_colours', 'diameter_mm', 'density_g_cm3', 'nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'label_weight_g', 'insert_type_id', 'low_stock_pct', 'cost_per_kg', 'weight_locked', 'note', 'grams_left'],
  move: ['location_id'],
  csvImport: ['rows', 'dry_run'],
  archive: ['archived'],
  tag: ['tag_uid', 'tray_uuid', 'kind', 'override'],
  event: ['kind', 'source', 'at', 'grams', 'temp_c', 'hours', 'note'],
  locationCreate: ['name', 'kind_id', 'parent_id', 'identifier', 'capacity', 'printer_id', 'ams_id', 'tray_id', 'note', 'sort_order'],
} as const
const PATCH_FIELDS = { type: [...FIELDS.typeCreate, 'archived'], spool: [...FIELDS.spoolCreate.filter((field) => field !== 'grams_left'), 'archived'], location: [...FIELDS.locationCreate, 'archived'] }

/** The 422 for a body (or each row of a list body) that has a key `allowed` lacks, or `null`. */
function extraKeys(body: unknown, allowed: readonly string[]): Response | null {
  for (const row of Array.isArray(body) ? body : [body]) {
    if (row === null || typeof row !== 'object') continue
    const extra = Object.keys(row).find((field) => !allowed.includes(field))
    if (extra) return problem(422, `${extra}: Extra inputs are not permitted`)
  }
  return null
}

/** The fields a spool may override, as `EffectiveValues` lists them. */
const OVERRIDABLE = [
  'brand_id',
  'material_id',
  'subtype_id',
  'category_id',
  'effect_id',
  'colour_name',
  'rgba',
  'extra_colours',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'insert_type_id',
  'low_stock_pct',
  'cost_per_kg',
] as const satisfies readonly (keyof S['EffectiveValues'] & keyof SpoolRow)[]

const nameOf = <K extends keyof Catalogs>(kind: K, id: string | null | undefined) =>
  id ? (state.catalogs[kind] as { id: string; name: string }[]).find((row) => row.id === id)?.name ?? null : null

function view(row: SpoolRow): FilamentSpool {
  const type = state.types.find((one) => one.id === row.type_id)
  const effective: S['EffectiveValues'] = {}
  const overridden: string[] = []
  for (const field of OVERRIDABLE) {
    const own = row[field]
    const inherited = type ? (type[field] as unknown) : null
    if (own !== null && own !== undefined) {
      ;(effective as Record<string, unknown>)[field] = own
      if (type) overridden.push(field)
    } else (effective as Record<string, unknown>)[field] = inherited ?? null
  }
  const label = effective.label_weight_g
  const pct = effective.low_stock_pct ?? LOW_STOCK_PCT
  return {
    ...row,
    effective,
    overridden,
    names: {
      brand: nameOf('brands', effective.brand_id),
      material: nameOf('materials', effective.material_id),
      subtype: nameOf('subtypes', effective.subtype_id),
      category: nameOf('categories', effective.category_id),
      effect: nameOf('effects', effective.effect_id),
      insert_type: nameOf('insert-types', effective.insert_type_id),
      location: state.locations.find((place) => place.id === row.location_id)?.name ?? null,
    },
    low_stock: row.grams_left != null && label != null ? row.grams_left < (label * pct) / 100 : false,
  }
}

const findSpool = (id: string) => state.spools.find((row) => row.id === id)

function listed(includeArchived: boolean) {
  return state.spools.filter((row) => includeArchived || !row.archived_at).sort((a, b) => a.number - b.number)
}

function trayOf(row: SpoolRow): S['TrayPlace'] | null {
  const place = state.locations.find((one) => one.id === row.location_id)
  if (!place?.printer_id || place.ams_id == null || place.tray_id == null) return null
  return { ...H2C, printer_id: place.printer_id, ams_id: place.ams_id, tray_id: place.tray_id }
}

function effectivePresets(spoolId: string): S['EffectivePreset'][] {
  const row = findSpool(spoolId)
  const own = state.spoolPresets.filter((one) => one.spool_id === spoolId)
  const key = (one: { printer_model: string; nozzle_diameter: string }) => `${one.printer_model}|${one.nozzle_diameter}`
  const owned = new Set(own.map(key))
  const inherited = state.typePresets.filter((one) => one.type_id === row?.type_id && !owned.has(key(one)))
  return [
    ...own.map(({ spool_id: _, migrated, ...rest }) => ({ ...rest, overridden: true, migrated: migrated === true })),
    ...inherited.map(({ type_id: _, ...rest }) => ({ ...rest, overridden: false, migrated: false })),
  ]
}

function effectiveKValues(spoolId: string): S['EffectiveKValue'][] {
  const row = findSpool(spoolId)
  const own = state.spoolKValues.filter((one) => one.spool_id === spoolId)
  const key = (one: { printer_id: string; extruder: number; nozzle_diameter: string }) => `${one.printer_id}|${one.extruder}|${one.nozzle_diameter}`
  const owned = new Set(own.map(key))
  const inherited = state.typeKValues.filter((one) => one.type_id === row?.type_id && !owned.has(key(one)))
  return [
    ...own.map(({ spool_id: _, ...rest }) => ({ ...rest, overridden: true })),
    ...inherited.map(({ type_id: _, ...rest }) => ({ ...rest, overridden: false })),
  ]
}

const tagById = (id: string) => state.tags.find((tag) => tag.id === id)
const openBinding = (tagId: string) => state.bindings.find((one) => one.tag_id === tagId && !one.unbound_at)
const numberOf = (spoolId: string) => findSpool(spoolId)?.number ?? null

function detail(row: SpoolRow): S['SpoolDetail'] {
  const current = state.bindings.find((one) => one.spool_id === row.id && !one.unbound_at)
  const last = state.bindings
    .filter((one) => one.spool_id === row.id && one.unbound_at)
    .sort((a, b) => (b.unbound_at ?? '').localeCompare(a.unbound_at ?? ''))[0]
  const lastTag = last ? tagById(last.tag_id) : undefined
  const ledger = state.events.filter((event) => event.spool_id === row.id).sort((a, b) => a.at.localeCompare(b.at) || a.seq - b.seq)
  const anchor = [...ledger].reverse().find((event) => event.kind === 'weighed' || event.kind === 'created') ?? null
  const deltas = anchor ? ledger.filter((event) => event.at > anchor.at && event.delta_g != null) : []
  return {
    ...view(row),
    tag: current ? (tagById(current.tag_id) ?? null) : null,
    last_tag:
      last && lastTag
        ? { tag: lastTag, unbound_at: last.unbound_at!, reason: last.reason, held_by: numberOf(openBinding(lastTag.id)?.spool_id ?? '') }
        : null,
    presets: effectivePresets(row.id),
    k_values: effectiveKValues(row.id),
    basis: { anchor, deltas },
    in_use: ledger.some((event) => event.kind === 'print_started' || event.kind === 'print_ended'),
  }
}

function addEvent(spoolId: string, kind: S['SpoolEvent']['kind'], values: Partial<S['SpoolEvent']> = {}): S['SpoolEvent'] {
  const event: S['SpoolEvent'] = {
    id: crypto.randomUUID(),
    spool_id: spoolId,
    at: now(),
    kind,
    source: 'person',
    printer_id: null,
    ams_id: null,
    tray_id: null,
    grams_before: null,
    grams_after: null,
    delta_g: null,
    ref_kind: null,
    ref_id: null,
    actor: 'mock@example.com',
    data: {},
    seq: state.events.reduce((top, row) => Math.max(top, row.seq), 0) + 1,
    ...values,
  }
  state.events.push(event)
  return event
}

/** A body's colours as the backend stores them: `#RRGGBBAA`, upper case. */
function normalised<T extends object>(body: T): Wire<T> {
  const out = { ...body } as Record<string, unknown>
  if (typeof out.rgba === 'string') out.rgba = parseRgba(out.rgba) ?? out.rgba
  if (Array.isArray(out.extra_colours)) out.extra_colours = out.extra_colours.map((one: string) => parseRgba(one) ?? one)
  for (const [key, value] of Object.entries(out)) if (typeof value === 'number' && DECIMALS.has(key)) out[key] = String(value)
  return out as Wire<T>
}

/** The `Decimal` fields: a body may send a number, and an answer carries a string. */
const DECIMALS = new Set(['diameter_mm', 'density_g_cm3', 'cost_per_kg', 'drying_hours', 'last_dried_hours', 'k_value', 'hours'])

/** A body as the store keeps it: a `Decimal` field (`number | string` in) as its string. */
type Wire<T> = { [K in keyof T]: [Extract<T[K], string>] extends [never] ? T[K] : Exclude<T[K], number> }

/** Applies `archived` as the routes do: it sets or clears `archived_at`. */
function patched<T extends { archived_at: string | null }>(row: T, body: Record<string, unknown>): T {
  const { archived, ...rest } = normalised(body)
  const next = { ...row, ...rest, updated_at: now() } as T
  if (archived === true && !row.archived_at) next.archived_at = now()
  if (archived === false) next.archived_at = null
  return next
}

function tree(includeArchived: boolean): S['LocationNode'][] {
  const shown = state.locations.filter((place) => includeArchived || !place.archived_at)
  const ids = new Set(shown.map((place) => place.id))
  const order = (a: S['Location'], b: S['Location']) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)
  const node = (place: S['Location']): S['LocationNode'] => ({
    ...place,
    spool_count: state.spools.filter((row) => row.location_id === place.id && !row.archived_at).length,
    children: shown.filter((child) => child.parent_id === place.id).sort(order).map(node),
  })
  return shown.filter((place) => !place.parent_id || !ids.has(place.parent_id)).sort(order).map(node)
}

/** `GET /filament/inventory`'s rows, from the same spools: for the orders and settings mocks too. */
export function inventoryRows(includeArchived = false): FilamentInventorySpool[] {
  return listed(includeArchived).map((row) => ({
    spool: view(row),
    loaded: trayOf(row),
    bb_spool_id: state.bbLinks.get(row.id) ?? null,
    presets: [],
  }))
}

function summary(): S['InventorySummary'] {
  const rows = listed(false).map(view)
  const grams = (spool: FilamentSpool) => spool.grams_left ?? 0
  const priced = rows.filter((spool) => spool.effective?.cost_per_kg != null)
  const byMaterial = new Map<string | null, { spools: number; grams_left: number }>()
  for (const spool of rows) {
    const name = spool.names?.material ?? null
    const total = byMaterial.get(name) ?? { spools: 0, grams_left: 0 }
    byMaterial.set(name, { spools: total.spools + 1, grams_left: total.grams_left + grams(spool) })
  }
  return {
    spools: rows.length,
    grams_left: rows.reduce((sum, spool) => sum + grams(spool), 0),
    value_left: priced.length
      ? priced.reduce((sum, spool) => sum + (grams(spool) / 1000) * Number(spool.effective?.cost_per_kg), 0).toFixed(2)
      : null,
    by_material: [...byMaterial.entries()]
      .map(([material, total]) => ({ material, ...total }))
      .sort((a, b) => b.grams_left - a.grams_left),
    loaded: rows.filter((spool) => state.locations.find((place) => place.id === spool.location_id)?.printer_id).length,
    low_stock: rows.filter((spool) => spool.low_stock).length,
    needs_type: rows.filter((spool) => !spool.type_id).length,
  }
}

const flag = (request: Request, name: string) => new URL(request.url).searchParams.get(name) === 'true'

// -- the backend's refusals -----------------------------------------------------------

/** Each refusal's text, as the store's error says it (`filament/*.py`). */
const REFUSED = {
  catalogExists: 'a row of this catalog already has that name',
  catalogRef: 'a row this one points at does not exist',
  typeless: 'a spool needs a type_id or a material_id',
  inUse: 'the spool has prints in its ledger; archive it instead',
  tagUnnamed: 'the tag has no tag_uid or tray_uuid once cleaned',
  tagKeysClash: 'the tag_uid and the tray_uuid name two different tags',
  rowsDuplicate: 'two rows name the same printer and nozzle',
  locationExists: 'a place with that name or that tray already exists',
  locationCycle: 'a place cannot be its own parent or sit under one of its own',
  moveTarget: 'the place to move to does not exist or is archived',
  badCursor: 'the cursor is not one this ledger gave',
} as const

/** Where an id field points. */
type Ref = keyof Catalogs | 'types' | 'locations'

const SPOOL_REFS: Record<string, Ref> = {
  type_id: 'types',
  brand_id: 'brands',
  material_id: 'materials',
  subtype_id: 'subtypes',
  category_id: 'categories',
  effect_id: 'effects',
  insert_type_id: 'insert-types',
}
const { type_id: _typeRef, ...TYPE_REFS } = SPOOL_REFS
const CATALOG_REFS: Partial<Record<keyof Catalogs, Record<string, Ref>>> = {
  subtypes: { material_id: 'materials' },
  colours: { material_id: 'materials', effect_id: 'effects' },
  'insert-types': { insert_material_id: 'insert-materials' },
}
const LOCATION_REFS: Record<string, Ref> = { kind_id: 'location-kinds', parent_id: 'locations' }

function rowsOf(where: Ref): { id: string }[] {
  if (where === 'types') return state.types
  if (where === 'locations') return state.locations
  return state.catalogs[where]
}

/** A field that names a row that does not exist: the foreign key's `CatalogRefError`. */
const badRef = (values: Record<string, unknown>, refs: Record<string, Ref>) =>
  Object.entries(refs).some(([field, where]) => values[field] != null && !rowsOf(where).some((row) => row.id === values[field]))

/** `check_temps`: the text of the first range whose minimum is above its maximum. */
function badTemps(values: Record<string, unknown>): string | null {
  for (const kind of ['nozzle', 'bed']) {
    const low = values[`${kind}_temp_min`]
    const high = values[`${kind}_temp_max`]
    if (typeof low === 'number' && typeof high === 'number' && low > high) return `${kind}_temp_min is above ${kind}_temp_max`
  }
  return null
}

/** Pydantic's answer for a required field left out or blank. */
function missing(values: Record<string, unknown>, fields: readonly string[]): string | null {
  const field = fields.find((name) => values[name] == null || (typeof values[name] === 'string' && !String(values[name]).trim()))
  return field ? `${field}: Field required` : null
}

/** `_Patch`'s answer for a NOT NULL field sent as `null`. */
function cleared(body: Record<string, unknown>, fields: readonly string[]): string | null {
  const field = [...fields, 'archived'].find((name) => name in body && body[name] === null)
  return field ? `${field} cannot be cleared` : null
}

/** The fields a catalog kind's create and patch take (`extra="forbid"`), from its seed row. */
const CATALOG_FIELDS = Object.fromEntries(
  Object.entries(seed().catalogs).map(([kind, rows]) => [
    kind,
    new Set(Object.keys(rows[0]!).filter((field) => !['id', 'created_at', 'updated_at', 'archived_at', 'bb_catalog_id'].includes(field))),
  ]),
) as Record<keyof Catalogs, Set<string>>

const sameName = (a: string, b: unknown) => typeof b === 'string' && a.trim().toUpperCase() === b.trim().toUpperCase()

/** What a catalog row would refuse, or `null`. */
function catalogRefusal(kind: keyof Catalogs, values: Record<string, unknown>, body: Record<string, unknown>, self?: string): Response | null {
  const extra = Object.keys(body).find((field) => field !== 'archived' && !CATALOG_FIELDS[kind].has(field))
  if (extra) return problem(422, `${extra}: Extra inputs are not permitted`)
  const absent = missing(values, ['name'])
  if (absent) return problem(422, absent)
  if (badRef(values, CATALOG_REFS[kind] ?? {})) return problem(422, REFUSED.catalogRef)
  const temps = kind === 'materials' || kind === 'subtypes' ? badTemps(values) : null
  if (temps) return problem(422, temps)
  // The name indexes are `WHERE archived_at IS NULL`: an archived row holds no name, and
  // restoring one whose name a live row took since is refused.
  if (values.archived_at) return null
  const rows = state.catalogs[kind] as {
    id: string
    name: string
    archived_at: string | null
    material_id?: string | null
    manufacturer?: string | null
  }[]
  const taken = rows.some(
    (row) =>
      row.id !== self &&
      !row.archived_at &&
      sameName(row.name, values.name) &&
      // `filament_subtypes_name`: a subtype's name is unique within its material.
      (kind !== 'subtypes' || (row.material_id ?? null) === (values.material_id ?? null)) &&
      // `filament_colours_name`: a colour's name is unique per manufacturer (none is one).
      (kind !== 'colours' || sameName(row.manufacturer ?? '', values.manufacturer ?? '')),
  )
  return taken ? problem(409, REFUSED.catalogExists) : null
}

/** What a spool type would refuse, or `null`. */
function typeRefusal(values: Record<string, unknown>, self?: string): Response | null {
  const absent = missing(values, ['code', 'material_id', 'rgba', 'label_weight_g'])
  if (absent) return problem(422, absent)
  const temps = badTemps(values)
  if (temps) return problem(422, temps)
  if (badRef(values, TYPE_REFS)) return problem(422, REFUSED.catalogRef)
  // `filament_spool_types_code`: upper(code), live types only.
  const taken = !values.archived_at && state.types.some((type) => type.id !== self && !type.archived_at && sameName(type.code, values.code))
  return taken ? problem(409, REFUSED.catalogExists) : null
}

/** The fields whose change rewrites a type's price book row (`types.py` `_PRICED`). */
const PRICED = ['cost_per_kg', 'material_id', 'subtype_id', 'brand_id', 'rgba']

/** `_link_price_book`: a priced type's book row, and its id as `filament_id`. */
function linkPriceBook(type: S['SpoolType']): S['SpoolType'] {
  if (type.cost_per_kg == null) return type
  const filamentId = linkTypeFilament({
    material: nameOf('materials', type.material_id) ?? '',
    subtype: nameOf('subtypes', type.subtype_id),
    brand: nameOf('brands', type.brand_id),
    effect: nameOf('effects', type.effect_id),
    colour_name: type.colour_name,
    rgba: type.rgba,
    extra_colours: type.extra_colours,
    cost_per_kg: type.cost_per_kg,
  })
  return { ...type, filament_id: filamentId }
}

/** What a spool would refuse once `values` are its own columns, or `null`. */
function spoolRefusal(values: SpoolRow): Response | null {
  if (!values.type_id && !values.material_id) return problem(422, REFUSED.typeless)
  if (badRef(values, SPOOL_REFS)) return problem(422, REFUSED.catalogRef)
  const temps = badTemps(view(values).effective ?? {})
  return temps ? problem(422, temps) : null
}

/** `_archive`: archiving closes the spool's open tag binding (reason `archived`). */
function setArchived(at: number, archived: boolean) {
  const row = state.spools[at]!
  if ((row.archived_at !== null) === archived) return
  if (archived) {
    const open = state.bindings.find((one) => one.spool_id === row.id && !one.unbound_at)
    if (open) {
      Object.assign(open, { unbound_at: now(), reason: 'archived' })
      addEvent(row.id, 'tag_unbound', { data: { tag_id: open.tag_id, reason: 'archived' } })
    }
  }
  state.spools[at] = { ...state.spools[at]!, archived_at: archived ? now() : null, updated_at: now() }
  addEvent(row.id, archived ? 'archived' : 'restored')
}

/** `spools.models` `clean_tag_uid` and `clean_tray_uuid`: hex digits only, zeros are none. */
const hexOf = (value: string | null | undefined) => (value ?? '').replace(/[^0-9a-fA-F]/g, '').toUpperCase()
const keyOf = (text: string) => (!text || /^0+$/.test(text) ? null : text)

function closeBinding(binding: S['TagBinding'], reason: string) {
  Object.assign(binding, { unbound_at: now(), reason })
  addEvent(binding.spool_id, 'tag_unbound', { data: { tag_id: binding.tag_id, reason } })
}

/** The rows of a list that name one key twice: `TypeRowsDuplicateError`. */
const duplicated = <T>(rows: T[], key: (row: T) => string) => new Set(rows.map(key)).size !== rows.length

/**
 * `filament_locations_name` (a name once under a parent, among places that are no
 * printer's tray) and `filament_locations_tray` (one place per printer tray), both over
 * live places only.
 */
const placeTaken = (values: Record<string, unknown>, self?: string) =>
  state.locations.some(
    (place) =>
      place.id !== self &&
      !place.archived_at &&
      ((place.printer_id == null &&
        values.printer_id == null &&
        sameName(place.name, values.name) &&
        (place.parent_id ?? null) === (values.parent_id ?? null)) ||
        (values.printer_id != null &&
          place.printer_id === values.printer_id &&
          place.ams_id === values.ams_id &&
          place.tray_id === values.tray_id)),
  )

/** Whether `parent` is `id` or sits under it. */
function under(parent: string, id: string): boolean {
  for (let at: string | null | undefined = parent; at; at = state.locations.find((place) => place.id === at)?.parent_id) {
    if (at === id) return true
  }
  return false
}

/** A spool's new own columns with defaults, from a create body. */
function newSpool(body: Record<string, unknown>): SpoolRow {
  const blank: SpoolRow = {
    id: crypto.randomUUID(),
    number: Math.max(0, ...state.spools.map((row) => row.number)) + 1,
    type_id: null,
    brand_id: null,
    material_id: null,
    subtype_id: null,
    category_id: null,
    effect_id: null,
    colour_name: null,
    rgba: null,
    extra_colours: null,
    diameter_mm: null,
    density_g_cm3: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    bed_temp_min: null,
    bed_temp_max: null,
    label_weight_g: null,
    insert_type_id: null,
    low_stock_pct: null,
    cost_per_kg: null,
    location_id: null,
    weight_locked: false,
    note: null,
    grams_left: null,
    last_weighed_g: null,
    last_weighed_at: null,
    last_dried_at: null,
    last_dried_temp_c: null,
    last_dried_hours: null,
    last_used_at: null,
    archived_at: null,
    created_at: now(),
    updated_at: now(),
  }
  return { ...blank, ...(body as Partial<SpoolRow>) }
}

// -- the CSV import (`filament/csv_import.py`, #6262 Task 15) ------------------------

/** `crud/idempotency.py`: each write's key and the body and answer it had. */
const imports = new Map<string, { body: string; answer: S['ImportPreview'] }>()

/** `csv_import.DERIVED_COLUMNS`: dropped from a row. */
const DERIVED_CSV = new Set(['grams_left', 'last_weighed_g', 'last_weighed_at', 'last_dried_at', 'last_used_at', 'low_stock', 'archived_at'])
const CSV_CATALOGS = { material: 'materials', subtype: 'subtypes', brand: 'brands', category: 'categories', effect: 'effects' } as const
type CsvCatalog = keyof typeof CSV_CATALOGS
const CSV_INTS = new Set(['nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'label_weight_g', 'low_stock_pct'])
const CSV_DECIMALS = new Set(['diameter_mm', 'density_g_cm3', 'cost_per_kg'])
const CSV_PLAIN = [
  'colour_name',
  'rgba',
  'extra_colours',
  'diameter_mm',
  'density_g_cm3',
  'nozzle_temp_min',
  'nozzle_temp_max',
  'bed_temp_min',
  'bed_temp_max',
  'label_weight_g',
  'low_stock_pct',
  'cost_per_kg',
  'weight_locked',
  'note',
] as const
const CSV_COLUMNS = new Set<string>(['number', 'type', ...Object.keys(CSV_CATALOGS), 'insert_type', 'location', ...CSV_PLAIN])
const TRUE = new Set(['true', '1', 'yes', 'y', 'on', 't'])
const FALSE = new Set(['false', '0', 'no', 'n', 'off', 'f'])
const BAD_COLOUR = 'Value error, a colour is RRGGBB or RRGGBBAA hex digits, with or without #'

class RowError extends Error {}

function fail(message: string): never {
  throw new RowError(message)
}

/** A plain cell typed as `SpoolPatch` types it; a bad one is the row's error. */
function csvValue(field: (typeof CSV_PLAIN)[number], text: string): unknown {
  if (field === 'rgba') return parseRgba(text) ?? fail(`rgba: ${BAD_COLOUR}`)
  if (field === 'extra_colours')
    return text
      .split(/[\s,;]+/)
      .filter(Boolean)
      .map((one) => parseRgba(one) ?? fail(`extra_colours: ${BAD_COLOUR}`))
  if (field === 'weight_locked') {
    const lower = text.toLowerCase()
    return TRUE.has(lower) ? true : FALSE.has(lower) ? false : fail('weight_locked: Input should be a valid boolean')
  }
  if (CSV_INTS.has(field)) {
    const value = Number(text)
    return Number.isInteger(value) ? value : fail(`${field}: Input should be a valid integer`)
  }
  if (CSV_DECIMALS.has(field)) return Number.isFinite(Number(text)) ? text : fail(`${field}: Input should be a valid decimal`)
  return text
}

type Named = { id: string; name: string; archived_at: string | null; material_id?: string | null }
const liveNamed = <T extends { name: string; archived_at: string | null }>(rows: T[], name: string) =>
  rows.filter((row) => !row.archived_at && sameName(row.name, name))

/** One row's plan, as `_plan` makes it; with `write`, done on `state`. */
function importRow(row: Record<string, string | null>, line: number, write: boolean): S['ImportRowResult'] {
  const cell = (name: string) => {
    const value = row[name]
    return typeof value === 'string' && value.trim() ? value.trim() : null
  }
  const plain: Record<string, unknown> = {}
  for (const field of CSV_PLAIN) {
    const text = cell(field)
    if (text !== null) plain[field] = csvValue(field, text)
  }
  let current: SpoolRow | undefined
  const number = cell('number')
  if (number !== null) {
    const match = /^#?(\d{1,9})$/.exec(number)
    if (!match) fail(`number: '${number}' is not a spool number`)
    current = state.spools.find((one) => one.number === Number(match[1]))
    if (!current) fail(`no spool #${Number(match[1])}`)
  } else if (cell('type') === null && cell('material') === null) fail('a new spool needs a type or a material')
  const shown = current ? view(current) : null
  const effective = (shown?.effective ?? {}) as Record<string, unknown>
  const changes: Record<string, unknown[]> = {}
  const values: Record<string, unknown> = {}
  const typeCode = cell('type')
  if (typeCode !== null) {
    const kind = state.types.find((one) => !one.archived_at && sameName(one.code, typeCode))
    if (!kind) fail(`no spool type with the code '${typeCode}'`)
    if (!current || current.type_id !== kind.id) {
      const was = current ? (state.types.find((one) => one.id === current.type_id)?.code ?? null) : null
      changes.type = [was, kind.code]
      values.type_id = kind.id
    }
  }
  const fresh: [CsvCatalog, string][] = []
  let material = (effective.material_id as string | null | undefined) ?? null
  for (const column of Object.keys(CSV_CATALOGS) as CsvCatalog[]) {
    const kind = CSV_CATALOGS[column]
    const name = cell(column)
    if (name === null) continue
    const before = shown?.names?.[column] ?? null
    if (before !== null && sameName(before, name)) continue
    const found = liveNamed(state.catalogs[kind] as Named[], name)
      .filter((one) => kind !== 'subtypes' || one.material_id === material || one.material_id == null)
      .sort((a, b) => Number(a.material_id == null) - Number(b.material_id == null))[0]
    if (column === 'material') material = found?.id ?? null
    if (found) {
      changes[column] = [before, found.name]
      values[`${column}_id`] = found.id
    } else {
      changes[column] = [before, name, { new: true }]
      fresh.push([column, name])
    }
  }
  const insert = cell('insert_type')
  const insertBefore = shown?.names?.insert_type ?? null
  if (insert !== null && !(insertBefore !== null && sameName(insertBefore, insert))) {
    const found = liveNamed(state.catalogs['insert-types'], insert)[0]
    if (!found) fail(`no insert type named '${insert}'; add it on the Catalog tab with its empty weight`)
    changes.insert_type = [insertBefore, found.name]
    values.insert_type_id = found.id
  }
  for (const [field, value] of Object.entries(plain)) {
    const own = field === 'weight_locked' || field === 'note'
    const before = !current ? null : own ? current[field as 'note'] : (effective[field] ?? null)
    if (current && JSON.stringify(before) === JSON.stringify(value)) continue
    if (current && CSV_DECIMALS.has(field) && before != null && Number(before) === Number(value)) continue
    changes[field] = [before, value]
    values[field] = value
  }
  const temps = badTemps({ ...effective, ...plain })
  if (temps) fail(temps)
  let place: string | null = null
  const location = cell('location')
  if (location !== null) {
    const found = liveNamed(state.locations, location)
    if (!found.length) fail(`no location named '${location}'`)
    if (found.length > 1) fail(`${found.length} locations are named '${location}'; rename one`)
    if (!current || current.location_id !== found[0]!.id) {
      place = found[0]!.id
      changes.location = [shown?.names?.location ?? null, found[0]!.name]
    }
  }
  const action = current ? 'update' : 'create'
  if (!write) return { line, action, number: current?.number ?? null, changes, error: null }
  for (const [column, name] of fresh) {
    const kind = CSV_CATALOGS[column]
    const blank = Object.fromEntries([...CATALOG_FIELDS[kind]].map((field) => [field, null]))
    const made = { ...blank, name, sort_order: 1000, id: crypto.randomUUID(), archived_at: null, created_at: now(), updated_at: now() }
    if (kind === 'subtypes') Object.assign(made, { material_id: (values.material_id as string | undefined) ?? material })
    ;(state.catalogs[kind] as unknown[]).push(made)
    values[`${column}_id`] = made.id
  }
  let target: SpoolRow
  if (!current) {
    target = newSpool(values)
    state.spools.push(target)
    addEvent(target.id, 'created', { source: null })
  } else {
    const was = current
    const edited = Object.fromEntries(
      Object.entries(values)
        .filter(([field, value]) => was[field as keyof SpoolRow] !== value)
        .map(([field, value]) => [field, [was[field as keyof SpoolRow], value]]),
    )
    target = { ...was, ...(values as Partial<SpoolRow>), updated_at: now() }
    state.spools[state.spools.indexOf(was)] = target
    if (Object.keys(edited).length) addEvent(target.id, 'edited', { data: { changes: edited } })
  }
  if (place !== null) {
    const at = state.spools.findIndex((one) => one.id === target.id)
    addEvent(target.id, 'moved', { data: { from: target.location_id, to: place } })
    state.spools[at] = { ...state.spools[at]!, location_id: place, updated_at: now() }
  }
  return { line, action, number: target.number, changes, error: null }
}

/** `import_on`: every row's plan; a write with any error leaves the state as it was. */
function importRows(rows: Record<string, string | null>[], write: boolean): S['ImportPreview'] | Response {
  const before = structuredClone(state)
  const results = rows.map((row, at): S['ImportRowResult'] => {
    try {
      return importRow(row, at + 2, write)
    } catch (error) {
      if (!(error instanceof RowError)) throw error
      return { line: at + 2, action: 'error', number: null, changes: {}, error: error.message }
    }
  })
  const errors = results.filter((one) => one.action === 'error')
  if (write && errors.length) {
    state = before
    return problem(422, errors.map((one) => `line ${one.line}: ${one.error}`).join('; '))
  }
  return { rows: results }
}

/** `CsvRow`'s before-validator: derived columns dropped; any other unknown column refused. */
function csvRows(body: { rows?: unknown }): Record<string, string | null>[] | Response {
  if (!Array.isArray(body.rows)) return problem(422, 'rows: Field required')
  const rows: Record<string, string | null>[] = []
  for (const [at, row] of (body.rows as Record<string, string | null>[]).entries()) {
    const kept = Object.fromEntries(Object.entries(row).filter(([name]) => !DERIVED_CSV.has(name)))
    const extra = Object.keys(kept).find((name) => !CSV_COLUMNS.has(name))
    if (extra) return problem(422, `rows.${at}.${extra}: Extra inputs are not permitted`)
    rows.push(kept)
  }
  return rows
}

export const handlers = [
  // -- catalogs --
  http.get(`${BASE}/catalog/:kind`, ({ params, request }) => {
    const rows = state.catalogs[params.kind as keyof Catalogs] as { archived_at: string | null; sort_order: number; name: string }[] | undefined
    if (!rows) return problem(404, `no catalog ${String(params.kind)}`)
    const archived = flag(request, 'include_archived')
    return HttpResponse.json(
      rows.filter((row) => archived || !row.archived_at).sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)),
    )
  }),
  http.post(`${BASE}/catalog/:kind`, async ({ params, request }) => {
    const kind = params.kind as keyof Catalogs
    if (!(kind in state.catalogs)) return problem(404, `no catalog ${String(kind)}`)
    const body = normalised((await request.json()) as Record<string, unknown>)
    const refused = catalogRefusal(kind, body, body)
    if (refused) return refused
    const blank = Object.fromEntries([...CATALOG_FIELDS[kind]].map((field) => [field, null]))
    const made = { ...blank, sort_order: 0, ...body, id: crypto.randomUUID(), archived_at: null, created_at: now(), updated_at: now() }
    ;(state.catalogs[kind] as unknown[]).push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.patch(`${BASE}/catalog/:kind/:id`, async ({ params, request }) => {
    const kind = params.kind as keyof Catalogs
    if (!(kind in state.catalogs)) return problem(404, `no catalog ${String(kind)}`)
    const rows = state.catalogs[kind] as { id: string; archived_at: string | null }[]
    const at = rows.findIndex((row) => row.id === params.id)
    if (at < 0) return problem(404, `no ${String(kind)} row ${String(params.id)}`)
    const body = (await request.json()) as Record<string, unknown>
    const clear = cleared(body, ['name', 'sort_order'])
    if (clear) return problem(422, clear)
    const next = patched(rows[at]!, body)
    const refused = catalogRefusal(kind, next, body, rows[at]!.id)
    if (refused) return refused
    rows[at] = next
    return HttpResponse.json(next)
  }),

  // -- spool types --
  http.get(`${BASE}/types`, ({ request }) => {
    const archived = flag(request, 'include_archived')
    return HttpResponse.json(state.types.filter((type) => archived || !type.archived_at).sort((a, b) => a.code.localeCompare(b.code)))
  }),
  http.post(`${BASE}/types`, async ({ request }) => {
    const raw = await request.json()
    const extra = extraKeys(raw, FIELDS.typeCreate)
    if (extra) return extra
    const body = normalised(raw as S['SpoolTypeCreate'])
    const refused = typeRefusal(body)
    if (refused) return refused
    const made = linkPriceBook({
      brand_id: null,
      subtype_id: null,
      category_id: null,
      effect_id: null,
      colour_name: null,
      extra_colours: null,
      density_g_cm3: null,
      nozzle_temp_min: null,
      nozzle_temp_max: null,
      bed_temp_min: null,
      bed_temp_max: null,
      insert_type_id: null,
      low_stock_pct: null,
      cost_per_kg: null,
      note: null,
      ...body,
      id: crypto.randomUUID(),
      filament_id: null,
      archived_at: null,
      created_at: now(),
      updated_at: now(),
    })
    state.types.push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.get(`${BASE}/types/:id`, ({ params }) => {
    const type = state.types.find((one) => one.id === params.id)
    if (!type) return problem(404, `no spool type ${String(params.id)}`)
    return HttpResponse.json({
      ...type,
      presets: state.typePresets.filter((one) => one.type_id === type.id),
      k_values: state.typeKValues.filter((one) => one.type_id === type.id),
    } satisfies S['SpoolTypeDetail'])
  }),
  http.patch(`${BASE}/types/:id`, async ({ params, request }) => {
    const at = state.types.findIndex((one) => one.id === params.id)
    if (at < 0) return problem(404, `no spool type ${String(params.id)}`)
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, PATCH_FIELDS.type)
    if (extra) return extra
    const clear = cleared(body, ['code', 'material_id', 'rgba', 'diameter_mm', 'label_weight_g'])
    if (clear) return problem(422, clear)
    const next = patched(state.types[at]!, body)
    const refused = typeRefusal(next, next.id)
    if (refused) return refused
    state.types[at] = PRICED.some((field) => field in body) ? linkPriceBook(next) : next
    return HttpResponse.json(state.types[at])
  }),
  http.put(`${BASE}/types/:id/presets`, async ({ params, request }) => {
    const typeId = String(params.id)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.preset)
    if (extra) return extra
    const rows = (sent as S['TypePresetCreate'][]).map((one) => ({ slicer_filament_name: null, ...one, type_id: typeId }))
    if (duplicated(rows, (row) => `${row.printer_model}|${row.nozzle_diameter}`)) return problem(422, REFUSED.rowsDuplicate)
    if (!state.types.some((one) => one.id === typeId)) return problem(404, `no spool type ${typeId}`)
    state.typePresets = [...state.typePresets.filter((one) => one.type_id !== typeId), ...rows]
    return HttpResponse.json(rows)
  }),
  http.put(`${BASE}/types/:id/k-values`, async ({ params, request }) => {
    const typeId = String(params.id)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.kValue)
    if (extra) return extra
    const rows = (sent as S['TypeKValueCreate'][]).map((one) => ({
      nozzle_type: null,
      name: null,
      cali_idx: null,
      setting_id: null,
      ...one,
      k_value: String(one.k_value),
      type_id: typeId,
    }))
    if (duplicated(rows, (row) => `${row.printer_id}|${row.extruder}|${row.nozzle_diameter}`)) return problem(422, REFUSED.rowsDuplicate)
    if (!state.types.some((one) => one.id === typeId)) return problem(404, `no spool type ${typeId}`)
    state.typeKValues = [...state.typeKValues.filter((one) => one.type_id !== typeId), ...rows]
    return HttpResponse.json(rows)
  }),

  // -- spools --
  http.get(`${BASE}/spools`, ({ request }) => {
    const search = new URL(request.url).searchParams
    const needsType = search.get('needs_type')
    return HttpResponse.json(
      listed(flag(request, 'include_archived'))
        .filter((row) => needsType === null || (needsType === 'true') === !row.type_id)
        .map(view),
    )
  }),
  http.post(`${BASE}/spools`, async ({ request }) => {
    const raw = await request.json()
    const extra = extraKeys(raw, FIELDS.spoolCreate)
    if (extra) return extra
    const body = normalised(raw as S['SpoolCreate'])
    const made = newSpool(body)
    const refused = spoolRefusal(made)
    if (refused) return refused
    state.spools.push(made)
    addEvent(made.id, 'created', { source: made.grams_left != null ? 'person' : null, grams_after: made.grams_left })
    return HttpResponse.json(view(made), { status: 201 })
  }),
  http.post(`${BASE}/spools/import`, async ({ request }) => {
    const body = (await request.json()) as { rows?: unknown; dry_run?: unknown }
    const extra = extraKeys(body, FIELDS.csvImport)
    if (extra) return extra
    if (typeof body.dry_run !== 'boolean') return problem(422, 'dry_run: Field required')
    const rows = csvRows(body)
    if (rows instanceof Response) return rows
    if (body.dry_run) return HttpResponse.json(importRows(rows, false) as S['ImportPreview'])
    const key = request.headers.get('Idempotency-Key')
    if (!key) return problem(428, 'This write needs an Idempotency-Key header. Nothing was done.')
    const asked = JSON.stringify(body)
    const seen = imports.get(key)
    if (seen) return seen.body === asked ? HttpResponse.json(seen.answer) : problem(422, 'This Idempotency-Key was used for another request.')
    const done = importRows(rows, true)
    if (done instanceof Response) return done
    imports.set(key, { body: asked, answer: done })
    return HttpResponse.json(done)
  }),
  http.get(`${BASE}/spools/:id`, ({ params }) => {
    const row = findSpool(String(params.id))
    return row ? HttpResponse.json(detail(row)) : problem(404, `no spool ${String(params.id)}`)
  }),
  http.patch(`${BASE}/spools/:id`, async ({ params, request }) => {
    const at = state.spools.findIndex((row) => row.id === params.id)
    if (at < 0) return problem(404, `no spool ${String(params.id)}`)
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, PATCH_FIELDS.spool)
    if (extra) return extra
    const clear = cleared(body, ['weight_locked'])
    if (clear) return problem(422, clear)
    const { archived, ...fields } = normalised(body)
    const current = state.spools[at]!
    const next = { ...current, ...(fields as Partial<SpoolRow>) }
    const refused = spoolRefusal(next)
    if (refused) return refused
    const changes = Object.fromEntries(
      Object.keys(fields)
        .filter((field) => current[field as keyof SpoolRow] !== next[field as keyof SpoolRow])
        .map((field) => [field, [current[field as keyof SpoolRow], next[field as keyof SpoolRow]]]),
    )
    if (Object.keys(changes).length) {
      state.spools[at] = { ...next, updated_at: now() }
      // `presets.drop_migrated`: a spool's first type drops the presets its migration made.
      if (!current.type_id && next.type_id) state.spoolPresets = state.spoolPresets.filter((one) => !(one.spool_id === current.id && one.migrated))
      addEvent(current.id, 'edited', { data: { changes } })
    }
    if (typeof archived === 'boolean') setArchived(at, archived)
    return HttpResponse.json(view(state.spools[at]!))
  }),
  http.delete(`${BASE}/spools/:id`, ({ params }) => {
    const row = findSpool(String(params.id))
    if (!row) return problem(404, `no spool ${String(params.id)}`)
    if (detail(row).in_use) return problem(409, REFUSED.inUse)
    state.spools = state.spools.filter((one) => one.id !== row.id)
    state.events = state.events.filter((event) => event.spool_id !== row.id)
    state.bindings = state.bindings.filter((one) => one.spool_id !== row.id)
    state.bbLinks.delete(row.id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post(`${BASE}/spools/:id/archive`, async ({ params, request }) => {
    const at = state.spools.findIndex((row) => row.id === params.id)
    if (at < 0) return problem(404, `no spool ${String(params.id)}`)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.archive)
    if (extra) return extra
    const { archived = true } = sent as S['SpoolArchive']
    setArchived(at, archived)
    return HttpResponse.json(view(state.spools[at]!))
  }),
  http.post(`${BASE}/spools/:id/tags`, async ({ params, request }) => {
    const body = (await request.json()) as S['TagBind']
    const extra = extraKeys(body, FIELDS.tag)
    if (extra) return extra
    const uid = keyOf(hexOf(body.tag_uid).slice(-16))
    const tray = keyOf(hexOf(body.tray_uuid).slice(0, 32))
    if (uid === null && tray === null) return problem(422, REFUSED.tagUnnamed)
    const row = findSpool(String(params.id))
    if (!row) return problem(404, `no spool ${String(params.id)}`)
    const found = state.tags.filter((one) => (uid !== null && one.tag_uid === uid) || (tray !== null && one.tray_uuid === tray))
    if (found.length > 1) return problem(409, REFUSED.tagKeysClash)
    let tag = found[0]
    if (tag) {
      // The tag takes a key it lacked.
      tag.tag_uid ??= uid
      tag.tray_uuid ??= tray
    } else {
      tag = { id: crypto.randomUUID(), kind: body.kind ?? 'other', tag_uid: uid, tray_uuid: tray, raw_blob: null, note: null, created_at: now() }
      state.tags.push(tag)
    }
    const held = openBinding(tag.id)
    if (held?.spool_id === row.id) return HttpResponse.json(held, { status: 201 })
    if (held) closeBinding(held, 'rebound')
    const other = state.bindings.find((one) => one.spool_id === row.id && !one.unbound_at)
    if (other) closeBinding(other, 'rebound')
    const binding: S['TagBinding'] = { id: crypto.randomUUID(), tag_id: tag.id, spool_id: row.id, override: body.override ?? false, bound_at: now(), unbound_at: null, reason: null }
    state.bindings.push(binding)
    addEvent(row.id, 'tag_bound', { data: { tag_id: tag.id } })
    return HttpResponse.json(binding, { status: 201 })
  }),
  http.delete(`${BASE}/tags/:id/binding`, ({ params }) => {
    const binding = openBinding(String(params.id))
    if (!binding) return problem(404, `tag ${String(params.id)} is on no spool`)
    closeBinding(binding, 'unbound')
    return HttpResponse.json(binding)
  }),
  http.get(`${BASE}/spools/:id/presets`, ({ params }) =>
    findSpool(String(params.id)) ? HttpResponse.json(effectivePresets(String(params.id))) : problem(404, `no spool ${String(params.id)}`),
  ),
  http.put(`${BASE}/spools/:id/presets`, async ({ params, request }) => {
    const spoolId = String(params.id)
    if (!findSpool(spoolId)) return problem(404, `no spool ${spoolId}`)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.preset)
    if (extra) return extra
    // `set_presets`: a row sent back unchanged (same key and values) stays migrated; a new or changed one is the person's.
    const was = state.spoolPresets.filter((one) => one.spool_id === spoolId && one.migrated)
    const rows = (sent as S['SpoolPresetCreate'][]).map((one) => {
      const row = { slicer_filament_name: null, ...one, spool_id: spoolId }
      const same = was.some((old) => old.printer_model === row.printer_model && old.nozzle_diameter === row.nozzle_diameter && old.slicer_filament === row.slicer_filament && old.slicer_filament_name === row.slicer_filament_name)
      return same ? { ...row, migrated: true } : row
    })
    if (duplicated(rows, (row) => `${row.printer_model}|${row.nozzle_diameter}`)) return problem(422, REFUSED.rowsDuplicate)
    state.spoolPresets = [...state.spoolPresets.filter((one) => one.spool_id !== spoolId), ...rows]
    return HttpResponse.json(effectivePresets(spoolId))
  }),
  http.get(`${BASE}/spools/:id/k-values`, ({ params }) =>
    findSpool(String(params.id)) ? HttpResponse.json(effectiveKValues(String(params.id))) : problem(404, `no spool ${String(params.id)}`),
  ),
  http.put(`${BASE}/spools/:id/k-values`, async ({ params, request }) => {
    const spoolId = String(params.id)
    if (!findSpool(spoolId)) return problem(404, `no spool ${spoolId}`)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.kValue)
    if (extra) return extra
    const rows = (sent as S['SpoolKValueCreate'][]).map((one) => ({
      nozzle_type: null,
      name: null,
      cali_idx: null,
      setting_id: null,
      ...one,
      k_value: String(one.k_value),
      spool_id: spoolId,
    }))
    if (duplicated(rows, (row) => `${row.printer_id}|${row.extruder}|${row.nozzle_diameter}`)) return problem(422, REFUSED.rowsDuplicate)
    state.spoolKValues = [...state.spoolKValues.filter((one) => one.spool_id !== spoolId), ...rows]
    return HttpResponse.json(effectiveKValues(spoolId))
  }),
  // Oldest first, as the route answers (`api/filament.py` `list_spool_events`).
  http.get(`${BASE}/spools/:id/events`, ({ params }) => {
    const spoolId = String(params.id)
    if (!findSpool(spoolId)) return problem(404, `no spool ${spoolId}`)
    return HttpResponse.json(state.events.filter((event) => event.spool_id === spoolId).sort((a, b) => a.at.localeCompare(b.at) || a.seq - b.seq))
  }),
  http.post(`${BASE}/spools/:id/events`, async ({ params, request }) => {
    const at = state.spools.findIndex((row) => row.id === params.id)
    if (at < 0) return problem(404, `no spool ${String(params.id)}`)
    const raw = await request.json()
    const extra = extraKeys(raw, FIELDS.event)
    if (extra) return extra
    const body = normalised(raw as S['SpoolEventAdd'])
    if (body.kind === 'weighed' && body.grams == null) return problem(422, 'body: Value error, a weighing needs grams')
    const row = state.spools[at]!
    const when = body.at ?? now()
    if (body.kind === 'weighed') {
      state.spools[at] = { ...row, grams_left: body.grams ?? null, last_weighed_g: body.grams ?? null, last_weighed_at: when }
      return HttpResponse.json(
        addEvent(row.id, 'weighed', { at: when, source: body.source ?? 'person', grams_before: row.grams_left, grams_after: body.grams ?? null }),
        { status: 201 },
      )
    }
    state.spools[at] = { ...row, last_dried_at: when, last_dried_temp_c: body.temp_c ?? null, last_dried_hours: body.hours ?? null }
    return HttpResponse.json(addEvent(row.id, 'dried', { at: when, data: { temp_c: body.temp_c ?? null, hours: body.hours ?? null } }), { status: 201 })
  }),
  http.post(`${BASE}/spools/:id/move`, async ({ params, request }) => {
    const at = state.spools.findIndex((row) => row.id === params.id)
    if (at < 0) return problem(404, `no spool ${String(params.id)}`)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.move)
    if (extra) return extra
    const { location_id } = sent as S['SpoolMove']
    if (location_id && !state.locations.some((place) => place.id === location_id && !place.archived_at)) {
      return problem(422, REFUSED.moveTarget)
    }
    const row = state.spools[at]!
    if (row.location_id !== location_id) {
      state.spools[at] = { ...row, location_id, updated_at: now() }
      addEvent(row.id, 'moved', { data: { from: row.location_id, to: location_id } })
    }
    return HttpResponse.json(view(state.spools[at]!))
  }),

  // -- tags --
  http.get(`${BASE}/tags`, ({ request }) => {
    const unbound = flag(request, 'include_unbound')
    const rows: S['TagRow'][] = state.tags.map((tag) => {
      const binding = openBinding(tag.id) ?? null
      return { ...tag, binding, spool_number: binding ? numberOf(binding.spool_id) : null }
    })
    return HttpResponse.json(rows.filter((row) => unbound || row.binding))
  }),
  http.get(`${BASE}/tags/:id`, ({ params }) => {
    const tag = tagById(String(params.id))
    if (!tag) return problem(404, `no tag ${String(params.id)}`)
    const binding = openBinding(tag.id) ?? null
    return HttpResponse.json({
      ...tag,
      binding,
      spool_number: binding ? numberOf(binding.spool_id) : null,
      history: state.bindings.filter((one) => one.tag_id === tag.id).sort((a, b) => b.bound_at.localeCompare(a.bound_at)),
    } satisfies S['TagDetail'])
  }),

  // -- the ledger, the totals, the trays --
  http.get(`${BASE}/events`, ({ request }) => {
    const search = new URL(request.url).searchParams
    const limit = Math.min(500, Number(search.get('limit') ?? 100))
    const offset = Number(search.get('cursor') ?? 0)
    if (!Number.isInteger(offset) || offset < 0) return problem(422, REFUSED.badCursor)
    const since = search.get('since')
    const until = search.get('until')
    const rows = state.events
      .filter((event) => !search.get('spool_id') || event.spool_id === search.get('spool_id'))
      .filter((event) => !search.get('kind') || event.kind === search.get('kind'))
      .filter((event) => !search.get('source') || event.source === search.get('source'))
      .filter((event) => !since || Date.parse(event.at) >= Date.parse(since))
      .filter((event) => !until || Date.parse(event.at) < Date.parse(until))
      // Newest first, then booking order: a page boundary never splits ties two ways.
      .sort((a, b) => b.at.localeCompare(a.at) || b.id.localeCompare(a.id))
    const page = rows.slice(offset, offset + limit).map((event) => ({ ...event, spool_number: numberOf(event.spool_id) ?? 0 }))
    return HttpResponse.json({
      events: page,
      next_cursor: offset + limit < rows.length ? String(offset + limit) : null,
    } satisfies S['EventPage'])
  }),
  http.get(`${BASE}/summary`, () => HttpResponse.json(summary())),
  http.get(`${BASE}/inventory`, ({ request }) =>
    HttpResponse.json({ spools: inventoryRows(flag(request, 'include_archived')), trays: [] } satisfies FilamentInventoryView),
  ),
  http.get(`${BASE}/spools/:id/summary`, ({ params }) => {
    const row = findSpool(String(params.id))
    return row ? HttpResponse.json(spoolSummary(row)) : problem(404, `no spool ${String(params.id)}`)
  }),

  // -- places --
  http.get(`${BASE}/locations`, ({ request }) => HttpResponse.json(tree(flag(request, 'include_archived')))),
  http.post(`${BASE}/locations`, async ({ request }) => {
    const body = (await request.json()) as S['LocationCreate']
    const extra = extraKeys(body, FIELDS.locationCreate)
    if (extra) return extra
    const absent = missing(body, ['name', 'kind_id'])
    if (absent) return problem(422, absent)
    if (placeTaken(body)) return problem(409, REFUSED.locationExists)
    if (badRef(body, LOCATION_REFS)) return problem(422, REFUSED.catalogRef)
    const made: S['Location'] = {
      parent_id: null,
      identifier: null,
      capacity: null,
      printer_id: null,
      ams_id: null,
      tray_id: null,
      note: null,
      ...body,
      id: crypto.randomUUID(),
      archived_at: null,
      created_at: now(),
      updated_at: now(),
    }
    state.locations.push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.patch(`${BASE}/locations/:id`, async ({ params, request }) => {
    const at = state.locations.findIndex((place) => place.id === params.id)
    if (at < 0) return problem(404, `no location ${String(params.id)}`)
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, PATCH_FIELDS.location)
    if (extra) return extra
    const clear = cleared(body, ['name', 'sort_order', 'kind_id'])
    if (clear) return problem(422, clear)
    const self = state.locations[at]!.id
    if (typeof body.parent_id === 'string' && under(body.parent_id, self)) return problem(409, REFUSED.locationCycle)
    const next = patched(state.locations[at]!, body)
    if (!next.archived_at && placeTaken(next, self)) return problem(409, REFUSED.locationExists)
    if (badRef(next, LOCATION_REFS)) return problem(422, REFUSED.catalogRef)
    state.locations[at] = next
    return HttpResponse.json(next)
  }),

  // -- the pull: the mock has no Bambuddy spools to pull, so it changes nothing --
  http.post(`${BASE}/bambuddy/import`, () =>
    HttpResponse.json({
      created: 0,
      linked: 0,
      updated: 0,
      needs_type: 0,
      archived: 0,
      invalid: 0,
      catalog_failed: false,
      catalog_skipped: 0,
      created_ids: [],
      linked_ids: [],
      updated_ids: [],
      needs_type_ids: [],
      archived_ids: [],
      failed_bb_ids: [],
      invalid_bb_ids: [],
    } satisfies S['PullReport']),
  ),
]

/**
 * `GET /filament/spools/{id}/summary`: the spool's stock and price, and every unarchived
 * spool of the same effective material, subtype, brand and `#RRGGBBAA`.
 */
function spoolSummary(row: SpoolRow): S['FilamentSpoolSummary'] {
  const [spool] = inventoryRows(true).filter((one) => one.spool.id === row.id)
  const effective = spool!.spool.effective ?? {}
  const same = inventoryRows(false).filter(({ spool: other }) =>
    (['material_id', 'subtype_id', 'brand_id', 'rgba'] as const).every((field) => (other.effective?.[field] ?? null) === (effective[field] ?? null)),
  )
  const cost = effective.cost_per_kg ?? null
  const pricePerG = cost === null ? null : Number((Number(cost) / 1000).toFixed(6))
  return {
    spool: spool!,
    cost_per_kg: cost,
    price_per_g: pricePerG,
    value_left: pricePerG === null ? null : Number(((row.grams_left ?? 0) * pricePerG).toFixed(4)),
    grams: null,
    grams_cost: null,
    filament: {
      material: spool!.spool.names?.material ?? null,
      subtype: spool!.spool.names?.subtype ?? null,
      brand: spool!.spool.names?.brand ?? null,
      colour_name: effective.colour_name ?? null,
      rgba: effective.rgba ?? null,
      spool_count: same.length,
      spool_numbers: same.map((one) => one.spool.number),
      grams_left: same.reduce((sum, one) => sum + (one.spool.grams_left ?? 0), 0),
      loaded: same.flatMap((one) => (one.loaded ? [{ spool_id: one.spool.id, spool_number: one.spool.number, loaded: one.loaded }] : [])),
    },
  }
}


import { HttpResponse, http } from 'msw'
import { newId } from '../../newId'
import type { components } from '../../../api/schema'
import type { FilamentInventorySpool, FilamentInventoryView, FilamentSpool } from '../../../api/types'
import { divDecimal, mulDecimal } from '../../../lib/decimal'
import { parseRgba } from '../../../lib/rgba'
// Also a cycle (`handlers.ts` loads every feature): only inside functions.
import { mockSettings } from '../../handlers'
// A module cycle (`orders/priceBook.ts` imports `inventoryRows` from here): use it only inside functions.
import { linkTypeFilament } from '../orders/priceBook'
// Also a cycle (`filamentPurchasing.ts` calls `addInvoicedSpools`): only inside functions.
import { invoiceRefOf, typeSuppliersOf } from '../filamentPurchasing'
import { printersState } from '../printers'
import { H2C, seed, spoolId, type Catalogs, type Seed, type SpoolRow } from './seed'
import { jobOf } from '../../../components/filament/eventText'

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

/** #6714 — the slicer profiles the mock fills a type from, by preset id. */
const PROFILES: Record<string, { name: string; values: Partial<Record<FillField, number | string>>; cost_hint: string }> = {
  GFA00: { name: 'Bambu PLA Basic @BBL H2C 0.4 nozzle', values: { nozzle_temp_min: 190, nozzle_temp_max: 230, bed_temp_min: 35, bed_temp_max: 65, density_g_cm3: '1.26', diameter_mm: '1.75', drying_temp_c: 55, drying_hours: '8' }, cost_hint: '24.99' },
  GFG02: { name: 'Bambu PETG HF @BBL H2C 0.4 nozzle', values: { nozzle_temp_min: 230, nozzle_temp_max: 260, bed_temp_min: 70, bed_temp_max: 80, density_g_cm3: '1.25', diameter_mm: '1.75', drying_temp_c: 65, drying_hours: '8' }, cost_hint: '19.99' },
}

const FILL_FIELDS = ['nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'density_g_cm3', 'diameter_mm', 'drying_temp_c', 'drying_hours'] as const
type FillField = (typeof FILL_FIELDS)[number]

/** The type's H2C 0.4 mm preset, else any 0.4 mm one, else the first, and its profile (R-6714-1). */
function profileOf(type: S['SpoolType']) {
  const presets = state.typePresets.filter((one) => one.type_id === type.id)
  const at04 = presets.filter((one) => Number(one.nozzle_diameter) === 0.4)
  const preset = at04.find((one) => one.printer_model.toUpperCase() === 'H2C') ?? at04[0] ?? presets[0]
  if (!preset) return null
  const profile = PROFILES[preset.slicer_filament]
  return { preset, values: profile?.values ?? null, profile }
}

/** `TypeFiller.fill` (and `apply`) over the mock's types: empty fields only, a refresh compares. */
function fillType(type: S['SpoolType'], options: { refresh?: boolean; apply?: Partial<Record<FillField, number | string>>; kValues?: boolean }): S['TypeFill'] {
  const result: S['TypeFill'] = { type_id: type.id, code: type.code, filled: {}, previous: {}, skipped: {}, k_values: [], written: false }
  const found = profileOf(type)
  if (!found) return { ...result, reason: 'the type has no slicer preset' }
  if (!found.profile) return { ...result, reason: `Bambu Studio 02.08.02.61 has no filament preset ${found.preset.slicer_filament}` }
  result.source = { preset_id: found.preset.slicer_filament, name: found.profile.name, origin: 'system', printer_model: found.preset.printer_model, nozzle_diameter: found.preset.nozzle_diameter }
  result.cost_hint = found.profile.cost_hint
  const at = state.types.findIndex((one) => one.id === type.id)
  const current = type as unknown as Record<FillField, number | string | null>
  if (options.apply) {
    for (const field of Object.keys(options.apply)) result.previous[field] = current[field as FillField]
    state.types[at] = { ...type, ...options.apply } as S['SpoolType']
    return { ...result, filled: options.apply, written: Object.keys(options.apply).length > 0 }
  }
  for (const [field, value] of Object.entries(found.profile.values)) {
    const now = current[field as FillField]
    if (options.refresh ? String(now) !== String(value) : now === null) {
      result.filled[field] = value
      if (options.refresh) result.previous[field] = now
    }
  }
  if (!options.refresh && Object.keys(result.filled).length > 0) {
    state.types[at] = { ...type, ...result.filled } as S['SpoolType']
    result.written = true
  }
  return result
}

/**
 * #6714 — the K values a type's linked Bambuddy spools give (`TypeFiller._k_values`), by type
 * id: none until a test sets them with `setMockBambuddyKValues`.
 */
const bambuddyKValues = new Map<string, S['TypeKValueCreate'][]>()

export function setMockBambuddyKValues(typeId: string, rows: S['TypeKValueCreate'][]) {
  bambuddyKValues.set(typeId, rows)
}

/** What the fill would import: Bambuddy's K values while the type has none. */
function bambuddyKValuesOf(typeId: string): S['TypeKValueCreate'][] {
  return state.typeKValues.some((one) => one.type_id === typeId) ? [] : (bambuddyKValues.get(typeId) ?? [])
}

/** K value rows compared as the backend compares them (`_k_key`): in no order, `0.020` is `0.02`. */
function kKey(rows: readonly S['TypeKValueCreate'][]): string {
  const key = (one: S['TypeKValueCreate']) =>
    JSON.stringify([one.printer_id, one.extruder, String(Number(one.nozzle_diameter)), one.nozzle_type ?? '', String(Number(one.k_value)), one.name ?? '', one.cali_idx ?? -1, one.setting_id ?? ''])
  return JSON.stringify(rows.map(key).sort())
}

/** The fill's import from Bambuddy's spools says `profile` (R-6714-19). */
const asTypeKValues = (typeId: string, rows: S['TypeKValueCreate'][]): S['TypeKValue'][] =>
  rows.map((one) => ({ type_id: typeId, nozzle_type: null, name: null, cali_idx: null, setting_id: null, ...one, k_value: String(one.k_value), source: 'profile' }))

/** Writes ``rows`` as the type's K values into ``result`` (`TypeFiller._write_k`). */
function writeKValues(typeId: string, rows: S['TypeKValueCreate'][], result: S['TypeFill']): S['TypeFill'] {
  if (rows.length === 0) return result
  const saved = asTypeKValues(typeId, rows)
  state.typeKValues = [...state.typeKValues, ...saved]
  return { ...result, k_values: saved, written: true }
}

let state: Seed = seed()

type ColourSync = components['schemas']['ColourSync']
/** #6907: each source's newest run; empty until a sync runs. */
let colourSyncs: ColourSync[] = []
/** What a mock sync adds: a few colours of each source. */
const MOCK_SYNC_COLOURS = {
  bambu: [
    { name: 'Black', manufacturer: 'Bambu Lab', rgba: '#000000FF', material_name: 'PLA Basic' },
    { name: 'Translucent Yellow', manufacturer: 'Bambu Lab', rgba: '#F4EE2A80', material_name: 'PLA Translucent' },
  ],
  filamentcolors: [
    { name: 'Galaxy Black', manufacturer: 'Polymaker', rgba: '#1A1A1EFF', material_name: 'PLA' },
    { name: 'Clear Red', manufacturer: 'Prusament', rgba: '#D0102080', material_name: 'PETG' },
  ],
} as const

export function reset() {
  state = seed()
  colourSyncs = []
  bambuddyKValues.clear()
  imports.clear()
  invoicedLine.clear()
  fetches.clear()
}

/** The invoice line each spool `addInvoicedSpools` made was bought on (#6266). */
const invoicedLine = new Map<string, string>()

/** The default low-stock percentage (`filament/settings.py`, F3). */
const LOW_STOCK_PCT = 20

const now = () => new Date().toISOString()

const TITLES = { 404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable Content', 428: 'Precondition Required' } as const

/** A refusal as `core/problems.py` `problem_response` writes it: every error here. */
const problem = (status: keyof typeof TITLES, detail: string, extensions: object = {}) =>
  HttpResponse.json(
    { type: 'about:blank', title: TITLES[status], status, detail, ...extensions },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )

/**
 * The fields each write body takes: every body model of `filament/models.py` extends
 * `_In` (`extra="forbid"`), so a key outside them is a 422. Read from the models' fields.
 */
const FIELDS = {
  preset: ['printer_model', 'nozzle_diameter', 'slicer_filament', 'slicer_filament_name'],
  kValue: ['printer_id', 'extruder', 'nozzle_diameter', 'nozzle_type', 'k_value', 'name', 'cali_idx', 'setting_id'],
  typeCreate: ['code', 'material_id', 'subtype_id', 'brand_id', 'category_id', 'effect_id', 'insert_type_id', 'rgba', 'colour_name', 'extra_colours', 'diameter_mm', 'density_g_cm3', 'cost_per_kg', 'lead_time_days', 'safety_margin_days', 'label_weight_g', 'low_stock_pct', 'nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'drying_temp_c', 'drying_hours', 'tdfp_id', 'note'],
  spoolCreate: ['type_id', 'brand_id', 'material_id', 'subtype_id', 'category_id', 'effect_id', 'colour_name', 'rgba', 'extra_colours', 'diameter_mm', 'density_g_cm3', 'nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'drying_temp_c', 'drying_hours', 'label_weight_g', 'insert_type_id', 'low_stock_pct', 'cost_per_kg', 'weight_locked', 'note', 'grams_left'],
  move: ['location_id'],
  csvImport: ['rows', 'dry_run'],
  proposalPick: ['brand_id', 'material_id', 'subtype_id', 'rgba', 'label_weight_g', 'material_number', 'code', 'spool_ids'],
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
  'drying_temp_c',
  'drying_hours',
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
    // The invoice line it was bought on (#6700): a line may fill a spool with none.
    invoice_line_id: invoicedLine.get(row.id) ?? invoiceRefOf(row.id)?.line_id ?? null,
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
    needs_drying: needsDrying(row, effective.material_id),
  }
}

/** `hygroscopic AND max_hours_outside_dry IS NOT NULL AND hours_outside_dry > max` (§5 of the sensors plan, decision 8). */
function needsDrying(row: SpoolRow, materialId: string | null | undefined): boolean {
  const material = state.catalogs.materials.find((one) => one.id === materialId)
  const max = material?.max_hours_outside_dry
  return Boolean(material?.hygroscopic && max != null && row.hours_outside_dry > Number(max))
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
    ...inherited.map(({ type_id: _, source: _source, ...rest }) => ({ ...rest, overridden: false })),
  ]
}

const tagById = (id: string) => state.tags.find((tag) => tag.id === id)
/** What the NTAG write route (`rfid.ts`) asks of a spool: its Bambuddy link, and whether its open tag was bound with `override`. */
export const hasBambuddyLink = (spoolId: string) => state.bbLinks.has(spoolId)
export const holdsOverrideTag = (spoolId: string) => state.bindings.some((one) => one.spool_id === spoolId && !one.unbound_at && one.override)
/** `ntag/confirm` when Bambuddy holds the written tag: it becomes the spool's open tag (`rfid.ts`). */
export function bindWrittenNtag(spoolId: string, tagUid: string) {
  const row = findSpool(spoolId)
  if (!row) return
  const tag: S['Tag'] = { id: newId(), kind: 'ntag_opentag', tag_uid: tagUid, tray_uuid: null, raw_blob: null, note: null, created_at: now() }
  state.tags.push(tag)
  const open = state.bindings.find((one) => one.spool_id === spoolId && !one.unbound_at)
  if (open) closeBinding(open, 'rebound')
  state.bindings.push({ id: newId(), tag_id: tag.id, spool_id: spoolId, override: false, bound_at: now(), unbound_at: null, reason: null })
  addEvent(spoolId, 'tag_bound', { data: { tag_id: tag.id } })
}
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
    invoice: invoiceRefOf(row.id, invoicedLine.get(row.id)),
    type_suppliers: row.type_id ? typeSuppliersOf(row.type_id) : [],
    drying_defaults: dryingDefaults(row),
  }
}

/** `spools.py` `_drying_defaults` (#6720, R-6714-18): each field from the spool, else the type, else the subtype, else the material. */
function dryingDefaults(row: SpoolRow): S['DryingDefaults'] {
  const type = state.types.find((one) => one.id === row.type_id)
  const subtype = state.catalogs.subtypes.find((one) => one.id === (row.subtype_id ?? type?.subtype_id))
  const material = state.catalogs.materials.find((one) => one.id === (row.material_id ?? type?.material_id))
  const levels = [
    ['spool', row],
    ['type', type],
    ['subtype', subtype],
    ['material', material],
  ] as const
  const out: S['DryingDefaults'] = { temp_c: null, hours: null, temp_from: null, hours_from: null }
  for (const [level, source] of levels) {
    if (out.temp_c == null && source?.drying_temp_c != null) Object.assign(out, { temp_c: source.drying_temp_c, temp_from: level })
    if (out.hours == null && source?.drying_hours != null) Object.assign(out, { hours: source.drying_hours, hours_from: level })
  }
  return out
}

function addEvent(spoolId: string, kind: S['SpoolEvent']['kind'], values: Partial<S['SpoolEvent']> = {}): S['SpoolEvent'] {
  const event: S['SpoolEvent'] = {
    id: newId(),
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

/**
 * #6574 — the types not archived as the shop slice profile's options name them
 * (`api/storefront_slice.py` `_types`), by code: for the settings mock.
 */
export function profileTypes(): S['ProfileType'][] {
  return state.types
    .filter((type) => !type.archived_at)
    .sort((a, b) => a.code.localeCompare(b.code))
    .map((type) => {
      const material = nameOf('materials', type.material_id)
      return { id: type.id, code: type.code, material: material ?? '', label: [type.code, material, type.colour_name].filter(Boolean).join(' ') }
    })
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

/**
 * `GET /filament/inventory`'s trays: the H2C's, one per placed spool. Slot 4's spool (#8,
 * clear PETG) does not match what the tray reports, grey PETG, so it carries a
 * `mismatch` (#6713).
 */
function inventoryTrays(): S['PrinterTrays'][] {
  const trays = listed(false).flatMap((row): S['InventoryTray'][] => {
    const place = trayOf(row)
    if (!place) return []
    const spool = view(row)
    const odd = place.ams_id === 0 && place.tray_id === 3
    return [
      {
        ams_id: place.ams_id,
        tray_id: place.tray_id,
        label: `AMS-A slot ${place.tray_id + 1}`,
        material: odd ? 'PETG' : (spool.names?.material ?? null),
        rgba: odd ? '#A7AEB2FF' : (spool.effective?.rgba ?? null),
        spool_id: row.id,
        spool_number: row.number,
        found_by: 'link',
        mismatch: odd ? { reported_type: 'PETG', reported_rgba: '#A7AEB2FF' } : null,
      },
    ]
  })
  return trays.length ? [{ printer_id: H2C.printer_id, printer_name: H2C.printer_name, trays }] : []
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
    // Every spool's plates, archived ones too (plan Decision 18), by the one plate rule (`metrics.plates`).
    consumed_g: plateGrams(state.events),
  }
}

const flag = (request: Request, name: string) => new URL(request.url).searchParams.get(name) === 'true'

// -- the backend's refusals -----------------------------------------------------------

/** Each refusal's text, as the store's error says it (`filament/*.py`). */
/** The Home Assistant entities the mock's Bambuddy can read. */
const HA_ENTITIES: S['HaEntity'][] = [
  { entity_id: 'sensor.garage_humidity', friendly_name: 'Garage humidity', state: '47.5', domain: 'sensor', device_class: 'humidity', unit_of_measurement: '%' },
  { entity_id: 'sensor.garage_temperature', friendly_name: 'Garage temperature', state: '19.2', domain: 'sensor', device_class: 'temperature', unit_of_measurement: '°C' },
  { entity_id: 'sensor.attic_humidity', friendly_name: 'Attic humidity', state: 'unavailable', domain: 'sensor', device_class: 'humidity', unit_of_measurement: '%' },
]

const isAlerting = (sensor: S['Sensor']) =>
  sensor.last_value != null &&
  ((sensor.alert_above != null && sensor.last_value > sensor.alert_above) || (sensor.alert_below != null && sensor.last_value < sensor.alert_below))

/** A day of readings every 15 minutes ending at the sensor's last one, oldest first: a slow drift that ends at its value. */
function readingsOf(sensor: S['Sensor']): S['SensorReading'][] {
  if (sensor.last_value == null || !sensor.last_at) return []
  const end = Date.parse(sensor.last_at)
  return Array.from({ length: 96 }, (_, i) => {
    const back = 95 - i
    const wave = Math.sin(i / 9) * 0.04 * (back / 95)
    const drift = (back / 95) * 0.12
    return { at: new Date(end - back * 15 * 60_000).toISOString(), value: Math.round(sensor.last_value! * (1 - drift + wave) * 10) / 10 }
  })
}

const REFUSED = {
  tdfpId: 'tdfp_id: Input should be a whole number greater than 0 and less than 2147483648',
  catalogExists: 'a row of this catalog already has that name',
  catalogRef: 'a row this one points at does not exist',
  typeless: 'a spool needs a type_id or a material_id',
  inUse: 'the spool has prints in its ledger; archive it instead',
  tagUnnamed: 'the tag has no tag_uid or tray_uuid once cleaned',
  tagKeysClash: 'the tag_uid and the tray_uuid name two different tags',
  rowsDuplicate: 'two rows name the same printer and nozzle',
  locationExists: 'a place with that name, that tray or that AMS unit already exists',
  locationCycle: 'a place cannot be its own parent or sit under one of its own',
  moveTarget: "the place to move to does not exist, is archived or is an AMS unit's place",
  badCursor: 'the cursor is not one this ledger gave',
  sensorAms: 'an AMS sensor is made by ScadBuddy and cannot be added by hand',
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
    new Set(Object.keys(rows[0]!).filter((field) => !['id', 'created_at', 'updated_at', 'archived_at', 'bb_catalog_id', 'aliases', 'source', 'synced_at', 'bb_colour_id'].includes(field))),
  ]),
) as Record<keyof Catalogs, Set<string>>

/**
 * #6706 — the catalogs with aliases (`filament/catalog.py` `NameTable`) and the noun a
 * clash names (`<noun>_id`, `<noun>_name`). Their names and aliases share one key space
 * per catalog (a subtype's within its material), archived rows included.
 */
const ALIASED = { materials: 'material', subtypes: 'subtype', brands: 'brand', categories: 'category', effects: 'effect' } as const
type Aliased = keyof typeof ALIASED
const isAliased = (kind: string): kind is Aliased => kind in ALIASED
type AliasedRow = { id: string; name: string; aliases: string[]; material_id?: string | null }

/** `catalog_key`: `+` read as `PLUS` (R-6706-17), then the letters and digits, upper case. */
export const catalogKey = (name: string) =>
  name
    .replace(/\+/g, 'PLUS')
    .replace(/[^\p{L}\p{N}]/gu, '')
    .toUpperCase()

/** The row of `kind` that holds `name`'s key in `scope` (a subtype's material), but `self`'s own names. */
function keyHolder(kind: Aliased, name: string, scope: string | null, self?: string, skipSelf = true): AliasedRow | undefined {
  const key = catalogKey(name)
  return (state.catalogs[kind] as AliasedRow[]).find(
    (row) =>
      !(skipSelf && row.id === self) &&
      (kind !== 'subtypes' || (row.material_id ?? null) === scope) &&
      [row.name, ...row.aliases].some((one) => catalogKey(one) === key),
  )
}

function keyClash(kind: Aliased, holder: AliasedRow, name: string): Response {
  const noun = ALIASED[kind]
  return problem(409, `the ${noun} '${holder.name}' already has a name or alias that matches '${name}'`, {
    [`${noun}_id`]: holder.id,
    [`${noun}_name`]: holder.name,
  })
}

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
  if (isAliased(kind)) {
    // A name takes its key; an archived row keeps its keys (#6706).
    const holder = typeof values.name === 'string' ? keyHolder(kind, values.name, (values.material_id as string | null | undefined) ?? null, self) : undefined
    // A rename to one of its own aliases takes that alias's key.
    return holder ? keyClash(kind, holder, values.name as string) : null
  }
  // The name indexes are `WHERE archived_at IS NULL`: an archived row holds no name, and
  // restoring one whose name a live row took since is refused.
  if (values.archived_at) return null
  const rows = state.catalogs[kind] as {
    id: string
    name: string
    archived_at: string | null
    manufacturer?: string | null
    material_name?: string | null
  }[]
  const taken = rows.some(
    (row) =>
      row.id !== self &&
      !row.archived_at &&
      sameName(row.name, values.name) &&
      // `filament_colours_name`: a colour is unique per manufacturer, name and material (none is one).
      (kind !== 'colours' ||
        (sameName(row.manufacturer ?? '', String(values.manufacturer ?? '')) &&
          sameName(row.material_name ?? '', String(values.material_name ?? '')))),
  )
  return taken ? problem(409, REFUSED.catalogExists) : null
}

/** What a spool type would refuse, or `null`. */
function typeRefusal(values: Record<string, unknown>, self?: string): Response | null {
  const absent = missing(values, ['code', 'material_id', 'rgba', 'label_weight_g'])
  if (absent) return problem(422, absent)
  const temps = badTemps(values)
  if (temps) return problem(422, temps)
  // `filament/models.py` `TdfpId`: a positive whole number below 2^31.
  const tdfp = values.tdfp_id
  if (tdfp != null && !(Number.isInteger(tdfp) && (tdfp as number) > 0 && (tdfp as number) < 2 ** 31)) return problem(422, REFUSED.tdfpId)
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
    id: newId(),
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
    hours_outside_dry: 0,
    needs_drying: false,
    last_used_at: null,
    archived_at: null,
    created_at: now(),
    updated_at: now(),
  }
  return { ...blank, ...(body as Partial<SpoolRow>) }
}

/** `POST /filament/types`: the new type, or the refusal; an invoice's confirm makes its proposed types here too (#6266). */
export function createType(raw: unknown): S['SpoolType'] | Response {
  const extra = extraKeys(raw, FIELDS.typeCreate)
  if (extra) return extra
  const body = normalised(raw as S['SpoolTypeCreate-Input'])
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
    drying_temp_c: null,
    insert_type_id: null,
    low_stock_pct: null,
    cost_per_kg: null,
    lead_time_days: null,
    safety_margin_days: null,
    note: null,
    tdfp_id: null,
    diameter_mm: '1.75',
    ...body,
    drying_hours: body.drying_hours ?? null,
    id: newId(),
    filament_id: null,
    archived_at: null,
    created_at: now(),
    updated_at: now(),
  })
  state.types.push(made)
  return made
}

// -- type proposals (#6701, `filament/type_proposals.py`) ----------------------------------

/** What a proposal groups by. The mock's spools have no Bambuddy reading, so `material_number` is `null`. */
const PROPOSAL_KEY = ['brand_id', 'material_id', 'subtype_id', 'rgba', 'label_weight_g'] as const

/** A spool or a pick, by the key fields; an absent one reads as `null`. */
type Keyed = { [K in (typeof PROPOSAL_KEY)[number]]?: unknown }

const proposalKey = (row: Keyed) => PROPOSAL_KEY.map((field) => String(row[field] ?? '')).join('|')

/** `type_proposals._match`'s identity match: the first live type of the key. */
const matchingType = (row: Keyed) =>
  state.types.find((type) => !type.archived_at && PROPOSAL_KEY.every((field) => (type[field] ?? null) === (row[field] ?? null)))

/** `_base_code`: `BRAND-MATERIAL-SUBTYPE-COLOUR`, upper case, the hex when there is no colour name. */
function baseCode(row: SpoolRow, colour: string | null): string {
  const rgba = row.rgba ?? ''
  const hex = rgba.endsWith('FF') ? rgba.slice(1, 7) : rgba.slice(1)
  const parts = [nameOf('brands', row.brand_id), nameOf('materials', row.material_id), nameOf('subtypes', row.subtype_id), colour ?? hex]
  return parts.filter(Boolean).join('-').toUpperCase().replace(/[^A-Z0-9]+/g, '-').replace(/^-|-$/g, '')
}

/** `_colour_name`: the one most of `rows` have, the first one's on a tie. */
function commonColour(rows: SpoolRow[]): string | null {
  const counted = new Map<string, number>()
  for (const row of rows) if (row.colour_name) counted.set(row.colour_name, (counted.get(row.colour_name) ?? 0) + 1)
  let best: string | null = null
  for (const [name, count] of counted) if (best === null || count > counted.get(best)!) best = name
  return best
}

function typeProposals(): S['TypeProposals'] {
  const open = state.spools.filter((row) => !row.type_id && !row.archived_at).sort((a, b) => a.number - b.number)
  const groups = new Map<string, SpoolRow[]>()
  const ungrouped: S['UngroupedSpool'][] = []
  for (const row of open) {
    const missing = (
      [
        ['material', row.material_id],
        ['rgba', row.rgba],
        ['label_weight_g', row.label_weight_g],
      ] as const
    ).flatMap(([name, value]) => (value == null ? [name] : []))
    if (missing.length) ungrouped.push({ spool_id: row.id, number: row.number, missing })
    else groups.set(proposalKey(row), [...(groups.get(proposalKey(row)) ?? []), row])
  }
  const taken = new Set(state.types.filter((type) => !type.archived_at).map((type) => type.code.toUpperCase()))
  const ordered = [...groups.values()].sort((a, b) => b.length - a.length || a[0]!.number - b[0]!.number)
  return {
    groups: ordered.map((members) => {
      const first = members[0]!
      const colour = commonColour(members)
      const kind = matchingType(first)
      let code = kind?.code ?? baseCode(first, colour)
      if (!kind && taken.has(code.toUpperCase())) {
        const weighed = `${code}-${first.label_weight_g}G`
        code = weighed
        for (let n = 2; taken.has(code.toUpperCase()); n++) code = `${weighed}-${n}`
      }
      taken.add(code.toUpperCase())
      return {
        code,
        type_id: kind?.id ?? null,
        brand_id: first.brand_id,
        material_id: first.material_id!,
        subtype_id: first.subtype_id,
        rgba: first.rgba!,
        label_weight_g: first.label_weight_g!,
        material_number: null,
        brand: nameOf('brands', first.brand_id),
        material: nameOf('materials', first.material_id) ?? '',
        subtype: nameOf('subtypes', first.subtype_id),
        colour_name: colour,
        spool_ids: members.map((row) => row.id),
        numbers: members.map((row) => row.number),
        count: members.length,
      }
    }),
    ungrouped,
  }
}

/** `confirm`: all or nothing, as the one transaction is. */
function confirmProposals(picks: S['TypeProposalPick'][]): S['TypeProposalResult'] | Response {
  const live = state.types.filter((type) => !type.archived_at).map((type) => type.code.toUpperCase())
  const codes = picks.map((pick) => pick.code.trim().toUpperCase())
  if (codes.some((code, at) => codes.indexOf(code) !== at || (live.includes(code) && !matchingType(normalised(picks[at]!)))))
    return problem(409, 'a spool type already has one of these codes; choose another name')
  const done: S['ConfirmedProposal'][] = []
  const madeHere = new Set<string>()
  for (const raw of picks) {
    const pick = normalised(raw)
    const want = proposalKey(pick)
    const eligible: SpoolRow[] = []
    const skipped: S['SkippedSpool'][] = []
    for (const id of new Set(pick.spool_ids)) {
      const row = state.spools.find((one) => one.id === id)
      const reason = !row ? 'not_found' : row.archived_at ? 'archived' : row.type_id ? 'typed' : proposalKey(row) !== want ? 'changed' : null
      if (reason) skipped.push({ spool_id: id, reason })
      else eligible.push(row!)
    }
    if (!eligible.length) {
      done.push({ code: pick.code, type_id: null, made: false, new: false, typed: [], skipped })
      continue
    }
    let kind = matchingType(eligible[0]!)
    const made = !kind
    if (!kind) {
      const created = createType({
        code: pick.code.trim(),
        brand_id: pick.brand_id ?? null,
        material_id: pick.material_id,
        subtype_id: pick.subtype_id ?? null,
        rgba: pick.rgba,
        label_weight_g: pick.label_weight_g,
        colour_name: commonColour(eligible),
      })
      if (created instanceof Response) return created
      kind = created
      madeHere.add(kind.id)
    }
    for (const row of eligible) {
      const changes: Record<string, unknown[]> = { type_id: [null, kind.id] }
      const next: SpoolRow = { ...row, type_id: kind.id, updated_at: now() }
      for (const field of OVERRIDABLE) {
        if (row[field] != null && JSON.stringify(row[field]) === JSON.stringify(kind[field])) {
          changes[field] = [row[field], null]
          ;(next as Record<string, unknown>)[field] = null
        }
      }
      state.spools[state.spools.indexOf(row)] = next
      addEvent(row.id, 'edited', { data: { changes } })
    }
    done.push({ code: kind.code, type_id: kind.id, made, new: madeHere.has(kind.id), typed: eligible.map((row) => row.id), skipped })
  }
  return { groups: done }
}

/** A type as the purchasing mocks read it (#6266): its code and label, its label weight, or `null` for none. */
export function typeInfo(id: string): { code: string; label: string; label_weight_g: number; material: string; subtype: string | null; brand: string | null; colour_name: string | null } | null {
  const type = state.types.find((one) => one.id === id)
  if (!type) return null
  return {
    code: type.code,
    label: type.colour_name ?? type.code,
    label_weight_g: type.label_weight_g,
    material: nameOf('materials', type.material_id) ?? '',
    subtype: nameOf('subtypes', type.subtype_id),
    brand: nameOf('brands', type.brand_id),
    colour_name: type.colour_name,
  }
}

/** Whether an unarchived type has the code (`filament_spool_types_code`, case-blind). */
export const typeCodeTaken = (code: string) => state.types.some((type) => !type.archived_at && sameName(type.code, code))

const DAY_MS = 86_400_000
const BB_OUTCOMES = new Set(['completed', 'failed', 'cancelled'])
const byTime = (a: S['SpoolEvent'], b: S['SpoolEvent']) => a.at.localeCompare(b.at) || a.seq - b.seq
const PLATE_KINDS = new Set<S['SpoolEvent']['kind']>(['print_started', 'print_ended'])

/** A plate as `metrics.Plate` has it, with the fields the mock reads. */
export interface MockPlate {
  at: string
  grams: number
  // `metrics_models.Outcome`.
  outcome: 'completed' | 'failed' | 'cancelled' | 'unknown'
  printer_id: string | null
}

/**
 * One spool's plates, oldest first, as `metrics.plates` counts them: an ended BB usage row,
 * and an ended SB print run that BB did not book too (a `covered` one is none, a
 * `not_applied` one a failed 0 g plate). An open `print_started` estimate and a `print`
 * ref are no plate. The one plate rule of the mock: the history and `consumed_g` use it.
 */
function platesOf(events: readonly S['SpoolEvent'][]): MockPlate[] {
  const bbJobs = new Set(events.filter((event) => event.ref_kind === 'bb_usage' && event.kind === 'print_ended').map(jobOf))
  return [...events].sort(byTime).flatMap((event) => {
    if (event.kind !== 'print_ended') return []
    const grams = event.delta_g ? -event.delta_g : 0
    if (event.ref_kind === 'bb_usage') {
      const status = event.data?.bb_status
      const outcome = typeof status === 'string' && BB_OUTCOMES.has(status) ? (status as MockPlate['outcome']) : 'unknown'
      return [{ at: event.at, grams, outcome, printer_id: event.printer_id }]
    }
    if (event.ref_kind !== 'print_run' || bbJobs.has(jobOf(event)) || event.data?.basis === 'covered') return []
    const failed = event.data?.basis === 'not_applied'
    return [{ at: event.at, grams: failed ? 0 : grams, outcome: failed ? 'failed' : 'completed', printer_id: event.printer_id }]
  })
}

/** The grams the plates in `events` used, each spool's plates counted by `platesOf` (`summary`'s `consumed_g`). */
function plateGrams(events: readonly S['SpoolEvent'][]): number {
  const spools = new Set(events.map((event) => event.spool_id))
  return [...spools].reduce((sum, id) => sum + platesOf(events.filter((event) => event.spool_id === id)).reduce((total, plate) => total + plate.grams, 0), 0)
}

/**
 * `metrics.burn`: the grams of the plates that ended in the last `windowDays`, over the
 * window, or over the age since `born` when that is shorter (never less than a day);
 * `null` when no plate ended in the window.
 */
export function burnOf(plates: readonly MockPlate[], windowDays: number, born: string, nowMs: number): number | null {
  const inside = plates.filter((plate) => nowMs - Date.parse(plate.at) < windowDays * DAY_MS && Date.parse(plate.at) <= nowMs)
  if (inside.length === 0) return null
  return inside.reduce((sum, plate) => sum + plate.grams, 0) / Math.max(1, Math.min(windowDays, (nowMs - Date.parse(born)) / DAY_MS))
}

/** `metrics.MAX_FORECAST_DAYS`: no forecast further out than this. */
export const MAX_FORECAST_DAYS = 3650

/** `metrics.empty_on`: the UTC day the grams run out at `burn`; `null` with no grams, no burn above 0, or past `MAX_FORECAST_DAYS`. */
export function emptyOn(grams: number | null | undefined, burn: number | null, nowMs: number): string | null {
  if (grams == null || burn == null || !Number.isFinite(grams) || !Number.isFinite(burn) || burn <= 0) return null
  const days = Math.max(grams, 0) / burn
  return days > MAX_FORECAST_DAYS ? null : new Date(nowMs + days * DAY_MS).toISOString().slice(0, 10)
}

/** `ledger.reading`: the grams an absolute reading gives (with an AMS reading's `remain_pct` × the label weight when it has none), or `null`. */
function readingOf(event: S['SpoolEvent'], labelWeight: number | null | undefined): number | null {
  if (!event.source || !['scale', 'person', 'bb', 'ams_remain'].includes(event.source)) return null
  if (event.grams_after != null) return event.grams_after
  const pct = event.source === 'ams_remain' ? event.data?.remain_pct : undefined
  return typeof pct === 'number' && labelWeight != null ? (pct * labelWeight) / 100 : null
}

/** `ledger.is_anchor`: a reading, and not an AMS one while the weight is locked or the AMS is printing. */
const isAnchor = (event: S['SpoolEvent'], weightLocked: boolean, labelWeight: number | null | undefined) =>
  readingOf(event, labelWeight) != null && !(event.source === 'ams_remain' && (weightLocked || event.data?.printing === true))

/**
 * `metrics.ams_pairs`: AMS grams less scale grams, one per scale reading with an AMS
 * reading to pair: the newest `ams_remain` reading before it that is not printing, at most
 * 24 h older, with no plate event that used grams between them (plan Decision 14).
 */
function amsErrors(events: readonly S['SpoolEvent'][], labelWeight: number | null | undefined): number[] {
  const errors: number[] = []
  let ams: { at: number; grams: number } | null = null
  for (const event of [...events].sort(byTime)) {
    if (PLATE_KINDS.has(event.kind)) {
      if (event.delta_g) ams = null
    } else if (event.source === 'ams_remain' && event.data?.printing !== true) {
      const grams = readingOf(event, labelWeight)
      if (grams != null) ams = { at: Date.parse(event.at), grams }
    } else if (event.source === 'scale' && event.grams_after != null && ams && Date.parse(event.at) - ams.at <= DAY_MS) {
      errors.push(ams.grams - event.grams_after)
    }
  }
  return errors
}

function amsError(errors: readonly number[]): S['AmsError'] | null {
  if (errors.length === 0) return null
  const sum = (values: readonly number[]) => values.reduce((total, value) => total + value, 0)
  return { pairs: errors.length, mean_abs_g: sum(errors.map(Math.abs)) / errors.length, bias_g: sum(errors) / errors.length }
}

/** The cost of `grams` at `perKg`, to the cent as `metrics._money` quantizes it; the mock rounds the grams to whole ones. */
const costOf = (perKg: string, grams: number) => divDecimal(mulDecimal(perKg, Math.round(grams)), '1000', 2)

/**
 * `GET /filament/spools/{id}/history` (#6269, `metrics_store.history`): the timeline newest
 * first by `(at, seq)` with each plate's order and print; the grams series as
 * `metrics.series` builds it (the value after an event is the next one's `grams_before` by
 * `seq`, the spool's grams left after the last, sorted by `(at, seq)`, an anchor by
 * `ledger.is_anchor`); the spool's metrics over the burn window the settings hold, its age
 * counted from `created_at`; and its type's AMS error.
 */
function spoolHistory(row: SpoolRow): S['SpoolHistory'] {
  const events = state.events.filter((event) => event.spool_id === row.id).sort(byTime)
  const windowDays = mockSettings().filament_burn_window_days ?? 30
  const nowMs = Date.now()
  const plates = platesOf(events)
  const used = plates.reduce((sum, plate) => sum + plate.grams, 0)
  const wasted = plates.filter((plate) => plate.outcome === 'failed' || plate.outcome === 'cancelled').reduce((sum, plate) => sum + plate.grams, 0)
  const burn = burnOf(plates, windowDays, row.created_at, nowMs)
  const left = row.grams_left
  const opened = events.find((event) => event.kind === 'loaded')?.at ?? plates[0]?.at ?? null
  const effective = view(row).effective
  const perKg = effective?.cost_per_kg ?? mockSettings().filament_default_cost_per_kg ?? null
  const labelWeight = effective?.label_weight_g
  const bySeq = [...events].sort((a, b) => a.seq - b.seq)
  const series: S['GramsPoint'][] = bySeq
    .flatMap((event, i) => {
      const grams = i + 1 < bySeq.length ? bySeq[i + 1]!.grams_before : left
      return grams == null ? [] : [{ event, point: { at: event.at, grams, anchor: isAnchor(event, row.weight_locked, labelWeight), kind: event.kind } }]
    })
    .sort((a, b) => byTime(a.event, b.event))
    .map(({ point }) => point)
  const sameType = row.type_id ? state.spools.filter((one) => one.type_id === row.type_id) : []
  return {
    events: [...events].reverse().map((event) => {
      const archive = event.data?.bb_archive_id
      const run = event.ref_kind === 'print_run' ? event.ref_id?.split('/')[0] : undefined
      return { event, archive_id: typeof archive === 'number' ? archive : null, order_id: (run && state.orderRuns.get(run)) || null }
    }),
    series,
    metrics: {
      used_g: used,
      prints: plates.length,
      grams_per_print: plates.length > 0 ? used / plates.length : null,
      wasted_g: wasted,
      cost_used: perKg != null ? costOf(perKg, used) : null,
      value_left: perKg != null && left != null ? costOf(perKg, left) : null,
      burn_g_per_day: burn,
      empty_on: emptyOn(left, burn, nowMs),
      opened_at: opened,
      days_since_opened: opened ? (nowMs - Date.parse(opened)) / DAY_MS : null,
      days_since_last_use: row.last_used_at ? (nowMs - Date.parse(row.last_used_at)) / DAY_MS : null,
      hours_outside_dry: row.hours_outside_dry,
      ams_error: amsError(amsErrors(events, labelWeight)),
    },
    type_ams_error: row.type_id
      ? amsError(sameType.flatMap((one) => amsErrors(state.events.filter((event) => event.spool_id === one.id), view(one).effective?.label_weight_g)))
      : null,
    currency: mockSettings().orders_currency ?? 'USD',
    window_days: windowDays,
  }
}

/** The spools of `ids` that exist, their grams left (unarchived only) and the grams their prints used (#6266 stats). */
export function spoolUsage(ids: readonly string[]): { count: number; remaining_g: number; consumed_g: number } {
  const rows = state.spools.filter((row) => ids.includes(row.id))
  return {
    count: rows.length,
    remaining_g: rows.filter((row) => !row.archived_at).reduce((sum, row) => sum + (row.grams_left ?? 0), 0),
    consumed_g: plateGrams(state.events.filter((event) => ids.includes(event.spool_id))),
  }
}

/**
 * #6269 — what the metrics routes (`metrics.ts`) read, as `metrics_store._read` does: every
 * spool (archived too) as the API lists it, its plates by the one plate rule (`platesOf`),
 * its supplier (its invoice line's confirmed invoice's), the types with their material's
 * name, and the printers' names.
 */
export function metricsInput() {
  const spools = listed(true).map(view)
  const suppliers = new Map<string, { id: string; name: string }>()
  for (const row of state.spools) {
    const ref = invoiceRefOf(row.id, invoicedLine.get(row.id))
    if (ref) suppliers.set(row.id, { id: ref.supplier_id, name: ref.supplier_name })
  }
  return {
    spools,
    plates: new Map(spools.map((spool) => [spool.id, platesOf(state.events.filter((event) => event.spool_id === spool.id))])),
    suppliers,
    types: state.types.map((type) => ({ ...type, material: nameOf('materials', type.material_id) })),
    printers: new Map([[H2C.printer_id, H2C.printer_name]]),
  }
}

/** What `createType` would refuse for a proposed type, or `null`: checked before an invoice's confirm writes anything (#6266). */
export function checkTypeProposal(raw: unknown): Response | null {
  return extraKeys(raw, FIELDS.typeCreate) ?? typeRefusal(normalised(raw as S['SpoolTypeCreate-Input']))
}

/**
 * Why a spool cannot be filled in by an invoice line of `typeId` (`null` for a type the
 * line has yet to make), or `null` when it can: `filament/invoices.py` `_claimed`.
 */
export function spoolClaimRefusal(id: string, typeId: string | null): string | null {
  const row = state.spools.find((one) => one.id === id)
  if (!row) return 'does not exist'
  if (invoicedLine.has(id) || invoiceRefOf(id)) return 'already belongs to an invoice line'
  if (row.type_id != null && row.type_id !== typeId) return 'has another spool type'
  return null
}

/** The spool's own label weight in grams, if it has one (`_claimed`: the cost uses it before the type's). */
export const spoolLabelWeight = (id: string) => state.spools.find((one) => one.id === id)?.label_weight_g ?? null

/**
 * Fills an existing spool in from an invoice line: its type and cost, and the line it was bought on (`_confirm_line`).
 * The line's cost replaces a spool's own (#6700); that `edited` event is the caller's. An archived spool takes its
 * cost and line only: it keeps its type, or none, and stays archived (#6707).
 */
export function updateInvoicedSpool(id: string, typeId: string, costPerKg: string, lineId: string): void {
  const at = state.spools.findIndex((one) => one.id === id)
  const row = state.spools[at]!
  const type = row.archived_at ? row.type_id : typeId
  state.spools[at] = { ...row, type_id: type, cost_per_kg: costPerKg, updated_at: now() }
  const changes: Record<string, [unknown, unknown]> = {}
  if (row.type_id !== type) changes.type_id = [row.type_id, type]
  if (row.cost_per_kg !== costPerKg) changes.cost_per_kg = [row.cost_per_kg, costPerKg]
  const actor = row.cost_per_kg == null ? `invoice:${lineId}` : 'mock@example.com'
  if (Object.keys(changes).length) addEvent(id, 'edited', { ref_kind: 'invoice_line', ref_id: lineId, actor, data: { changes } })
  invoicedLine.set(id, lineId)
}

/**
 * #6266 — what an invoice's confirm makes: `count` new spools of the type at `costPerKg`
 * (a decimal string), full to the type's label weight, each bought on invoice line
 * `lineId` (`filament/invoices.py` `_confirm_line`). Answers their ids.
 */
export function addInvoicedSpools(typeId: string, count: number, costPerKg: string, lineId: string): string[] {
  const type = state.types.find((one) => one.id === typeId)
  const label = type?.label_weight_g ?? 1000
  const ids: string[] = []
  for (let i = 0; i < count; i++) {
    const made = newSpool({ type_id: typeId, cost_per_kg: costPerKg, grams_left: label })
    state.spools.push(made)
    addEvent(made.id, 'created', { source: null, grams_after: label, ref_kind: 'invoice_line', ref_id: lineId, actor: `invoice:${lineId}` })
    invoicedLine.set(made.id, lineId)
    ids.push(made.id)
  }
  return ids
}

// -- the CSV import (`filament/csv_import.py`, #6262 Task 15) ------------------------

/** `crud/idempotency.py`: each write's key and the body and answer it had. */
const imports = new Map<string, { body: string; answer: S['ImportPreview'] }>()

// -- 3DFP preset fetch (#6625) ----------------------------------------------------------

/** Each type's last fetch (`ProfileFetches.latest`), by type id. */
const fetches = new Map<string, S['ProfileFetch']>()

/** `filament/profile_fetch.py` `NO_PRESET`: a gap no run gave a reason for. */
const NO_PRESET = 'no preset for this printer and nozzle yet'
const FETCH_NOZZLES = ['0.2', '0.4', '0.6', '0.8'] as const

/** A refusal of the start as `api/filament.py` writes it: a problem typed by its `code`. */
const fetchRefused = (code: 'no_tdfp_id' | 'no_printer_models', detail: string) =>
  HttpResponse.json(
    { type: `https://scadbuddy.dev/problems/profile-fetch/${code}`, title: TITLES[422], status: 422, detail, code },
    { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
  )

/** `ProfileFetches._targets`: each printer model ScadBuddy's printers have, with the nozzle setting. */
function fetchTargets(): S['ExportTarget'][] {
  const nozzles = mockSettings().profile_fetch_nozzles ?? [...FETCH_NOZZLES]
  const models = [...new Set(printersState().map((printer) => printer.model).filter((model): model is string => !!model))].sort()
  return models.map((printer_model) => ({ printer_model, nozzles: [...nozzles] }))
}

/**
 * The mock has no worker: a running fetch ends `imported` on the next read of it, with
 * one preset per target key (a Bambuddy local preset id and a 3DFP-style name), as the
 * API's import writes them.
 */
function finishFetch(run: S['ProfileFetch']): S['ProfileFetch'] {
  if (run.status !== 'running') return run
  const type = state.types.find((one) => one.id === run.type_id)
  const made: S['TypePreset'][] = run.targets.flatMap((target, t) =>
    target.nozzles.map((nozzle, n) => ({
      type_id: run.type_id,
      printer_model: target.printer_model,
      nozzle_diameter: nozzle,
      slicer_filament: String(9000 + t * 10 + n),
      slicer_filament_name: `${type?.code ?? 'Filament'} @BBL ${target.printer_model} ${nozzle} nozzle`,
    })),
  )
  const keys = new Set(made.map((row) => `${row.printer_model}|${row.nozzle_diameter}`))
  state.typePresets = [...state.typePresets.filter((one) => one.type_id !== run.type_id || !keys.has(`${one.printer_model}|${one.nozzle_diameter}`)), ...made]
  const ended = { ...run, status: 'imported' as const, finished_at: now() }
  fetches.set(run.type_id, ended)
  return ended
}

/** `ProfileFetches.gaps`: each target key the type has no preset for, with its last run's reason. */
function fetchGaps(typeId: string): S['MissingKey'][] {
  const have = new Set(state.typePresets.filter((one) => one.type_id === typeId).map((one) => `${one.printer_model}|${one.nozzle_diameter}`))
  const reasons = new Map((fetches.get(typeId)?.missing ?? []).map((one) => [`${one.printer_model}|${one.nozzle_diameter}`, one.reason]))
  return fetchTargets().flatMap((target) =>
    target.nozzles
      .filter((nozzle) => !have.has(`${target.printer_model}|${nozzle}`))
      .map((nozzle) => ({ printer_model: target.printer_model, nozzle_diameter: nozzle, reason: reasons.get(`${target.printer_model}|${nozzle}`) ?? NO_PRESET })),
  )
}

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
    // No letter or digit, so no key: the cell is skipped and the row goes on (R-6706-18).
    if (catalogKey(name) === '') {
      changes[column] = [before, name, { skipped: 'no letter or digit' }]
      continue
    }
    // By key, against every name and alias, archived rows too (#6706).
    const found = kind === 'subtypes' ? (keyHolder(kind, name, material, undefined, false) ?? keyHolder(kind, name, null, undefined, false)) : keyHolder(kind, name, null, undefined, false)
    if (column === 'material') material = found?.id ?? null
    if (found && before !== null && found.name === before) continue
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
    const made = { ...blank, name, sort_order: 1000, aliases: [], id: newId(), archived_at: null, created_at: now(), updated_at: now() }
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
    // #6907: every word of `q` matches some text of the row (`CatalogStore.list`).
    const words = (new URL(request.url).searchParams.get('q') ?? '').toLowerCase().split(/\s+/).filter(Boolean)
    const texts = (row: object) =>
      ['name', 'manufacturer', 'material_name', 'rgba'].map((field) => (row as Record<string, unknown>)[field]).filter((v): v is string => typeof v === 'string').map((v) => v.toLowerCase())
    return HttpResponse.json(
      rows
        .filter((row) => archived || !row.archived_at)
        .filter((row) => words.every((word) => texts(row).some((text) => text.includes(word))))
        .sort((a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name)),
    )
  }),
  // -- the colour catalog's syncs (#6907) --
  http.get(`${BASE}/colour-syncs`, () => HttpResponse.json(colourSyncs)),
  http.post(`${BASE}/colour-syncs`, async ({ request }) => {
    const { source } = (await request.json()) as { source?: string }
    if (source !== 'filamentcolors' && source !== 'bambu') return problem(422, "source: Input should be 'filamentcolors' or 'bambu'")
    // `colour_catalog.upsert_colours`: a row is found by maker, name and material, case-blind;
    // a person's row (`synced_at` null), an archived one or another source's is kept as it is
    // (Decisions 209, 217, 221).
    const counts = { added: 0, updated: 0, unchanged: 0, kept: 0 }
    for (const colour of MOCK_SYNC_COLOURS[source]) {
      const row = state.catalogs.colours.find(
        (one) =>
          sameName(one.manufacturer ?? '', colour.manufacturer) && sameName(one.name, colour.name) && sameName(one.material_name ?? '', colour.material_name),
      )
      if (!row) {
        state.catalogs.colours.push({ ...colour, id: newId(), sort_order: 0, material_id: null, effect_id: null, extra_colours: null, bb_colour_id: null, source, archived_at: null, created_at: now(), updated_at: now(), synced_at: now() })
        counts.added += 1
      } else if (row.synced_at === null || row.archived_at !== null || row.source !== source) {
        counts.kept += 1
      } else if (row.rgba === colour.rgba) {
        counts.unchanged += 1
      } else {
        Object.assign(row, { rgba: colour.rgba, synced_at: now(), updated_at: now() })
        counts.updated += 1
      }
    }
    const run: ColourSync = {
      id: newId(),
      source,
      state: 'done',
      pages_read: 1,
      pages_total: 1,
      total: MOCK_SYNC_COLOURS[source].length,
      ...counts,
      skipped: 0,
      error: null,
      started_at: now(),
      finished_at: now(),
    }
    colourSyncs = [...colourSyncs.filter((one) => one.source !== source), run]
    return HttpResponse.json(run)
  }),
  http.post(`${BASE}/catalog/:kind`, async ({ params, request }) => {
    const kind = params.kind as keyof Catalogs
    if (!(kind in state.catalogs)) return problem(404, `no catalog ${String(kind)}`)
    const body = normalised((await request.json()) as Record<string, unknown>)
    const refused = catalogRefusal(kind, body, body)
    if (refused) return refused
    const blank = Object.fromEntries([...CATALOG_FIELDS[kind]].map((field) => [field, null]))
    // #6907: a person's colour has no source, no sync time and no Bambuddy link.
    const own = kind === 'colours' ? { source: null, synced_at: null, bb_colour_id: null } : {}
    const made = { ...blank, sort_order: 0, ...body, ...own, ...(isAliased(kind) ? { aliases: [] } : {}), id: newId(), archived_at: null, created_at: now(), updated_at: now() }
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
    if (isAliased(kind) && typeof body.name === 'string') {
      const key = catalogKey(body.name)
      const own = next as unknown as AliasedRow
      own.aliases = own.aliases.filter((one) => catalogKey(one) !== key)
    }
    // #6907 Decision 217: a person's write of a value a sync writes makes the colour theirs.
    // A copy of the backend's `_SYNCED_VALUES` (`backend/scadbuddy/filament/catalog.py`).
    const values = ['name', 'manufacturer', 'rgba', 'material_name', 'material_id', 'extra_colours', 'effect_id']
    if (kind === 'colours' && values.some((field) => field in body)) (next as unknown as { synced_at: string | null }).synced_at = null
    rows[at] = next
    return HttpResponse.json(next)
  }),
  http.get(`${BASE}/catalog/:kind/resolve`, ({ params, request }) => {
    const kind = String(params.kind)
    if (!isAliased(kind)) return problem(404, `the ${kind} catalog has no aliases; these do: ${Object.keys(ALIASED).join(', ')}`)
    const url = new URL(request.url)
    const name = url.searchParams.get('name') ?? ''
    const material = url.searchParams.get('material_id')
    const found = kind === 'subtypes' ? (keyHolder(kind, name, material, undefined, false) ?? keyHolder(kind, name, null, undefined, false)) : keyHolder(kind, name, null, undefined, false)
    return found ? HttpResponse.json(found) : problem(404, `no ${kind} row is named '${name}'`)
  }),
  http.put(`${BASE}/catalog/:kind/:id/aliases`, async ({ params, request }) => {
    const kind = String(params.kind)
    if (!isAliased(kind)) return problem(404, `the ${kind} catalog has no aliases; these do: ${Object.keys(ALIASED).join(', ')}`)
    const row = (state.catalogs[kind] as AliasedRow[]).find((one) => one.id === params.id)
    if (!row) return problem(404, `no ${kind} row ${String(params.id)}`)
    const body = (await request.json()) as { aliases?: unknown }
    if (!Array.isArray(body.aliases)) return problem(422, 'aliases: Field required')
    const aliases = body.aliases.map((one) => String(one).trim())
    if (aliases.some((one) => one === '')) return problem(422, 'aliases: String should have at least 1 character')
    const empty = aliases.find((one) => catalogKey(one) === '')
    if (empty !== undefined) return problem(422, `the alias '${empty}' has no letter or digit`)
    const scope = row.material_id ?? null
    const taken: string[] = [catalogKey(row.name)]
    for (const alias of aliases) {
      const holder = keyHolder(kind, alias, scope, row.id)
      if (holder) return keyClash(kind, holder, alias)
      if (taken.includes(catalogKey(alias))) return keyClash(kind, row, alias)
      taken.push(catalogKey(alias))
    }
    row.aliases = [...aliases].sort((a, b) => a.toUpperCase().localeCompare(b.toUpperCase()))
    return HttpResponse.json(row)
  }),

  // -- spool types --
  http.get(`${BASE}/types`, ({ request }) => {
    const archived = flag(request, 'include_archived')
    return HttpResponse.json(state.types.filter((type) => archived || !type.archived_at).sort((a, b) => a.code.localeCompare(b.code)))
  }),
  http.post(`${BASE}/types`, async ({ request }) => {
    const made = createType(await request.json())
    return made instanceof Response ? made : HttpResponse.json(made, { status: 201 })
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
  // #6714: the fill from a type's slicer profile, over `PROFILES` (`fillType`).
  http.post(`${BASE}/types/fill-from-profile`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { refresh?: boolean }
    const refresh = body.refresh === true
    return HttpResponse.json(
      state.types
        .filter((type) => !type.archived_at)
        .map((type) => {
          const done = fillType(type, { refresh })
          const rows = bambuddyKValuesOf(type.id)
          return refresh ? { ...done, k_values: asTypeKValues(type.id, rows) } : writeKValues(type.id, rows, done)
        }),
    )
  }),
  http.post(`${BASE}/types/:id/fill-from-profile`, async ({ params, request }) => {
    const type = state.types.find((one) => one.id === params.id)
    if (!type) return problem(404, `no spool type ${String(params.id)}`)
    const body = (await request.json().catch(() => ({}))) as S['FillRequest']
    if (body.apply) {
      const unknown = Object.keys(body.apply).find((field) => !(FILL_FIELDS as readonly string[]).includes(field))
      if (unknown) return problem(422, `apply.${unknown}: Input should be ${FILL_FIELDS.join(', ')}`)
      const given = profileOf(type)?.values ?? {}
      for (const [field, value] of Object.entries(body.apply)) {
        if (String(given[field as FillField]) !== String(value)) return problem(409, `the profile now gives ${field} ${String(given[field as FillField] ?? 'None')}, not ${String(value)}: refresh again`)
      }
      // R-6714-i, R-6714-g: the K values the refresh showed come back, and a change since is refused.
      if (body.k_values === true && (body.shown_k_values === undefined || body.shown_k_values === null)) {
        return problem(422, "k_values needs shown_k_values: the K values the refresh showed ([] for none)")
      }
      const rows = body.k_values === true ? bambuddyKValuesOf(type.id) : []
      if (body.k_values === true && kKey(rows) !== kKey(body.shown_k_values ?? [])) return problem(409, 'the K values now are not the ones shown: refresh again')
      return HttpResponse.json(writeKValues(type.id, rows, fillType(type, { apply: body.apply as Partial<Record<FillField, number | string>>, kValues: body.k_values === true })))
    }
    const refresh = body.refresh === true
    const done = fillType(type, { refresh })
    const rows = bambuddyKValuesOf(type.id)
    if (refresh) return HttpResponse.json({ ...done, k_values: asTypeKValues(type.id, rows) })
    return HttpResponse.json(writeKValues(type.id, rows, done))
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
  http.post(`${BASE}/types/:id/profile-fetch`, ({ params }) => {
    const typeId = String(params.id)
    const type = state.types.find((one) => one.id === typeId)
    if (!type) return problem(404, `no spool type ${typeId}`)
    const was = fetches.get(typeId)
    if (was?.status === 'running') return HttpResponse.json({ run: was, joined: true } satisfies S['ProfileFetchStarted'], { status: 202 })
    if (type.tdfp_id == null) return fetchRefused('no_tdfp_id', 'the type has no 3dfilamentprofiles id')
    const targets = fetchTargets()
    if (targets.length === 0) return fetchRefused('no_printer_models', 'no printer has a model to fetch presets for')
    const run: S['ProfileFetch'] = {
      id: newId(),
      type_id: typeId,
      tdfp_id: type.tdfp_id,
      status: 'running',
      targets,
      missing: [],
      k_values: 0,
      notes: [],
      error: null,
      started_at: now(),
      finished_at: null,
    }
    fetches.set(typeId, run)
    return HttpResponse.json({ run, joined: false } satisfies S['ProfileFetchStarted'], { status: 202 })
  }),
  http.get(`${BASE}/types/:id/profile-fetch`, ({ params }) => {
    const typeId = String(params.id)
    if (!state.types.some((one) => one.id === typeId)) return problem(404, `no spool type ${typeId}`)
    const was = fetches.get(typeId)
    const run = was ? finishFetch(was) : null
    return HttpResponse.json({ run, gaps: fetchGaps(typeId) } satisfies S['ProfileFetchStatus'])
  }),
  http.put(`${BASE}/types/:id/k-values`, async ({ params, request }) => {
    const typeId = String(params.id)
    const sent = await request.json()
    const extra = extraKeys(sent, FIELDS.kValue)
    if (extra) return extra
    // A person's save: a row left as it was keeps its source, any other says `person` (R-6714-19).
    const was = state.typeKValues.filter((one) => one.type_id === typeId)
    const rows: S['TypeKValue'][] = (sent as S['TypeKValueCreate'][]).map((one) => {
      const row = { nozzle_type: null, name: null, cali_idx: null, setting_id: null, ...one, k_value: String(one.k_value), type_id: typeId }
      const same = was.find((old) => kKey([old]) === kKey([row]))
      return { ...row, source: same?.source ?? 'person' }
    })
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
      tag = { id: newId(), kind: body.kind ?? 'other', tag_uid: uid, tray_uuid: tray, raw_blob: null, note: null, created_at: now() }
      state.tags.push(tag)
    }
    const held = openBinding(tag.id)
    if (held?.spool_id === row.id) {
      // As the backend's `bind_on`: the same tag again with another `override` changes it in place.
      const wanted = body.override ?? false
      if (held.override !== wanted) {
        held.override = wanted
        if (wanted) row.weight_locked = true
      }
      return HttpResponse.json(held, { status: 201 })
    }
    if (held) closeBinding(held, 'rebound')
    const other = state.bindings.find((one) => one.spool_id === row.id && !one.unbound_at)
    if (other) closeBinding(other, 'rebound')
    const binding: S['TagBinding'] = { id: newId(), tag_id: tag.id, spool_id: row.id, override: body.override ?? false, bound_at: now(), unbound_at: null, reason: null }
    state.bindings.push(binding)
    // Decision 3: a bind with `override` locks the spool's weight; nothing unlocks it but a person.
    if (binding.override) row.weight_locked = true
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
  http.get(`${BASE}/spools/:id/history`, ({ params }) => {
    const row = findSpool(String(params.id))
    if (!row) return problem(404, `no spool ${String(params.id)}`)
    return HttpResponse.json(spoolHistory(row))
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
    // A field left out takes the spool's drying default; `null` records none (#6720).
    const defaults = dryingDefaults(row)
    const sent = raw as Record<string, unknown>
    const temp = 'temp_c' in sent ? (body.temp_c ?? null) : defaults.temp_c
    const hours = 'hours' in sent ? (body.hours ?? null) : defaults.hours
    state.spools[at] = { ...row, last_dried_at: when, last_dried_temp_c: temp ?? null, last_dried_hours: hours ?? null }
    return HttpResponse.json(addEvent(row.id, 'dried', { at: when, data: { temp_c: temp ?? null, hours: hours ?? null } }), { status: 201 })
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
    HttpResponse.json({ spools: inventoryRows(flag(request, 'include_archived')), trays: inventoryTrays() } satisfies FilamentInventoryView),
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
      sort_order: 0,
      ...body,
      id: newId(),
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

  // -- AMS weights (#6268): the mock books the same three spools, skipping one locked and two on a printing printer --
  http.post(`${BASE}/spools/sync-ams-weights`, () =>
    HttpResponse.json({ booked: 3, locked: 1, printing: 2, no_remain: 0, stale: 0, stale_printers: [], spools: [spoolId(1), spoolId(4), spoolId(8)] } satisfies S['AmsSyncResult']),
  ),

  // -- location sensors (#6268) --
  http.get(`${BASE}/sensors`, ({ request }) => {
    const place = new URL(request.url).searchParams.get('location_id')
    const live = new Set(state.locations.filter((one) => !one.archived_at).map((one) => one.id))
    return HttpResponse.json(state.sensors.filter((one) => !one.archived_at && live.has(one.location_id) && (!place || one.location_id === place)))
  }),
  http.get(`${BASE}/sensors/entities`, ({ request }) => {
    const search = (new URL(request.url).searchParams.get('search') ?? '').toLowerCase()
    return HttpResponse.json(HA_ENTITIES.filter((one) => !search || `${one.entity_id} ${one.friendly_name}`.toLowerCase().includes(search)))
  }),
  http.post(`${BASE}/sensors`, async ({ request }) => {
    const body = (await request.json()) as S['SensorIn']
    if (body.source === 'ams') return problem(422, REFUSED.sensorAms)
    if (!state.locations.some((place) => place.id === body.location_id)) return problem(404, `no location ${body.location_id}`)
    const made: S['Sensor'] = {
      entity_id: null,
      printer_id: null,
      ams_id: null,
      unit: null,
      alert_above: null,
      alert_below: null,
      ...body,
      notify: body.notify ?? false,
      id: newId(),
      last_value: null,
      last_at: null,
      alerting: false,
      reachable: true,
      archived_at: null,
      created_at: now(),
      last_notification: null,
    }
    state.sensors.push(made)
    return HttpResponse.json(made, { status: 201 })
  }),
  http.patch(`${BASE}/sensors/:id`, async ({ params, request }) => {
    const at = state.sensors.findIndex((one) => one.id === params.id && !one.archived_at)
    if (at < 0) return problem(404, `no sensor ${String(params.id)}`)
    const body = (await request.json()) as Record<string, unknown>
    const extra = extraKeys(body, ['name', 'unit', 'alert_above', 'alert_below', 'notify', 'location_id'])
    if (extra) return extra
    const next = patched(state.sensors[at]!, body)
    next.alerting = isAlerting(next)
    state.sensors[at] = next
    return HttpResponse.json(next)
  }),
  http.post(`${BASE}/sensors/:id/archive`, ({ params }) => {
    const found = state.sensors.find((one) => one.id === params.id && !one.archived_at)
    if (!found) return problem(404, `no sensor ${String(params.id)}`)
    found.archived_at = now()
    return new HttpResponse(null, { status: 204 })
  }),
  http.get(`${BASE}/sensors/:id/readings`, ({ params, request }) => {
    const found = state.sensors.find((one) => one.id === params.id)
    if (!found) return problem(404, `no sensor ${String(params.id)}`)
    const url = new URL(request.url)
    const since = url.searchParams.get('since')
    const limit = Number(url.searchParams.get('limit') ?? 500)
    const all = readingsOf(found).filter((one) => !since || one.at >= since)
    return HttpResponse.json(all.slice(-limit))
  }),

  // -- type proposals (#6701) --
  http.get(`${BASE}/type-proposals`, () => HttpResponse.json(typeProposals())),
  http.post(`${BASE}/type-proposals/confirm`, async ({ request }) => {
    const body = (await request.json()) as { groups?: S['TypeProposalPick'][] }
    const extra = extraKeys(body, ['groups']) ?? extraKeys(body.groups ?? [], FIELDS.proposalPick)
    if (extra) return extra
    if (!Array.isArray(body.groups) || !body.groups.length) return problem(422, 'groups: List should have at least 1 item after validation, not 0')
    const done = confirmProposals(body.groups)
    return done instanceof Response ? done : HttpResponse.json(done)
  }),

  // -- the pull: the mock has no Bambuddy spools to pull, so it changes nothing --
  http.post(`${BASE}/bambuddy/import`, () =>
    HttpResponse.json({
      created: 0,
      archived_created: 0,
      linked: 0,
      updated: 0,
      needs_type: 0,
      archived: 0,
      invalid: 0,
      catalog_failed: false,
      catalog_skipped: 0,
      names_skipped: 0,
      created_ids: [],
      linked_ids: [],
      updated_ids: [],
      needs_type_ids: [],
      archived_ids: [],
      failed_bb_ids: [],
      invalid_bb_ids: [],
      usage_booked: 0,
      usage_unlinked: 0,
      rfid_duplicates: 0,
      rfid_duplicate_bb_ids: [],
      archived_deferred: 0,
      archived_deferred_bb_ids: [],
      claimed: 0,
      suppliers: 0,
      locations: 0,
      moved: 0,
      conflicts: 0,
      colours: 0,
      colours_kept: 0,
      colours_failed: false,
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


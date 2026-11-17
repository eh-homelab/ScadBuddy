/**
 * #6262 — the Filament section's client for `/api/v1/filament/*`
 * (`backend/scadbuddy/api/filament.py`), typed from the generated `schema.d.ts`.
 */
import { request } from './client'
import type { components, operations } from './schema'

type Schemas = components['schemas']
type Query<Op extends keyof operations> = NonNullable<operations[Op]['parameters']['query']>

export type Spool = Schemas['Spool']
/** The spec lists a field the server always sends (it has a default) as optional; here it is required. */
export type SpoolDetail = Schemas['SpoolDetail'] & Required<Pick<Schemas['SpoolDetail'], 'effective' | 'overridden' | 'names' | 'presets' | 'k_values' | 'low_stock' | 'in_use'>>
export type SpoolCreate = Schemas['SpoolCreate']
export type SpoolPatch = Schemas['SpoolPatch']
export type TagBind = Schemas['TagBind']
export type TagBinding = Schemas['TagBinding']
export type PresetInput = Schemas['SpoolPresetCreate']
export type EffectivePreset = Schemas['EffectivePreset']
export type KValueInput = Schemas['SpoolKValueCreate']
export type EffectiveKValue = Schemas['EffectiveKValue']
export type SpoolType = Schemas['SpoolType']
export type SpoolTypeDetail = Schemas['SpoolTypeDetail']
export type SpoolTypeCreate = Schemas['SpoolTypeCreate']
export type SpoolTypePatch = Schemas['SpoolTypePatch']
export type TypePresetInput = Schemas['TypePresetCreate']
export type TypePreset = Schemas['TypePreset']
export type TypeKValueInput = Schemas['TypeKValueCreate']
export type TypeKValue = Schemas['TypeKValue']
export type Location = Schemas['Location']
export type LocationNode = Schemas['LocationNode']
export type LocationCreate = Schemas['LocationCreate']
export type LocationPatch = Schemas['LocationPatch']
export type TagRow = Schemas['TagRow']
export type TagDetail = Schemas['TagDetail']
export type EventPage = Schemas['EventPage']
export type SpoolEvent = Schemas['SpoolEvent']
export type SpoolEventAdd = Omit<Schemas['SpoolEventAdd'], 'source'> & Partial<Pick<Schemas['SpoolEventAdd'], 'source'>>
export type LedgerRow = Schemas['LedgerRow']
export type EventQuery = Query<'list_events_api_v1_filament_events_get'>
export type InventorySummary = Schemas['InventorySummary']
export type FilamentInventoryView = Schemas['FilamentInventoryView']
export type CsvRow = Schemas['CsvRow']
export type ImportPreview = Schemas['ImportPreview']
export type ImportRowResult = Schemas['ImportRowResult']

/** Each catalog kind (`filament/catalog.py` `CATALOGS`) and the row it holds. */
export interface CatalogRows {
  materials: Schemas['Material']
  subtypes: Schemas['Subtype']
  brands: Schemas['Brand']
  categories: Schemas['Category']
  effects: Schemas['Effect']
  'insert-materials': Schemas['InsertMaterial']
  'insert-types': Schemas['InsertType']
  colours: Schemas['Colour']
  'location-kinds': Schemas['LocationKind']
}
export type CatalogKind = keyof CatalogRows
/**
 * A catalog row to add or the fields to change. The route takes any object and checks it
 * against the kind's model (`MaterialCreate`, `BrandPatch` …), which the spec does not
 * carry, so this is the row's own fields, all optional, and `archived`.
 */
export type CatalogInput<K extends CatalogKind> = Partial<
  Omit<CatalogRows[K], 'id' | 'created_at' | 'updated_at' | 'archived_at' | 'bb_catalog_id'>
> & { archived?: boolean }

const BASE = '/filament'
const seg = encodeURIComponent

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value != null) search.set(key, String(value))
  const text = search.toString()
  return text ? `?${text}` : ''
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

// -- spools ---------------------------------------------------------------------------

export const listSpools = (options: { includeArchived?: boolean; needsType?: boolean } = {}) =>
  request<Spool[]>(`${BASE}/spools${query({ include_archived: options.includeArchived || undefined, needs_type: options.needsType })}`)

export const getSpool = (id: string) => request<SpoolDetail>(`${BASE}/spools/${seg(id)}`)

/**
 * Spools from CSV rows the browser parsed (no file bytes are sent). `dry_run` answers what
 * each row would do; a write needs `key`, one `Idempotency-Key` per opened preview, so a
 * retry answers the first result and writes nothing.
 */
export const importSpools = (rows: CsvRow[], options: { dryRun: true } | { dryRun: false; key: string }) =>
  request<ImportPreview>(`${BASE}/spools/import`, {
    ...json('POST', { rows, dry_run: options.dryRun }),
    ...(options.dryRun ? {} : { headers: { 'Idempotency-Key': options.key } }),
  })

export const createSpool = (body: SpoolCreate) => request<Spool>(`${BASE}/spools`, json('POST', body))

/** Only the fields sent change; `null` clears an override. */
export const patchSpool = (id: string, body: SpoolPatch) => request<Spool>(`${BASE}/spools/${seg(id)}`, json('PATCH', body))

/** `archived: false` restores it. */
export const archiveSpool = (id: string, archived = true) =>
  request<Spool>(`${BASE}/spools/${seg(id)}/archive`, json('POST', { archived }))

/** Only a spool that never printed (409 otherwise: archive it). */
export const deleteSpool = (id: string) => request<void>(`${BASE}/spools/${seg(id)}`, { method: 'DELETE' })

export const bindTag = (spoolId: string, body: TagBind) =>
  request<TagBinding>(`${BASE}/spools/${seg(spoolId)}/tags`, json('POST', body))

export const unbindTag = (tagId: string) => request<TagBinding>(`${BASE}/tags/${seg(tagId)}/binding`, { method: 'DELETE' })

/** Replaces the spool's own presets; the answer is its effective set. */
export const setSpoolPresets = (id: string, rows: PresetInput[]) =>
  request<EffectivePreset[]>(`${BASE}/spools/${seg(id)}/presets`, json('PUT', rows))

/** Replaces the spool's own K values; the answer is its effective set. */
export const setSpoolKValues = (id: string, rows: KValueInput[]) =>
  request<EffectiveKValue[]>(`${BASE}/spools/${seg(id)}/k-values`, json('PUT', rows))

/** One spool's ledger, oldest first. */
export const listSpoolEvents = (id: string) => request<SpoolEvent[]>(`${BASE}/spools/${seg(id)}/events`)

/** A weighing (`grams`: the filament left, without the empty spool) or a drying (`temp_c`, `hours`). */
export const addSpoolEvent = (id: string, body: SpoolEventAdd) => request<SpoolEvent>(`${BASE}/spools/${seg(id)}/events`, json('POST', body))

/** `null` takes it out of every place. */
export const moveSpool = (id: string, locationId: string | null) =>
  request<Spool>(`${BASE}/spools/${seg(id)}/move`, json('POST', { location_id: locationId }))

// -- spool types ----------------------------------------------------------------------

export const listTypes = (options: { includeArchived?: boolean } = {}) =>
  request<SpoolType[]>(`${BASE}/types${query({ include_archived: options.includeArchived || undefined })}`)

export const getType = (id: string) => request<SpoolTypeDetail>(`${BASE}/types/${seg(id)}`)

/** Adds a type, or with `id` changes one (only the fields sent). */
export function upsertType(body: SpoolTypeCreate): Promise<SpoolType>
export function upsertType(body: SpoolTypePatch, id: string): Promise<SpoolType>
export function upsertType(body: SpoolTypeCreate | SpoolTypePatch, id?: string): Promise<SpoolType> {
  return id === undefined
    ? request<SpoolType>(`${BASE}/types`, json('POST', body))
    : request<SpoolType>(`${BASE}/types/${seg(id)}`, json('PATCH', body))
}

export const setTypePresets = (id: string, rows: TypePresetInput[]) =>
  request<TypePreset[]>(`${BASE}/types/${seg(id)}/presets`, json('PUT', rows))

export const setTypeKValues = (id: string, rows: TypeKValueInput[]) =>
  request<TypeKValue[]>(`${BASE}/types/${seg(id)}/k-values`, json('PUT', rows))

// -- catalogs, locations ----------------------------------------------------------------

export const listCatalog = <K extends CatalogKind>(kind: K, options: { includeArchived?: boolean } = {}) =>
  request<CatalogRows[K][]>(`${BASE}/catalog/${seg(kind)}${query({ include_archived: options.includeArchived || undefined })}`)

/** Adds a row, or with `id` changes one (only the fields sent). */
export const upsertCatalog = <K extends CatalogKind>(kind: K, body: CatalogInput<K>, id?: string) =>
  id === undefined
    ? request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}`, json('POST', body))
    : request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}/${seg(id)}`, json('PATCH', body))

export const listLocations = (options: { includeArchived?: boolean } = {}) =>
  request<LocationNode[]>(`${BASE}/locations${query({ include_archived: options.includeArchived || undefined })}`)

/** Adds a place, or with `id` changes one (only the fields sent). */
export function upsertLocation(body: LocationCreate): Promise<Location>
export function upsertLocation(body: LocationPatch, id: string): Promise<Location>
export function upsertLocation(body: LocationCreate | LocationPatch, id?: string): Promise<Location> {
  return id === undefined
    ? request<Location>(`${BASE}/locations`, json('POST', body))
    : request<Location>(`${BASE}/locations/${seg(id)}`, json('PATCH', body))
}

// -- tags, the ledger, the totals ------------------------------------------------------------

export const listTags = (options: { includeUnbound?: boolean } = {}) =>
  request<TagRow[]>(`${BASE}/tags${query({ include_unbound: options.includeUnbound || undefined })}`)

export const getTag = (id: string) => request<TagDetail>(`${BASE}/tags/${seg(id)}`)

/** A page of the ledger, newest first; pass `next_cursor` back as `cursor`. */
export const listEvents = (filters: EventQuery = {}) => request<EventPage>(`${BASE}/events${query(filters)}`)

export const getSummary = () => request<InventorySummary>(`${BASE}/summary`)

/** Every spool with the tray it is in, and each registry printer's trays (Task 9). */
export const getInventory = (options: { includeArchived?: boolean } = {}) =>
  request<FilamentInventoryView>(`${BASE}/inventory${query({ include_archived: options.includeArchived || undefined })}`)

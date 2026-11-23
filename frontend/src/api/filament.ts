/**
 * #6262 — the Filament section's client for `/api/v1/filament/*`
 * (`backend/scadbuddy/api/filament.py`), typed from the generated `schema.d.ts`.
 */
import { command, request } from './client'
import type { components, operations } from './schema'

type Schemas = components['schemas']
type Query<Op extends keyof operations> = NonNullable<operations[Op]['parameters']['query']>

export type Spool = Schemas['Spool']
/** The spec lists a field the server always sends (it has a default) as optional; here it is required. */
export type SpoolDetail = Schemas['SpoolDetail'] & Required<Pick<Schemas['SpoolDetail'], 'effective' | 'overridden' | 'names' | 'presets' | 'k_values' | 'low_stock' | 'in_use' | 'type_suppliers'>>
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
export type SpoolTypeCreate = Schemas['SpoolTypeCreate-Input']
export type SpoolTypePatch = Schemas['SpoolTypePatch']
export type TypePresetInput = Schemas['TypePresetCreate']
export type TypePreset = Schemas['TypePreset']
export type TypeKValueInput = Schemas['TypeKValueCreate']
export type TypeKValue = Schemas['TypeKValue']
export type TypeFill = Schemas['TypeFill']
export type FillRequest = Schemas['FillRequest']
/** The type fields a slicer profile fills (#6714, `filament/profile_fill.py` `Field`). */
export const FILL_FIELDS = ['nozzle_temp_min', 'nozzle_temp_max', 'bed_temp_min', 'bed_temp_max', 'density_g_cm3', 'diameter_mm', 'drying_temp_c', 'drying_hours'] as const
export type FillField = (typeof FILL_FIELDS)[number]
export type Location = Schemas['Location']
export type LocationDetail = Schemas['LocationDetail']
export type PathStep = Schemas['PathStep']
export type PlacedSpool = Schemas['PlacedSpool']
export type Resolved = Schemas['Resolved']
export type PrinterLabels = Schemas['PrinterLabels']
export type PrinterLabel = Schemas['PrinterLabel']
export type LocationNode = Schemas['LocationNode']
export type LocationCreate = Schemas['LocationCreate']
export type LocationPatch = Schemas['LocationPatch']
export type Sensor = Schemas['Sensor']
export type SensorIn = Schemas['SensorIn']
export type SensorPatch = Schemas['SensorPatch']
export type SensorReading = Schemas['SensorReading']
export type SensorNotification = Schemas['SensorNotification']
export type HaEntity = Schemas['HaEntity']
export type AmsSyncResult = Schemas['AmsSyncResult']
export type TagRow = Schemas['TagRow']
export type TagDetail = Schemas['TagDetail']
export type EventPage = Schemas['EventPage']
export type SpoolEvent = Schemas['SpoolEvent']
export type SpoolEventAdd = Omit<Schemas['SpoolEventAdd'], 'source'> & Partial<Pick<Schemas['SpoolEventAdd'], 'source'>>
export type LedgerRow = Schemas['LedgerRow']
export type EventQuery = Query<'list_events_api_v1_filament_events_get'>
export type InventorySummary = Schemas['InventorySummary']
export type SpoolHistory = Schemas['SpoolHistory']
export type TimelineRow = Schemas['TimelineRow']
export type GramsPoint = Schemas['GramsPoint']
export type SpoolMetrics = Schemas['SpoolMetrics']
export type AmsError = Schemas['AmsError']
export type Forecast = Schemas['Forecast']
export type SpoolForecast = Schemas['SpoolForecast']
export type TypeForecast = Schemas['TypeForecast']
export type InventoryMetrics = Schemas['InventoryMetrics']
export type PurchasingMetrics = Schemas['PurchasingMetrics']
export type PriceSeries = Schemas['PriceSeries']
export type SpendRow = Schemas['SpendRow']
export type MetricsQueryParams = Query<'get_metrics_api_v1_filament_metrics_get'>
export type PurchasingQueryParams = Query<'get_purchasing_metrics_api_v1_filament_metrics_purchasing_get'>
export type MaterialNumberStats = Schemas['MaterialNumberStats']
export type MaterialNumberRow = Schemas['MaterialNumberRow']
export type MaterialNumberQueryParams = Query<'get_material_number_stats_api_v1_filament_metrics_material_numbers_get'>
export type CostCalculation = Schemas['CostCalculation']
/**
 * #7152 — the route's query, with money narrowed to exact decimal strings (the spec also
 * allows a number, which would go through a float); `tax`, `shipping` and `quantity` may be left out.
 */
export type CostQueryParams = Omit<Query<'get_cost_per_kg_api_v1_filament_cost_per_kg_get'>, 'price' | 'tax' | 'shipping'> & {
  price: string
  tax?: string
  shipping?: string
}
export type FilamentInventoryView = Schemas['FilamentInventoryView']
export type CsvRow = Schemas['CsvRow']
export type ImportPreview = Schemas['ImportPreview']
export type ImportRowResult = Schemas['ImportRowResult']
export type TypeProposals = Schemas['TypeProposals']
export type TypeProposal = Schemas['TypeProposal']
export type UngroupedSpool = Schemas['UngroupedSpool']
export type TypeProposalPick = Schemas['TypeProposalPick']
export type TypeProposalResult = Schemas['TypeProposalResult']

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
  Omit<CatalogRows[K], 'id' | 'created_at' | 'updated_at' | 'archived_at' | 'bb_catalog_id' | 'aliases' | 'source' | 'synced_at' | 'bb_colour_id'>
> & { archived?: boolean }

/**
 * #6706 — the catalogs whose rows have aliases (`filament/catalog.py` `NameTable`): each
 * name and alias is a key (letters and digits, any case) no other name or alias of the
 * catalog has, a subtype's within its material.
 */
export const ALIASED_KINDS = ['materials', 'subtypes', 'brands', 'categories', 'effects'] as const
export type AliasedKind = (typeof ALIASED_KINDS)[number]
export const isAliasedKind = (kind: CatalogKind): kind is AliasedKind => (ALIASED_KINDS as readonly string[]).includes(kind)

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

// -- NTAG through SpoolBuddy (#6264) --------------------------------------------------

export type ScanResult = Schemas['ScanResult']
export type NtagQueued = Schemas['NtagQueued']
export type NtagConfirm = Schemas['NtagConfirm']
export type RfidStatus = Schemas['RfidStatus']
export type RfidReconcileReport = Schemas['RfidReconcileReport']
export type SpoolBuddyDevice = Schemas['SpoolBuddyDevice']

/** `devices` is read only by the GET: the PUT answers an empty list. */
export const getRfidStatus = () => request<RfidStatus>(`${BASE}/rfid/status`)

/** Waits up to 5 s for the write to land: `pending` with the detail `busy` means it applies on the next refresh. */
export const setOverrideMode = (enabled: boolean) => request<RfidStatus>(`${BASE}/rfid/override-mode`, json('PUT', { enabled }))

/** Runs once what the spool refresh Schedule runs: the mode, the pending tags, the override slots. */
export const reconcileRfid = () => request<RfidReconcileReport>(`${BASE}/rfid/reconcile`, { method: 'POST' })

// -- The Bambuddy sync (#6265) ----------------------------------------------------------

export type SyncStatus = Schemas['SyncStatus']
export type SyncError = Schemas['SyncError']

/** The last pass, the last clean one, its counts, the items whose last write failed, and why no pass runs (`skipped`). */
export const getSyncStatus = () => request<SyncStatus>(`${BASE}/sync/status`)

/**
 * Asks for a full pass now; it runs in the background, and `getSyncStatus` shows how it went.
 * 409 (`code` `no_bambuddy` or `nothing_pushes`) when no pass can run, 503 when it could not be asked for.
 */
export const runSync = () => request<Schemas['SyncRun']>(`${BASE}/sync/run`, { method: 'POST' })

export type SyncPass = Schemas['SyncPass']
export type SyncPassPage = Schemas['SyncPassPage']

/** #6905 — a page of sync passes, newest first, failed ones too; pass `next_cursor` back as `cursor`. */
export const listSyncPasses = (params: Query<'list_filament_sync_passes_api_v1_filament_sync_passes_get'> = {}) =>
  request<SyncPassPage>(`${BASE}/sync/passes${query(params)}`)

export type LinksResetCounts = Schemas['LinksResetCounts']
export type LinksResetPreview = Schemas['LinksResetPreview']
export type LinksResetDone = Schemas['LinksResetDone']

/**
 * #6773 — what a reset of the Bambuddy links would delete, and the instance they belong to.
 * #6845 — `same_bambuddy`: the Bambuddy set up now is the links' own, so a reset with `expectKept`
 * keeps them. `bambuddy_unreadable`: that Bambuddy could not be read, so only the delete is offered.
 * Reads ScadBuddy and the configured Bambuddy's spools.
 */
export const getResetPreview = () => request<LinksResetPreview>(`${BASE}/sync/reset-links`)

/**
 * #6773 — deletes the Bambuddy links of `confirmInstance` (the preview's `instance`), then a full pass
 * links the configured Bambuddy afresh. #6845 — `expectKept` is the preview's `same_bambuddy`: true
 * reads that Bambuddy again and, while it is still the links' own, deletes nothing (`kept`); false is
 * the confirmed delete, which reads no Bambuddy. 409 with `code` `confirm_mismatch`, `same_instance`,
 * `nothing_to_reset` or `proof_changed` (the Bambuddy could not be read or changed since the preview)
 * when it deletes nothing.
 */
export const resetSyncLinks = (confirmInstance: string, expectKept: boolean) =>
  request<LinksResetDone>(
    `${BASE}/sync/reset-links`,
    json('POST', { confirm_instance: confirmInstance, expect_kept: expectKept }),
  )

/**
 * Waits up to `timeoutS` seconds (1 to 50) for a tag at a SpoolBuddy reader; a timeout is a 200 with
 * `status: 'timeout'`. The request stays open that long: pass a `signal` to stop waiting.
 */
export const scanTag = (options: { timeoutS?: number; deviceId?: string } = {}, signal?: AbortSignal) =>
  request<ScanResult>(`${BASE}/rfid/scan`, {
    ...json('POST', { timeout_s: options.timeoutS, device_id: options.deviceId }),
    signal,
  })

/** Queues one tag write on the device for the spool's Bambuddy record; binds nothing (see `confirmNtag`). */
export const writeNtag = (spoolId: string, deviceId: string, replace = false) =>
  request<NtagQueued>(`${BASE}/spools/${seg(spoolId)}/ntag/write`, json('POST', { device_id: deviceId, replace }))

/** Binds the tag Bambuddy wrote, when it did; `bound: false` changes nothing. */
export const confirmNtag = (spoolId: string) =>
  request<NtagConfirm>(`${BASE}/spools/${seg(spoolId)}/ntag/confirm`, { method: 'POST' })

/** Drops the write the device has queued. */
export const cancelNtag = (spoolId: string, deviceId: string) =>
  request<void>(`${BASE}/spools/${seg(spoolId)}/ntag/cancel`, json('POST', { device_id: deviceId }))

/**
 * #7155 — asks Bambuddy to tare the SpoolBuddy scale; it zeroes at the device's next check-in.
 * 404 `no_device`, 409 `no_scale` or `device_busy` (another command, such as a tag write, is pending).
 */
export const tareScale = (deviceId: string) =>
  request<Schemas['ScaleTareQueued']>(`${BASE}/spoolbuddy/devices/${seg(deviceId)}/tare`, { method: 'POST' })

export const unbindTag = (tagId: string) => request<TagBinding>(`${BASE}/tags/${seg(tagId)}/binding`, { method: 'DELETE' })

/** Replaces the spool's own presets; the answer is its effective set. */
export const setSpoolPresets = (id: string, rows: PresetInput[]) =>
  request<EffectivePreset[]>(`${BASE}/spools/${seg(id)}/presets`, json('PUT', rows))

/** Replaces the spool's own K values; the answer is its effective set. */
export const setSpoolKValues = (id: string, rows: KValueInput[]) =>
  request<EffectiveKValue[]>(`${BASE}/spools/${seg(id)}/k-values`, json('PUT', rows))

/** One spool's ledger, oldest first. */
export const listSpoolEvents = (id: string) => request<SpoolEvent[]>(`${BASE}/spools/${seg(id)}/events`)

/** #6269 — the spool's timeline (newest first), its grams series, its metrics and the burn window. */
export const getSpoolHistory = (id: string) => request<SpoolHistory>(`${BASE}/spools/${seg(id)}/history`)

/** A weighing (`grams`: the filament left, without the empty spool) or a drying (`temp_c`, `hours`). */
export const addSpoolEvent = (id: string, body: SpoolEventAdd) => request<SpoolEvent>(`${BASE}/spools/${seg(id)}/events`, json('POST', body))

/** `null` takes it out of every place. */
export const moveSpool = (id: string, locationId: string | null) =>
  request<Spool>(`${BASE}/spools/${seg(id)}/move`, json('POST', { location_id: locationId }))

/** Books each loaded spool's AMS remaining percent (#6268); the answer counts what it did and what it skipped. */
export const syncAmsWeights = () => request<AmsSyncResult>(`${BASE}/spools/sync-ams-weights`, { method: 'POST' })

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

/**
 * #6714 — fills the type's empty fields from its slicer profile. `refresh` answers the profile's
 * values that differ and writes nothing; `apply` writes the fields a person confirmed (409 when
 * the profile no longer gives one), and with `k_values` the K values the refresh showed, sent back
 * as `shown_k_values` (409 when Bambuddy now gives others).
 */
export const fillTypeFromProfile = (id: string, body: FillRequest = {}) =>
  request<TypeFill>(`${BASE}/types/${seg(id)}/fill-from-profile`, json('POST', body))

/** #6714 — fills every type's empty fields from its slicer profile; `refresh` only compares. */
export const fillTypesFromProfile = (body: { refresh?: boolean } = {}) =>
  request<TypeFill[]>(`${BASE}/types/fill-from-profile`, json('POST', body))

/** #6701 — the types proposed for the spools that need one, and the spools in no group. */
export const listTypeProposals = () => request<TypeProposals>(`${BASE}/type-proposals`)

/** #6701 — makes (or reuses) each picked group's type and types its spools, in one transaction. */
export const confirmTypeProposals = (groups: TypeProposalPick[]) =>
  request<TypeProposalResult>(`${BASE}/type-proposals/confirm`, json('POST', { groups }))

// -- 3DFP preset fetch (#6625) ---------------------------------------------------------

export type ProfileFetch = Schemas['ProfileFetch']
export type ProfileFetchStarted = Schemas['ProfileFetchStarted']
export type ProfileFetchStatus = Schemas['ProfileFetchStatus']
export type MissingKey = Schemas['MissingKey']

/**
 * Starts a fetch of the type's presets from 3dfilamentprofiles.com (its saved `tdfp_id`),
 * or joins the one that runs (`joined`). 422 `no_tdfp_id` or `no_printer_models`; 503 when
 * the fetch could not be started.
 */
export const startProfileFetch = (typeId: string) =>
  request<ProfileFetchStarted>(`${BASE}/types/${seg(typeId)}/profile-fetch`, { method: 'POST' })

/** The type's last fetch (`run`, null when it never had one) and the keys it has no preset for (`gaps`). */
export const getProfileFetch = (typeId: string) => request<ProfileFetchStatus>(`${BASE}/types/${seg(typeId)}/profile-fetch`)

// -- catalogs, locations ----------------------------------------------------------------

/** A catalog in sort order; `q` keeps the rows where every word matches some text (a colour's maker, material and hex too, #6907). */
export const listCatalog = <K extends CatalogKind>(kind: K, options: { includeArchived?: boolean; q?: string } = {}) =>
  request<CatalogRows[K][]>(`${BASE}/catalog/${seg(kind)}${query({ include_archived: options.includeArchived || undefined, q: options.q?.trim() || undefined })}`)

// -- the colour catalog's syncs (#6907) -------------------------------------------------

export type ColourSync = Schemas['ColourSync']
export type ColourSyncSource = ColourSync['source']

/** Each source's newest run, with its progress while it runs. */
export const listColourSyncs = () => request<ColourSync[]>(`${BASE}/colour-syncs`)

/** Runs a sync as a command (followed past its 202); a failed read is a run with `state: 'failed'`, not an error. */
export const startColourSync = (source: ColourSyncSource, force = false) =>
  command<ColourSync>(`${BASE}/colour-syncs`, json('POST', { source, force }))

/** Adds a row, or with `id` changes one (only the fields sent). */
/** Replace a row's aliases; a key another name or alias holds is a 409 that names the holder. */
export const setCatalogAliases = <K extends AliasedKind>(kind: K, id: string, aliases: string[]) =>
  request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}/${seg(id)}/aliases`, json('PUT', { aliases }))

/** The row whose name or alias has `name`'s key (a subtype: `materialId`'s own, else one for every material); 404 for none. */
export const resolveCatalog = <K extends AliasedKind>(kind: K, name: string, materialId?: string) =>
  request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}/resolve${query({ name, material_id: materialId })}`)

export const upsertCatalog = <K extends CatalogKind>(kind: K, body: CatalogInput<K>, id?: string) =>
  id === undefined
    ? request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}`, json('POST', body))
    : request<CatalogRows[K]>(`${BASE}/catalog/${seg(kind)}/${seg(id)}`, json('PATCH', body))

export const listLocations = (options: { includeArchived?: boolean } = {}) =>
  request<LocationNode[]>(`${BASE}/locations${query({ include_archived: options.includeArchived || undefined })}`)

/** #6955 — one place with its code, kind, path, children and spools; an archived place still answers. */
export const getLocation = (id: string) => request<LocationDetail>(`${BASE}/locations/${seg(id)}`)

/** #6955 — what a short code (`L-12`, `S-142`) names; the route is `/api/v1/codes/{code}`, outside `BASE`. */
export const resolveCode = (code: string) => request<Resolved>(`/codes/${seg(code)}`)

/** #6955 — the printers' place labels; `null` is every printer. */
export const printerLabels = (printerIds: string[] | null) =>
  request<PrinterLabels>(`${BASE}/printer-labels`, json('POST', { printer_ids: printerIds }))

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

/** #6269 — each unarchived spool's burn and empty day, and each type's days of stock and reorder day. */
export const getForecast = () => request<Forecast>(`${BASE}/forecast`)

/** #6269 — stock now, and the consumption and waste from `since` to `until` (both days included). */
export const getMetrics = (params: MetricsQueryParams = {}) => request<InventoryMetrics>(`${BASE}/metrics${query(params)}`)

/** #6269 — price per kg over time and confirmed invoice spend, each in its own currency. */
export const getPurchasingMetrics = (params: PurchasingQueryParams = {}) =>
  request<PurchasingMetrics>(`${BASE}/metrics/purchasing${query(params)}`)

/** #7152 — stock, use in the range and value by material number (a type code, case folded). */
export const getMaterialNumberStats = (params: MaterialNumberQueryParams = {}) =>
  request<MaterialNumberStats>(`${BASE}/metrics/material-numbers${query(params)}`)

/** #7152 — what a price, tax, shipping and net weight come to per kg, in the shop's currency; writes nothing. */
export const calculateCostPerKg = (params: CostQueryParams) => request<CostCalculation>(`${BASE}/cost-per-kg${query(params)}`)

/** Every spool with the tray it is in, and each registry printer's trays (Task 9). */
export const getInventory = (options: { includeArchived?: boolean } = {}) =>
  request<FilamentInventoryView>(`${BASE}/inventory${query({ include_archived: options.includeArchived || undefined })}`)

// -- location sensors (#6268) -------------------------------------------------------------

export const listSensors = (locationId?: string) => request<Sensor[]>(`${BASE}/sensors${query({ location_id: locationId })}`)

/** A Home Assistant sensor; the AMS ones are made by ScadBuddy and cannot be added by hand. */
export const createSensor = (body: SensorIn) => request<Sensor>(`${BASE}/sensors`, json('POST', body))

/** Only the fields sent change; the source, kind and entity stay as made. */
export const patchSensor = (id: string, body: SensorPatch) => request<Sensor>(`${BASE}/sensors/${seg(id)}`, json('PATCH', body))

/** Takes the sensor off the lists; its readings are kept. */
export const archiveSensor = (id: string) => request<void>(`${BASE}/sensors/${seg(id)}/archive`, { method: 'POST' })

/** The newest `limit` readings since `since`, oldest first. */
export const listSensorReadings = (id: string, options: { since?: string; limit?: number } = {}) =>
  request<SensorReading[]>(`${BASE}/sensors/${seg(id)}/readings${query({ since: options.since, limit: options.limit })}`)

/** The Home Assistant entities Bambuddy can read (409 when no Bambuddy is set up). */
export const listHaEntities = (search?: string) => request<HaEntity[]>(`${BASE}/sensors/entities${query({ search: search || undefined })}`)

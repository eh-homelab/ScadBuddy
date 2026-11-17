/**
 * #6262 — the Spools tab's view, filters, grouping, columns and sort, kept in the URL
 * (`?view=&status=&needs_type=&low=&used=&new=&loaded=&material=&brand=&insert=&location=&group=&cols=&sort=`)
 * as `listingsQuery.ts` keeps the Storefront list's, so back, reload and a shared link show
 * the same rows. The columns are also remembered per viewer (`readStoredColumns`), a view
 * convenience used when the URL names none.
 */
import type { Spool } from '../../api/filament'

export type SpoolsView = 'table' | 'cards'
export type SpoolsStatus = 'active' | 'archived' | 'all'
export type SpoolsGroup = 'none' | 'material' | 'brand' | 'type' | 'location'

/** The table's columns, in the order they are drawn. */
export const COLUMN_IDS = [
  'number',
  'colour',
  'material',
  'brand',
  'subtype',
  'type',
  'grams_left',
  'label_weight',
  'location',
  'last_used',
  'note',
] as const
export type ColumnId = (typeof COLUMN_IDS)[number]

export const COLUMN_TITLES: Record<ColumnId, string> = {
  number: '#',
  colour: 'Colour',
  material: 'Material',
  brand: 'Brand',
  subtype: 'Subtype',
  type: 'Type',
  grams_left: 'Left (g)',
  label_weight: 'Label (g)',
  location: 'Location',
  last_used: 'Last used',
  note: 'Note',
}

export const DEFAULT_COLUMNS: ColumnId[] = ['number', 'colour', 'material', 'brand', 'grams_left', 'location', 'last_used']

/** The columns a sort can name: `number`, or `-number` for descending. */
const SORTABLE: readonly ColumnId[] = ['number', 'colour', 'material', 'brand', 'subtype', 'grams_left', 'label_weight', 'location', 'last_used']

export interface SpoolsQuery {
  view: SpoolsView
  status: SpoolsStatus
  needsType?: boolean
  lowStock?: boolean
  used?: boolean
  fresh?: boolean
  /** In a printer's tray (a place with a printer). */
  loaded?: boolean
  material?: string
  brand?: string
  insertType?: string
  location?: string
  group?: SpoolsGroup
  columns: ColumnId[]
  sort: string
}

export const DEFAULT_SPOOLS_QUERY: SpoolsQuery = { view: 'table', status: 'active', columns: DEFAULT_COLUMNS, sort: 'number' }

const FLAGS = [
  ['needsType', 'needs_type'],
  ['lowStock', 'low'],
  ['used', 'used'],
  ['fresh', 'new'],
  ['loaded', 'loaded'],
] as const
const NAMES = [
  ['material', 'material'],
  ['brand', 'brand'],
  ['insertType', 'insert'],
  ['location', 'location'],
] as const
const GROUPS: readonly SpoolsGroup[] = ['material', 'brand', 'type', 'location']

function isColumn(value: string): value is ColumnId {
  return (COLUMN_IDS as readonly string[]).includes(value)
}

function validSort(value: string | null): string | null {
  if (!value) return null
  const column = value.startsWith('-') ? value.slice(1) : value
  return isColumn(column) && SORTABLE.includes(column) ? value : null
}

/** The query in `params`; the columns fall back to `columns` (the viewer's), then the defaults. */
export function readQuery(params: URLSearchParams, columns: ColumnId[] = DEFAULT_COLUMNS): SpoolsQuery {
  const status = params.get('status')
  const group = params.get('group') as SpoolsGroup | null
  const cols = params.get('cols')
  const q: SpoolsQuery = {
    view: params.get('view') === 'cards' ? 'cards' : 'table',
    status: status === 'archived' || status === 'all' ? status : 'active',
    columns: cols === null ? columns : cols.split(',').filter(isColumn),
    sort: validSort(params.get('sort')) ?? 'number',
  }
  for (const [key, param] of FLAGS) {
    const value = params.get(param)
    if (value === 'true' || value === 'false') q[key] = value === 'true'
  }
  for (const [key, param] of NAMES) {
    const value = params.get(param)
    if (value) q[key] = value
  }
  if (group && GROUPS.includes(group)) q.group = group
  return q
}

/** The query as the page's URL parameters, leaving out every default. */
export function writeQuery(q: SpoolsQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (q.view !== 'table') params.set('view', q.view)
  if (q.status !== 'active') params.set('status', q.status)
  for (const [key, param] of FLAGS) if (q[key] !== undefined) params.set(param, String(q[key]))
  for (const [key, param] of NAMES) if (q[key]) params.set(param, q[key])
  if (q.group && q.group !== 'none') params.set('group', q.group)
  if (q.columns.join(',') !== DEFAULT_COLUMNS.join(',')) params.set('cols', q.columns.join(','))
  if (q.sort !== 'number') params.set('sort', q.sort)
  return params
}

/** The list's filters a spool or type page was opened from (`state.back`, a search string), or none. */
export const listBack = (state: unknown): string => {
  const back = (state as { back?: unknown } | null)?.back
  return typeof back === 'string' && back.startsWith('?') ? back : ''
}

/** A spool's page (Task 17). */
export const spoolPath = (spool: Pick<Spool, 'id'>) => `/filament/spools/${encodeURIComponent(spool.id)}`

/** Grams as a whole number with the viewer's grouping; empty when unknown. */
export const grams = (value: number | null | undefined) => (value == null ? '' : Math.round(value).toLocaleString())

const same = (a: string | null | undefined, b: string) => (a ?? '').toUpperCase() === b.toUpperCase()

/** Grams left below the label weight: the spool has been printed from (or weighed light). */
export function isUsed(spool: Spool): boolean {
  const label = spool.effective?.label_weight_g
  return spool.grams_left != null && label != null && spool.grams_left < label
}

/** Grams left at the label weight, and never printed. */
export function isFresh(spool: Spool): boolean {
  const label = spool.effective?.label_weight_g
  return spool.grams_left != null && label != null && spool.grams_left >= label && !spool.last_used_at
}

/**
 * A spool with no type yet. This is the backend's meaning (`type_id` null), which the
 * "Needs type" tile and filter count; a spool with a type is never in it.
 */
export function needsType(spool: Spool): boolean {
  return spool.type_id == null
}

/** A spool that has a type but Bambuddy's `Unknown` material: a badge of its own, not "Needs type". */
export function hasUnknownMaterial(spool: Spool): boolean {
  return spool.type_id != null && spool.names?.material === 'Unknown'
}

/**
 * The spools `q` keeps. `lowStock` reads the server's `low_stock` (F3); `loaded` reads
 * `trays`, the places that are a printer's tray.
 */
export function filterSpools(spools: readonly Spool[], q: SpoolsQuery, trays: ReadonlySet<string> = new Set()): Spool[] {
  return spools.filter((spool) => {
    const archived = spool.archived_at != null
    if (q.status === 'active' ? archived : q.status === 'archived' ? !archived : false) return false
    if (q.needsType !== undefined && needsType(spool) !== q.needsType) return false
    if (q.lowStock !== undefined && Boolean(spool.low_stock) !== q.lowStock) return false
    if (q.used !== undefined && isUsed(spool) !== q.used) return false
    if (q.fresh !== undefined && isFresh(spool) !== q.fresh) return false
    if (q.loaded !== undefined && (spool.location_id != null && trays.has(spool.location_id)) !== q.loaded) return false
    if (q.material && !same(spool.names?.material, q.material)) return false
    if (q.brand && !same(spool.names?.brand, q.brand)) return false
    if (q.insertType && !same(spool.names?.insert_type, q.insertType)) return false
    if (q.location && !same(spool.names?.location, q.location)) return false
    return true
  })
}

function sortValue(spool: Spool, column: ColumnId): string | number | null {
  switch (column) {
    case 'number':
      return spool.number
    case 'colour':
      return spool.effective?.colour_name ?? null
    case 'material':
    case 'brand':
    case 'subtype':
    case 'location':
      return spool.names?.[column] ?? null
    case 'grams_left':
      return spool.grams_left ?? null
    case 'label_weight':
      return spool.effective?.label_weight_g ?? null
    case 'last_used':
      return spool.last_used_at ?? null
    default:
      return null
  }
}

/** The spools in `sort`'s order (`-` for descending); an unknown value always last. */
export function sortSpools(spools: readonly Spool[], sort: string): Spool[] {
  const descending = sort.startsWith('-')
  const column = (descending ? sort.slice(1) : sort) as ColumnId
  return [...spools].sort((a, b) => {
    const left = sortValue(a, column)
    const right = sortValue(b, column)
    if (left === right) return a.number - b.number
    if (left === null) return 1
    if (right === null) return -1
    const order = typeof left === 'number' && typeof right === 'number' ? left - right : String(left).localeCompare(String(right))
    return descending ? -order : order
  })
}

export interface SpoolGroup {
  key: string
  label: string
  spools: Spool[]
}

const NONE_LABEL: Record<Exclude<SpoolsGroup, 'none'>, string> = {
  material: 'No material',
  brand: 'No brand',
  type: 'Needs a type',
  location: 'No location',
}

/** The spools in groups by name (in order, a spool with none last); `none` is one group. */
export function groupSpools(spools: readonly Spool[], group: SpoolsGroup | undefined, typeCodes: ReadonlyMap<string, string> = new Map()): SpoolGroup[] {
  if (!group || group === 'none') return [{ key: '', label: '', spools: [...spools] }]
  const groups = new Map<string, SpoolGroup>()
  for (const spool of spools) {
    const name = group === 'type' ? (spool.type_id ? (typeCodes.get(spool.type_id) ?? spool.type_id) : null) : (spool.names?.[group] ?? null)
    const key = name ?? ''
    const found = groups.get(key) ?? { key, label: name ?? NONE_LABEL[group], spools: [] }
    found.spools.push(spool)
    groups.set(key, found)
  }
  return [...groups.values()].sort((a, b) => (a.key === '' ? 1 : b.key === '' ? -1 : a.label.localeCompare(b.label)))
}

/** The viewer's columns (a per-browser convenience; a storage that throws is ignored). */
export const SPOOL_COLUMNS_KEY = 'scadbuddy.filament.spools.columns'

export function readStoredColumns(): ColumnId[] | null {
  try {
    const raw = window.localStorage.getItem(SPOOL_COLUMNS_KEY)
    if (!raw) return null
    const columns = raw.split(',').filter(isColumn)
    return columns.length ? columns : null
  } catch {
    return null
  }
}

export function storeColumns(columns: ColumnId[]): void {
  try {
    window.localStorage.setItem(SPOOL_COLUMNS_KEY, columns.join(','))
  } catch {
    // Private mode or a sandboxed frame: the URL still holds the choice.
  }
}

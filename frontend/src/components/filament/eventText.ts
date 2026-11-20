import type { SpoolEvent } from '../../api/filament'
import { formatDecimal } from '../../lib/decimal'

/** An id (a catalog row, a type, a place) and the name a person knows it by. */
export type EventNames = Map<string, string>

/**
 * #6269 — the names the timeline shows for the ids an event holds: every catalog row and
 * place by its `name`, every type by its `code`. A later list wins a clash, which ids
 * never have.
 */
export function eventNames(catalogRows: readonly (readonly { id: string; name: string }[])[], types: readonly { id: string; code: string }[]): EventNames {
  const names: EventNames = new Map()
  for (const rows of catalogRows) for (const row of rows) names.set(row.id, row.name)
  for (const type of types) names.set(type.id, type.code)
  return names
}

/** Who an event's author is, from `data.author` (plan Decision 10), never `data.by`. */
interface Author {
  principal?: string
  session?: string
  flow_run?: string
  tool?: string
}

function authorOf(event: SpoolEvent): Author | null {
  const author = (event.data as { author?: unknown }).author
  return author !== null && typeof author === 'object' ? (author as Author) : null
}

/**
 * Who made an event, as a person reads it: the assistant, a flow run, the Bambuddy sync,
 * the printer, ScadBuddy itself, or the person's principal.
 */
export function actorText(event: SpoolEvent): string {
  const actor = event.actor
  if (actor == null) return 'Unknown'
  if (actor === 'service:agent') {
    const author = authorOf(event)
    if (author?.session) return 'Assistant'
    if (author?.flow_run) return 'Flow run'
    return 'Assistant'
  }
  if (actor === 'bambuddy') return 'Bambuddy sync'
  if (actor === 'print' || actor === 'system:printer-watch') return 'Printer'
  if (actor.startsWith('system:')) return 'ScadBuddy'
  return actor
}

/** What each kind of event is called on the timeline. */
export const KIND_TEXT: Record<SpoolEvent['kind'], string> = {
  created: 'Added',
  edited: 'Edited',
  weighed: 'Weighed',
  ams_reading: 'AMS reading',
  dried: 'Dried',
  moved: 'Moved',
  loaded: 'Loaded into a printer',
  unloaded: 'Taken out of a printer',
  print_started: 'Print started',
  print_ended: 'Print ended',
  tag_bound: 'Tag bound',
  tag_unbound: 'Tag taken off',
  archived: 'Archived',
  restored: 'Restored',
  synced_from_bb: 'Synced from Bambuddy',
}

/** The names of the fields an `edited` event can change, as the spool page labels them. */
const FIELD_TEXT: Record<string, string> = {
  type_id: 'Type',
  material_id: 'Material',
  subtype_id: 'Subtype',
  brand_id: 'Brand',
  category_id: 'Category',
  effect_id: 'Effect',
  insert_type_id: 'Spool',
  rgba: 'Colour',
  colour_name: 'Colour name',
  extra_colours: 'Extra colours',
  diameter_mm: 'Diameter (mm)',
  density_g_cm3: 'Density (g/cm³)',
  cost_per_kg: 'Cost per kg',
  label_weight_g: 'Label weight (g)',
  low_stock_pct: 'Low stock at (%)',
  nozzle_temp_min: 'Nozzle temperature, min (°C)',
  nozzle_temp_max: 'Nozzle temperature, max (°C)',
  bed_temp_min: 'Bed temperature, min (°C)',
  bed_temp_max: 'Bed temperature, max (°C)',
  weight_locked: 'Weight lock',
  note: 'Note',
  location_id: 'Location',
  invoice_line_id: 'Invoice line',
}

/** Lists a change names as a whole, not value by value. */
const WHOLE_LIST: Record<string, string> = { presets: 'Slicer presets changed', k_values: 'K values changed' }

/** The fields whose values are exact decimal strings (`"20.0000"`), shown as decimals. */
const DECIMAL_FIELDS = new Set(['cost_per_kg', 'diameter_mm', 'density_g_cm3'])
const DECIMAL = /^-?\d+(\.\d+)?$/

const fieldText = (field: string) => FIELD_TEXT[field] ?? field.charAt(0).toUpperCase() + field.slice(1).replace(/_/g, ' ')

function valueText(field: string, value: unknown, names: EventNames): string {
  if (value == null || value === '') return 'not set'
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (Array.isArray(value)) return value.length === 0 ? 'not set' : value.map((one) => valueText(field, one, names)).join(', ')
  if (typeof value === 'string') {
    if (field.endsWith('_id')) return names.get(value) ?? value
    if (DECIMAL_FIELDS.has(field) && DECIMAL.test(value)) return formatDecimal(value)
    return value
  }
  if (typeof value === 'number') return String(value)
  return JSON.stringify(value)
}

/**
 * One line per changed field of an `edited` event's `data.changes` (`{field: [before,
 * after]}`): `Cost per kg: 20.00 → 25.00`. Ids read as their names through `names`; a
 * spool's presets and K values read as one line each.
 */
export function changeLines(changes: unknown, names: EventNames): string[] {
  if (changes === null || typeof changes !== 'object' || Array.isArray(changes)) return []
  return Object.entries(changes as Record<string, unknown>).map(([field, pair]) => {
    if (field in WHOLE_LIST) return WHOLE_LIST[field]!
    const [before, after] = Array.isArray(pair) && pair.length === 2 ? pair : [undefined, pair]
    if (before === undefined) return `${fieldText(field)}: ${valueText(field, after, names)}`
    return `${fieldText(field)}: ${valueText(field, before, names)} → ${valueText(field, after, names)}`
  })
}

/** An `edited` event a type's change wrote on the spool (plan Decision 11) says so, with the type's code. */
export function fromTypeText(event: SpoolEvent, names: EventNames): string | null {
  const typeId = (event.data as { type_id?: unknown }).type_id
  if (event.kind !== 'edited' || typeof typeId !== 'string') return null
  const code = names.get(typeId)
  return code ? `from its type ${code}` : 'from its type'
}

/** `ledger.job_of`: `data.job`, else a print run's subtask in its `ref_id` (`run/subtask/slot`). The mock uses it too. */
export function jobOf(event: SpoolEvent): string | null {
  const job = (event.data as { job?: unknown }).job
  if (typeof job === 'string') return job
  const parts = event.ref_kind === 'print_run' ? event.ref_id?.split('/') : undefined
  return parts && parts.length >= 3 ? parts.slice(1, -1).join('/') : null
}

/**
 * When each plate ended, one time per plate (plan Decision 82, R-T7-7), as `metrics.plates` counts
 * them: an ended BB usage row, and an ended SB print run BB did not book too. A `covered`
 * end is no plate and a `not_applied` one used no grams, so neither gets a tick.
 */
export function plateTimes(events: readonly SpoolEvent[]): string[] {
  const ended = events.filter((event) => event.kind === 'print_ended')
  const bbJobs = new Set(ended.filter((event) => event.ref_kind === 'bb_usage').map(jobOf))
  const basis = (event: SpoolEvent) => (event.data as { basis?: unknown }).basis
  return ended
    .filter((event) => event.ref_kind === 'bb_usage' || (event.ref_kind === 'print_run' && !bbJobs.has(jobOf(event)) && basis(event) !== 'covered' && basis(event) !== 'not_applied'))
    .map((event) => event.at)
}

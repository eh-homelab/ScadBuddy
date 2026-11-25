import { filamentPaths } from './filamentLinks.js'

// #7067: the inventory tools answer each entity's ScadBuddy page as `ui_path` beside its id
// (the #6878 pattern of filamentLinks.ts). The paths are the frontend's
// (`frontend/src/lib/inventoryLinks.ts`). The two packages share no code, so
// `test-fixtures/inventory/ui-paths.json` holds the vectors both are tested against.

const BASE = '/inventory'
const at = (list: string, id: string) => `${list}/${encodeURIComponent(id)}`
const anchored = (path: string, anchor: string) => `${path}#${encodeURIComponent(anchor)}`
const item = (id: string) => at(`${BASE}/items`, id)

/** Each inventory entity's page, relative to ScadBuddy's own origin. */
export const inventoryPaths = {
  /** The items of a kind and of its descendants (the list's `kind` filter). */
  kind: (kindId: string) => `${BASE}?${new URLSearchParams({ kind: kindId }).toString()}`,
  kinds: `${BASE}/kinds`,
  item,
  unit: (id: string) => at(`${BASE}/units`, id),
  /** The router sends a lot's path to its item's page, at the lot's row. */
  lot: (id: string) => at(`${BASE}/lots`, id),
  /** A supplier offer's row on its item's page. */
  offer: (itemId: string, offerId: string) => anchored(item(itemId), `offer-${offerId}`),
}

/** What a tool's answer is (or lists). A move and a price point have no page of their own. */
export type InventoryKind = 'kind' | 'item' | 'lot' | 'unit' | 'offer' | 'move' | 'price'

type Row = Record<string, unknown>

const str = (value: unknown): value is string => typeof value === 'string' && value !== ''

function ownPath(kind: InventoryKind, row: Row): string | null {
  if (!str(row.id)) return null
  switch (kind) {
    case 'kind':
      return inventoryPaths.kind(row.id)
    case 'item':
      return inventoryPaths.item(row.id)
    case 'lot':
      return inventoryPaths.lot(row.id)
    case 'unit':
      return inventoryPaths.unit(row.id)
    case 'offer':
      return str(row.item_id) ? inventoryPaths.offer(row.item_id, row.id) : null
    default:
      return null
  }
}

/** The fields whose values are entities of a kind, wherever they are. */
const NESTED: Record<string, InventoryKind> = {
  kinds: 'kind',
  items: 'item',
  item: 'item',
  lots: 'lot',
  lot: 'lot',
  units: 'unit',
  unit: 'unit',
  offers: 'offer',
  moves: 'move',
  prices: 'price',
  latest_price: 'price',
}

/** A field naming another entity by id, and the page of that id. */
const FOREIGN: Record<string, (id: string) => string> = {
  item_id: inventoryPaths.item,
  lot_id: inventoryPaths.lot,
  unit_id: inventoryPaths.unit,
  installed_in_unit_id: inventoryPaths.unit,
  kind_id: inventoryPaths.kind,
  supplier_id: filamentPaths.supplier,
  location_id: filamentPaths.location,
}

/**
 * `value` with each entity's `ui_path` beside its `id` (when `kind` or the field that holds it
 * says what it is) and, beside each field that names another entity by id (`item_id`,
 * `lot_id`, ...), that entity's page as `<field>_ui_path` (`item_ui_path`).
 */
export function withInventoryPaths(value: unknown, kind?: InventoryKind): unknown {
  if (Array.isArray(value)) return value.map((one) => withInventoryPaths(one, kind))
  if (value === null || typeof value !== 'object') return value
  const row = value as Row
  const out: Row = {}
  const own = kind ? ownPath(kind, row) : null
  for (const [key, field] of Object.entries(row)) {
    out[key] = withInventoryPaths(field, NESTED[key])
    if (key === 'id' && own) out.ui_path = own
    const one = FOREIGN[key]
    if (one && str(field)) out[`${key.slice(0, -'_id'.length)}_ui_path`] = one(field)
  }
  return out
}

/** What every inventory tool's description says about the paths. */
export const INVENTORY_UI_PATH_NOTE =
  " Each item, lot, unit, offer and kind in the answer has `ui_path`, its page in ScadBuddy's UI, beside its id, and" +
  ' each field that names one by id (`item_id`, `lot_id`, `unit_id`, `kind_id`, `supplier_id`, `location_id`) has' +
  ' `<field>_ui_path` beside it (`item_ui_path`). When a reply names one, link the name with that path as it is, a' +
  ' relative link on the host the user is on (`[M3 nut](/inventory/items/…)`).'

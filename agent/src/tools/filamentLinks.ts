import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import type { Tool } from './registry.js'

// #6869 (R-6869-4): the filament tools answer each entity's ScadBuddy page as `ui_path` beside
// its id, so the assistant can link the names it writes. The paths are the frontend's
// (`frontend/src/lib/filamentLinks.ts`). The two packages share no code, so
// `test-fixtures/filament/ui-paths.json` holds the vectors both are tested against.

const BASE = '/filament'
const at = (list: string, id: string) => `${list}/${encodeURIComponent(id)}`
const query = (path: string, params: Record<string, string>) => `${path}?${new URLSearchParams(params).toString()}`
const anchored = (path: string, anchor: string) => `${path}#${encodeURIComponent(anchor)}`

/** Each filament entity's page, relative to ScadBuddy's own origin. */
export const filamentPaths = {
  spool: (id: string) => at(`${BASE}/spools`, id),
  type: (id: string) => at(`${BASE}/types`, id),
  supplier: (id: string) => at(`${BASE}/suppliers`, id),
  supplierProduct: (supplierId: string, productId: string) => anchored(at(`${BASE}/suppliers`, supplierId), `product-${productId}`),
  invoice: (id: string) => at(`${BASE}/invoices`, id),
  invoiceLine: (invoiceId: string, lineId: string) => anchored(at(`${BASE}/invoices`, invoiceId), `line-${lineId}`),
  /** #6955 — a place's own page (its path, children and spools), the target of `/x/L-<n>`. */
  location: (id: string) => at(`${BASE}/locations`, id),
  /** A catalog row's catalog (the Catalog tab opens on materials). */
  catalog: (kind: string) => (kind === 'materials' ? `${BASE}/catalog` : query(`${BASE}/catalog`, { kind })),
  shoppingList: `${BASE}/shopping-list`,
}

/**
 * What a tool's answer is (or lists): an entity with a page, or a shape whose fields hold
 * entities (`price_sheet_row`, `line_candidates`). `catalog:<kind>` is a row of that catalog.
 */
export type LinkKind =
  | 'spool'
  | 'type'
  | 'supplier'
  | 'product'
  | 'invoice'
  | 'invoice_line'
  | 'location'
  | 'shopping_item'
  | 'price_sheet_row'
  | 'line_candidates'
  | `catalog:${string}`

type Row = Record<string, unknown>

const str = (value: unknown): value is string => typeof value === 'string' && value !== ''

/** An entity's own page, from its fields; none for a shape that is no entity. */
function ownPath(kind: LinkKind, row: Row): string | null {
  if (kind.startsWith('catalog:')) return filamentPaths.catalog(kind.slice('catalog:'.length))
  if (kind === 'shopping_item') return filamentPaths.shoppingList
  if (!str(row.id)) return null
  switch (kind) {
    case 'location':
      return filamentPaths.location(row.id)
    case 'spool':
      return filamentPaths.spool(row.id)
    case 'type':
      return filamentPaths.type(row.id)
    case 'supplier':
      return filamentPaths.supplier(row.id)
    case 'invoice':
      return filamentPaths.invoice(row.id)
    case 'product':
      return str(row.supplier_id) ? filamentPaths.supplierProduct(row.supplier_id, row.id) : null
    case 'invoice_line':
      return str(row.invoice_id) ? filamentPaths.invoiceLine(row.invoice_id, row.id) : null
    default:
      return null
  }
}

/** The fields of a kind whose values are entities of another kind. */
const NESTED: Partial<Record<LinkKind, Record<string, LinkKind>>> = {
  invoice: { lines: 'invoice_line' },
  location: { children: 'location', path: 'location', spools: 'spool' },
  price_sheet_row: { product: 'product' },
  line_candidates: { costed: 'spool', uncosted: 'spool' },
}

/** A field naming another entity by id, and the page of that id. */
const FOREIGN: Record<string, (id: string) => string> = {
  spool_id: filamentPaths.spool,
  sb_spool_id: filamentPaths.spool,
  empty_spool_id: filamentPaths.spool,
  location_id: filamentPaths.location,
  type_id: filamentPaths.type,
  supplier_id: filamentPaths.supplier,
  invoice_id: filamentPaths.invoice,
  existing_invoice_id: filamentPaths.invoice,
}
const FOREIGN_LISTS: Record<string, (id: string) => string> = {
  spool_ids: filamentPaths.spool,
  update_spool_ids: filamentPaths.spool,
}

/** A Bambuddy sync error's item (`SyncError`), by its `kind`; a gone item (`label` null) has none. */
function syncItemPath(row: Row): string | null {
  if (!str(row.sb_id) || !str(row.kind) || !str(row.label)) return null
  switch (row.kind) {
    case 'spool':
      return filamentPaths.spool(row.sb_id)
    case 'filament':
      return filamentPaths.type(row.sb_id)
    case 'supplier':
      return filamentPaths.supplier(row.sb_id)
    case 'location':
      return filamentPaths.location(row.sb_id)
    case 'insert_type':
      return filamentPaths.catalog('insert-types')
    default:
      return null
  }
}

/**
 * `value` with each entity's `ui_path` beside its `id` (when `kind` says what it is) and,
 * beside each field that names another entity by id (`type_id`, `spool_ids`, ...), that
 * entity's page as `<field>_ui_path` (`type_ui_path`, `location_ui_path`, `spool_ui_paths`).
 */
export function withUiPaths(value: unknown, kind?: LinkKind): unknown {
  if (Array.isArray(value)) return value.map((one) => withUiPaths(one, kind))
  if (value === null || typeof value !== 'object') return value
  const row = value as Row
  const out: Row = {}
  const own = kind ? ownPath(kind, row) : null
  const nested = kind ? (NESTED[kind] ?? {}) : {}
  for (const [key, field] of Object.entries(row)) {
    out[key] = withUiPaths(field, nested[key])
    if (key === 'id' && own) out.ui_path = own
    const one = FOREIGN[key]
    if (one && str(field)) out[`${key.slice(0, -'_id'.length)}_ui_path`] = one(field)
    const many = FOREIGN_LISTS[key]
    if (many && Array.isArray(field)) out[`${key.slice(0, -'_ids'.length)}_ui_paths`] = field.filter(str).map(many)
  }
  if (own && !('ui_path' in out)) out.ui_path = own
  const lineId = row.invoice_line_id ?? row.line_id
  if (str(row.invoice_id) && str(lineId)) out.invoice_line_ui_path = filamentPaths.invoiceLine(row.invoice_id, lineId)
  const synced = syncItemPath(row)
  if (synced) out.ui_path = synced
  return out
}

/** What every filament tool's description says about the paths (R-6869-4). */
export const UI_PATH_NOTE =
  " Each entity in the answer has `ui_path`, its page in ScadBuddy's UI, beside its id, and each field that names another" +
  ' by id (`type_id`, `spool_id`, `supplier_id`, `invoice_id`, `spool_ids`) has `<field>_ui_path` beside it' +
  ' (`type_ui_path`, `spool_ui_paths`). When a reply names a spool, type, supplier, invoice, place or catalog entry,' +
  ' link the name with that path as it is, a relative link on the host the user is on (`[Spool #12](/filament/spools/…)`).' +
  ' An item with no path (a deleted one) is named as text, never linked.'

/**
 * The tools with `ui_path`s in their answers: each answer's JSON text gets them
 * (`withUiPaths`, the root of the kind `kinds` names for the tool, else only the id fields),
 * and each description says so.
 */
export function linkingTools(tools: Tool[], kinds: Record<string, LinkKind | ((args: Record<string, unknown>) => LinkKind)>): Tool[] {
  return tools.map((tool) => {
    const kindOf = kinds[tool.name]
    return {
      ...tool,
      description: tool.description + UI_PATH_NOTE,
      async execute(args, ctx) {
        const result = await tool.execute(args, ctx)
        const kind = typeof kindOf === 'function' ? kindOf(tool.parse(args)) : kindOf
        return linked(result, kind)
      },
    }
  })
}

/** A result's JSON text with the paths added; any other content as it is. */
function linked(result: CallToolResult, kind: LinkKind | undefined): CallToolResult {
  if (result.isError) return result
  return {
    ...result,
    content: result.content.map((part) => {
      if (part.type !== 'text') return part
      let data: unknown
      try {
        data = JSON.parse(part.text)
      } catch {
        return part // a plain-text answer names no entity by id
      }
      return { ...part, text: JSON.stringify(withUiPaths(data, kind), null, 2) }
    }),
  }
}

import { z } from 'zod'
import { ok } from './call.js'
import { PROPERTY_RULES, propertyChanges, propertySet, setPropertiesAfter } from './entities.js'
import { INVENTORY_UI_PATH_NOTE, type InventoryKind, withInventoryPaths } from './inventoryLinks.js'
import { defineTool, json, ToolError } from './registry.js'

// The inventory (#7067, spec 2026-11-21-inventory-and-purchasing-design.md §6.4–6.6): item
// kinds, items, supplier offers and their pack prices, and the stock of each item in lots or
// units, changed only by moves (backend/scadbuddy/api/inventory.py, inventory_stock.py).
// Reads are `read`; every write is `write`. Every write changes ScadBuddy only:
// nothing here reaches Bambuddy (spec D16). "Re-cost past uses" is `write` too (the
// owner's Ruling 8 on #7067), though it changes the parts cost of orders already fulfilled.

const SOURCE = "ScadBuddy's own inventory"

const id = (what: string) => z.guid().describe(what)
const itemId = id("An inventory item's id, as inventory_item_list gives it")
const lotId = id("A lot's id, as inventory_item_get gives it")
const unitId = id("A unit's id, as inventory_item_get gives it")
const offerId = id("A supplier offer's id, as inventory_offer_list gives it")
const supplierId = id("A supplier's id, as filament_supplier_list gives it")
const locationId = id("A place's id, as filament_location_list gives it")
const kindId = z.string().regex(/^[a-z][a-z0-9_]{0,39}$/).describe('A kind id, `^[a-z][a-z0-9_]{0,39}$`: `part`, `hardware`, `tool`, `spool_part`, ...')
const noNul = (s: string) => !s.includes('\u0000')
const text = (max: number) => z.string().max(max).refine(noNul, 'no U+0000')
/**
 * A decimal the backend reads exactly: a number, or a string such as `"2.5"`, with at most
 * `whole` digits before the point and `places` after it (the column's `max_digits` and
 * `decimal_places`). A number is read as JavaScript writes it, so one with more places (or in
 * exponent form) is refused, never rounded.
 */
const decimalOf = (whole: number, places: number) => {
  const form = new RegExp(`^[0-9]{1,${whole}}(\\.[0-9]{1,${places}})?$`)
  const message = `a decimal of up to ${whole} digits and ${places} decimal places, such as "2.5"`
  return z
    .union([z.number().finite().min(0).refine((v) => form.test(String(v)), message), z.string().regex(form, message)])
    .describe(`A decimal with up to ${places} decimal places: a number, or a string such as "2.5"`)
}
const decimal = decimalOf(14, 6)
const above0 = (v: number | string) => Number(v) > 0
const positive = decimal.refine(above0, 'above 0')
const entityRef = z.string().min(3).max(200).refine(noNul, 'no U+0000').describe('What it was for, `kind:ref`: `printer:<id>`, `unit:<id>`, `order:<id>`, ...')
const note = text(4000)

/**
 * #6949 K1 (R-K1-24): a kind, item or stock write that runs past 15 s answers 202 with the
 * operation (`OperationAccepted`), and it still runs. The tool says so and names the
 * operation; it never sends the write again, which would be a second write.
 */
type StillRunning = { still_running: true; operation_id: string; note: string }
type FetchResult<T> = { data?: T; error?: unknown; response: Response }

async function wrote<T>(pending: Promise<FetchResult<T>>, what: string): Promise<T | StillRunning> {
  const result = await pending
  if (result.response.status !== 202) return ok(Promise.resolve(result), what)
  const id = String((result.data as { id?: unknown } | undefined)?.id ?? '')
  return {
    still_running: true,
    operation_id: id,
    note: `${what}: still running, operation ${id}. It may finish later; do not call it again. Read the row later to see the result.`,
  }
}

const isRunning = (row: unknown): row is StillRunning => typeof row === 'object' && row !== null && (row as StillRunning).still_running === true

/** The tool's answer, with the page of each entity in it. */
const answer = (data: unknown, kind?: InventoryKind) => json(withInventoryPaths(data, kind))
const changes = <T extends Record<string, unknown>>(fields: T) =>
  Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined)) as Partial<T>
const empty = (fields: Record<string, unknown>) => Object.keys(changes(fields)).length === 0
/** Exactly one of `given` is set, else a ToolError naming them. */
function oneOf<T extends Record<string, unknown>>(given: T): keyof T {
  const set = Object.keys(given).filter((k) => given[k] !== undefined)
  if (set.length !== 1) throw new ToolError(`give one of ${Object.keys(given).map((k) => `\`${k}\``).join(', ')}`)
  return set[0] as keyof T
}

const STOCK_RULES =
  'Quantities and costs are decimal strings; costs are in minor units of the shop currency (`orders_currency`). ' +
  "A move's cost is a snapshot of its lot's cost when it was written (spec D14): a later lot cost change moves no past move " +
  'until inventory_lot_recost re-costs it.'

const kindFields = z.object({
  name: z.string().min(1).max(100).refine(noNul, 'no U+0000').optional(),
  tracking: z.enum(['lot', 'unit', 'filament', 'none']).optional().describe('`lot`, `unit`, `none` (not counted); `filament` waits for the filament phase'),
  parent_id: kindId.nullable().optional().describe('The parent kind; null makes it a root'),
  uom: z.string().regex(/^[a-z]{1,8}$/).optional().describe('The unit of a quantity: `each`, `g`, `m`, ...'),
  attribute_schema: z.record(z.string(), z.unknown()).optional().describe("The schema of its items' properties, as entity_property_schema_set takes one"),
  life_basis: z.enum(['none', 'hours', 'months']).optional(),
  saleable: z.boolean().optional(),
})

const itemFields = z.object({
  kind_id: kindId.optional().describe("The item's kind, only when it is added; it never changes"),
  name: z.string().min(1).max(200).refine(noNul, 'no U+0000').optional(),
  manufacturer: text(200).optional(),
  compatible_with: z.array(z.string().min(1).max(100).refine(noNul, 'no U+0000')).max(100).optional().describe('The printer model codes it fits'),
  low_stock: decimal.optional().describe('Warn at or below this on hand; 0 never warns'),
  life_hours: decimalOf(10, 2).refine(above0, 'above 0').nullable().optional().describe('Service life in print hours; null clears it'),
  life_months: z.number().int().min(1).max(1200).nullable().optional().describe('Service life in months; null clears it'),
  saleable: z.boolean().nullable().optional().describe("Its own choice; null takes its kind's"),
  note: note.optional(),
})

const offerFields = z.object({
  item_id: itemId.optional().describe('The item offered, only when the offer is added'),
  supplier_id: supplierId.optional().describe('The supplier, only when the offer is added'),
  sku: z.string().min(1).max(100).refine(noNul, 'no U+0000').optional().describe("The supplier's SKU"),
  url: text(2000).nullable().optional().describe('The purchase link, http or https; null clears it'),
  pack_size: positive.optional().describe('Units of the item in one pack'),
  pack_uom: z.string().min(1).max(100).refine(noNul, 'no U+0000').optional(),
  note: note.nullable().optional(),
})

export const inventoryTools = [
  defineTool({
    name: 'inventory_kind_list',
    label: 'List inventory kinds',
    description:
      'The item kinds, a tree by `parent_id` (`part` > `hardware`, `tool`, `spool_part`, ...): each with its tracking ' +
      '(`lot`: stock in lots, each at its own cost; `unit`: one row per thing, with a serial; `none`: not counted, ' +
      'offers and prices only), its unit, its own `attribute_schema` and its count of items. With `archived`, archived ' +
      "kinds too. With `kind_id`, that kind's property schema instead: `schema` merged down the tree (a child's key wins), " +
      '`own_schema`, and `keys_in_use` by its items and its descendants\'.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({ kind_id: kindId.optional(), archived: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/inventory/kinds', 'GET /api/v1/inventory/kinds/{kind_id}/schema'],
    handler: async ({ kind_id, archived }, { backend }) => {
      if (kind_id !== undefined) {
        return answer(await ok(backend.GET('/api/v1/inventory/kinds/{kind_id}/schema', { params: { path: { kind_id } } }), `read the schema of kind ${kind_id}`))
      }
      return answer(await ok(backend.GET('/api/v1/inventory/kinds', { params: { query: { archived } } }), 'list the item kinds'))
    },
  }),
  defineTool({
    name: 'inventory_kind_upsert',
    label: 'Add or edit an inventory kind',
    description:
      'Add a kind (`create: true`, with `name` and `tracking`), or edit one: only the `fields` given change. ' +
      'A new kind may leave out `kind_id`: it is then numbered, id `k<number>` and code `K-<number>`, which the answer ' +
      'holds. A `kind_id` of `k` and digits only (`k12`) is 422: that id is a numbered kind\'s. ' +
      '`archived: true` archives it (409 `kind_in_use` while it has unarchived items), `false` restores it. The ' +
      "`attribute_schema` documents and types its items' properties and never refuses a key (422 `bad_property_schema` " +
      'for a keyword outside the subset entity_property_schema_set lists, and the old schema stays); a child kind ' +
      'inherits its parent\'s keys. A parent at or below the kind is 422 `kind_cycle`; a tracking change on a built-in ' +
      'kind or one with items is 409 `kind_in_use`. Only what the user says. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      kind_id: kindId.optional().describe('The kind; when it is added, leave it out to number the kind (`k<number>`)'),
      create: z.boolean().default(false).describe('Add the kind `kind_id`, or a numbered kind with no `kind_id`'),
      fields: kindFields.default({}),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'POST /api/v1/inventory/kinds',
      'PATCH /api/v1/inventory/kinds/{kind_id}',
      'POST /api/v1/inventory/kinds/{kind_id}/archive',
      'POST /api/v1/inventory/kinds/{kind_id}/restore',
    ],
    summarize: ({ kind_id, create, archived }) =>
      `${create ? 'Add' : 'Edit'} inventory kind ${kind_id ?? '(numbered)'}${archived === true ? ' and archive it' : archived === false ? ' and restore it' : ''}`,
    handler: async ({ kind_id: given, create, fields, archived }, { backend }) => {
      if (!create && given === undefined) throw new ToolError('an edit needs `kind_id`; only a new kind may leave it out')
      let kind_id = given ?? ''
      let row: unknown
      if (create) {
        const { name, tracking } = fields
        if (name === undefined || tracking === undefined) throw new ToolError('a new kind needs `name` and `tracking`')
        if (archived === false) throw new ToolError('a new kind is not archived: leave out `archived: false`')
        const body = { ...changes(fields), name, tracking, ...(given === undefined ? {} : { id: given }) }
        row = await wrote(backend.POST('/api/v1/inventory/kinds', { body }), `add kind ${given ?? name}`)
        // A numbered kind's id is in the answer.
        if (given === undefined && row && typeof row === 'object' && 'id' in row && typeof row.id === 'string') kind_id = row.id
      } else if (!empty(fields)) {
        row = await wrote(backend.PATCH('/api/v1/inventory/kinds/{kind_id}', { params: { path: { kind_id } }, body: changes(fields) }), `edit kind ${kind_id}`)
      } else if (archived === undefined) {
        throw new ToolError('nothing to change: give `fields`, `archived` or `create`')
      }
      if (isRunning(row)) return answer(row)
      const path = { params: { path: { kind_id } } }
      if (archived === true) row = await wrote(backend.POST('/api/v1/inventory/kinds/{kind_id}/archive', path), `archive kind ${kind_id}`)
      if (archived === false) row = await wrote(backend.POST('/api/v1/inventory/kinds/{kind_id}/restore', path), `restore kind ${kind_id}`)
      return answer(row, 'kind')
    },
  }),
  defineTool({
    name: 'inventory_item_list',
    label: 'List inventory items',
    description:
      'Inventory items by name, with their stock: `on_hand` (the lots summed, or the units in stock; 0 for a kind ' +
      "that is not counted), `stock_value_minor`, `cost_unknown` when some stock has no known cost, `low` at or below " +
      'its `low_stock`, and `offer_count`. `kind` takes the kind and its descendants (`part` lists every part) unless ' +
      '`descendants` is false; `q` matches the name, the manufacturer or any property value, in any case; `low` keeps ' +
      'the low ones; `compatible` those that fit a printer model code; `archived` adds archived items. Up to `limit` ' +
      `(200 when absent, at most 5000). ${STOCK_RULES}` +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      kind: kindId.optional(),
      descendants: z.boolean().optional(),
      q: z.string().min(1).max(200).refine(noNul, 'no U+0000').optional(),
      low: z.boolean().optional(),
      archived: z.boolean().optional(),
      compatible: z.string().min(1).max(100).refine(noNul, 'no U+0000').optional(),
      limit: z.number().int().min(1).max(5000).default(200),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/inventory/items'],
    handler: async (query, { backend }) => answer(await ok(backend.GET('/api/v1/inventory/items', { params: { query } }), 'list the inventory items')),
  }),
  defineTool({
    name: 'inventory_item_get',
    label: 'Read an inventory item',
    description:
      'One inventory item whole: the item, its supplier offers (each with its newest pack price), its lots in FIFO ' +
      'order or its units (by its kind\'s tracking; neither for a kind that is not counted or a filament kind) and its last `moves` stock ' +
      'moves, newest first. Give `item_id`, or a `lot_id` or `unit_id` to read that lot or unit and its item. ' +
      `${STOCK_RULES} A lot's \`on_hand\` is below 0 after a use past the stock (\`needs_review\` on that move).` +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId.optional(),
      lot_id: lotId.optional(),
      unit_id: unitId.optional(),
      moves: z.number().int().min(1).max(200).default(20).describe('How many of its last moves'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: [
      'GET /api/v1/inventory/items/{item_id}',
      'GET /api/v1/inventory/items/{item_id}/offers',
      'GET /api/v1/inventory/items/{item_id}/lots',
      'GET /api/v1/inventory/items/{item_id}/units',
      'GET /api/v1/inventory/lots/{lot_id}',
      'GET /api/v1/inventory/units/{unit_id}',
      'GET /api/v1/inventory/moves',
    ],
    handler: async ({ item_id, lot_id, unit_id, moves }, { backend }) => {
      const by = oneOf({ item_id, lot_id, unit_id })
      const out: Record<string, unknown> = {}
      let found = item_id
      if (by === 'lot_id') {
        const lot = await ok(backend.GET('/api/v1/inventory/lots/{lot_id}', { params: { path: { lot_id: lot_id! } } }), `read lot ${lot_id}`)
        out.lot = lot
        found = lot.item_id
      } else if (by === 'unit_id') {
        const unit = await ok(backend.GET('/api/v1/inventory/units/{unit_id}', { params: { path: { unit_id: unit_id! } } }), `read unit ${unit_id}`)
        out.unit = unit
        found = unit.item_id
      }
      const path = { params: { path: { item_id: found! } } }
      const item = await ok(backend.GET('/api/v1/inventory/items/{item_id}', path), `read inventory item ${found}`)
      const counted = item.tracking === 'lot' || item.tracking === 'unit'
      const [offers, stock, moved] = await Promise.all([
        ok(backend.GET('/api/v1/inventory/items/{item_id}/offers', path), `list the offers of item ${found}`),
        item.tracking === 'lot'
          ? ok(backend.GET('/api/v1/inventory/items/{item_id}/lots', path), `list the lots of item ${found}`)
          : item.tracking === 'unit'
            ? ok(backend.GET('/api/v1/inventory/items/{item_id}/units', path), `list the units of item ${found}`)
            : undefined,
        counted ? ok(backend.GET('/api/v1/inventory/moves', { params: { query: { item_id: found!, limit: moves } } }), `list the moves of item ${found}`) : undefined,
      ])
      return answer({ item, ...out, offers: offers.offers, ...(stock ?? {}), ...(moved ?? {}) })
    },
  }),
  defineTool({
    name: 'inventory_operation_get',
    label: 'Read an inventory write that is still working',
    description:
      'Reads an inventory write that answered 202 "still working", by the `id` that answer gave. It says `running`, ' +
      '`succeeded` with the route\'s answer as `result`, or `failed` with the problem as `error`. Call it instead of ' +
      'repeating the write.',
    input: z.object({
      operation_id: z.string().min(1).max(100).describe('The operation id the 202 answer gave'),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/inventory/operations/{operation_id}'],
    handler: async ({ operation_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/inventory/operations/{operation_id}', { params: { path: { operation_id } } }),
          `read inventory operation ${operation_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'inventory_item_upsert',
    label: 'Add or edit an inventory item',
    description:
      'Add an item (no `item_id`; `fields` needs `kind_id` and `name`), or edit one: only the `fields` given change, and ' +
      '`null` clears `life_hours`, `life_months` or `saleable`. The kind never changes. 409 `item_exists` when an ' +
      'unarchived item of the kind has the name, in any case; 409 `filament_item_refused` for a `filament` kind. Its ' +
      "attributes (a part number, a thread size) are its `properties`, typed by its kind's merged schema " +
      "(inventory_kind_list with `kind_id`). They go in the same request as the item, so the item and its properties " +
      'are written together or not at all. The item must have a value for each key its kind requires (the kind ' +
      "schema's `item_required`, such as a printer's `model_code`): a create or an edit that leaves one missing or " +
      'ill-fitting writes nothing (422 `missing_required_properties` names each missing and each ill-fitting key; 422 ' +
      '`bad_property_value` names a sent value that does not fit). An edit with only `properties` leaves the item ' +
      `itself untouched. ${PROPERTY_RULES} Stock changes through the lot, unit and move tools. Only what the ` +
      'user says. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId.optional().describe("An item's id; omit to add"),
      fields: itemFields.default({}),
      properties: propertySet.optional().describe("The item's properties, written with the item; `null` removes a key"),
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'POST /api/v1/inventory/items',
      'PATCH /api/v1/inventory/items/{item_id}',
      'PATCH /api/v1/entities/{ref}/properties',
      'GET /api/v1/inventory/items/{item_id}',
    ],
    summarize: ({ item_id, fields, properties }) =>
      (item_id ? `Edit inventory item ${item_id}` : `Add inventory item ${fields.name ?? ''}`.trim()) + propertyChanges(properties),
    handler: async ({ item_id, fields, properties }, { backend }) => {
      let row
      if (item_id === undefined) {
        const { kind_id, name } = fields
        if (kind_id === undefined || name === undefined) throw new ToolError('a new item needs `kind_id` and `name` in `fields`')
        row = await wrote(
          backend.POST('/api/v1/inventory/items', { body: { ...changes(fields), kind_id, name, ...(properties === undefined ? {} : { properties }) } }),
          'add an inventory item',
        )
      } else {
        if (fields.kind_id !== undefined) throw new ToolError("an item's kind does not change: leave `kind_id` out of an edit")
        const path = { params: { path: { item_id } } }
        if (empty(fields)) {
          if (properties === undefined) throw new ToolError('nothing to change: give `fields` or `properties`')
          // Properties only: no row PATCH, which would mark an imported item's fields edited.
          const set = await setPropertiesAfter(backend, `item:${item_id}`, properties)
          row = await ok(backend.GET('/api/v1/inventory/items/{item_id}', path), `read inventory item ${item_id}`)
          return answer({ ...row, properties: set }, 'item')
        }
        row = await wrote(
          backend.PATCH('/api/v1/inventory/items/{item_id}', { ...path, body: { ...changes(fields), ...(properties === undefined ? {} : { properties }) } }),
          `edit inventory item ${item_id}`,
        )
      }
      // A 202: the item and its properties are written together, once it is done.
      if (isRunning(row)) return answer(row)
      return answer(row, 'item')
    },
  }),
  defineTool({
    name: 'inventory_item_archive',
    label: 'Archive or restore an inventory item',
    description:
      'Archive an item: it leaves the lists; its stock, moves and offers stay. With `restore: true`, restore it ' +
      '(409 `kind_archived` while its kind is archived, `item_exists` when an unarchived item of its kind took its name). ' +
      'Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({ item_id: itemId, restore: z.boolean().default(false) }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/items/{item_id}/archive', 'POST /api/v1/inventory/items/{item_id}/restore'],
    summarize: ({ item_id, restore }) => `${restore ? 'Restore' : 'Archive'} inventory item ${item_id}`,
    handler: async ({ item_id, restore }, { backend }) => {
      const path = { params: { path: { item_id } } }
      const row = restore
        ? await wrote(backend.POST('/api/v1/inventory/items/{item_id}/restore', path), `restore inventory item ${item_id}`)
        : await wrote(backend.POST('/api/v1/inventory/items/{item_id}/archive', path), `archive inventory item ${item_id}`)
      return answer(row, 'item')
    },
  }),
  defineTool({
    name: 'inventory_offer_list',
    label: 'List supplier offers of items',
    description:
      "Supplier offers of inventory items, by SKU, each with its pack (`pack_size` units of `pack_uom`), its purchase " +
      "`url` and its newest pack price (`latest_price`, in major units of its `currency`). Give `item_id` for an item's " +
      "offers or `supplier_id` for a supplier's (one SKU may cover several items, a bundle); with `archived`, archived " +
      "offers too. Give `offer_id` for that offer's pack prices instead, newest first. Filament offers are " +
      'filament_product_list\'s.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId.optional(),
      supplier_id: supplierId.optional(),
      offer_id: offerId.optional(),
      archived: z.boolean().default(false),
    }),
    risk: 'read',
    source: SOURCE,
    routes: [
      'GET /api/v1/inventory/items/{item_id}/offers',
      'GET /api/v1/inventory/suppliers/{supplier_id}/offers',
      'GET /api/v1/inventory/offers/{offer_id}/prices',
    ],
    handler: async ({ item_id, supplier_id, offer_id, archived }, { backend }) => {
      const by = oneOf({ item_id, supplier_id, offer_id })
      if (by === 'offer_id') {
        return answer(await ok(backend.GET('/api/v1/inventory/offers/{offer_id}/prices', { params: { path: { offer_id: offer_id! } } }), `list the prices of offer ${offer_id}`))
      }
      if (by === 'supplier_id') {
        return answer(
          await ok(
            backend.GET('/api/v1/inventory/suppliers/{supplier_id}/offers', { params: { path: { supplier_id: supplier_id! }, query: { archived } } }),
            `list the offers of supplier ${supplier_id}`,
          ),
        )
      }
      return answer(
        await ok(backend.GET('/api/v1/inventory/items/{item_id}/offers', { params: { path: { item_id: item_id! }, query: { archived } } }), `list the offers of item ${item_id}`),
      )
    },
  }),
  defineTool({
    name: 'inventory_offer_upsert',
    label: 'Add or edit a supplier offer of an item',
    description:
      'Add an offer of an item at a supplier (no `offer_id`; `fields` needs `item_id`, `supplier_id` and `sku`), or edit ' +
      'one: only the `fields` given change, and `null` clears `url` or `note`. A SKU is unique per supplier and item ' +
      'among the unarchived, in any case (409 `offer_exists`); the same SKU for two items is a bundle and is allowed. ' +
      '`archived: true` archives it, which frees its SKU; its prices stay. Offers of filament items wait for the filament ' +
      'phase (409 `filament_item_refused`). Add its pack price with inventory_offer_price_add. Writes ScadBuddy only; ' +
      'nothing here reaches Bambuddy.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      offer_id: offerId.optional().describe("An offer's id; omit to add"),
      fields: offerFields.default({}),
      archived: z.literal(true).optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/offers', 'PATCH /api/v1/inventory/offers/{offer_id}', 'POST /api/v1/inventory/offers/{offer_id}/archive'],
    summarize: ({ offer_id, fields, archived }) =>
      (offer_id ? `Edit supplier offer ${offer_id}` : `Add supplier offer ${fields.sku ?? ''}`.trim()) + (archived ? ' and archive it' : ''),
    handler: async ({ offer_id, fields, archived }, { backend }) => {
      let row
      if (offer_id === undefined) {
        const { item_id, supplier_id, sku } = fields
        if (item_id === undefined || supplier_id === undefined || sku === undefined) {
          throw new ToolError('a new offer needs `item_id`, `supplier_id` and `sku` in `fields`')
        }
        if (archived) throw new ToolError('archived needs an offer_id')
        row = await ok(backend.POST('/api/v1/inventory/offers', { body: { ...changes(fields), item_id, supplier_id, sku } }), 'add a supplier offer')
        return answer(row, 'offer')
      }
      if (fields.item_id !== undefined || fields.supplier_id !== undefined) {
        throw new ToolError("an offer's item and supplier do not change: archive it and add another")
      }
      const path = { params: { path: { offer_id } } }
      if (!empty(fields)) row = await ok(backend.PATCH('/api/v1/inventory/offers/{offer_id}', { ...path, body: changes(fields) }), `edit offer ${offer_id}`)
      else if (!archived) throw new ToolError('nothing to change: give `fields` or `archived`')
      if (archived) row = await ok(backend.POST('/api/v1/inventory/offers/{offer_id}/archive', path), `archive offer ${offer_id}`)
      return answer(row, 'offer')
    },
  }),
  defineTool({
    name: 'inventory_offer_price_add',
    label: 'Add a pack price to an offer',
    description:
      "A pack price of a supplier offer (`manual`): `price` is one pack's, in major units of `currency` (`4.99`; up to " +
      "4 decimals), in any ISO currency, the shop's `orders_currency` when absent; `on_date` is today when absent. The " +
      'newest by date is the offer\'s `latest_price`, which a receive of the item prefills its cost from. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      offer_id: offerId,
      price: decimalOf(8, 4),
      currency: z.string().regex(/^[A-Z]{3}$/).optional().describe('An ISO 4217 code: `USD`, `EUR`, ...'),
      on_date: z.iso.date().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/offers/{offer_id}/prices'],
    summarize: ({ offer_id, price, currency }) => `Add the pack price ${price}${currency ? ` ${currency}` : ''} to offer ${offer_id}`,
    handler: async ({ offer_id, ...body }, { backend }) =>
      answer(await ok(backend.POST('/api/v1/inventory/offers/{offer_id}/prices', { params: { path: { offer_id } }, body }), `add a price to offer ${offer_id}`), 'price'),
  }),
  defineTool({
    name: 'inventory_lot_receive',
    label: 'Receive a lot of an item',
    description:
      'Stock received with no invoice yet: one lot (`origin` `manual`) and its `receive` move. The cost is ' +
      '`unit_cost_minor` or `total_cost_minor` (for the whole `qty`), not both, in minor units of `currency` (the shop\'s ' +
      'when absent). With no cost, or a cost in another currency, the lot opens at 0 with `cost_unknown` (a foreign cost ' +
      "is kept as `foreign_unit_cost_minor`); set its cost later with inventory_lot_update. An `each` item takes whole " +
      'quantities only (422 `bad_quantity`). Only for an item counted in lots (409 `wrong_counting`; ' +
      '`step_not_applicable` for an item whose class has no receive, such as a service). Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId,
      qty: positive,
      unit_cost_minor: decimal.optional(),
      total_cost_minor: decimal.optional(),
      currency: z.string().regex(/^[A-Z]{3}$/).optional(),
      location_id: locationId.optional(),
      note: note.optional(),
      received_at: z.iso.datetime({ offset: true }).optional(),
    }).refine((v) => v.unit_cost_minor === undefined || v.total_cost_minor === undefined, 'give unit_cost_minor or total_cost_minor, not both'),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/items/{item_id}/lots'],
    summarize: ({ item_id, qty }) => `Receive ${qty} of inventory item ${item_id}`,
    handler: async ({ item_id, ...body }, { backend }) =>
      answer(await wrote(backend.POST('/api/v1/inventory/items/{item_id}/lots', { params: { path: { item_id } }, body }), `receive a lot of item ${item_id}`), 'lot'),
  }),
  defineTool({
    name: 'inventory_lot_update',
    label: 'Edit a lot',
    description:
      "Change a lot's cost, place or note; only what is given changes, and `location_id` null clears the place. " +
      "`unit_cost_minor` (minor units of the shop currency) makes the lot's cost known from now on. No past move " +
      "changes: each move's cost is a snapshot (spec D14) until inventory_lot_recost re-costs the lot's past uses. " +
      'Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      lot_id: lotId,
      unit_cost_minor: decimal.optional(),
      location_id: locationId.nullable().optional(),
      note: note.optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PATCH /api/v1/inventory/lots/{lot_id}'],
    summarize: ({ lot_id, unit_cost_minor }) => `Edit lot ${lot_id}${unit_cost_minor !== undefined ? `: unit cost ${unit_cost_minor}` : ''}`,
    handler: async ({ lot_id, ...fields }, { backend }) => {
      if (empty(fields)) throw new ToolError('nothing to change: give `unit_cost_minor`, `location_id` or `note`')
      return answer(await wrote(backend.PATCH('/api/v1/inventory/lots/{lot_id}', { params: { path: { lot_id } }, body: changes(fields) }), `edit lot ${lot_id}`), 'lot')
    },
  }),
  defineTool({
    name: 'inventory_lot_recost',
    label: "Re-cost a lot's past uses",
    description:
      "\"Re-cost past uses\": every past `use`, `return` and `adjust` move of the lot takes the lot's cost now (and its " +
      "`cost_unknown`). This changes past order costs: each order item whose parts cost changes is named in `orders` " +
      'with its cost before and after (`before_minor`, `after_minor`), and its order gets an `item.parts_recosted` ' +
      'event. `moves` counts the moves that changed. Only when the user asks for it, after the lot\'s cost was ' +
      'corrected. 409 `recost_busy` when new order moves kept reaching the lot; try again. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({ lot_id: lotId }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/lots/{lot_id}/recost'],
    summarize: ({ lot_id }) => `Re-cost the past uses of lot ${lot_id}`,
    handler: async ({ lot_id }, { backend }) =>
      answer(await ok(backend.POST('/api/v1/inventory/lots/{lot_id}/recost', { params: { path: { lot_id } } }), `re-cost lot ${lot_id}`)),
  }),
  defineTool({
    name: 'inventory_unit_receive',
    label: 'Receive units of an item',
    description:
      '`count` units (1 to 100) in stock, each with a `receive` move, at `cost_minor` each in minor units of the shop ' +
      'currency (unknown when absent). `serials`, when given, has one entry per unit; a serial is unique within the ' +
      'item (409 `serial_exists`). Only for an item counted in units (409 `wrong_counting`). Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId,
      count: z.number().int().min(1).max(100),
      cost_minor: decimal.optional(),
      serials: z.array(text(200)).max(100).optional(),
      location_id: locationId.optional(),
      name: text(200).optional(),
    }).refine((v) => v.serials === undefined || v.serials.length === v.count, 'serials has one entry per unit'),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/items/{item_id}/units'],
    summarize: ({ item_id, count }) => `Receive ${count} unit(s) of inventory item ${item_id}`,
    handler: async ({ item_id, ...body }, { backend }) =>
      answer(await wrote(backend.POST('/api/v1/inventory/items/{item_id}/units', { params: { path: { item_id } }, body }), `receive units of item ${item_id}`)),
  }),
  defineTool({
    name: 'inventory_unit_update',
    label: 'Edit a unit',
    description:
      "Change a unit's name, serial or place; only what is given changes, and `serial` or `location_id` null clears it. " +
      'A serial is unique within the item (409 `serial_exists`). No stock moves; its status changes with ' +
      'inventory_unit_status. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      unit_id: unitId,
      name: text(200).optional(),
      serial: text(200).nullable().optional(),
      location_id: locationId.nullable().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PATCH /api/v1/inventory/units/{unit_id}'],
    summarize: ({ unit_id }) => `Edit unit ${unit_id}`,
    handler: async ({ unit_id, ...fields }, { backend }) => {
      if (empty(fields)) throw new ToolError('nothing to change: give `name`, `serial` or `location_id`')
      return answer(await wrote(backend.PATCH('/api/v1/inventory/units/{unit_id}', { params: { path: { unit_id } }, body: changes(fields) }), `edit unit ${unit_id}`), 'unit')
    },
  }),
  defineTool({
    name: 'inventory_unit_status',
    label: "Change a unit's status",
    description:
      "A unit's status: `in_stock`, `in_service`, `retired`, `scrapped` or `sold`. Leaving `in_stock` writes a move of " +
      "-1 at the unit's cost (`in_service` a `use`, `sold` a `sell`, `retired` a `retire`, `scrapped` a `scrap`); coming " +
      'back writes a `return` of +1; a change between two statuses out of stock moves no stock. `entity`, in the ' +
      '`kind:ref` form (`printer:<id>`), names what it went into or to. A unit fitted in another is 409 ' +
      '`unit_installed`. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      unit_id: unitId,
      status: z.enum(['in_stock', 'in_service', 'retired', 'scrapped', 'sold']),
      entity: entityRef.optional(),
      note: note.optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/units/{unit_id}/status'],
    summarize: ({ unit_id, status }) => `Set unit ${unit_id} to ${status}`,
    handler: async ({ unit_id, ...body }, { backend }) =>
      answer(await wrote(backend.POST('/api/v1/inventory/units/{unit_id}/status', { params: { path: { unit_id } }, body }), `change the status of unit ${unit_id}`), 'unit'),
  }),
  defineTool({
    name: 'inventory_move_list',
    label: 'List stock moves',
    description:
      'Stock moves matching every filter given, newest first, up to `limit` (50 when absent, at most 1000): `receive`, ' +
      '`use`, `return`, `adjust`, `sell`, `retire`, `scrap`, ... `qty` below 0 takes stock. Each holds its cost when it ' +
      'was written (`unit_cost_minor`, `amount_minor`), a snapshot; `cost_unknown` when that cost was unknown, ' +
      '`needs_review` for a take past the stock on hand. `order_item_id` gives the parts an order item took and gave back.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId.optional(),
      lot_id: lotId.optional(),
      unit_id: unitId.optional(),
      order_item_id: z.string().min(1).max(100).refine(noNul, 'no U+0000').optional(),
      limit: z.number().int().min(1).max(1000).default(50),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/inventory/moves'],
    handler: async (query, { backend }) => answer(await ok(backend.GET('/api/v1/inventory/moves', { params: { query } }), 'list the stock moves')),
  }),
  defineTool({
    name: 'inventory_move_use',
    label: 'Use some of an item',
    description:
      'Take `qty` of a lot item: FIFO by received time (the named `lot_id` first; 409 `lot_empty` when it holds ' +
      "nothing), one `use` move per lot at that lot's cost now, a snapshot that a later cost change does not move. What " +
      'no lot holds goes on the newest lot, below 0, flagged `needs_review`; an item with no lot opens a zero-cost ' +
      "`adjustment` lot of unknown cost. `entity`, in the `kind:ref` form (`printer:<id>`), names what it was used on. " +
      "An order item's parts leave stock when it is fulfilled, not through this tool. Answers the moves written. " +
      'Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId,
      qty: positive,
      lot_id: lotId.optional(),
      entity: entityRef.optional(),
      reason: note.optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/items/{item_id}/use'],
    summarize: ({ item_id, qty }) => `Use ${qty} of inventory item ${item_id}`,
    handler: async ({ item_id, ...body }, { backend }) =>
      answer(await wrote(backend.POST('/api/v1/inventory/items/{item_id}/use', { params: { path: { item_id } }, body }), `use some of item ${item_id}`)),
  }),
  defineTool({
    name: 'inventory_move_count',
    label: 'Record a stock count',
    description:
      "A count on the shelf, with a `note` saying why it differs. With `item_id`, the item's whole `on_hand` (its lots " +
      'summed): a lower count takes the difference FIFO as `adjust` moves; a higher one adds it to the newest lot at ' +
      'its cost, or to a new `adjustment` lot of unknown cost when there is none; answers the moves written. With ' +
      "`lot_id`, that lot's `on_hand`, one `adjust` move of the difference at the lot's cost; answers the lot. No move " +
      'when the count matches. Only what the user counted. Writes ScadBuddy only.' +
      INVENTORY_UI_PATH_NOTE,
    input: z.object({
      item_id: itemId.optional(),
      lot_id: lotId.optional(),
      on_hand: decimal,
      note: z.string().min(1).max(4000).refine(noNul, 'no U+0000').refine((s) => s.trim() !== '', 'a note is not blank'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/inventory/items/{item_id}/count', 'POST /api/v1/inventory/lots/{lot_id}/adjust'],
    summarize: ({ item_id, lot_id, on_hand }) => `Count ${on_hand} on hand of ${lot_id ? `lot ${lot_id}` : `inventory item ${item_id}`}`,
    handler: async ({ item_id, lot_id, on_hand, note }, { backend }) => {
      const body = { on_hand, note }
      if (oneOf({ item_id, lot_id }) === 'lot_id') {
        return answer(await wrote(backend.POST('/api/v1/inventory/lots/{lot_id}/adjust', { params: { path: { lot_id: lot_id! } }, body }), `count lot ${lot_id}`), 'lot')
      }
      return answer(await wrote(backend.POST('/api/v1/inventory/items/{item_id}/count', { params: { path: { item_id: item_id! } }, body }), `count item ${item_id}`))
    },
  }),
]

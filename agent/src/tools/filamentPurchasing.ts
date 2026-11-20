import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// Filament purchasing (#6266, epic #6260): suppliers, their products, price points and invoice drafts.
// Write tier: nothing leaves ScadBuddy. Confirming an invoice is outward: it makes spools and costs.
// A price is per pack, with tax, a decimal string.
// Routes: backend/scadbuddy/api/filament_purchasing.py.

const SOURCE = "ScadBuddy's filament purchasing records"

export const filamentPurchasingTools: Tool[] = [
  defineTool({
    name: 'filament_supplier_list',
    label: 'List filament suppliers',
    description:
      'The shops the user buys filament from, by name, each with its `product_count`. Give `supplier_id` to ' +
      'read one supplier (archived too). Archived suppliers only with `include_archived`.',
    input: z.object({
      supplier_id: z.string().uuid().optional().describe("A supplier's `id`; omit to list"),
      include_archived: z.boolean().default(false),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/suppliers', 'GET /api/v1/filament/suppliers/{supplier_id}'],
    handler: async ({ supplier_id, include_archived }, { backend }) =>
      supplier_id === undefined
        ? json(
            await ok(
              backend.GET('/api/v1/filament/suppliers', { params: { query: { include_archived } } }),
              'list the suppliers',
            ),
          )
        : json(
            await ok(
              backend.GET('/api/v1/filament/suppliers/{supplier_id}', { params: { path: { supplier_id } } }),
              `read supplier ${supplier_id}`,
            ),
          ),
  }),

  defineTool({
    name: 'filament_supplier_upsert',
    label: 'Add or edit a filament supplier',
    description:
      'Add a supplier (no `supplier_id`), or edit one: only the `fields` given change, and `null` clears a ' +
      'nullable one. A new supplier needs `name`; it may have `website` (an http or https URL), `customer_number` ' +
      'and `note`. `archived: true` takes a supplier off the lists and frees its name. 409 when an unarchived ' +
      'supplier has the name; 422 for a website that is not an http(s) URL. Only what the user says. Writes ' +
      'ScadBuddy only.',
    input: z.object({
      supplier_id: z.string().uuid().optional().describe("A supplier's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The supplier fields to set'),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/suppliers', 'PATCH /api/v1/filament/suppliers/{supplier_id}'],
    summarize: ({ supplier_id, fields }) =>
      supplier_id ? `Edit supplier ${supplier_id}` : `Add supplier ${String(fields['name'] ?? '')}`.trim(),
    handler: async ({ supplier_id, fields, archived }, { backend }) => {
      if (supplier_id === undefined) {
        if (archived !== undefined) throw new ToolError('archived needs a supplier_id')
        return json(await ok(backend.POST('/api/v1/filament/suppliers', { body: fields as never }), 'add a supplier'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/suppliers/{supplier_id}', {
            params: { path: { supplier_id } },
            body: archived === undefined ? fields : { ...fields, archived },
          }),
          `edit supplier ${supplier_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_product_list',
    label: "List a supplier's products",
    description:
      "A supplier's products by SKU: one SKU for one spool type (`type_id`), `pack_size` spools to a SKU, and " +
      'its `newest_price`. Archived products only with `include_archived`.',
    input: z.object({
      supplier_id: z.string().uuid().describe("A supplier's `id` from filament_supplier_list"),
      include_archived: z.boolean().default(false),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/suppliers/{supplier_id}/products'],
    handler: async ({ supplier_id, include_archived }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/suppliers/{supplier_id}/products', {
            params: { path: { supplier_id }, query: { include_archived } },
          }),
          `list the products of supplier ${supplier_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_product_upsert',
    label: 'Add or edit a supplier product',
    description:
      'Add a product to a supplier (`supplier_id`, no `product_id`), or edit one (`product_id`): only the ' +
      '`fields` given change, and `null` clears a nullable one. A new product needs `type_id` ' +
      '(filament_type_list) and `sku`; it may have `product_url` (an http or https URL), `pack_size` (spools ' +
      'to a SKU, default 1) and `note`. `archived: true` takes a product off the lists and frees its SKU. 409 ' +
      'when the supplier has an unarchived product with the SKU; 422 for a type that does not exist. Only what ' +
      'the user says. Writes ScadBuddy only.',
    input: z.object({
      supplier_id: z.string().uuid().optional().describe('The supplier to add to; omit to edit'),
      product_id: z.string().uuid().optional().describe("A product's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The product fields to set'),
      archived: z.boolean().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/suppliers/{supplier_id}/products', 'PATCH /api/v1/filament/products/{product_id}'],
    summarize: ({ product_id, supplier_id, fields }) =>
      product_id
        ? `Edit supplier product ${product_id}`
        : `Add product ${String(fields['sku'] ?? '')} to supplier ${supplier_id ?? ''}`.trim(),
    handler: async ({ supplier_id, product_id, fields, archived }, { backend }) => {
      if (product_id === undefined) {
        if (supplier_id === undefined) throw new ToolError('give a supplier_id to add a product, or a product_id to edit one')
        if (archived !== undefined) throw new ToolError('archived needs a product_id')
        return json(
          await ok(
            backend.POST('/api/v1/filament/suppliers/{supplier_id}/products', {
              params: { path: { supplier_id } },
              body: fields as never,
            }),
            `add a product to supplier ${supplier_id}`,
          ),
        )
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/filament/products/{product_id}', {
            params: { path: { product_id } },
            body: archived === undefined ? fields : { ...fields, archived },
          }),
          `edit supplier product ${product_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_price_list',
    label: 'List price points',
    description:
      "A supplier product's price points (`product_id`), or every price point of a spool type's products " +
      '(`type_id`, with the supplier and SKU), newest first. `price` is per pack, with tax; `price_per_kg` is ' +
      "derived from the pack size and the type's label weight. Give one of the two.",
    input: z.object({
      product_id: z.string().uuid().optional().describe("A product's `id` from filament_product_list"),
      type_id: z.string().uuid().optional().describe("A spool type's `id` from filament_type_list"),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/products/{product_id}/prices', 'GET /api/v1/filament/types/{type_id}/prices'],
    handler: async ({ product_id, type_id }, { backend }) => {
      if ((product_id === undefined) === (type_id === undefined)) {
        throw new ToolError('give a product_id or a type_id, not both and not neither')
      }
      if (product_id !== undefined) {
        return json(
          await ok(
            backend.GET('/api/v1/filament/products/{product_id}/prices', { params: { path: { product_id } } }),
            `list the prices of product ${product_id}`,
          ),
        )
      }
      return json(
        await ok(
          backend.GET('/api/v1/filament/types/{type_id}/prices', { params: { path: { type_id: type_id as string } } }),
          `list the prices of spool type ${type_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'filament_price_add',
    label: 'Add a price point',
    description:
      'Record a price for a supplier product: `price` per pack, with tax, as a decimal string (`"21.40"`). ' +
      "`currency` defaults to the shop's and `on_date` (`YYYY-MM-DD`) to today. The point is a manual one. " +
      'Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      product_id: z.string().uuid(),
      price: z.string().regex(/^\d+(\.\d{1,4})?$/, 'a decimal string, e.g. "21.40"'),
      currency: z.string().regex(/^[A-Z]{3}$/).optional(),
      on_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/products/{product_id}/prices'],
    summarize: ({ product_id, price }) => `Add price ${price} to product ${product_id}`,
    handler: async ({ product_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/products/{product_id}/prices', {
            params: { path: { product_id } },
            body,
          }),
          `add a price to product ${product_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_price_delete',
    label: 'Delete a price point',
    description:
      'Delete a manual price point. 409 for one an invoice made: it goes with its invoice. Only when the user ' +
      'asks. Writes ScadBuddy only.',
    input: z.object({ price_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/filament/prices/{price_id}'],
    summarize: ({ price_id }) => `Delete price point ${price_id}`,
    handler: async ({ price_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/filament/prices/{price_id}', { params: { path: { price_id } } }),
        `delete price point ${price_id}`,
      )
      return json({ deleted: price_id })
    },
  }),

  defineTool({
    name: 'filament_price_sheet',
    label: "Read a supplier's price sheet",
    description:
      "One row per unarchived product of a supplier, by type code: the product, its type's code and label, " +
      'its newest and previous price points, and `change_pct`, the change of the price per kg between them ' +
      '(`null` without two points).',
    input: z.object({ supplier_id: z.string().uuid() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/suppliers/{supplier_id}/price-sheet'],
    handler: async ({ supplier_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/suppliers/{supplier_id}/price-sheet', { params: { path: { supplier_id } } }),
          `read the price sheet of supplier ${supplier_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_supplier_stats',
    label: 'Read supplier stats',
    description:
      'Per supplier with a confirmed invoice, archived suppliers too (`archived`): `spool_count` (every spool its ' +
      'invoices made, archived ones included), `remaining_g` (grams left of the unarchived ones only), `consumed_g` (grams they used; a print\'s actual replaces its estimate) and ' +
      '`spend` (the invoices\' `allocated_cost`, a decimal string). Most consumed first, then by name.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/suppliers/stats'],
    handler: async (_input, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/suppliers/stats', {}), 'read the supplier stats')),
  }),

  defineTool({
    name: 'filament_invoice_list',
    label: 'List filament invoices',
    description:
      'Filament invoices, newest `invoice_date` first. Filter by `supplier_id` and `status` (`draft` or ' +
      '`confirmed`). Each has `difference` (its total less its lines and shipping), `reconciles`, `line_count`, ' +
      '`spool_count` (the spools its lines made or filled) and a `file` pointer. Money is a decimal string.',
    input: z.object({
      supplier_id: z.string().uuid().optional(),
      status: z.enum(['draft', 'confirmed']).optional(),
    }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/invoices'],
    handler: async ({ supplier_id, status }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/invoices', { params: { query: { supplier_id, status } } }),
          'list the invoices',
        ),
      ),
  }),

  defineTool({
    name: 'filament_invoice_get',
    label: 'Read a filament invoice',
    description:
      'One invoice with its lines: each line has its tax, `line_total`, `allocated_cost` (with its share of ' +
      'shipping), `unit_cost` and the `spool_ids` made from it.',
    input: z.object({ invoice_id: z.string().uuid() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/invoices/{invoice_id}'],
    handler: async ({ invoice_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/invoices/{invoice_id}', { params: { path: { invoice_id } } }),
          `read invoice ${invoice_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_bambu_colour',
    label: 'Look up a Bambu colour code',
    description:
      "Bambu Studio's colour table, by the `code` digits of a Bambu spool's SKU (the five digits after the " +
      'material, e.g. `10100`). Returns its `name`, `rgba` (`#RRGGBBAA`, one per colour; a translucent colour ' +
      'keeps its alpha, e.g. `80`), `fila_type` and `fila_id`. 404 means the table does not have the code: ' +
      'say so, and ask the user for the colour. 503 means this install has no table. Reads only.',
    input: z.object({ code: z.string().min(1).describe('The colour code: the digits of a Bambu SKU') }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/bambu-colour-codes'],
    handler: async ({ code }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/bambu-colour-codes', { params: { query: { code } } }),
          `look up Bambu colour code ${code}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_invoice_candidates',
    label: "Read an invoice's spool candidates",
    description:
      "For each line of an invoice draft: the spools with no cost whose effective material and RGBA match " +
      "the line's type (or its proposed type) exactly, and those with a cost already. A costed spool with no " +
      '`invoice_line_id` may be filled after the user agrees, and the line cost replaces its own; one with an ' +
      '`invoice_line_id` belongs to another invoice. Archived (used-up) spools are listed too, with ' +
      '`archived: true`; a line may fill one. Use it before you create ' +
      'spools for a line, so a spool the user already has is costed, not added twice. When a line has more ' +
      'candidates than you can place, ask the user. 404 for no such invoice. Reads only.',
    input: z.object({ invoice_id: z.string().uuid().describe("An invoice draft's `id`") }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/invoices/{invoice_id}/candidates'],
    handler: async ({ invoice_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/invoices/{invoice_id}/candidates', { params: { path: { invoice_id } } }),
          `read the candidates of invoice ${invoice_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_invoice_upsert',
    label: 'Add or edit a filament invoice draft',
    description:
      'Without `invoice_id`, add a draft: `fields` needs `supplier_id`, `invoice_date`, `currency`, `subtotal`, ' +
      '`total`, and may have `number`, `shipping`, `tax` and `note`; `lines` are `{description, quantity, unit_price, line_subtotal?, line_tax?, sku?, ' +
      'type_id?, ...}`. With `invoice_id`, edit a draft: the `fields` given change (`supplier_id` moves it to ' +
      'another unarchived supplier, and its lines drop their supplier product) and, when `lines` is sent, it ' +
      'replaces every line. Tax and shipping are spread over the lines on each write: give `line_tax` on every ' +
      'line or on none (422 for some). Money is a decimal string. 409 for a taken number (`existing_invoice_id`) ' +
      'or any field but `note` on a confirmed invoice (it still takes a note-only edit). An edit with both `fields` and `lines` is two requests: if the second fails, the ' +
      'header change stays. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      invoice_id: z.string().uuid().optional().describe("An invoice's `id`; omit to add"),
      fields: z.record(z.string(), z.unknown()).default({}).describe('The invoice header fields'),
      lines: z.array(z.record(z.string(), z.unknown())).optional().describe('The whole list of lines'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: [
      'POST /api/v1/filament/invoices',
      'PATCH /api/v1/filament/invoices/{invoice_id}',
      'PUT /api/v1/filament/invoices/{invoice_id}/lines',
    ],
    summarize: ({ invoice_id, fields }) =>
      invoice_id ? `Edit invoice ${invoice_id}` : `Add invoice ${String(fields['number'] ?? '')}`.trim(),
    handler: async ({ invoice_id, fields, lines }, { backend }) => {
      // The link to an import session is the import route's alone (#6267): a model edit must not move it.
      if ('import_run_id' in fields || 'if_import_run_id' in fields) {
        throw new ToolError('`import_run_id` and `if_import_run_id` are not yours to set; leave them out')
      }
      if (invoice_id === undefined) {
        return json(
          await ok(
            backend.POST('/api/v1/filament/invoices', { body: { ...fields, lines: lines ?? [] } as never }),
            'add an invoice',
          ),
        )
      }
      if (Object.keys(fields).length === 0 && lines === undefined) {
        throw new ToolError('give fields or lines to change')
      }
      let result: unknown
      if (Object.keys(fields).length > 0) {
        result = await ok(
          backend.PATCH('/api/v1/filament/invoices/{invoice_id}', {
            params: { path: { invoice_id } },
            body: fields,
          }),
          `edit invoice ${invoice_id}`,
        )
      }
      if (lines !== undefined) {
        result = await ok(
          backend.PUT('/api/v1/filament/invoices/{invoice_id}/lines', {
            params: { path: { invoice_id } },
            body: lines as never,
          }),
          `replace the lines of invoice ${invoice_id}`,
        )
      }
      return json(result)
    },
  }),

  defineTool({
    name: 'filament_invoice_confirm',
    label: 'Confirm a filament invoice',
    description:
      'Confirm a draft invoice: it makes the spools of every line (`quantity` of them, or fewer new ones when a ' +
      'line fills the spools in `update_spool_ids`), each with a `cost_per_kg` from the line cost (it replaces a ' +
      "filled spool's own cost; an archived spool takes its cost and invoice line only, and stays archived), " +
      'any new spool ' +
      'type or supplier product, and one price point per line, in one transaction. A confirmed invoice is frozen. ' +
      'Confirming it again changes nothing. 409, with nothing written, for no lines, a currency other than the ' +
      "shop's, a total more than 0.05 off its lines plus shipping, or a line that cannot be confirmed. Only when " +
      'the user asks to confirm.',
    input: z.object({ invoice_id: z.string().uuid() }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/filament/invoices/{invoice_id}/confirm'],
    summarize: ({ invoice_id }) => `Confirm invoice ${invoice_id}`,
    handler: async ({ invoice_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/filament/invoices/{invoice_id}/confirm', {
            params: { path: { invoice_id } },
          }),
          `confirm invoice ${invoice_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_invoice_delete',
    label: 'Delete a filament invoice draft',
    description:
      'Delete a draft invoice and its lines. 409 for a confirmed invoice: archive its spools instead. Only ' +
      'when the user asks. Writes ScadBuddy only.',
    input: z.object({ invoice_id: z.string().uuid() }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/filament/invoices/{invoice_id}'],
    summarize: ({ invoice_id }) => `Delete invoice ${invoice_id}`,
    handler: async ({ invoice_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/filament/invoices/{invoice_id}', { params: { path: { invoice_id } } }),
        `delete invoice ${invoice_id}`,
      )
      return json({ deleted: invoice_id })
    },
  }),

  defineTool({
    name: 'filament_shopping_list',
    label: 'Read the filament shopping list',
    description:
      'The spools the user means to buy: pending items first, then the newest. Each has its `material`, ' +
      '`subtype`, `brand`, `colour_name`, `quantity_spools`, `note` and `status` (`pending` or `purchased`). ' +
      'Only one `status` when given.',
    input: z.object({ status: z.enum(['pending', 'purchased']).optional() }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/filament/shopping-list'],
    handler: async ({ status }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/shopping-list', { params: { query: { status } } }),
          'read the shopping list',
        ),
      ),
  }),

  defineTool({
    name: 'filament_shopping_add',
    label: 'Add to the filament shopping list',
    description:
      'Add a spool to buy. Give `type_id` (filament_type_list): its material, subtype, brand and colour name ' +
      'fill what you leave out. Or give `material`, and any of `subtype`, `brand`, `colour_name`. ' +
      '`quantity_spools` is 1 to 1000, default 1. 422 for neither a type nor a material, or a type that does ' +
      'not exist. Only what the user says. Writes ScadBuddy only.',
    input: z.object({
      type_id: z.string().uuid().optional(),
      material: z.string().optional(),
      subtype: z.string().optional(),
      brand: z.string().optional(),
      colour_name: z.string().optional(),
      quantity_spools: z.number().int().min(1).max(1000).optional(),
      note: z.string().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/filament/shopping-list'],
    summarize: ({ material, type_id }) => `Add ${material ?? type_id ?? 'a spool'} to the shopping list`,
    handler: async (body, { backend }) =>
      json(await ok(backend.POST('/api/v1/filament/shopping-list', { body }), 'add to the shopping list')),
  }),

  defineTool({
    name: 'filament_shopping_update',
    label: 'Change a shopping list item',
    description:
      'Change one item: only what is given changes. `status: purchased` marks it bought; `pending` puts it back. ' +
      '`quantity_spools` is 1 to 1000. `note: null` clears the note. 404 for no such item. Only what the user ' +
      'says. Writes ScadBuddy only.',
    input: z.object({
      item_id: z.string().uuid(),
      status: z.enum(['pending', 'purchased']).optional(),
      quantity_spools: z.number().int().min(1).max(1000).optional(),
      note: z.string().nullable().optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PATCH /api/v1/filament/shopping-list/{item_id}'],
    summarize: ({ item_id }) => `Change shopping list item ${item_id}`,
    handler: async ({ item_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.PATCH('/api/v1/filament/shopping-list/{item_id}', { params: { path: { item_id } }, body }),
          `change shopping list item ${item_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'filament_shopping_remove',
    label: 'Remove from the shopping list',
    description:
      'Remove one item: give `item_id`. To clear the list, give `clear` instead: `purchased` removes only the ' +
      'purchased items, `all` removes every item. Give exactly one of them: a call with neither removes nothing. ' +
      'Returns `{removed: n}`, the number removed. Only when the user asks. Writes ScadBuddy only.',
    input: z.object({
      item_id: z.string().uuid().optional().describe('One item to remove'),
      clear: z.enum(['all', 'purchased']).optional().describe('Clear the list: every item, or only the purchased ones'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/filament/shopping-list/{item_id}', 'DELETE /api/v1/filament/shopping-list'],
    summarize: ({ item_id, clear }) =>
      item_id ? `Remove shopping list item ${item_id}` : clear === 'purchased' ? 'Clear purchased items' : 'Clear the shopping list',
    handler: async ({ item_id, clear }, { backend }) => {
      if (item_id !== undefined && clear !== undefined) throw new ToolError('give item_id or clear, not both')
      if (item_id === undefined) {
        if (clear === undefined) throw new ToolError('give item_id to remove one item, or clear: "all" or "purchased" to clear the list')
        return json(
          await ok(
            backend.DELETE('/api/v1/filament/shopping-list', { params: { query: { purchased_only: clear === 'purchased' } } }),
            'clear the shopping list',
          ),
        )
      }
      await ok(
        backend.DELETE('/api/v1/filament/shopping-list/{item_id}', { params: { path: { item_id } } }),
        `remove shopping list item ${item_id}`,
      )
      return json({ removed: 1 })
    },
  }),
]

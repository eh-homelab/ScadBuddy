import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// Filament purchasing (#6266, epic #6260): suppliers, their products and price points.
// Write tier: nothing leaves ScadBuddy. A price is per pack, with tax, a decimal string.
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
            body: (archived === undefined ? fields : { ...fields, archived }) as never,
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
            body: (archived === undefined ? fields : { ...fields, archived }) as never,
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
            body: body as never,
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
]

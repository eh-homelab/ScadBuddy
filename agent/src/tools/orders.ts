import { z } from 'zod'
import { ok } from './call.js'
import { command } from './command.js'
import { nozzles, presetRef, slotChoice, tier } from './print.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'

// Orders phase 1 (#4249, spec 2026-10-18 §7.3, plan Ruling 14): customers, orders and line
// items as ScadBuddy's own records. Routes: backend/scadbuddy/api/customers.py and
// api/orders.py. `read` lists and gets; `write` is a record the shop keeps (a customer, an
// order and its edits, a line item, a typed price, a payment the shop already received);
// `outward` is a step that commits the shop to the customer or counts work as done
// (accepting, the price, a made quantity, a shipment, closing, and cancelling, which is
// terminal). An order on hold answers 409 `order_on_hold` to every tool that would advance
// it, from the backend's lifecycle, not from each tool. The audit line (GET
// /orders/{id}/events) is the page's; order_get carries the status (coverage.ts).
//
// Phase 2 (#4250, plan Ruling 11): the price book is `read` to look at and `write` to keep
// (the policy, a filament's record); order_item_quote takes both modes, a typed price or a
// slice of the item through the print dialog's own choices (the `order_item_quote`
// operation, followed as every command is), and stays `write`: a quote records a price,
// accepting it is the outward step. The rough estimate is a `read`, never a quote.

const ID = z.string().min(1).max(100)
const orderId = ID.describe('An order id, as order_list or order_create returns it')
const itemId = ID.describe('A line item id, as order_get lists them')
const customerId = ID.describe('A customer id, as customer_list returns it')
const money = z.number().int().describe('Integer minor units (cents, pence) in the order\'s currency')
const datetime = z.string().datetime({ offset: true })

const contact = z
  .object({
    id: ID.optional().describe('Keep this row (update it in place); without an id a row is inserted'),
    kind: z.enum(['phone', 'email', 'instagram', 'facebook', 'tiktok', 'etsy', 'website', 'other']),
    value: z.string().min(1).max(500),
    label: z.string().max(100).optional(),
    primary: z.boolean().default(false).describe('At most one per kind'),
  })
  .strict()
const address = z
  .object({
    id: ID.optional().describe('Keep this row (update it in place); without an id a row is inserted'),
    label: z.string().max(100).optional(),
    recipient: z.string().min(1).max(200),
    lines: z.array(z.string()).max(6).default([]),
    locality: z.string().optional(),
    region: z.string().optional(),
    postal_code: z.string().optional(),
    country: z.string().regex(/^[A-Z]{2}$/).optional().describe('ISO 3166-1 alpha-2'),
    phone: z.string().optional(),
    default_shipping: z.boolean().default(false),
    default_billing: z.boolean().default(false),
  })
  .strict()
const charge = z
  .object({
    kind: z.enum(['shipping', 'discount', 'fee', 'tax']),
    label: z.string().min(1).max(200),
    amount_minor: money.describe('Signed: a discount is negative'),
  })
  .strict()
const reference = z
  .object({
    slug: z.string().optional(),
    commit: z.string().regex(/^[0-9a-f]{7,40}$/).optional().describe('A commit of the template, never HEAD'),
    inputs: z.record(z.string(), z.unknown()).optional().describe('The customizer inputs, `{params: {...}}`'),
  })
  .strict()
const attempt = z
  .object({
    quantity: z.number().int().min(0).describe('Made in this attempt'),
    outcome: z.enum(['succeeded', 'failed', 'partial', 'unknown']),
    failure_reason: z.string().optional(),
    started_at: datetime.optional(),
    finished_at: datetime.optional(),
    notes: z.string().default(''),
  })
  .strict()
const shipmentItem = z.object({ order_item_id: itemId, quantity: z.number().int().min(1) }).strict()

/** A decimal string, as the price book keeps every rate (§3.6: never a float). */
const decimal = z.string().regex(/^\d+(\.\d+)?$/).describe('A decimal string, such as "25.50"')
const policyFields = z
  .object({
    machine_rate_per_hour: decimal.optional(),
    setup_fee: decimal.optional().describe('Per plate'),
    labour_per_item: decimal.optional(),
    markup_percent: decimal.optional().describe('A percentage of the other lines: "15" is 15 %'),
    rounding: z.enum(['to_minor_unit', 'to_major']).optional(),
  })
  .strict()
const filamentFields = {
  material: z.string().min(1).max(100).optional(),
  subtype: z.string().max(100).nullable().optional(),
  brand: z.string().max(100).nullable().optional(),
  colour_name: z.string().max(100).nullable().optional(),
  colour: z.string().regex(/^#[0-9a-fA-F]{6}$/).optional(),
  cost_per_kg: decimal.optional().describe('In the shop\'s currency'),
  density_g_cm3: decimal.nullable().optional().describe('g/cm³, for the rough estimate'),
}
/** The print dialog's choices (`PrintChoices`): what the item is sliced with. */
const printChoices = z
  .object({
    nozzles,
    tier: tier.nullable().optional(),
    process_name: z.string().max(200).nullable().optional(),
    bed_type: z.string().max(64).optional(),
    // `catchall`, not `z.record`: see `params` in common.ts.
    filament_overrides: z.object({}).catchall(presetRef).optional().describe("A filament preset per slot id, in place of the spool's own"),
    sides: z.object({}).catchall(z.enum(['L', 'R'])).optional(),
  })
  .strict()
const sliceQuote = z
  .object({
    printer_id: z.number().int().optional().describe('Omit for the configured printer'),
    printer: z
      .string()
      .uuid()
      .optional()
      .describe(
        "A registry printer's `id` (list_printers, get_printers_now), as print_output takes it: beside " +
          '`printer_id` it must link that Bambuddy printer; a row only the LAN reaches is named by this alone, ' +
          'and a printer a run reaches over the LAN is sliced for with no Bambuddy call',
      ),
    filament_plan: z
      .object({ slots: z.array(slotChoice), force_colour_match: z.boolean().default(false) })
      .describe('One spool per slot (slot_id from 1), as the print dialog sends it'),
    choices: printChoices,
    filaments: z
      .object({})
      .catchall(ID)
      .describe(
        "The price-book filament (price_book_get) of each of the item's colour slots, keyed by `colours[].slot` " +
          '(from 0; the dialog\'s slot_id - 1): the spool slices, the filament prices',
      ),
  })
  .strict()

/** The quiet-path answer for a `delete`: openapi-fetch gives no body for a 204. */
const deleted = (what: string) => json({ deleted: what })

export const orderTools: Tool[] = [
  defineTool({
    name: 'customer_list',
    label: 'List customers',
    description:
      'List customers by name, at most `limit` (100 by default, 500 at most): search with `q` (name, company ' +
      'or any contact point) rather than scroll. Archived customers are left out unless asked for.',
    input: z.object({
      q: z.string().max(200).optional(),
      include_archived: z.boolean().default(false),
      limit: z.number().int().min(1).max(500).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/customers'],
    handler: async (query, { backend }) => json(await ok(backend.GET('/api/v1/customers', { params: { query } }), 'list customers')),
  }),
  defineTool({
    name: 'customer_get',
    label: 'Read a customer',
    description: 'One customer with their contact points and addresses.',
    input: z.object({ customer_id: customerId }),
    risk: 'read',
    routes: ['GET /api/v1/customers/{customer_id}'],
    handler: async ({ customer_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/customers/{customer_id}', { params: { path: { customer_id } } }),
          `get customer ${customer_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'customer_upsert',
    label: 'Create or edit a customer',
    description:
      'Make a customer (no `customer_id`), or edit one: each field given replaces theirs, and a `contacts` or ' +
      '`addresses` list replaces the rows whole, keeping any row it names by `id` (an address an open order ' +
      'points at keeps its id that way). `archive: true` hides them from the pickers; their orders keep naming them. ' +
      '`unarchive: true` puts an archived customer back in the pickers.',
    input: z.object({
      customer_id: customerId.optional(),
      display_name: z.string().min(1).max(200).optional(),
      company: z.string().max(200).nullable().optional(),
      notes: z.string().optional(),
      tags: z.array(z.string()).optional(),
      contacts: z.array(contact).optional(),
      addresses: z.array(address).optional(),
      archive: z.boolean().default(false),
      unarchive: z.boolean().default(false),
    }),
    risk: 'write',
    routes: [
      'POST /api/v1/customers',
      'PATCH /api/v1/customers/{customer_id}',
      'POST /api/v1/customers/{customer_id}/archive',
      'POST /api/v1/customers/{customer_id}/unarchive',
    ],
    title: ({ customer_id, display_name, archive, unarchive }) =>
      archive
        ? `Archive customer ${customer_id}`
        : unarchive
          ? `Unarchive customer ${customer_id}`
          : customer_id
            ? `Edit customer ${customer_id}`
            : `Add customer ${display_name ?? ''}`.trim(),
    handler: async ({ customer_id, archive, unarchive, ...fields }, { backend }) => {
      const body = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))
      if (archive && unarchive) throw new ToolError('archive and unarchive cannot both be true')
      if (customer_id === undefined) {
        if (archive || unarchive) throw new ToolError(`${archive ? 'archive' : 'unarchive'} needs a customer_id`)
        if (typeof body.display_name !== 'string') throw new ToolError('a new customer needs a display_name')
        return json(
          await ok(
            backend.POST('/api/v1/customers', { body: { ...body, display_name: body.display_name } as never }),
            'create a customer',
          ),
        )
      }
      const path = { customer_id }
      let customer: unknown
      if (Object.keys(body).length > 0) {
        customer = await ok(
          backend.PATCH('/api/v1/customers/{customer_id}', { params: { path }, body: body as never }),
          `edit customer ${customer_id}`,
        )
      }
      if (archive) {
        customer = await ok(
          backend.POST('/api/v1/customers/{customer_id}/archive', { params: { path } }),
          `archive customer ${customer_id}`,
        )
      }
      if (unarchive) {
        customer = await ok(
          backend.POST('/api/v1/customers/{customer_id}/unarchive', { params: { path } }),
          `unarchive customer ${customer_id}`,
        )
      }
      if (customer === undefined) {
        customer = await ok(backend.GET('/api/v1/customers/{customer_id}', { params: { path } }), `get customer ${customer_id}`)
      }
      return json(customer)
    },
  }),

  defineTool({
    name: 'order_list',
    label: 'List orders',
    description:
      'Orders, newest first, a page at a time (`limit` up to 100): pass `next_cursor` back as `cursor`. Filter by ' +
      '`status`, `customer_id`, `q` (number, notes, customer name) and a `from`/`to` window on when they were made.',
    input: z.object({
      status: z.enum(['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled']).optional(),
      customer_id: customerId.optional(),
      q: z.string().max(200).optional(),
      from: datetime.optional(),
      to: datetime.optional(),
      cursor: z.string().max(400).optional(),
      limit: z.number().int().min(1).max(100).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/orders'],
    handler: async (query, { backend }) => json(await ok(backend.GET('/api/v1/orders', { params: { query } }), 'list orders')),
  }),
  defineTool({
    name: 'order_get',
    label: 'Read an order',
    description:
      'One order whole: its status, number and addresses, its line items with their quotes and fulfilment ' +
      'attempts, charges, payments, shipments and totals.',
    input: z.object({ order_id: orderId }),
    risk: 'read',
    routes: ['GET /api/v1/orders/{order_id}'],
    handler: async ({ order_id }, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders/{order_id}', { params: { path: { order_id } } }), `get order ${order_id}`)),
  }),
  defineTool({
    name: 'order_create',
    label: 'Create an order',
    description:
      "A draft order for a customer, numbered, in the shop's currency setting; it points at the customer's " +
      'default addresses until accepted. Add its line items with order_item_add.',
    input: z.object({ customer_id: customerId, notes: z.string().default(''), due_at: datetime.optional() }),
    risk: 'write',
    routes: ['POST /api/v1/orders'],
    title: ({ customer_id }) => `Create an order for customer ${customer_id}`,
    handler: async (body, { backend }) => json(await ok(backend.POST('/api/v1/orders', { body }), 'create an order')),
  }),
  defineTool({
    name: 'order_update',
    label: 'Edit an order',
    description:
      "Edit an order: notes and due date in any open state; the address ids (the order's own customer's) and the " +
      'charges, replaced whole, while draft or quoted. `hold` puts it on hold or releases it (true/false): on hold, ' +
      'every step that advances the order is refused until released. Releasing is a write, as holding is: it ' +
      'commits nothing to the customer, and every step that does (accepting, fulfilling, shipping, closing) stays ' +
      'outward. `payment` records money the shop already received, in any state but cancelled.',
    input: z.object({
      order_id: orderId,
      notes: z.string().optional(),
      due_at: datetime.nullable().optional(),
      shipping_address_id: ID.nullable().optional(),
      billing_address_id: ID.nullable().optional(),
      charges: z.array(charge).optional(),
      hold: z.boolean().optional(),
      payment: z
        .object({
          amount_minor: money,
          method: z.string().optional(),
          reference: z.string().optional(),
          received_at: datetime.optional(),
          notes: z.string().default(''),
        })
        .strict()
        .optional(),
    }),
    risk: 'write',
    routes: [
      'PATCH /api/v1/orders/{order_id}',
      'POST /api/v1/orders/{order_id}/hold',
      'POST /api/v1/orders/{order_id}/release',
      'POST /api/v1/orders/{order_id}/payments',
    ],
    title: ({ order_id, hold, payment }) =>
      payment ? `Record a payment on order ${order_id}` : hold === undefined ? `Edit order ${order_id}` : hold ? `Hold order ${order_id}` : `Release order ${order_id}`,
    handler: async ({ order_id, hold, payment, ...fields }, { backend }) => {
      const path = { order_id }
      const edits = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))
      let order: unknown
      if (Object.keys(edits).length > 0) {
        order = await ok(backend.PATCH('/api/v1/orders/{order_id}', { params: { path }, body: edits as never }), `edit order ${order_id}`)
      }
      if (hold !== undefined) {
        order = await ok(
          backend.POST(hold ? '/api/v1/orders/{order_id}/hold' : '/api/v1/orders/{order_id}/release', { params: { path } }),
          `${hold ? 'hold' : 'release'} order ${order_id}`,
        )
      }
      let recorded: unknown
      if (payment !== undefined) {
        recorded = await ok(
          backend.POST('/api/v1/orders/{order_id}/payments', { params: { path }, body: payment }),
          `record a payment on order ${order_id}`,
        )
      }
      if (order === undefined && recorded === undefined) throw new ToolError('nothing to change: give a field, hold or payment')
      return json(recorded === undefined ? order : { order, payment: recorded })
    },
  }),

  defineTool({
    name: 'order_item_add',
    label: 'Add an order line item',
    description:
      'Add a line item: from an output (`output_id`: its commit, inputs, libraries and colours), or by hand (`slug`, ' +
      '`commit`, `inputs`). Beside `output_id`, leave the others out: one given must be the output\'s own (`inputs` ' +
      'whole, `v` included). The reference is checked against the catalogue and the commit\'s schema; a commit ' +
      'that is not a revision of the template, or an input the template does not take, is refused naming the field. ' +
      '`kind: "custom"` adds a custom item instead: a `title`, a `quantity` and a `unit_price_minor`, with no ' +
      'source (no `output_id`, `slug`, `commit` or `inputs`), for design work, a fee, packaging or a part made ' +
      'elsewhere. Its price is recorded, not accepted: the item is quoted until order_item_accept_quote accepts it. ' +
      '`requires_printing` (default ' +
      'false) marks one that still has to be made. `sku` is the shop\'s stock code, on either kind.',
    input: z.object({
      order_id: orderId,
      kind: z.enum(['model', 'custom']).optional().describe('`model` (the default) or `custom`'),
      output_id: ID.optional(),
      ...reference.shape,
      quantity: z.number().int().min(1).default(1),
      title: z
        .string()
        .max(200)
        .optional()
        .describe("Defaults to the template's name; a custom item needs one"),
      notes: z.string().default(''),
      sku: z.string().max(100).optional(),
      unit_price_minor: money.min(0).optional().describe('A custom item\'s price per unit'),
      requires_printing: z.boolean().optional().describe('A custom item\'s only'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/items'],
    title: ({ order_id, slug, output_id, kind, title }) =>
      `Add ${kind === 'custom' ? (title ?? 'a custom item') : (slug ?? output_id ?? 'an item')} to order ${order_id}`,
    handler: async ({ order_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items', {
            params: { path: { order_id } },
            // An accept is outward: this write tool leaves a custom item's price quoted.
            body: (body.kind === 'custom' ? { ...body, accept: false } : body) as never,
          }),
          `add an item to order ${order_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_update',
    label: 'Edit an order line item',
    description:
      'Edit a line item: `reference` (replaced whole) and `quantity` while the order is draft or quoted, each dropping ' +
      'the accepted quote; `title`, `notes` and `sku` in any open state. A custom item takes `unit_price_minor` and ' +
      '`requires_printing`: a new quantity or price records a new quote at it, not accepted (the item is quoted until ' +
      'order_item_accept_quote). A `reference` or ' +
      '`output_id` given to a custom item makes it a model item, back on the quote flow. `delete: true` removes a ' +
      'draft item; a quoted one is refused with `item_not_draft` (cancel it with order_cancel instead).',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      reference: reference.optional(),
      output_id: ID.nullable().optional(),
      quantity: z.number().int().min(1).optional(),
      title: z.string().max(200).optional(),
      notes: z.string().optional(),
      sku: z.string().max(100).nullable().optional(),
      unit_price_minor: money.min(0).optional().describe('A custom item\'s price per unit'),
      requires_printing: z.boolean().optional(),
      delete: z.boolean().default(false),
    }),
    risk: 'write',
    routes: ['PATCH /api/v1/orders/{order_id}/items/{item_id}', 'DELETE /api/v1/orders/{order_id}/items/{item_id}'],
    title: ({ order_id, item_id, delete: remove }) => `${remove ? 'Delete' : 'Edit'} item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, delete: remove, ...fields }, { backend }) => {
      const path = { order_id, item_id }
      if (remove) {
        await ok(backend.DELETE('/api/v1/orders/{order_id}/items/{item_id}', { params: { path } }), `delete item ${item_id}`)
        return deleted(item_id)
      }
      const body = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))
      // An accept is outward: a custom item repriced here stays quoted. The tool does not
      // read the item's kind, so it sends the flag on any edit of a quantity or price; on a
      // model item the backend reads nothing of it (a quantity there drops the quote).
      if (body.quantity !== undefined || body.unit_price_minor !== undefined) body.accept = false
      return json(
        await ok(
          backend.PATCH('/api/v1/orders/{order_id}/items/{item_id}', { params: { path }, body: body as never }),
          `edit item ${item_id}`,
        ),
      )
    },
  }),
  defineTool({
    name: 'order_item_quote',
    label: 'Quote an order line item',
    description:
      'Quote a line item, in one of two modes. `manual`: a typed price (one line, cost = price, the quantity as ' +
      'it is now). `slice`: render the item if it has no matching output, slice every plate with the print ' +
      "dialog's own choices (printer, a spool per slot, nozzles and quality, as print_output takes them) and price " +
      "the plates' grams and seconds with the price book in force, each slot at the filament `filaments` names; " +
      "those filaments are written onto the item first (a change drops its accepted quote). A slice quote may run " +
      'for minutes: past the follow window it answers the running operation, to follow with get_operation. The ' +
      'item is quoted; order_item_accept_quote pins it.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      manual: z
        .object({ unit_price_minor: money.describe('Per unit'), label: z.string().min(1).max(200).optional() })
        .strict()
        .optional(),
      slice: sliceQuote.optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/quotes', 'POST /api/v1/orders/{order_id}/items/{item_id}/quote'],
    title: ({ order_id, item_id, slice }) => `${slice ? 'Quote from a slice' : 'Price'} item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, manual, slice }, ctx) => {
      if ((manual === undefined) === (slice === undefined)) {
        throw new ToolError('give exactly one of `manual` (a typed price) or `slice` (the print dialog\'s choices)')
      }
      const path = { order_id, item_id }
      if (manual !== undefined) {
        return json(
          await ok(
            ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quotes', { params: { path }, body: manual as never }),
            `quote item ${item_id}`,
          ),
        )
      }
      return json(
        await command(ctx, `quote item ${item_id} from a slice`, (headers) =>
          ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quote', { params: { path }, body: slice as never, headers }),
        ),
      )
    },
  }),
  defineTool({
    name: 'order_item_accept_quote',
    label: "Accept a line item's quote",
    description:
      "Pin a quote as the item's price: the item becomes approved, and the order quoted once every open item is. " +
      'Commits the shop to that price.',
    input: z.object({ order_id: orderId, item_id: itemId, quote_id: ID }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/quotes/{quote_id}/accept'],
    summarize: ({ order_id, item_id, quote_id }) => `Accept quote ${quote_id} for item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, quote_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quotes/{quote_id}/accept', {
            params: { path: { order_id, item_id, quote_id } },
          }),
          `accept quote ${quote_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'order_accept',
    label: 'Accept an order',
    description:
      'Accept an order: every open item must have an accepted quote (`items_not_quoted` names the rest). Copies ' +
      "the customer's addresses onto the order and pins every item; the shop is committed to the customer.",
    input: z.object({ order_id: orderId }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/accept'],
    summarize: ({ order_id }) => `Accept order ${order_id}`,
    handler: async ({ order_id }, { backend }) =>
      json(await ok(backend.POST('/api/v1/orders/{order_id}/accept', { params: { path: { order_id } } }), `accept order ${order_id}`)),
  }),
  defineTool({
    name: 'order_item_fulfil',
    label: 'Record or edit a fulfilment attempt',
    description:
      'Record a fulfilment attempt by hand (`attempt`: the quantity made and the outcome; a failed one fails the ' +
      'item, the next returns it to approved), edit one not yet accepted (`fulfilment_id` with `attempt`), or accept ' +
      'one (`fulfilment_id` with `accept: true`): its quantity counts as made, and the item is fulfilled once its ' +
      "accepted attempts reach its quantity. Needs an accepted order.",
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      fulfilment_id: ID.optional(),
      attempt: attempt.optional(),
      accept: z.boolean().default(false),
    }),
    risk: 'outward',
    routes: [
      'POST /api/v1/orders/{order_id}/items/{item_id}/fulfilments',
      'PATCH /api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}',
      'POST /api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/accept',
    ],
    summarize: ({ order_id, item_id, fulfilment_id, attempt: made, accept }) =>
      accept
        ? `Count attempt ${fulfilment_id ?? '?'} of item ${item_id} (order ${order_id}) as made`
        : `${fulfilment_id ? 'Edit' : 'Record'} a ${made?.outcome ?? ''} attempt (${made?.quantity ?? '?'} made) for item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, fulfilment_id, attempt: made, accept }, { backend }) => {
      if (fulfilment_id === undefined) {
        if (made === undefined) throw new ToolError('give an attempt to record, or a fulfilment_id to edit or accept')
        return json(
          await ok(
            backend.POST('/api/v1/orders/{order_id}/items/{item_id}/fulfilments', {
              params: { path: { order_id, item_id } },
              body: made,
            }),
            `record an attempt for item ${item_id}`,
          ),
        )
      }
      const path = { order_id, item_id, fulfilment_id }
      let result: unknown
      if (made !== undefined) {
        result = await ok(
          backend.PATCH('/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}', { params: { path }, body: made }),
          `edit attempt ${fulfilment_id}`,
        )
      }
      if (accept) {
        result = await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/accept', { params: { path } }),
          `accept attempt ${fulfilment_id}`,
        )
      }
      if (result === undefined) throw new ToolError('give an attempt to edit, or accept: true')
      return json(result)
    },
  }),
  defineTool({
    name: 'order_item_unfulfil',
    description:
      "Undo a line item's fulfilment (#4952): its latest accepted attempt is no longer counted as made (the attempt " +
      'stays, and order_item_fulfil may edit or accept it again), and the item goes back to the status its latest ' +
      'attempt gives it (`approved`, or `failed`); the order follows (`ready` goes back to `in_production`). ' +
      'Refused with `item_not_fulfilled`, `order_item_shipped` (a shipment carries the item: its `shipments` name ' +
      'them; take the item off them with order_ship first), `order_on_hold`, and `unfulfil_disabled` while the shop turned ' +
      'Unfulfil off in Settings (`orders_allow_unfulfil`). Outward, as accepting the attempt is.',
    input: z.object({ order_id: orderId, item_id: itemId }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/unfulfil'],
    summarize: ({ order_id, item_id }) => `Undo the fulfilment of item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/unfulfil', { params: { path: { order_id, item_id } } }),
          `unfulfil item ${item_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_ship',
    label: 'Record a shipment',
    description:
      'Record a shipment of fulfilled items (or edit one with `shipment_id`): carrier, service, tracking, when, the ' +
      "address (the order's shipping address when none is given) and which items in what quantity. The order is " +
      'shipped once its shipments cover every fulfilled item.',
    input: z.object({
      order_id: orderId,
      shipment_id: ID.optional(),
      carrier: z.string().optional(),
      service: z.string().optional(),
      tracking: z.string().optional(),
      shipped_at: datetime.optional(),
      address: address.omit({ default_shipping: true, default_billing: true }).optional(),
      cost_minor: money.optional(),
      notes: z.string().optional(),
      items: z.array(shipmentItem).optional(),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/shipments', 'PATCH /api/v1/orders/{order_id}/shipments/{shipment_id}'],
    summarize: ({ order_id, shipment_id, items }) =>
      shipment_id
        ? `Edit shipment ${shipment_id} of order ${order_id}${items ? ` (${items.length} item line(s))` : ''}`
        : `Ship ${items?.length ?? 0} item line(s) of order ${order_id}`,
    handler: async ({ order_id, shipment_id, ...body }, { backend }) =>
      json(
        shipment_id === undefined
          ? await ok(
              backend.POST('/api/v1/orders/{order_id}/shipments', { params: { path: { order_id } }, body: body as never }),
              `ship order ${order_id}`,
            )
          : await ok(
              backend.PATCH('/api/v1/orders/{order_id}/shipments/{shipment_id}', {
                params: { path: { order_id, shipment_id } },
                body: body as never,
              }),
              `edit shipment ${shipment_id}`,
            ),
      ),
  }),
  defineTool({
    name: 'order_close',
    label: 'Close an order',
    description:
      'Close an order: refused with `order_not_closable` naming every item still open and every fulfilled item no ' +
      'shipment covers.',
    input: z.object({ order_id: orderId }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/close'],
    summarize: ({ order_id }) => `Close order ${order_id}`,
    handler: async ({ order_id }, { backend }) =>
      json(await ok(backend.POST('/api/v1/orders/{order_id}/close', { params: { path: { order_id } } }), `close order ${order_id}`)),
  }),
  defineTool({
    name: 'order_reopen',
    label: 'Reopen an order',
    description:
      'Undo a close or a cancel. A closed order takes the status its items and shipments give it (`shipped` as ' +
      'it closed); a cancelled order goes back to the status it was cancelled from, with the items that cancel took. ' +
      'With `item_id`, one cancelled line item comes back as its quotes and attempts say (an accepted order takes ' +
      'back a priced item only, `item_not_priced`). Outward, as a cancel is: it changes what the shop told the customer.',
    input: z.object({ order_id: orderId, item_id: itemId.optional() }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/reopen', 'POST /api/v1/orders/{order_id}/items/{item_id}/reopen'],
    summarize: ({ order_id, item_id }) => `Reopen ${item_id ? `item ${item_id} of ` : ''}order ${order_id}`,
    handler: async ({ order_id, item_id }, { backend }) =>
      json(
        item_id === undefined
          ? await ok(backend.POST('/api/v1/orders/{order_id}/reopen', { params: { path: { order_id } } }), `reopen order ${order_id}`)
          : await ok(
              backend.POST('/api/v1/orders/{order_id}/items/{item_id}/reopen', { params: { path: { order_id, item_id } } }),
              `reopen item ${item_id}`,
            ),
      ),
  }),
  defineTool({
    name: 'order_cancel',
    label: 'Cancel an order',
    description:
      'Cancel an order (from any state before shipped: its open items are cancelled, fulfilled ones keep their ' +
      'attempts), or one line item (`item_id`). Terminal: it commits the shop to the customer as an accept does.',
    input: z.object({ order_id: orderId, item_id: itemId.optional(), reason: z.string().max(500).optional() }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/cancel', 'POST /api/v1/orders/{order_id}/items/{item_id}/cancel'],
    summarize: ({ order_id, item_id, reason }) =>
      `Cancel ${item_id ? `item ${item_id} of ` : ''}order ${order_id}${reason ? `: ${reason}` : ''}`,
    handler: async ({ order_id, item_id, reason }, { backend }) =>
      json(
        item_id === undefined
          ? await ok(
              backend.POST('/api/v1/orders/{order_id}/cancel', { params: { path: { order_id } }, body: { reason: reason ?? null } }),
              `cancel order ${order_id}`,
            )
          : await ok(
              backend.POST('/api/v1/orders/{order_id}/items/{item_id}/cancel', { params: { path: { order_id, item_id } } }),
              `cancel item ${item_id}`,
            ),
      ),
  }),

  defineTool({
    name: 'price_book_get',
    label: 'Read the price book',
    description:
      "The price book in force: the currency the next order copies (and its `minor_exponent`), the pricing policy " +
      '(machine rate per hour, setup fee per plate, labour per item, markup percent, rounding), the rough ' +
      "estimate's factor, and the filament records not archived, each with its cost per kilogram.",
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/orders/price-book'],
    handler: async (_input, { backend }) => json(await ok(backend.GET('/api/v1/orders/price-book'), 'read the price book')),
  }),
  defineTool({
    name: 'price_book_set',
    label: 'Edit the price book',
    description:
      'Keep the price book: `policy` (the fields given replace those in force, the rest stay), ' +
      "`rough_estimate_factor`, and `filament`: a new record (no `filament_id`; material, colour and cost_per_kg), " +
      'an edit of one (`filament_id` with the fields to change), or `archive: true` to hide it from the book. A ' +
      'recorded quote keeps the rates it was priced at.',
    input: z.object({
      policy: policyFields.optional(),
      rough_estimate_factor: decimal.optional(),
      filament: z
        .object({ filament_id: ID.optional(), archive: z.boolean().default(false), ...filamentFields })
        .strict()
        .optional(),
    }),
    risk: 'write',
    routes: [
      'PUT /api/v1/orders/price-book',
      'POST /api/v1/orders/price-book/filaments',
      'PATCH /api/v1/orders/price-book/filaments/{filament_id}',
      'POST /api/v1/orders/price-book/filaments/{filament_id}/archive',
    ],
    title: ({ policy, filament }) =>
      filament?.archive
        ? `Archive filament ${filament.filament_id ?? ''}`.trim()
        : filament
          ? filament.filament_id
            ? `Edit filament ${filament.filament_id}`
            : `Add filament ${filament.material ?? ''}`.trim()
          : policy
            ? 'Set the pricing policy'
            : 'Set the price book',
    handler: async ({ policy, rough_estimate_factor, filament }, { backend }) => {
      if (policy === undefined && rough_estimate_factor === undefined && filament === undefined) {
        throw new ToolError('nothing to change: give a policy, a rough_estimate_factor or a filament')
      }
      const answer: Record<string, unknown> = {}
      if (policy !== undefined || rough_estimate_factor !== undefined) {
        // The PUT takes the whole policy: the fields not given keep their values.
        const current = await ok(backend.GET('/api/v1/orders/price-book'), 'read the price book')
        const given = Object.fromEntries(Object.entries(policy ?? {}).filter(([, v]) => v !== undefined))
        answer.book = await ok(
          backend.PUT('/api/v1/orders/price-book', {
            body: { ...current.policy, ...given, ...(rough_estimate_factor === undefined ? {} : { rough_estimate_factor }) } as never,
          }),
          'set the pricing policy',
        )
      }
      if (filament !== undefined) {
        const { filament_id, archive, ...fields } = filament
        const body = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))
        if (filament_id === undefined) {
          if (archive) throw new ToolError('archive needs a filament_id')
          answer.filament = await ok(backend.POST('/api/v1/orders/price-book/filaments', { body: body as never }), 'add a filament')
        } else {
          const path = { filament_id }
          if (Object.keys(body).length > 0) {
            answer.filament = await ok(
              backend.PATCH('/api/v1/orders/price-book/filaments/{filament_id}', { params: { path }, body: body as never }),
              `edit filament ${filament_id}`,
            )
          }
          if (archive) {
            answer.filament = await ok(
              backend.POST('/api/v1/orders/price-book/filaments/{filament_id}/archive', { params: { path } }),
              `archive filament ${filament_id}`,
            )
          }
          if (answer.filament === undefined) throw new ToolError('give the fields to change, or archive: true')
        }
      }
      return json(answer)
    },
  }),
  defineTool({
    name: 'order_estimate',
    label: "Estimate an output's filament cost",
    description:
      "A rough estimate of an output's filament before anything is sliced: its parts' enclosed volumes over every " +
      "plate times the filament's density and the shop's factor, in grams and at the filament's price. Labelled " +
      'an estimate and never recorded: quote with order_item_quote. A filament with no density gives none.',
    input: z.object({ output_id: ID, filament_id: ID.describe('A price-book filament (price_book_get)') }),
    risk: 'read',
    routes: ['GET /api/v1/orders/estimate'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders/estimate', { params: { query } }), `estimate output ${query.output_id}`)),
  }),
]

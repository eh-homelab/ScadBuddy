import { z } from 'zod'
import type { components } from '../api/schema.js'
import { ok } from './call.js'
import { command } from './command.js'
import {
  nozzles,
  partSettings,
  partSettingsPhrase,
  presetRef,
  primeTowerChoice,
  printOptionFields,
  printPath,
  printSequence,
  setupPhrase,
  slotChoice,
  supportsChoice,
  tier,
} from './print.js'
import { defineTool, json, type Tool, type ToolContext, ToolError } from './registry.js'
import { FROM_WORKSPACE_ONLY, uploadInit, uploadName, withSent, workspaceFile, workspacePathInput, workspaceShaInput } from './workspaceUploads.js'

/** A request body as sent: every field may be left out, each with the schema's type. */
type Sent<T> = { [K in keyof T]?: T[K] }
type MachineRateSent = {
  profile: Sent<components['schemas']['CostProfile']>
  wear_parts?: components['schemas']['WearPart'][]
  maintenance?: Sent<components['schemas']['MaintenanceCost']>[]
}

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
// An item added to an order past `quoted` (#5349) changes what the customer gets, so it is
// its own route and its own `outward` tool (order_item_add_after_acceptance); order_item_add
// stays `write` for a draft or quoted order. The charges past `quoted` (#5351) change what
// the customer pays, so they are order_charges_update_after_acceptance, `outward`, too. A new
// design of an accepted item (#5350) changes what the customer gets, so it is
// order_item_revision_add_after_acceptance, `outward`; order_item_revision_add stays `write`.
//
// Phase 2 (#4250, plan Ruling 11): the price book is `read` to look at and `write` to keep
// (the policy, a filament's record); order_item_quote takes both modes, a typed price or a
// slice of the item through the print dialog's own choices (the `order_item_quote`
// operation, followed as every command is), and stays `write`: a quote records a price,
// accepting it is the outward step. The rough estimate is a `read`, never a quote.
//
// Attachments (#4251, #4722, spec 2026-10-18 §7.3): order_attach sends a workspace file by
// path and sha256, streamed into the multipart route (workspaceUploads.ts), and is `write`
// with its caption, role and delivered mark: a record the shop keeps, which tells the
// customer nothing. Removing one is `outward` (AI spec §8.1, a delete): its bytes are
// freed and no history brings them back. The list, the bytes and the thumbnail stay the
// page's (coverage.ts): order_get carries the list.

const ID = z.string().min(1).max(100)
const orderId = ID.describe('An order id, as order_list or order_create returns it')

/** The messages `order_notify_customer` composes (#4252, orders spec §7.3). */
const NOTIFY_TEMPLATES = ['quoted', 'accepted', 'ready', 'shipped'] as const
type NotifyTemplate = (typeof NOTIFY_TEMPLATES)[number]

interface NoticeOrder {
  order: { number: string; currency: string; minor_exponent: number }
  totals: { total_minor: number }
}
interface NoticeCustomer {
  display_name: string
  contacts: { kind: string; value: string; primary?: boolean }[]
}

/** `minor` integer units as a major amount with `exponent` places, from the integer's
 *  digits: a division through a float misplaces the last digit of a large total
 *  (review of #5682). */
function majorText(minor: number, exponent: number): string {
  const sign = minor < 0 ? '-' : ''
  const digits = String(Math.abs(minor)).padStart(exponent + 1, '0')
  if (exponent === 0) return `${sign}${digits}`
  return `${sign}${digits.slice(0, -exponent)}.${digits.slice(-exponent)}`
}

/** The message for `template`, addressed to the customer's primary contacts (every
 *  contact when none is primary). Plain text, never sent from here. */
export function composeNotice(
  template: NotifyTemplate,
  detail: NoticeOrder,
  customer: NoticeCustomer,
  note?: string,
): {
  template: NotifyTemplate
  to: { kind: string; value: string }[]
  subject: string
  text: string
  sent: false
  warning?: string
} {
  const { number, currency, minor_exponent } = detail.order
  const total = `${majorText(detail.totals.total_minor, minor_exponent)} ${currency}`
  const lines: Record<NotifyTemplate, [string, string]> = {
    quoted: [`Your quote for order ${number}`, `Your quote for order ${number} is ready: ${total}. Reply to accept it.`],
    accepted: [`Order ${number} accepted`, `We have accepted order ${number} (${total}) and will start on it.`],
    ready: [`Order ${number} is ready`, `Order ${number} is ready.`],
    shipped: [`Order ${number} has shipped`, `Order ${number} has shipped.`],
  }
  const [subject, body] = lines[template]
  const primary = customer.contacts.filter((c) => c.primary)
  const to = (primary.length > 0 ? primary : customer.contacts).map(({ kind, value }) => ({ kind, value }))
  const text = [`Hello ${customer.display_name},`, '', body, ...(note ? ['', note] : [])].join('\n')
  // Review of #5682: an unaddressed message says so, so a sender (#5660) never sends it blind.
  if (to.length === 0) {
    return { template, to, subject, text, sent: false, warning: `${customer.display_name} has no contacts to send it to.` }
  }
  return { template, to, subject, text, sent: false }
}
const itemId = ID.describe('A line item id, as order_get lists them')
const customerId = ID.describe('A customer id, as customer_list returns it')
const attachmentId = ID.describe("An attachment id, as order_get lists them under `attachments`")
const attachmentRole = z
  .enum(['reference', 'issue', 'finished', 'packed', 'timelapse', 'render', 'test_print', 'other'])
  .describe("Why the file is kept; `render` and `test_print` only on a design revision's file")
const attachmentCaption = z.string().max(1000)
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

// order_item_add and order_item_add_after_acceptance take one body (#5349).
const itemAddInput = z.object({
  order_id: orderId,
  kind: z
    .enum(['model', 'custom', 'request', 'library_file'])
    .optional()
    .describe('`model` (the default), `custom`, `request` or `library_file`'),
  output_id: ID.optional(),
  library_file_id: z
    .number()
    .int()
    .min(1)
    .optional()
    .describe('A file of the library (from search_library or list_library), as print_output takes it: the item prints it'),
  archive_id: z
    .number()
    .int()
    .min(1)
    .optional()
    .describe('A past print (a Bambuddy archive, from list_print_archives), alone: leave `kind` and every other source out'),
  ...reference.shape,
  quantity: z.number().int().min(1).default(1),
  title: z
    .string()
    .max(200)
    .optional()
    .describe("Defaults to the template's name; a custom or request item needs one"),
  notes: z.string().default(''),
  sku: z.string().max(100).optional(),
  unit_price_minor: money.min(0).optional().describe('A custom item\'s price per unit'),
  requires_printing: z.boolean().optional().describe('A custom item\'s only'),
})

/** Most items `order_item_add` takes in one request (backend `MAX_BATCH_ITEMS`, #4777). */
const MAX_BATCH_ITEMS = 50

const itemBody = itemAddInput.omit({ order_id: true })
type ItemBody = z.infer<typeof itemBody>

/** The single-item fields a call with `items` must leave out. */
const SINGLE_FIELDS = ['kind', 'output_id', 'library_file_id', 'archive_id', 'slug', 'commit', 'inputs', 'title', 'sku', 'unit_price_minor', 'requires_printing'] as const

const itemsAddInput = itemAddInput
  .extend({
    items: z
      .array(itemBody)
      .min(1)
      .max(MAX_BATCH_ITEMS)
      .optional()
      .describe(
        `Several items in one request (at most ${MAX_BATCH_ITEMS}), each what a single call takes, added in order, all or none: ` +
          'a refused one adds none and names its place (`item_index`). With `items`, leave the single-item fields out: ' +
          'each item carries its own `quantity` and `notes`, and a top-level one is refused',
      ),
  })
  // `quantity` and `notes` have defaults, so only a value other than the default is told apart.
  .refine((o) => o.items === undefined || (SINGLE_FIELDS.every((f) => o[f] === undefined) && o.quantity === 1 && o.notes === ''), {
    message: 'with items, name each item inside items: leave the single-item fields out',
    path: ['items'],
  })

/** The body a write tool sends: an accept is outward, so a custom item's price stays quoted. */
const recorded = (body: ItemBody) => (body.kind === 'custom' ? { ...body, accept: false } : body)

function itemsAddTitle(input: Parameters<typeof itemAddTitle>[0] & { items?: unknown[] }): string {
  if (input.items === undefined) return itemAddTitle(input)
  return `Add ${input.items.length} item${input.items.length === 1 ? '' : 's'} to order ${input.order_id}`
}

function itemAddTitle({
  order_id,
  slug,
  output_id,
  library_file_id,
  archive_id,
  kind,
  title,
}: {
  order_id: string
  slug?: string
  output_id?: string
  library_file_id?: number
  archive_id?: number
  kind?: string
  title?: string
}): string {
  const what =
    kind === 'custom' || kind === 'request'
      ? (title ?? `a ${kind} item`)
      : library_file_id !== undefined
        ? (title ?? `library file ${library_file_id}`)
        : archive_id !== undefined
          ? (title ?? `past print ${archive_id}`)
          : (slug ?? output_id ?? 'an item')
  return `Add ${what} to order ${order_id}`
}

const attempt = z
  .object({
    quantity: z.number().int().min(0).describe('Made in this attempt'),
    outcome: z.enum(['succeeded', 'failed', 'partial', 'unknown']),
    failure_reason: z.string().optional(),
    started_at: datetime.optional(),
    finished_at: datetime.optional(),
    notes: z.string().default(''),
    archive_id: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe("A Bambuddy archive (a print) this attempt is; its time and grams are copied from it. Read on a record only. Refused with `archive_claimed` when another attempt already has it"),
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
    // #5352: the quote slices on the route its print will take, so that print queues the quote's slice.
    path: printPath,
    // #5571: recorded with the quote; order_item_print prints with them when it sends none.
    part_settings: partSettings,
    // #4673: print_output's own, sliced with and recorded as the quote's print setup.
    prime_tower: primeTowerChoice,
    supports: supportsChoice,
    print_sequence: printSequence,
  })
  .strict()

/**
 * The quote warning (job cost spec 2026-11-05 §6.3, #5328): the model's real cost per unit
 * in its past jobs against the quote's unit price, when that price is thin. Undefined with
 * no warning, for an answer that is not a quote (a slice quote still running), and when the
 * lookup fails: the quote is recorded by then, and a failed tool call would invite a second.
 */
async function priceWarning(ctx: ToolContext, orderId: string, answer: unknown): Promise<unknown> {
  const quote = answer as { order_item_id?: unknown; unit_price_minor?: unknown } | null
  if (typeof quote?.order_item_id !== 'string' || typeof quote.unit_price_minor !== 'number') return undefined
  try {
    // The whole order: no route reads one item, and the order holds the currency.
    const order = await ok(ctx.backend.GET('/api/v1/orders/{order_id}', { params: { path: { order_id: orderId } } }), `get order ${orderId}`)
    const slug = order.items.find((item) => item.id === quote.order_item_id)?.reference?.slug
    if (!slug) return undefined
    const history = await ok(
      ctx.backend.GET('/api/v1/orders/history/unit-cost', {
        params: { query: { slug, currency: order.order.currency, unit_price_minor: quote.unit_price_minor } },
      }),
      `read the unit cost history of ${slug}`,
    )
    if (!history.warning) return undefined
    const { jobs, unit_cost_minor, main_reason, main_reason_minor, suggested_unit_price_minor, margin_percent, estimated, below_cost } = history
    return { jobs, unit_cost_minor, main_reason, main_reason_minor, suggested_unit_price_minor, margin_percent, below_cost, estimated }
  } catch {
    return undefined
  }
}

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
      '`status`, `customer_id`, `q` (number, notes, customer name) and a `from`/`to` window on when they were made. ' +
      "Each order carries its margin, the figures order_costs gives: `margin_minor`, `margin_percent`, `margin_state` " +
      '(`loss`, `thin`, `unknown`, `ok`; null with no charged figure) and `margin_estimated`. `margin_state` filters by ' +
      'any of the states given (`["loss", "thin"]` is the orders list\'s "Loss or thin"). With `margin_state`, each ' +
      'call reads a limited number of orders, so a page can be short or empty while `next_cursor` remains: follow ' +
      'the cursor until it is null before you say no order matches. `product`, `accepted_from` (inclusive), ' +
      '`accepted_to` (exclusive) and `currency` list the orders behind an orders_profit_report row: pass its ' +
      '`product`, its `period_from` and `period_to`, and its `currency`. `source` is where an order was made ' +
      '(`back_office` or `storefront`). The storefront\'s unsubmitted carts are left out; `unsubmitted: true` lists ' +
      'only them. A cart has no `number` and no `customer_id` (both null) until it is submitted.',
    input: z.object({
      status: z.enum(['draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled']).optional(),
      customer_id: customerId.optional(),
      q: z.string().max(200).optional(),
      from: datetime.optional(),
      to: datetime.optional(),
      accepted_from: datetime.optional(),
      accepted_to: datetime.optional(),
      currency: z.string().regex(/^[A-Z]{3}$/).optional().describe('An ISO 4217 code, such as USD'),
      product: z
        .string()
        .min(1)
        .max(210)
        .regex(/^(custom|(model|sku):.+)$/)
        .optional()
        .describe('`model:<slug>`, `sku:<sku>` or `custom`, as a profit report row names it'),
      margin_state: z.array(z.enum(['loss', 'thin', 'unknown', 'ok'])).min(1).max(4).optional(),
      source: z.enum(['back_office', 'storefront']).optional(),
      unsubmitted: z.boolean().optional(),
      cursor: z.string().max(400).optional(),
      limit: z.number().int().min(1).max(100).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/orders'],
    // The route takes `status` repeated (#5276); the tool names one.
    handler: async ({ status, ...query }, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders', { params: { query: { ...query, ...(status ? { status: [status] } : {}) } } }), 'list orders')),
  }),
  defineTool({
    name: 'orders_profit_report',
    label: 'Read the profit report',
    description:
      'The profit report by model (job cost spec §6.4): the orders accepted at or after `from` and before `to` ' +
      '(by default the last 90 days), cancelled ones too. Rows are per currency, never summed across currencies. ' +
      'Each product row is one model `slug` (`group` `model`), one custom item `sku` (`sku`), or the custom items ' +
      'with no SKU (`custom`), with its `orders`, `units`, what was charged (`revenue_minor`), the real cost ' +
      '(`cost_minor`), `margin_minor`, `margin_percent`, `margin_state` and the waste (`waste_minor`, ' +
      '`waste_percent`: the cost of test and failed prints). Each item counts as order_costs gives it. The `order` ' +
      "row is the orders' own entries, shipments' cost and charges (its `orders` counts only the orders that have " +
      "any; no order_list filter matches them), and the `total` row each currency's sum. Pass a product " +
      "row's `product`, `period_from`, `period_to` and `currency` to order_list for its orders. With `complete` " +
      'false the report stopped at `max_orders` and covers the orders accepted from `read_from` only.',
    input: z.object({ from: datetime.optional(), to: datetime.optional() }),
    risk: 'read',
    routes: ['GET /api/v1/orders/reports/profit'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders/reports/profit', { params: { query } }), 'read the profit report')),
  }),
  defineTool({
    name: 'order_get',
    label: 'Read an order',
    description:
      'One order whole: its status, number and addresses, its line items with their quotes, design `revisions` ' +
      'and fulfilment attempts (every attempt has a `purpose`: `production`, or `test` for a revision\'s test print), ' +
      'charges, payments, shipments and totals.',
    input: z.object({ order_id: orderId }),
    risk: 'read',
    routes: ['GET /api/v1/orders/{order_id}'],
    handler: async ({ order_id }, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders/{order_id}', { params: { path: { order_id } } }), `get order ${order_id}`)),
  }),
  defineTool({
    name: 'order_request_payment',
    label: 'Request a payment',
    description:
      'Ask the customer to pay (#4957): a `deposit` or a `balance` request, for `amount_minor` (the balance now when ' +
      "left out), through `provider` (the default provider when left out; null collects by hand). This sends the " +
      'customer something to pay, so it is outward: a person must approve it first (it returns a pending action). ' +
      'The reply has the request with its `hosted_url` and `link_error`. ' +
      'The link is for the shop to pass on: the tool does not send it. A failed link leaves the request open with ' +
      '`link_error` set.',
    input: z.object({
      order_id: orderId,
      kind: z.enum(['deposit', 'balance']).optional(),
      amount_minor: money.min(1).optional(),
      provider: z.string().min(1).max(100).nullable().optional().describe('A provider id; null collects by hand'),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/payment-requests'],
    title: ({ order_id, kind }) => `Request a ${kind ?? 'balance'} payment on order ${order_id}`,
    handler: async ({ order_id, ...fields }, { backend }) => {
      const body = Object.fromEntries(
        Object.entries(fields).filter(([, v]) => v !== undefined),
      ) as Partial<components['schemas']['PaymentRequestCreate']> as components['schemas']['PaymentRequestCreate']
      return json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/payment-requests', { params: { path: { order_id } }, body }),
          `request a payment on order ${order_id}`,
        ),
      )
    },
  }),
  defineTool({
    name: 'order_payment_requests',
    label: "Read an order's payment requests",
    description:
      "An order's payment requests (#4957): each with its kind, amount, provider, `hosted_url`, `link_error` and " +
      'status (`open`, `pending`, `paid`, `expired`, `cancelled`, `failed`; `pending` while a bank debit settles).',
    input: z.object({ order_id: orderId }),
    risk: 'read',
    routes: ['GET /api/v1/orders/{order_id}/payment-requests'],
    handler: async ({ order_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/orders/{order_id}/payment-requests', { params: { path: { order_id } } }),
          `list the payment requests of order ${order_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_notify_customer',
    label: 'Write a message to an order’s customer',
    description:
      "Compose a message to an order's customer from a template (`quoted`, `accepted`, `ready`, `shipped`), " +
      "addressed to the customer's primary contacts, with an optional `note` added. It sends nothing: the " +
      'answer is the text (`sent: false`) for a person to send. Sending through a messaging plugin is a ' +
      'later change (#5660).',
    input: z.object({
      order_id: orderId,
      template: z.enum(NOTIFY_TEMPLATES),
      note: z.string().min(1).max(2000).optional().describe('A line added after the template’s text'),
    }),
    // It composes text and changes nothing; the person who sends it decides.
    risk: 'read',
    source: 'the order and the customer record, which anyone who edits them can write',
    routes: ['GET /api/v1/orders/{order_id}', 'GET /api/v1/customers/{customer_id}'],
    handler: async ({ order_id, template, note }, { backend }) => {
      const detail = await ok(
        backend.GET('/api/v1/orders/{order_id}', { params: { path: { order_id } } }),
        `get order ${order_id}`,
      )
      const { customer_id, number } = detail.order
      // A visitor's cart (#5282) has no customer and no number yet: nobody to address.
      if (customer_id == null || number == null) {
        throw new ToolError(`order ${order_id} has no customer or number yet (a cart), so there is no one to notify`)
      }
      const customer = await ok(
        backend.GET('/api/v1/customers/{customer_id}', { params: { path: { customer_id } } }),
        `get customer ${customer_id}`,
      )
      return json(composeNotice(template, { order: { ...detail.order, number }, totals: detail.totals }, customer, note))
    },
  }),
  defineTool({
    name: 'order_costs',
    label: "Read an order's real cost",
    description:
      "An order's real cost and margin (#4251, #5326): per item, its accepted quote beside every attempt, failed " +
      'ones included, each costed in filament and machine time (`estimated` names a figure taken from the quote ' +
      "or today's settings; `unknown` an attempt with no source), its labour (`time_entries`, else a default, " +
      '`labour_default`), its `cost_entries`, what it was charged (`revenue_minor`) and its `margin_minor`, ' +
      '`margin_percent` and `margin_state` (`ok`, `thin`, `loss`, `unknown`); the order adds its own entries, its ' +
      "shipments' cost and every charge but tax. `bambuddy_cost` is Bambuddy's own cost of an attempt, shown and " +
      'never in any sum.',
    input: z.object({ order_id: orderId }),
    risk: 'read',
    routes: ['GET /api/v1/orders/{order_id}/costs'],
    handler: async ({ order_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/orders/{order_id}/costs', { params: { path: { order_id } } }),
          `get the costs of order ${order_id}`,
        ),
      ),
  }),
  // Job cost (#5326, spec 2026-11-05 §4.3, §4.4): a record of what the job took, in any
  // order status. Removing one is the order page's (coverage.ts).
  defineTool({
    name: 'order_time_add',
    label: 'Log time on an order',
    description:
      'Record time spent on an order, or on one of its items with `order_item_id`: `design`, `setup`, ' +
      "`post_processing`, `packing` or `other`, in whole minutes. It copies the shop's labour rate now, so a later " +
      "change never rewrites it, and one entry replaces the item's default labour in order_costs. With `source` " +
      'a second call with the same source returns the first entry as it was recorded, whatever its kind, and ' +
      'writes nothing. Allowed in every order status, closed, cancelled and on hold included.',
    input: z.object({
      order_id: orderId,
      order_item_id: itemId.optional(),
      kind: z.enum(['design', 'setup', 'post_processing', 'packing', 'other']),
      minutes: z.number().int().min(1).max(1_000_000),
      note: z.string().max(1000).default(''),
      started_at: datetime.optional(),
      source: z
        .string()
        .regex(/^[a-z_]+:\S{1,200}$/)
        .optional()
        .describe('What wrote it, `<kind>:<id>`, so a retry writes once'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/time-entries'],
    title: ({ order_id, minutes }) => `Log ${minutes} minutes on order ${order_id}`,
    handler: async ({ order_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/time-entries', { params: { path: { order_id } }, body }),
          `log time on order ${order_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_cost_add',
    label: 'Add a cost to an order',
    description:
      "Record money a job cost, in the order's currency's minor units, on the order or one of its items: " +
      "`assistant` (the assistant's spend), `payment_fee`, `material` (magnets, keyrings, paint) or `other`. " +
      "Shipping is a shipment's cost, never an entry. With `source` (such as `ai_session:<id>`) a second call " +
      'with the same kind and source returns the first entry as it was recorded, whatever the amount, note or item ' +
      'this call names, and writes nothing. Allowed in every order status.',
    input: z.object({
      order_id: orderId,
      order_item_id: itemId.optional(),
      kind: z.enum(['assistant', 'payment_fee', 'material', 'other']),
      amount_minor: money.min(1),
      note: z.string().max(1000).default(''),
      source: z
        .string()
        .regex(/^[a-z_]+:\S{1,200}$/)
        .optional()
        .describe('What wrote it, `<kind>:<id>`, so a retry writes once'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/cost-entries'],
    title: ({ order_id, kind }) => `Add a ${kind} cost to order ${order_id}`,
    handler: async ({ order_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/cost-entries', { params: { path: { order_id } }, body }),
          `add a cost to order ${order_id}`,
        ),
      ),
  }),
  // Parts inventory (#5543, job cost spec §11): the hooks, rings and magnets an item uses
  // beside its filament. Making a part, editing it, correcting a count, its stock line and
  // a model's rule are the Parts page's (coverage.ts).
  defineTool({
    name: 'part_list',
    label: 'List parts',
    description:
      'The parts inventory (keychain hooks, rings, magnets): each part\'s pack, its unit cost (a decimal string in ' +
      'minor units, the weighted average of the stock on hand), what is `on_hand` (below zero when more was used ' +
      'than recorded) and `low` at or below its low-stock level. With `low`, only the low ones. With `order_id`, ' +
      "that order's items' parts per unit and `short`: the parts its open items need beyond what is on hand.",
    input: z.object({
      low: z.boolean().default(false),
      order_id: orderId.optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/parts', 'GET /api/v1/orders/{order_id}/parts'],
    handler: async ({ low, order_id }, { backend }) => {
      if (order_id !== undefined) {
        return json(
          await ok(
            backend.GET('/api/v1/orders/{order_id}/parts', { params: { path: { order_id } } }),
            `get the parts of order ${order_id}`,
          ),
        )
      }
      return json(await ok(backend.GET('/api/v1/parts', { params: { query: { low } } }), 'list the parts'))
    },
  }),
  defineTool({
    name: 'part_stock_add',
    label: 'Add stock to a part',
    description:
      "Record packs bought: `packs` of the part's pack join the stock, at `pack_price_minor` each (with tax, in " +
      "the part's currency's minor units) when given, which becomes the part's pack price. The unit cost becomes " +
      'the weighted average of the stock on hand and the new packs; orders already fulfilled keep their cost.',
    input: z.object({
      part_id: ID.describe('A part id, as part_list returns it'),
      packs: z.number().int().min(1).max(10_000).default(1),
      pack_price_minor: z.number().int().min(0).optional(),
      note: z.string().max(1000).default(''),
    }),
    risk: 'write',
    routes: ['POST /api/v1/parts/{part_id}/stock'],
    title: ({ part_id, packs }) => `Add ${packs} pack(s) to part ${part_id}`,
    handler: async ({ part_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/parts/{part_id}/stock', { params: { path: { part_id } }, body }),
          `add stock to part ${part_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_parts_set',
    label: "Set a line item's parts",
    description:
      'The parts one unit of a line item uses, replaced whole (an empty list clears them). A new item takes its ' +
      "model's rule; this changes it. When the item is fulfilled its parts times its quantity leave stock as a " +
      '`material` cost entry, and come back when it is unfulfilled. Refused (409 `item_parts_used`) for a ' +
      'fulfilled item. Stock never blocks: it can go below zero.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      parts: z
        .array(z.object({ part_id: ID.describe('A part id, as part_list returns it'), per_unit: z.number().int().min(1).max(1000) }).strict())
        .max(50),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/orders/{order_id}/items/{item_id}/parts'],
    title: ({ order_id, item_id }) => `Set the parts of item ${item_id} on order ${order_id}`,
    handler: async ({ order_id, item_id, parts }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/orders/{order_id}/items/{item_id}/parts', {
            params: { path: { order_id, item_id } },
            body: { parts },
          }),
          `set the parts of item ${item_id}`,
        ),
      ),
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
      'charges, replaced whole, while draft or quoted (past that, charges answer 409 `order_not_editable`: use ' +
      'order_charges_update_after_acceptance). `hold` puts it on hold or releases it (true/false): on hold, ' +
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
          payment_request_id: ID.optional().describe(
            'The payment request this money pays, as order_payment_requests lists them; it closes that request',
          ),
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
      'Add a line item to a draft or quoted order: from an output (`output_id`: its commit, inputs, libraries and colours), or by hand (`slug`, ' +
      '`commit`, `inputs`). Beside `output_id`, leave the others out: one given must be the output\'s own (`inputs` ' +
      'whole, `v` included). The reference is checked against the catalogue and the commit\'s schema; a commit ' +
      'that is not a revision of the template, or an input the template does not take, is refused naming the field. ' +
      '`kind: "custom"` adds a custom item instead: a `title`, a `quantity` and a `unit_price_minor`, with no ' +
      'source (no `output_id`, `slug`, `commit` or `inputs`), for design work, a fee, packaging or a part made ' +
      'elsewhere. Its price is recorded, not accepted: the item is quoted until order_item_accept_quote accepts it. ' +
      '`requires_printing` (default ' +
      'false) marks one that still has to be made. ' +
      '`kind: "request"` adds a design request: a `title` and `notes` only, no source and no price; its first ' +
      'revision (order_item_revision_add) gives it its design. `library_file_id` adds a library file instead ' +
      '(#5544): a file of the library that serves (from search_library), with no `output_id`, `slug`, `commit` ' +
      'or `inputs`; the item names the file (no template), defaults its title to the file\'s name, is priced by ' +
      'order_item_quote (a slice of the file, or `manual`), prints the file, and has no revisions. A file the ' +
      'library does not have or does not print is 422; one deleted later is 409 `library_file_gone` on quote ' +
      'and print. `archive_id` adds a past print (#4777): a Bambuddy archive from list_print_archives, alone (no ' +
      '`kind` or other source). The item is a model item of the output that printed it, a library file item of the ' +
      'file that printed it, or else a library file item of a copy of its printed file in the native library (one ' +
      'copy per archive, reused); it keeps `origin_archive_ids` and is priced from the print with ' +
      "order_item_quote's `archive`. An archive Bambuddy no longer has is 409 `archive_gone`; a copy needed while " +
      'the Bambuddy library serves is 409 `archive_copy_not_native`. `sku` is the shop\'s stock code, on any kind. ' +
      'An accepted, in-production or ready order answers 409 `item_locked`: use order_item_add_after_acceptance. ' +
      '`items` adds several in one request (#4777; e.g. outputs found with list_recent_outputs), all or none.',
    input: itemsAddInput,
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/items', 'POST /api/v1/orders/{order_id}/items/batch'],
    title: itemsAddTitle,
    handler: async ({ order_id, items, ...body }, { backend }) =>
      json(
        items === undefined
          ? await ok(
              backend.POST('/api/v1/orders/{order_id}/items', {
                params: { path: { order_id } },
                body: recorded(body) as never,
              }),
              `add an item to order ${order_id}`,
            )
          : await ok(
              backend.POST('/api/v1/orders/{order_id}/items/batch', {
                params: { path: { order_id } },
                body: { items: items.map(recorded) } as never,
              }),
              `add ${items.length} items to order ${order_id}`,
            ),
      ),
  }),
  defineTool({
    name: 'order_item_add_after_acceptance',
    label: 'Add a line item to an accepted order',
    description:
      'Add a line item to an order that is accepted, in production or ready (#5349). It takes what order_item_add ' +
      'takes. It changes what the customer gets. The item starts unpriced (a custom item quoted, its price not ' +
      'accepted): it needs order_item_quote and order_item_accept_quote before it is printed or fulfilled. The ' +
      'accepted prices and charges do not change. A ready order goes back to in production. Refused: ' +
      '`order_not_accepted` on a draft or quoted order (use order_item_add), `item_locked` once shipped, ' +
      '`order_closed`/`order_cancelled`, and `order_on_hold`. `items` adds several in one request, all or none.',
    input: itemsAddInput,
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/after-acceptance', 'POST /api/v1/orders/{order_id}/items/after-acceptance/batch'],
    summarize: (input) => `${itemsAddTitle(input)}, after its acceptance`,
    handler: async ({ order_id, items, ...body }, { backend }) =>
      json(
        items === undefined
          ? await ok(
              backend.POST('/api/v1/orders/{order_id}/items/after-acceptance', {
                params: { path: { order_id } },
                body: body as never,
              }),
              `add an item to accepted order ${order_id}`,
            )
          : await ok(
              backend.POST('/api/v1/orders/{order_id}/items/after-acceptance/batch', {
                params: { path: { order_id } },
                body: { items } as never,
              }),
              `add ${items.length} items to accepted order ${order_id}`,
            ),
      ),
  }),
  defineTool({
    name: 'order_charges_update_after_acceptance',
    label: 'Change the charges of an accepted order',
    description:
      'Replace the charges of an order that is accepted, in production or ready (#5351), whole, as order_update ' +
      'takes them before that. It changes what the customer pays. Use it to keep an agreed total: when an item ' +
      'is added after acceptance, a `discount` (negative) or `fee` line absorbs its price. The audit row holds the ' +
      'charges before and after. Refused: `order_not_accepted` on a draft or quoted order (use order_update), ' +
      '`order_not_editable` once shipped, `order_closed`/`order_cancelled`, and `order_on_hold`.',
    input: z.object({ order_id: orderId, charges: z.array(charge) }),
    risk: 'outward',
    routes: ['PUT /api/v1/orders/{order_id}/charges/after-acceptance'],
    summarize: ({ order_id, charges }) =>
      `Set ${charges.length} charge${charges.length === 1 ? '' : 's'} on accepted order ${order_id}`,
    handler: async ({ order_id, charges }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/orders/{order_id}/charges/after-acceptance', {
            params: { path: { order_id } },
            body: { charges },
          }),
          `change the charges of accepted order ${order_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_revision_add',
    label: 'Add a design revision to an order line item',
    description:
      "Record a new design revision of a line item (storefront spec §4): a `reference` (slug, commit, inputs) or an " +
      "`output_id`, with the `filaments` per slot and a `note` for the customer. A request item's first revision gives " +
      'it its design. A new revision supersedes a sent, unanswered one and drops the item\'s quotes. It is a draft ' +
      'until order_item_revision_send sends it to the customer. Past `quoted` an item with an accepted quote answers ' +
      '409 `item_locked`: use order_item_revision_add_after_acceptance (an item already revised after acceptance ' +
      'takes this one too, and keeps its old accepted quote).',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      reference: reference.optional(),
      output_id: ID.optional(),
      filaments: z.record(z.string(), ID.nullable()).optional().describe('A filament id (or null) per slot'),
      note: z.string().max(4000).optional(),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/revisions'],
    title: ({ order_id, item_id }) => `Add a design revision to item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, reference, output_id, filaments, note }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/revisions', {
            params: { path: { order_id, item_id } },
            body: {
              note: note ?? '',
              ...(reference === undefined ? {} : { reference }),
              ...(output_id === undefined ? {} : { output_id }),
              ...(filaments === undefined ? {} : { filaments }),
            },
          }),
          `add a revision to item ${item_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_revision_add_after_acceptance',
    label: 'Revise the design of an accepted order line item',
    description:
      'Record a new design revision of a line item that has an accepted quote, on an order that is accepted, in ' +
      'production or ready (#5350). The body is order_item_revision_add\'s. It changes what the customer gets. The ' +
      'old accepted quote stays accepted, so the order total keeps the agreed price, until a quote of the new ' +
      'design is accepted: quote it with order_item_quote, then accept with order_item_accept_quote. To keep the ' +
      'old price, quote the same price. The item is `draft` until then and is not printed or fulfilled. Refused: ' +
      '`order_not_accepted` on a draft or quoted order (use order_item_revision_add), `order_closed`/' +
      '`order_cancelled`, `order_on_hold`, `item_not_open` for a fulfilled or cancelled item (every item of a ' +
      'ready or shipped order is one, so the tool never works there and `item_locked` is not reached), ' +
      '`item_not_priced` for an item with no accepted quote (use order_item_revision_add), `item_custom`, ' +
      '`print_in_flight` while a print of it runs, and `item_partly_fulfilled` once an attempt of it is accepted ' +
      '(those units are the agreed design: cancel the item and add one with the new design).',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      reference: reference.optional(),
      output_id: ID.optional(),
      filaments: z.record(z.string(), ID.nullable()).optional().describe('A filament id (or null) per slot'),
      note: z.string().max(4000).optional(),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/revisions/after-acceptance'],
    summarize: ({ order_id, item_id }) => `Revise the design of accepted item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, reference, output_id, filaments, note }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/revisions/after-acceptance', {
            params: { path: { order_id, item_id } },
            body: {
              note: note ?? '',
              ...(reference === undefined ? {} : { reference }),
              ...(output_id === undefined ? {} : { output_id }),
              ...(filaments === undefined ? {} : { filaments }),
            },
          }),
          `revise accepted item ${item_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_revision_send',
    label: 'Send a design revision to the customer',
    description:
      "Send a design revision to the customer for their answer. Outward: it commits the shop to the customer. The " +
      "customer's answer is recorded by a person, never by an agent.",
    input: z.object({ order_id: orderId, item_id: itemId, revision_id: ID.describe("A revision id, as order_get lists an item's `revisions`") }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/send'],
    summarize: ({ order_id, item_id, revision_id }) =>
      `Send revision ${revision_id} of item ${item_id} (order ${order_id}) to the customer`,
    handler: async ({ order_id, item_id, revision_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/send', {
            params: { path: { order_id, item_id, revision_id } },
          }),
          `send revision ${revision_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_item_update',
    label: 'Edit an order line item',
    description:
      'Edit a line item: `reference` (replaced whole) and `quantity` while the order is draft or quoted (or, past ' +
      'that, while the item has no accepted quote: one added by order_item_add_after_acceptance), each dropping ' +
      'the accepted quote; `title`, `notes` and `sku` in any open state. A custom item takes `unit_price_minor` and ' +
      '`requires_printing`: a new quantity or price records a new quote at it, not accepted (the item is quoted until ' +
      'order_item_accept_quote). A `reference` or ' +
      '`output_id` given to a custom item makes it a model item, back on the quote flow. `delete: true` removes a ' +
      'draft item; a quoted one is refused with `item_not_draft` (cancel it with order_cancel instead), and one ' +
      'with time or cost entries with `item_has_entries` (remove them, or cancel it). ' +
      '`approval` sets how the customer approves the item: the design first, or design and price together.',
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
      approval: z
        .enum(['design_first', 'together'])
        .optional()
        .describe('`design_first`: the customer approves the design before the price; `together`: both at once'),
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
      'Quote a line item, in one of three modes. `manual`: a typed price (one line, cost = price, the quantity as ' +
      'it is now). `slice`: render the item if it has no matching output, slice every plate with the print ' +
      "dialog's own choices (printer, a spool per slot, nozzles and quality, prime tower, supports and print " +
      'sequence, as print_output takes them; the quote records them as its print setup, `choices` and ' +
      '`filament_plan`, which order_get lists) and price ' +
      "the plates' grams and seconds with the price book in force, each slot at the filament `filaments` names; " +
      "those filaments are written onto the item first (a change drops its accepted quote). A slice quote may run " +
      'for minutes: past the follow window it answers the running operation, to follow with get_operation. The ' +
      'item is quoted; order_item_accept_quote pins it. `archive` (#4777): priced from what a past print used, ' +
      'with no re-slice: every plate archive of its print run, grams per slot as the slicer estimated them and the ' +
      "actual seconds, at the price book in force, each slot at `filaments`' filament (written onto the item first). " +
      '`archive_id` defaults to the first archive the item was taken from (order_item_add `archive_id`). Refused ' +
      '409 `archive_plates_missing` (a plate of the run has no archive), `archive_not_succeeded` (a failed or ' +
      'cancelled print: quote by slice or by hand) and `archive_gone`. When the unit price leaves a margin below the thin ' +
      "threshold against the model's real cost per unit in its last fulfilled jobs, the answer adds " +
      '`price_warning`: `unit_cost_minor` and `jobs`, `below_cost` (true: the price is below the real cost; ' +
      'false: it is above it, but the margin is thin), the `main_reason` (the cost part that most exceeded what ' +
      'their quotes priced) and a `suggested_unit_price_minor`. It is advice: tell the user, never re-quote ' +
      'without asking.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      manual: z
        .object({ unit_price_minor: money.describe('Per unit'), label: z.string().min(1).max(200).optional() })
        .strict()
        .optional(),
      slice: sliceQuote.optional(),
      archive: z
        .object({
          archive_id: z
            .number()
            .int()
            .min(1)
            .optional()
            .describe("The past print: one the item was taken from or one its output or file printed; omitted, the item's first"),
          filaments: z
            .object({})
            .catchall(ID)
            .default({})
            .describe('Price-book filament per colour slot, `{slot: filament_id}` (slot from 0), as `slice` takes them'),
          filament_id: ID.optional().describe(
            'For an item with no colour slots yet (a library file item): the filament of every slot the print used',
          ),
        })
        .strict()
        .optional(),
    }),
    risk: 'write',
    routes: [
      'POST /api/v1/orders/{order_id}/items/{item_id}/quotes',
      'POST /api/v1/orders/{order_id}/items/{item_id}/quote',
      'POST /api/v1/orders/{order_id}/items/{item_id}/quotes/archive',
      'GET /api/v1/orders/history/unit-cost',
    ],
    title: ({ order_id, item_id, slice, archive }) =>
      `${slice ? 'Quote from a slice' : archive ? 'Quote from a past print' : 'Price'} item ${item_id} of order ${order_id}` +
      (slice ? setupPhrase(slice) : ''),
    handler: async ({ order_id, item_id, manual, slice, archive }, ctx) => {
      if ([manual, slice, archive].filter((mode) => mode !== undefined).length !== 1) {
        throw new ToolError(
          'give exactly one of `manual` (a typed price), `slice` (the print dialog\'s choices) or `archive` (a past print)',
        )
      }
      const path = { order_id, item_id }
      const quote =
        manual !== undefined
          ? await ok(
              ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quotes', { params: { path }, body: manual as never }),
              `quote item ${item_id}`,
            )
          : archive !== undefined
            ? await ok(
                ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quotes/archive', {
                  params: { path },
                  body: archive as never,
                }),
                `quote item ${item_id} from a past print`,
              )
            : await command(ctx, `quote item ${item_id} from a slice`, (headers) =>
                ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/quote', { params: { path }, body: slice as never, headers }),
              )
      const warning = await priceWarning(ctx, order_id, quote)
      return json(warning === undefined ? quote : { ...(quote as object), price_warning: warning })
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
      'accepted attempts reach its quantity. An attempt made before the design the accepted quote prices (an item ' +
      'revised after acceptance) is refused `fulfilment_stale`; order_get marks such an attempt `stale: true`. ' +
      'Needs an accepted order.',
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
    name: 'order_item_print',
    label: 'Print an order line item',
    description:
      "Print an approved line item of an accepted order from its accepted quote (#4719): a print run of the item's " +
      "output with the print dialog's choices (printer, a spool per slot, nozzles and quality, as print_output takes " +
      'them), every plate, for the quantity still to print. Answers with the attempt it recorded (outcome unknown, ' +
      'nothing made yet), the item queued; the item then follows the print (printing, then printed or failed), and ' +
      'a print whose end could not be read is settled with order_item_fulfil. Refused with `requote_needed` (no ' +
      "accepted quote, the item's output gone, or a slice quote whose sliced file is gone or changed: quote it " +
      'again), `item_not_approved` (printing already), `item_made` (its attempts made its quantity: accept them), ' +
      '`order_not_accepted` and `order_on_hold`, and the print run\'s own refusals. With Bambuddy, a slot whose ' +
      "filament the item names and the plan leaves out takes that filament's spool loaded on the printer, else the " +
      'shelf spool with most left (never one loaded in another printer); `needs_spool_choice` names the slots with ' +
      "none (name a spool for them), `spool_not_filament` a named spool not of its slot's filament. " +
      '`path: "bambuddy"` (#5352) prints it through Bambuddy\'s queue even where it would go over the LAN: it is ' +
      "filed under the item's project in the order's, and `options` sets the calibrations and timelapse. " +
      'Over the LAN the print files under no project, and a calibration value the LAN start cannot honour is refused ' +
      "(see `path` on print_output). A quote sliced over the LAN is sliced again when printed with `path: \"bambuddy\"`; " +
      'quote with that `path` to print the quote\'s own slice. Outward: it prints.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      printer_id: sliceQuote.shape.printer_id,
      printer: sliceQuote.shape.printer,
      filament_plan: sliceQuote.shape.filament_plan,
      choices: printChoices,
      options: printOptionFields.optional().describe(
        "Per-print queue options, as print_output takes them: bed_levelling, flow_cali, nozzle_offset_cali, " +
          "vibration_cali, timelapse and the rest. Its project_id and quantity are the item's, never these",
      ),
      path: printPath,
      part_settings: partSettings.describe(
        "Omit to print with the accepted quote's own (order_item_quote's `slice.part_settings`), so the print slices " +
          'what was priced. Settings that differ from the quote\'s are refused `requote_needed`: quote with them first',
      ),
      // #4673: each omitted is the accepted quote's own (its `choices`); one that differs is `requote_needed`.
      prime_tower: primeTowerChoice.describe(
        "Omit to print with the accepted quote's (order_item_quote's `slice.prime_tower`); another is refused `requote_needed`",
      ),
      supports: supportsChoice.describe(
        "Omit to print with the accepted quote's (order_item_quote's `slice.supports`); others are refused `requote_needed`",
      ),
      print_sequence: printSequence.describe(
        "Omit to print with the accepted quote's (order_item_quote's `slice.print_sequence`); another is refused `requote_needed`",
      ),
      test_revision_id: ID.optional().describe(
        "Print this design revision of the item as a test instead (order_item_revision_add makes one): an attempt of " +
          'purpose `test` that counts for nothing. It needs no accepted quote, but does need a slice quote of that revision (else `requote_needed`)',
      ),
    }),
    risk: 'outward',
    routes: [
      'POST /api/v1/orders/{order_id}/items/{item_id}/print',
      'POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/test-print',
    ],
    summarize: ({ order_id, item_id, printer, printer_id, test_revision_id, path, part_settings, ...setup }) =>
      `Print ${test_revision_id ? `a test of revision ${test_revision_id} of ` : ''}item ${item_id} of order ${order_id}` +
      (printer ? ` on printer ${printer}` : printer_id !== undefined ? ` on Bambuddy printer ${printer_id}` : '') +
      (path === 'bambuddy' ? " through Bambuddy's queue" : path === 'native' ? ' over the LAN' : '') +
      partSettingsPhrase(part_settings) +
      setupPhrase(setup),
    handler: async ({ order_id, item_id, test_revision_id, ...request }, ctx) =>
      json(
        test_revision_id !== undefined
          ? await command(ctx, `test print revision ${test_revision_id}`, (headers) =>
              ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/test-print', {
                params: { path: { order_id, item_id, revision_id: test_revision_id } },
                body: request as never,
                headers,
              }),
            )
          : await command(ctx, `print item ${item_id}`, (headers) =>
              ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/print', {
                params: { path: { order_id, item_id } },
                body: request as never,
                headers,
              }),
            ),
      ),
  }),
  defineTool({
    name: 'order_attempt_archives',
    label: "Read an attempt's Bambuddy archives",
    description:
      "The Bambuddy archives of a print attempt's run (#4253, one per plate) and what each offers to copy: its " +
      '`finish_photos` and whether it has a `timelapse`. Reading them copies what they say onto the attempt once ' +
      'its print settled (`archive_ids`, the times, `actual_seconds`, `actual_grams`). `linked` is false without ' +
      'Bambuddy; an attempt by hand naming no archive, or one Bambuddy has not linked an archive to yet, has none.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      fulfilment_id: ID.describe("A fulfilment attempt's id, as order_get lists an item's fulfilments"),
    }),
    risk: 'read',
    routes: ['GET /api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives'],
    handler: async ({ order_id, item_id, fulfilment_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives', {
            params: { path: { order_id, item_id, fulfilment_id } },
          }),
          `read attempt ${fulfilment_id}'s archives`,
        ),
      ),
  }),
  defineTool({
    name: 'order_attempt_copy',
    label: "Copy a print's photo or timelapse into an order",
    description:
      "Copy an attempt's Bambuddy archive's finish photo (`finished_photo`, its latest unless `name` names one of " +
      "order_attempt_archives' `finish_photos`) or its `timelapse` onto the attempt, listed under its line item (#4253, #4251), with the role " +
      '`finished` or `timelapse`; the same bytes again answer with the file already there. Refused with ' +
      '`not_linked`, `archive_not_attempts`, `archive_gone`, `no_finish_photo` and `no_timelapse` (pull it off the ' +
      'printer first).',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      fulfilment_id: ID.describe("A fulfilment attempt's id, as order_get lists an item's fulfilments"),
      archive_id: z.number().int().min(1).describe('One of order_attempt_archives\' archive ids'),
      what: z.enum(['finished_photo', 'timelapse']),
      name: z.string().max(255).optional().describe('A finish photo of the archive, for `finished_photo`'),
    }),
    risk: 'write',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives/{archive_id}/copy'],
    title: ({ what, archive_id }) => `Copy archive ${archive_id}'s ${what === 'timelapse' ? 'timelapse' : 'finish photo'}`,
    handler: async ({ order_id, item_id, fulfilment_id, archive_id, ...body }, ctx) =>
      json(
        await command(ctx, `copy archive ${archive_id}'s ${body.what}`, (headers) =>
          ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/archives/{archive_id}/copy', {
            params: { path: { order_id, item_id, fulfilment_id, archive_id } },
            body,
            headers,
          }),
        ),
      ),
  }),
  defineTool({
    name: 'order_item_unfulfil',
    label: 'Undo a line item\'s fulfilment',
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
    name: 'order_item_duplicate',
    label: 'Duplicate a line item',
    description:
      "Duplicate a line item (#4955): a new item with the item's kind, current design (template reference, colours and " +
      'output, or library file), past print archives, quantity, title, notes, SKU and the parts one unit uses. No quote is copied: price the copy ' +
      "with order_item_quote. A custom item's price is its own, so the copy keeps it. On an accepted, in-production or " +
      'ready order set `after_acceptance`: the copy then changes what the customer gets, as ' +
      'order_item_add_after_acceptance does. Refused as an add is: `item_locked` (past `quoted` without ' +
      '`after_acceptance`), `order_not_accepted`, `order_on_hold`, `order_not_submitted` (a storefront visitor cart), and ' +
      '`template_deleted` for a model item whose template was deleted since. Outward, as an add after acceptance is.',
    input: z.object({
      order_id: orderId,
      item_id: itemId,
      after_acceptance: z.boolean().optional().describe('The order is accepted, in production or ready: add the copy after acceptance'),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/items/{item_id}/duplicate'],
    summarize: ({ order_id, item_id }) => `Duplicate item ${item_id} of order ${order_id}`,
    handler: async ({ order_id, item_id, after_acceptance }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/items/{item_id}/duplicate', {
            params: { path: { order_id, item_id } },
            body: { after_acceptance: after_acceptance ?? false },
          }),
          `duplicate item ${item_id}`,
        ),
      ),
  }),
  defineTool({
    name: 'order_fulfil',
    label: 'Fulfil several line items',
    description:
      'Fulfil several line items of an accepted order at once (#4955): for each, `quantity` made by hand, recorded as ' +
      'an attempt that succeeded and accepted, all in one transaction. An item is fulfilled once its accepted attempts ' +
      'reach its quantity. Only `approved` or `failed` items. A printed item\'s print attempt is accepted with ' +
      'order_item_fulfil. One refused item fulfils nothing: `order_not_accepted`, `order_on_hold`, `item_not_open`, ' +
      '`item_not_approved` (its `items` names it), `items` for an item named twice. Outward, as order_item_fulfil is.',
    input: z.object({
      order_id: orderId,
      items: z
        .array(z.object({ item_id: itemId, quantity: z.number().int().min(1).describe('How many of it were made') }))
        .min(1)
        .max(50),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/orders/{order_id}/fulfil'],
    summarize: ({ order_id, items }) => `Fulfil ${items.length} item${items.length === 1 ? '' : 's'} of order ${order_id}`,
    handler: async ({ order_id, items }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/orders/{order_id}/fulfil', { params: { path: { order_id } }, body: { items } }),
          `fulfil ${items.length} items of order ${order_id}`,
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
      'With `item_id`, one cancelled line item comes back as its quotes and attempts say (a shipped order takes ' +
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
    name: 'order_attach',
    label: 'Attach a file to an order',
    description:
      'Attach a file from your workspace to an order, or with `item_id` to one of its line items: a reference ' +
      'drawing, a photo of the finished or packed piece, an issue, a timelapse. Any file; what it is (`kind`, ' +
      '`media_type`) comes from its bytes, and it is kept in ScadBuddy\'s own store while the attachment stands. ' +
      "Allowed in any state of the order; order_get lists it under `attachments`. `role` says why it is kept " +
      '(`other` when left out). An item\'s image can then be marked delivered with order_attachment_update.' +
      FROM_WORKSPACE_ONLY,
    input: z
      .object({
        order_id: orderId,
        item_id: itemId.optional().describe('Attach to this line item; the order itself when left out'),
        fulfilment_id: ID.optional().describe(
          "Attach to this fulfilment attempt of `item_id` (a print's photo, its timelapse); beside `revision_id`, the test attempt the file shows",
        ),
        revision_id: ID.optional().describe(
          "Attach to this design revision of `item_id`: its `render`, the photo of a `test_print`, or a `reference`",
        ),
        workspace_path: workspacePathInput,
        workspace_sha256: workspaceShaInput,
        filename: z.string().min(1).max(255).optional().describe("e.g. \"finished.jpg\"; the workspace file's own name by default"),
        role: attachmentRole.optional(),
        caption: attachmentCaption.optional(),
      })
      .strict(),
    risk: 'write',
    routes: [
      'POST /api/v1/orders/{order_id}/attachments',
      'POST /api/v1/orders/{order_id}/items/{item_id}/attachments',
      'POST /api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/attachments',
      'POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/attachments',
    ],
    title: ({ order_id, item_id, fulfilment_id, revision_id, filename, workspace_path }) =>
      `Attach ${filename ?? workspace_path} to ${revision_id ? `revision ${revision_id} of ` : fulfilment_id ? `attempt ${fulfilment_id} of ` : ''}${item_id ? `item ${item_id} of ` : ''}order ${order_id}`,
    handler: async (
      { order_id, item_id, fulfilment_id, revision_id, workspace_path, workspace_sha256, filename, role, caption },
      ctx,
    ) => {
      if ((fulfilment_id !== undefined || revision_id !== undefined) && item_id === undefined)
        throw new ToolError('an attempt or a revision is attached to with its item_id')
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'order_attach')
      const name = filename ?? uploadName(info.path)
      const fields = {
        ...(role === undefined ? {} : { role }),
        ...(caption === undefined ? {} : { caption }),
        ...(revision_id !== undefined && fulfilment_id !== undefined ? { fulfilment_id } : {}),
      }
      const body = { file: name, role: role ?? null, caption: caption ?? null }
      const part = { name: 'file', filename: name, type: info.mediaType }
      const answer = await command(ctx, `attach ${name} to ${item_id ? `item ${item_id}` : `order ${order_id}`}`, (headers) =>
        item_id === undefined
          ? ctx.backend.POST('/api/v1/orders/{order_id}/attachments', {
              params: { path: { order_id } },
              body,
              ...uploadInit(ctx, info, part, headers, fields),
            })
          : revision_id !== undefined
            ? ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/attachments', {
                params: { path: { order_id, item_id, revision_id } },
                body: { ...body, fulfilment_id: fulfilment_id ?? null },
                ...uploadInit(ctx, info, part, headers, fields),
              })
          : fulfilment_id !== undefined
            ? ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/attachments', {
                params: { path: { order_id, item_id, fulfilment_id } },
                body,
                ...uploadInit(ctx, info, part, headers, fields),
              })
            : ctx.backend.POST('/api/v1/orders/{order_id}/items/{item_id}/attachments', {
              params: { path: { order_id, item_id } },
              body,
              ...uploadInit(ctx, info, part, headers, fields),
            }),
      )
      return json(withSent(answer, info))
    },
  }),
  defineTool({
    name: 'order_attachment_update',
    label: "Edit an order's attachment",
    description:
      "Caption or re-role an attachment (order_get lists them under `attachments`), in any state of the order. " +
      "A file of a design revision sent to the customer, or answered, keeps its role and caption (refused, " +
      "`revision_sent`); no file is re-roled to `test_print` (upload the photo of a test print). " +
      "`delivered` with the attachment's `item_id`: true marks this image as what the item delivered (its picture " +
      "from then on; refused unless it is one of that item's images), false clears the item's delivered mark.",
    input: z.object({
      order_id: orderId,
      attachment_id: attachmentId,
      caption: attachmentCaption.optional(),
      role: attachmentRole.optional(),
      item_id: itemId.optional().describe('The line item the attachment is on; needed with `delivered`'),
      delivered: z.boolean().optional(),
    }),
    risk: 'write',
    routes: ['PATCH /api/v1/orders/{order_id}/attachments/{attachment_id}', 'PUT /api/v1/orders/{order_id}/items/{item_id}/delivered'],
    title: ({ order_id, attachment_id, delivered }) =>
      `${delivered === undefined ? 'Edit' : delivered ? 'Mark delivered' : 'Clear the delivered mark of'} attachment ${attachment_id} of order ${order_id}`,
    handler: async ({ order_id, attachment_id, caption, role, item_id, delivered }, { backend }) => {
      if (delivered !== undefined && item_id === undefined) {
        throw new ToolError("`delivered` needs `item_id`: the line item the attachment is on (order_get lists it as the attachment's `item_id`)")
      }
      const edits = Object.fromEntries(Object.entries({ caption, role }).filter(([, v]) => v !== undefined))
      if (Object.keys(edits).length === 0 && delivered === undefined) throw new ToolError('nothing to change: give `caption`, `role` or `delivered`')
      const answer: { attachment?: unknown; item?: unknown } = {}
      if (Object.keys(edits).length > 0) {
        answer.attachment = await ok(
          backend.PATCH('/api/v1/orders/{order_id}/attachments/{attachment_id}', { params: { path: { order_id, attachment_id } }, body: edits }),
          `edit attachment ${attachment_id}`,
        )
      }
      if (delivered !== undefined && item_id !== undefined) {
        answer.item = await ok(
          backend.PUT('/api/v1/orders/{order_id}/items/{item_id}/delivered', {
            params: { path: { order_id, item_id } },
            body: { attachment_id: delivered ? attachment_id : null },
          }),
          `mark what item ${item_id} delivered`,
        )
      }
      return json(answer)
    },
  }),
  defineTool({
    name: 'order_attachment_delete',
    label: "Remove an order's attachment",
    description:
      'Remove an attachment (a mis-upload): it goes, recorded on the order\'s audit line, with an item\'s ' +
      'delivered mark when it was this one, and its file is freed once nothing else holds the same bytes. ' +
      'Outward: no history brings the file back.',
    input: z.object({ order_id: orderId, attachment_id: attachmentId }),
    risk: 'outward',
    routes: ['DELETE /api/v1/orders/{order_id}/attachments/{attachment_id}'],
    summarize: ({ order_id, attachment_id }) => `Remove attachment ${attachment_id} from order ${order_id}`,
    handler: async ({ order_id, attachment_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/orders/{order_id}/attachments/{attachment_id}', { params: { path: { order_id, attachment_id } } }),
        `remove attachment ${attachment_id}`,
      )
      return deleted(attachment_id)
    },
  }),

  defineTool({
    name: 'price_book_get',
    label: 'Read the price book',
    description:
      "The price book in force: the currency the next order copies (and its `minor_exponent`), the pricing policy " +
      '(machine rate per hour, setup fee per plate, labour per item, markup percent, rounding), the rough ' +
      "estimate's factor, and the filament records not archived, each with its cost per kilogram. " +
      '`include_archived: true` lists the archived records too (each with its `archived_at`), to unarchive one.',
    input: z.object({ include_archived: z.boolean().default(false) }),
    risk: 'read',
    routes: ['GET /api/v1/orders/price-book'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/orders/price-book', { params: { query } }), 'read the price book')),
  }),
  defineTool({
    name: 'price_book_set',
    label: 'Edit the price book',
    description:
      'Keep the price book: `policy` (the fields given replace those in force, the rest stay), ' +
      "`rough_estimate_factor`, and `filament`: a new record (no `filament_id`; material, colour and cost_per_kg), " +
      'an edit of one (`filament_id` with the fields to change), `archive: true` to hide it from the book, or ' +
      '`unarchive: true` to put an archived one back with its id (`filament_exists` when the same filament was added ' +
      'again since). A recorded quote keeps the rates it was priced at.',
    input: z.object({
      policy: policyFields.optional(),
      rough_estimate_factor: decimal.optional(),
      filament: z
        .object({ filament_id: ID.optional(), archive: z.boolean().default(false), unarchive: z.boolean().default(false), ...filamentFields })
        .strict()
        .optional(),
    }),
    risk: 'write',
    routes: [
      'PUT /api/v1/orders/price-book',
      'POST /api/v1/orders/price-book/filaments',
      'PATCH /api/v1/orders/price-book/filaments/{filament_id}',
      'POST /api/v1/orders/price-book/filaments/{filament_id}/archive',
      'POST /api/v1/orders/price-book/filaments/{filament_id}/unarchive',
    ],
    title: ({ policy, filament }) =>
      filament?.archive
        ? `Archive filament ${filament.filament_id ?? ''}`.trim()
        : filament?.unarchive
          ? `Unarchive filament ${filament.filament_id ?? ''}`.trim()
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
        const { filament_id, archive, unarchive, ...fields } = filament
        const body = Object.fromEntries(Object.entries(fields).filter(([, v]) => v !== undefined))
        if (archive && unarchive) throw new ToolError('archive and unarchive cannot both be true')
        if (filament_id === undefined) {
          if (archive || unarchive) throw new ToolError(`${archive ? 'archive' : 'unarchive'} needs a filament_id`)
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
          if (unarchive) {
            answer.filament = await ok(
              backend.POST('/api/v1/orders/price-book/filaments/{filament_id}/unarchive', { params: { path } }),
              `unarchive filament ${filament_id}`,
            )
          }
          if (answer.filament === undefined) throw new ToolError('give the fields to change, or archive or unarchive: true')
        }
      }
      return json(answer)
    },
  }),
  defineTool({
    name: 'machine_rate_get',
    label: "Read a printer's machine rate",
    description:
      "A registry printer's machine rate (list_printers' `id`): its inputs (`inputs`, null with none) and the rate " +
      'they give now with each part (`breakdown`): each maintenance task\'s minutes at the labour rate plus its ' +
      'consumables over its interval in print-hours, each wear part\'s cost over its life, the power (watts / 1000 x ' +
      'the price of a kWh) and the purchase price over the life, in the shop\'s currency. A printer linked to Bambuddy ' +
      'takes its tasks and intervals from Bambuddy (`intervals` `bambuddy`, or `stored` when Bambuddy did not ' +
      "answer). A printer with no inputs is the price book's machine rate (`source` `price_book`). A slice quote " +
      'for the printer is priced at this rate and records each part.',
    input: z.object({ printer: ID.describe("The registry printer (list_printers' `id`)") }),
    risk: 'read',
    routes: ['GET /api/v1/printers/{printer_id}/machine-rate'],
    handler: async ({ printer }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/printers/{printer_id}/machine-rate', { params: { path: { printer_id: printer } } }),
          `read printer ${printer}'s machine rate`,
        ),
      ),
  }),
  defineTool({
    name: 'machine_rate_set',
    label: "Set a printer's machine rate",
    description:
      "Keep a registry printer's machine rate inputs (machine_rate_get): `inputs` replaces them whole: `profile` " +
      '(purchase_price, life_hours (null: no depreciation), watts, kwh_price, labour_rate_per_hour (null: the ' +
      "shop's labour rate)), `wear_parts` ({name, cost, life_hours}) and `maintenance` ({task, minutes, " +
      'consumables, interval_hours}; a printer linked to Bambuddy names its Bambuddy tasks and keeps Bambuddy\'s ' +
      "intervals). `remove: true` drops them, so the printer is priced at the price book's machine rate again. " +
      'Decimals are strings. A recorded quote keeps the rate it was priced at.',
    input: z.object({
      printer: ID.describe("The registry printer (list_printers' `id`)"),
      inputs: z
        .object({
          profile: z
            .object({
              purchase_price: decimal.optional(),
              life_hours: decimal.nullable().optional(),
              watts: decimal.optional(),
              kwh_price: decimal.optional(),
              labour_rate_per_hour: decimal.nullable().optional(),
            })
            .strict(),
          wear_parts: z
            .array(z.object({ name: z.string().min(1).max(100), cost: decimal, life_hours: decimal }).strict())
            .max(50)
            .default([]),
          maintenance: z
            .array(
              z
                .object({
                  task: z.string().min(1).max(100),
                  minutes: decimal.optional(),
                  consumables: decimal.optional(),
                  interval_hours: decimal.nullable().optional(),
                })
                .strict(),
            )
            .max(50)
            .default([]),
        })
        .strict()
        .optional(),
      remove: z.boolean().default(false),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/printers/{printer_id}/machine-rate', 'DELETE /api/v1/printers/{printer_id}/machine-rate'],
    title: ({ printer, remove }) => (remove ? `Remove printer ${printer}'s machine rate` : `Set printer ${printer}'s machine rate`),
    handler: async ({ printer, inputs, remove }, { backend }) => {
      if (remove && inputs !== undefined) throw new ToolError('give inputs or remove: true, not both')
      const path = { printer_id: printer }
      if (remove) {
        await ok(backend.DELETE('/api/v1/printers/{printer_id}/machine-rate', { params: { path } }), `remove printer ${printer}'s machine rate`)
        return json({ removed: printer })
      }
      if (inputs === undefined) throw new ToolError('nothing to change: give inputs, or remove: true')
      // Checked against the route's schema, so the zod shape cannot drift from it. The
      // generated type marks a field with a default as required (openapi-typescript's
      // defaultNonNullable), though the API takes it omitted: `Sent` lets it be left out.
      const sent: MachineRateSent = inputs
      return json(
        await ok(
          backend.PUT('/api/v1/printers/{printer_id}/machine-rate', {
            params: { path },
            body: sent as components['schemas']['MachineRateInputs'],
          }),
          `set printer ${printer}'s machine rate`,
        ),
      )
    },
  }),
  defineTool({
    name: 'price_book_import_spools',
    label: "Import filaments from Bambuddy's spools",
    description:
      "Make or update the price book's filament records from Bambuddy's spool inventory: one record per filament " +
      '(material, subtype, brand and colour), made when the book has none, else given the most recent priced ' +
      "spool's cost per kilogram; `min_cost_per_kg`/`max_cost_per_kg` show the spread when the spools' prices " +
      'differ. A filament known only by its colour name, or with no priced spool, is `skipped` with its `reason`. ' +
      "Only when Bambuddy is linked (price_book_get's `spool_import`); else `not_linked`. Quotes never read spools.",
    input: z.object({}),
    risk: 'write',
    routes: ['POST /api/v1/orders/price-book/import-spools'],
    title: () => 'Import filaments from spools',
    handler: async (_input, { backend }) =>
      json(await ok(backend.POST('/api/v1/orders/price-book/import-spools'), 'import filaments from spools')),
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

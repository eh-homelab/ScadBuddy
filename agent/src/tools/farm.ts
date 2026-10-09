import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'
import { offsetPage, page, PAGED, pageInput } from './pagination.js'

// Farm context (issue #1912, #251's read tier): the queue, aggregate stats, every
// Bambuddy archive with its outcome, and the spool inventory with each loaded slot's
// remaining grams, which feed the analyzers (#284). Read only: none of these writes to
// Bambuddy. Routes: backend/scadbuddy/api/farm.py.

const SOURCE = "Bambuddy's records: names and notes typed by whoever uses that Bambuddy"

const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'must be a day, YYYY-MM-DD')
const printerId = z.number().int().describe('A Bambuddy printer id, as get_print_targets lists them')

export const farmTools: Tool[] = [
  defineTool({
    name: 'list_print_queue',
    description:
      "Bambuddy's print queue, in its order: each item's status, printer (or the printer model it waits for), " +
      'file, times, estimated duration and filament, and why it is waiting or failed. Finished items stay in the ' +
      'queue, so pass `status` (pending or printing for what is to come). `printer_id` -1 is items not assigned ' +
      'to a printer.' +
      PAGED,
    input: z.object({
      printer_id: z.number().int().min(-1).optional(),
      status: z.enum(['pending', 'printing', 'completed', 'failed', 'cancelled']).optional(),
      ...pageInput,
    }),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/farm/queue'],
    handler: async (args, { backend }) => {
      const query = { printer_id: args.printer_id, status: args.status }
      const items = await ok(backend.GET('/api/v1/farm/queue', { params: { query } }), 'list the print queue')
      return json(page(items, args, (item) => String(item.id), 'list_print_queue'))
    },
  }),

  defineTool({
    name: 'get_print_stats',
    description:
      "The farm's print statistics from Bambuddy: prints by outcome (successful, failed, cancelled), total print " +
      'hours, filament grams, cost and energy, prints by filament type and by printer, and how close the time ' +
      'estimates came. Over every archive, or those created between `from` and `to` (inclusive).',
    input: z.object({ from: day.optional(), to: day.optional() }),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/farm/stats'],
    handler: async ({ from, to }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/farm/stats', { params: { query: { date_from: from, date_to: to } } }),
          'get the print statistics',
        ),
      ),
  }),

  defineTool({
    name: 'list_print_archives',
    description:
      "Every print Bambuddy archived, ScadBuddy's or not (list_prints is ScadBuddy's own, with their parameters): " +
      'each with its status, printer, project, times, filament, cost, failure reason and run counts. Filter by ' +
      '`printer_id`, `project_id` and creation day (`from`/`to`, inclusive).' +
      PAGED,
    input: z.object({
      printer_id: printerId.optional(),
      project_id: z.number().int().min(1).optional(),
      from: day.optional(),
      to: day.optional(),
      ...pageInput,
    }),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/farm/archives'],
    handler: async (args, { backend }) => {
      const filters = { printer_id: args.printer_id, project_id: args.project_id, date_from: args.from, date_to: args.to }
      return json(
        await offsetPage(args, (row: { id: number }) => String(row.id), 'list_print_archives', (window) =>
          ok(
            backend.GET('/api/v1/farm/archives', { params: { query: { ...filters, ...window } } }),
            'list the print archives',
          ),
        ),
      )
    },
  }),

  defineTool({
    name: 'get_spool_inventory',
    description:
      "Bambuddy's spool inventory: every spool with its material, colour, brand, label weight and what is left, " +
      'and where it is loaded; and every loaded slot of each active printer (AMS, tray, extruder) with the ' +
      "remaining grams Bambuddy reconciled from the AMS. `printer_id` limits the slots to one printer; archived " +
      'spools only with `include_archived`.',
    input: z.object({ include_archived: z.boolean().optional(), printer_id: printerId.optional() }),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/farm/inventory'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/farm/inventory', { params: { query } }), 'get the spool inventory')),
  }),
]

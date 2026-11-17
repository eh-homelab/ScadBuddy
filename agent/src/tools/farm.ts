import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'
import { offsetPage, page, PAGED, pageInput } from './pagination.js'

// Farm context (issue #1912, #251's read tier): the queue, aggregate stats and every
// Bambuddy archive with its outcome, which feed the analyzers (#284). Read only: none of
// these writes to Bambuddy. Routes: backend/scadbuddy/api/farm.py. The spool inventory and
// a spool's summary are ScadBuddy's own spools (#6262 Task 9), read from
// backend/scadbuddy/api/filament.py.

const SOURCE = "Bambuddy's records: names and notes typed by whoever uses that Bambuddy"
const SPOOLS = "ScadBuddy's own filament inventory: names and notes typed by its users"

const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'must be a day, YYYY-MM-DD')
const printerId = z.number().int().describe('A Bambuddy printer id, as get_print_targets lists them')

export const farmTools: Tool[] = [
  defineTool({
    name: 'list_print_queue',
    label: 'List the print queue',
    description:
      "Bambuddy's print queue, in its order: each item's status, printer (or the printer model it waits for), " +
      'file, times, estimated duration and filament, and why it is waiting or failed. Finished items stay in the ' +
      'queue, so pass `status` (pending or printing for what is to come). `printer_id` -1 is items not assigned ' +
      'to a printer. A pending or printing item ScadBuddy queued (a print run or a reprint) carries ' +
      '`scadbuddy` (its output or library file, template `slug`, `run_id` and linked `archive_id`), matched ' +
      "only by ScadBuddy's record of that item; a finished item has none, and so has an item queued from " +
      'Bambuddy, its reprint of a ScadBuddy print too. Each item has `picture` (#3742), a URL on ScadBuddy of its ' +
      "image: ScadBuddy's own for its item (the output's or library file's thumbnail), else Bambuddy's thumbnail " +
      'through ScadBuddy; null when there is none.' +
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
    name: 'get_print_queue_item',
    label: 'Get a print queue item',
    description:
      "One item of Bambuddy's print queue by its id (#3744), whoever queued it and in any state: status, " +
      'position, printer (or the printer model it waits for), file, plate, scheduled time, manual start, ' +
      'estimated time and filament, `filament_short`, the AMS tray per filament slot (`ams_mapping`), and why ' +
      'it waits or failed. A job ScadBuddy queued carries `scadbuddy` (its output or library file, template ' +
      "`slug`, `run_id` and linked `archive_id`) even once it has finished; with `archive_id`, get_print reads " +
      'its print. An item Bambuddy no longer has (deleted, or gone with its archive) is a not-found error.',
    input: z.object({ item_id: z.number().int().min(1).describe('A queue item id, as list_print_queue lists them') }),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/farm/queue/{item_id}'],
    handler: async ({ item_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/farm/queue/{item_id}', { params: { path: { item_id } } }),
          `read queue item ${item_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'get_printers_now',
    label: 'See what each printer is doing',
    description:
      "What each active printer is doing now, whoever started it (#977): its activity (printing, paused, idle, " +
      'finished, failed, offline or unknown), the job name, progress, layer and minutes left while it works, its ' +
      "HMS errors, whether it waits for the plate to be cleared, and Bambuddy's archive of the job. A printer " +
      'whose status cannot be read is listed with an error rather than dropped. Each printer has `printer_id`, ' +
      'the Bambuddy printer id every print tool takes (as get_print_targets lists them), `id`, its registry id, ' +
      'which the registry tools and print_output (`printer`) take, and `path`, the link that answered (native ' +
      'over the LAN, bambuddy, or none). `printer_id` is null for a printer reached only over the LAN, which the ' +
      'print tools address by `id` (print_output, get_print_choices and get_print_filaments take it as `printer`). ' +
      'A printing or paused job ScadBuddy queued or started carries `scadbuddy` (its output or library file, ' +
      'template `slug`, `run_id` and linked `archive_id`). On a printer linked to Bambuddy, when its queue has a ' +
      "printing item on the printer, only ScadBuddy's record of that item matches, so a job queued from Bambuddy " +
      '(its reprint of a ScadBuddy print too) has none; when its queue cannot be read, nothing matches. Otherwise ' +
      "a run's LAN start on that printer under the job's name matches until the run records that start ended; a " +
      'job started on the printer itself has none unless it has such a name. A working job has `picture` (#3742), a ' +
      "URL on ScadBuddy of its image: ScadBuddy's own for its job, else Bambuddy's cover of the job through " +
      'ScadBuddy on a card Bambuddy answered; null when there is none.',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/print/printers/now'],
    handler: async (_args, { backend }) =>
      json(await ok(backend.GET('/api/v1/print/printers/now'), 'read what each printer is doing')),
  }),

  defineTool({
    name: 'get_print_stats',
    label: 'Read print statistics',
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
    label: 'List archived prints',
    description:
      "Every print Bambuddy archived, ScadBuddy's or not (list_prints is ScadBuddy's own, with their parameters): " +
      'each with its status, printer, project, times, filament, cost, failure reason and run counts, and ' +
      '`scadbuddy_print` (false: a print ScadBuddy did not start, which get_print_sources and import_print can ' +
      'import; the print watcher may have imported it already or put it in Unsorted imports: list_auto_imports). ' +
      'Filter by ' +
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
    label: 'List spools',
    description:
      "ScadBuddy's spool inventory: every spool by `id` (a uuid) and `number` (#N), with its effective material, " +
      'brand, colour (`rgba`, `#RRGGBBAA`: an alpha below FF is a translucent spool), label weight, grams left ' +
      "and cost per kg, its presets, and `loaded`: the registry printer and tray it is in (null on the shelf; " +
      "while Bambuddy is linked, a spool no registry printer reports is in the tray Bambuddy assigns it). " +
      "`trays` lists each registry printer's reported trays (label, material, `rgba`) with the spool each holds. " +
      'Archived spools only with `include_archived`. Needs no Bambuddy: a refused assignment read places the ' +
      'spools by the reports alone.',
    input: z.object({ include_archived: z.boolean().optional() }),
    risk: 'read',
    source: SPOOLS,
    routes: ['GET /api/v1/filament/inventory'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/filament/inventory', { params: { query } }), 'get the spool inventory')),
  }),

  defineTool({
    name: 'get_spool_summary',
    label: "Read one spool's stock and price",
    description:
      "One ScadBuddy spool's stock and price (#785): its grams left, the tray it is in and its effective cost per " +
      'kg, what the grams left are worth (`value_left`), and the stock of every unarchived spool of the same ' +
      'filament (material, subtype, brand and `#RRGGBBAA`, so a translucent spool is another filament): how ' +
      "many, their numbers, their total grams left and where each is loaded. `grams` prices that many grams at " +
      "this spool's cost, e.g. a plate's use. Every price and cost is null when the spool has no cost per kg; " +
      'never estimate one.',
    input: z.object({
      spool_id: z.string().uuid().describe("A spool's `id` (a uuid), as get_spool_inventory lists them"),
      grams: z.number().min(0).optional(),
    }),
    risk: 'read',
    source: SPOOLS,
    routes: ['GET /api/v1/filament/spools/{spool_id}/summary'],
    handler: async ({ spool_id, grams }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/filament/spools/{spool_id}/summary', {
            params: { path: { spool_id }, query: { grams } },
          }),
          `summarise spool ${spool_id}`,
        ),
      ),
  }),
]

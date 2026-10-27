import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'

// The printer registry (#3823, plan 2026-10-16-printer-identity-native-path, Task 11):
// one row per physical printer, keyed by its serial, with the Bambuddy printer it is and
// how ScadBuddy reaches it over the LAN. Routes: backend/scadbuddy/api/printers.py.
//
// The serial stays out of every result (it is an identifier the printer's cloud account
// is bound to): a tool returns the row's `id`, which every tool here takes. Settings >
// Printers, where these tools send the model, shows it as plain text, which the browser
// bridge's page read (`frontend/src/agent/snapshot.ts`: controls, alerts and status only)
// leaves out; `Printers.test.tsx` holds it to that (#4375). No tool takes
// the LAN access code (#4340 review I1): it is a device credential, typed only in
// Settings > Printers (`PUT /printers/{id}/lan` is a coverage.ts NOT_A_TOOL), so it never
// enters a model's context, a transcript or a flow script. Unlinking the LAN deletes that
// stored code and stops the printer's watch, so it is outward, as delete_printer is.

const SOURCE = "ScadBuddy's printer registry, with names typed by whoever set it up and Bambuddy's printer names"

const printer = z.string().uuid().describe("A registry printer's `id`, as list_printers lists them")
const path = z
  .enum(['auto', 'native', 'bambuddy'])
  .describe('How a print reaches it: native over the LAN, bambuddy through Bambuddy, auto the LAN when it answers')

type Row = { serial?: unknown } & Record<string, unknown>

/** A row without its serial. */
function shown<T extends Row>(row: T): Omit<T, 'serial'> {
  const { serial: _serial, ...rest } = row
  return rest
}

const ROW_FIELDS =
  'Each row has `id` (the registry id the printer tools take), `name`, `model`, `bambuddy_printer_id` (the ' +
  'Bambuddy printer it is, or null), `lan_host` and `has_access_code` (its LAN link, the code itself never ' +
  'shown), `preferred_path` and `bambuddy_active`; `kind` (`physical`, or `bambuddy_virtual` for one of ' +
  "Bambuddy's virtual printers, never a printer to print on: `bambuddy_vp_id`, `vp_mode`, and `forwards_to`, " +
  "the registry printer it forwards to: null with `bambuddy_view.target_printer_id` null is Bambuddy's 'any " +
  "printer of its model', but null with a `target_printer_id` means its target has no row here, never 'any'); " +
  "`bambuddy_view`, Bambuddy's own values at the last sync (name, model, IP, active, nozzle count; a virtual printer's " +
  'settings); `lan_sync_error`, why the sync could not fill or move the LAN link; and `lan_move_pending`, ' +
  "whether the next sync tries again to move the link to Bambuddy's new address."

/** Where a LAN link is made: by the user in Settings, or by the sync from Bambuddy's code (#4798). */
const LAN_IN_SETTINGS =
  ' A user links a printer over the LAN in Settings > Printers, where they type its access code: ask the ' +
  'user to do it there (open the page with browser_navigate to /settings#printers), and never ask for the ' +
  'code in chat. The one tool that makes a LAN link is sync_printers_from_bambuddy (outward, approved), which ' +
  "fills an empty link from Bambuddy's address and access code when Bambuddy gives the code, and moves a " +
  "link that follows Bambuddy's address when that address changes."

export const printerTools: Tool[] = [
  defineTool({
    name: 'list_printers',
    description:
      `Every printer in ScadBuddy's registry, with its Bambuddy link and its LAN link. ${ROW_FIELDS}${LAN_IN_SETTINGS}` +
      PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers'],
    handler: async (args, { backend }) => {
      const rows = (await ok(backend.GET('/api/v1/printers'), 'list the printers')).map(shown)
      return json(page(rows, args, (row) => row.id, 'list_printers'))
    },
  }),

  defineTool({
    name: 'get_printer',
    description: `One printer in the registry. ${ROW_FIELDS}${LAN_IN_SETTINGS}`,
    input: z.object({ printer }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers/{printer_id}'],
    handler: async ({ printer: id }, { backend }) =>
      json(
        shown(await ok(backend.GET('/api/v1/printers/{printer_id}', { params: { path: { printer_id: id } } }), 'get the printer')),
      ),
  }),

  defineTool({
    name: 'discover_printers',
    description:
      "Bambuddy's active printers, each with `linked_to` (the registry printer that is it, or null) and whether " +
      'it can be linked: one whose serial Bambuddy does not report can only be linked to a printer created by ' +
      'serial (`reason` says so).',
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/printers/discover'],
    handler: async (_args, { backend }) =>
      json((await ok(backend.GET('/api/v1/printers/discover'), "list Bambuddy's printers")).map(shown)),
  }),

  defineTool({
    name: 'sync_printers_from_bambuddy',
    description:
      "Imports and links Bambuddy's printers and virtual printers into the registry in one idempotent pass " +
      '(#4439, #4747): a linked row for every active Bambuddy printer that reports a serial; an empty LAN link ' +
      "filled from Bambuddy's address and access code through the same probe as Settings > Printers (Bambuddy " +
      'gives the code only while its auth is off; otherwise the row says it is withheld); a `bambuddy_virtual` ' +
      'row for every Bambuddy virtual printer. It never unlinks, deletes or overwrites: each entry says ' +
      '`created`, `linked`, `already_linked`, `skipped`, `conflict` or `missing`, with a `reason`, its LAN ' +
      'outcome, and whether the sync `changed` the row. `dry_run` plans the same and writes nothing. The access code is never in the result. Running ' +
      'it again changes nothing.',
    input: z.object({
      dry_run: z.boolean().default(false).describe('Plan and report without writing anything'),
    }),
    // Outward: it stores printers' LAN access codes, read from Bambuddy, and dials the printers.
    risk: 'outward',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['POST /api/v1/printers/sync-bambuddy'],
    summarize: ({ dry_run }) =>
      dry_run
        ? "Plan an import of Bambuddy's printers (nothing written)"
        : "Import and link Bambuddy's printers and virtual printers, filling empty LAN links from Bambuddy",
    handler: async ({ dry_run }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/printers/sync-bambuddy', { params: { query: { dry_run } } }),
          'sync the printers from Bambuddy',
        ),
      ),
  }),

  defineTool({
    name: 'create_printer',
    description:
      'Adds a printer to the registry by its serial (on the printer, or in Bambu Handy), with a name. Link it to ' +
      'Bambuddy or the LAN afterwards. A serial already registered is refused, naming the row that has it.',
    // The backend's bounds (printers/models.py PrinterCreate), so a bad serial is refused here, saying why.
    input: z.object({
      serial: z
        .string()
        .trim()
        .toUpperCase()
        .pipe(z.string().regex(/^[0-9A-Z]{8,32}$/, 'must be the printer serial: 8 to 32 letters and digits')),
      name: z.string().min(1).max(80),
      model: z.string().max(40).optional(),
      preferred_path: path.default('auto'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/printers'],
    title: ({ name }) => `Add printer ${name}`,
    handler: async (body, { backend }) =>
      json(shown(await ok(backend.POST('/api/v1/printers', { body }), 'add the printer'))),
  }),

  defineTool({
    name: 'update_printer',
    description: "Renames a printer or sets its `preferred_path`, how a print reaches it.",
    input: z.object({ printer, name: z.string().min(1).max(80).optional(), preferred_path: path.optional() }),
    risk: 'write',
    source: SOURCE,
    routes: ['PATCH /api/v1/printers/{printer_id}'],
    handler: async ({ printer: id, ...body }, { backend }) =>
      json(
        shown(
          await ok(
            backend.PATCH('/api/v1/printers/{printer_id}', { params: { path: { printer_id: id } }, body }),
            'update the printer',
          ),
        ),
      ),
  }),

  defineTool({
    name: 'link_printer_bambuddy',
    description:
      'Links a registry printer to the Bambuddy printer it is (`bambuddy_printer_id`, as discover_printers lists ' +
      'them). Refused when the serials differ, or another row already links that Bambuddy printer.',
    input: z.object({ printer, bambuddy_printer_id: z.number().int().positive() }),
    risk: 'write',
    source: SOURCE,
    bambuddyScope: ['Read Status'],
    routes: ['PUT /api/v1/printers/{printer_id}/bambuddy'],
    handler: async ({ printer: id, bambuddy_printer_id }, { backend }) =>
      json(
        shown(
          await ok(
            backend.PUT('/api/v1/printers/{printer_id}/bambuddy', {
              params: { path: { printer_id: id } },
              body: { bambuddy_printer_id },
            }),
            'link the Bambuddy printer',
          ),
        ),
      ),
  }),

  defineTool({
    name: 'unlink_printer_bambuddy',
    description: 'Unlinks a registry printer from its Bambuddy printer; the row keeps its LAN link and history.',
    input: z.object({ printer }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/bambuddy'],
    handler: async ({ printer: id }, { backend }) =>
      json(
        shown(
          await ok(
            backend.DELETE('/api/v1/printers/{printer_id}/bambuddy', { params: { path: { printer_id: id } } }),
            'unlink the Bambuddy printer',
          ),
        ),
      ),
  }),

  defineTool({
    name: 'unlink_printer_lan',
    description:
      "Removes a printer's LAN link: its stored access code is deleted and ScadBuddy stops watching the printer, " +
      'so printing to it over the LAN needs the user to link it again in Settings > Printers.',
    input: z.object({ printer }),
    risk: 'outward',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/lan'],
    summarize: ({ printer: id }) => `Unlink printer ${id} from the LAN, deleting its stored access code`,
    handler: async ({ printer: id }, { backend }) =>
      json(
        shown(
          await ok(
            backend.DELETE('/api/v1/printers/{printer_id}/lan', { params: { path: { printer_id: id } } }),
            'unlink the LAN',
          ),
        ),
      ),
  }),

  defineTool({
    name: 'delete_printer',
    description:
      'Deletes a printer from the registry, with its LAN link. Refused (HTTP 409) while it is linked to a ' +
      'Bambuddy printer: unlink it first.',
    input: z.object({ printer }),
    risk: 'outward',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}'],
    summarize: ({ printer: id }) => `Delete printer ${id} from the registry`,
    handler: async ({ printer: id }, { backend }) => {
      await ok(backend.DELETE('/api/v1/printers/{printer_id}', { params: { path: { printer_id: id } } }), 'delete the printer')
      return json({ deleted: id })
    },
  }),
]

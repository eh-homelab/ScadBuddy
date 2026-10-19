import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'

// The printer registry (#3823, plan 2026-10-16-printer-identity-native-path, Task 11):
// one row per physical printer, keyed by its serial, with the Bambuddy printer it is and
// how ScadBuddy reaches it over the LAN. Routes: backend/scadbuddy/api/printers.py.
//
// The serial stays out of every result (it is an identifier the printer's cloud account
// is bound to): a tool returns the row's `id`, which every tool here takes. The LAN
// access code is sent once, by link_printer_lan (outward: a human approves it), and
// never returned: a row says only `has_access_code`. Its argument is named so that the
// session log and an approval's summary blank it (sessions/sdkEvents.ts SENSITIVE_KEY).

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
  'shown), `preferred_path` and `bambuddy_active`.'

export const printerTools: Tool[] = [
  defineTool({
    name: 'list_printers',
    description: `Every printer in ScadBuddy's registry, with its Bambuddy link and its LAN link. ${ROW_FIELDS}`,
    input: z.object({}),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers'],
    handler: async (_args, { backend }) =>
      json((await ok(backend.GET('/api/v1/printers'), 'list the printers')).map(shown)),
  }),

  defineTool({
    name: 'get_printer',
    description: `One printer in the registry. ${ROW_FIELDS}`,
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
    name: 'create_printer',
    description:
      'Adds a printer to the registry by its serial (on the printer, or in Bambu Handy), with a name. Link it to ' +
      'Bambuddy or the LAN afterwards. A serial already registered is refused, naming the row that has it.',
    input: z.object({
      serial: z.string().min(1).max(64),
      name: z.string().min(1).max(100),
      model: z.string().max(64).optional(),
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
    input: z.object({ printer, name: z.string().min(1).max(100).optional(), preferred_path: path.optional() }),
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
    name: 'link_printer_lan',
    description:
      "Links a printer over the LAN: its host or IP address and its 8-digit access code (on the printer's screen). " +
      'The backend tests both on the printer before it stores anything, so a printer that does not answer is ' +
      'refused (HTTP 502), as is an address off the LAN. The code is stored sealed and never shown again, here ' +
      'or anywhere. Prefer that the user types it in Settings > Printers rather than in chat.',
    input: z.object({
      printer,
      host: z.string().min(1).max(253),
      access_code: z.string().regex(/^\d{8}$/, 'must be the 8-digit code the printer shows'),
    }),
    risk: 'outward',
    source: SOURCE,
    routes: ['PUT /api/v1/printers/{printer_id}/lan'],
    summarize: ({ printer: id, host }) => `Link printer ${id} over the LAN at ${host}`,
    title: ({ host }) => `Link printer at ${host}`,
    handler: async ({ printer: id, host, access_code }, { backend }) =>
      json(
        shown(
          await ok(
            backend.PUT('/api/v1/printers/{printer_id}/lan', {
              params: { path: { printer_id: id } },
              body: { host, access_code },
            }),
            'link the printer over the LAN',
          ),
        ),
      ),
  }),

  defineTool({
    name: 'unlink_printer_lan',
    description: "Removes a printer's LAN link and its stored access code.",
    input: z.object({ printer }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/lan'],
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

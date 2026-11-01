import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// ScadBuddy's own spool inventory (#4643, tier B of #3818): the records of the native
// spool provider, `native_spools`, which the print dialog's tray overlay shows as provider
// "native" beside Bambuddy's. Write tier: nothing leaves ScadBuddy.
// Routes: backend/scadbuddy/api/native_spools.py.

const SOURCE = "ScadBuddy's own spool inventory (the native spool provider)"

const spoolId = z.string().uuid().describe("A spool's id on ScadBuddy's shelf (native_spool_list's `id`)")

const fields = {
  material: z.string().max(128).nullable().optional().describe('PLA, PETG, …'),
  colour: z
    .string()
    .regex(/^#[0-9A-Fa-f]{6}$/)
    .nullable()
    .optional()
    .describe('#RRGGBB'),
  brand: z.string().max(128).nullable().optional(),
  name: z.string().max(128).nullable().optional().describe('What the user calls it, e.g. the colour name'),
  label_weight_g: z.number().int().positive().max(100_000).nullable().optional().describe('Filament weight when full'),
  remaining_g: z.number().min(0).max(100_000).nullable().optional(),
  tray_uuid: z
    .string()
    .max(64)
    .nullable()
    .optional()
    .describe("The RFID tray_uuid a printer's report shows for the spool; all zeros is none"),
  tag_uid: z.string().max(64).nullable().optional().describe("The RFID tag_uid; all zeros is none"),
  preset: z.string().max(128).nullable().optional().describe('The filament preset id it prints with, e.g. GFA00'),
  note: z.string().max(2000).nullable().optional(),
}

export const nativeSpoolTools: Tool[] = [
  defineTool({
    name: 'native_spool_list',
    label: "List ScadBuddy's own spools",
    description:
      "The spools ScadBuddy keeps itself (not Bambuddy's inventory), oldest first, with material, colour, " +
      'remaining and label grams, RFID keys and preset. Archived (used-up) ones only with `include_archived`.',
    input: z.object({ include_archived: z.boolean().default(false) }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/native-spools'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/native-spools', { params: { query } }), "list ScadBuddy's spools")),
  }),

  defineTool({
    name: 'native_spool_get',
    label: "Read one of ScadBuddy's own spools",
    description: "One spool on ScadBuddy's own shelf.",
    input: z.object({ spool_id: spoolId }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/native-spools/{spool_id}'],
    handler: async ({ spool_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/native-spools/{spool_id}', { params: { path: { spool_id } } }),
          `get spool ${spool_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'native_spool_upsert',
    label: "Add or edit one of ScadBuddy's own spools",
    description:
      "Add a spool to ScadBuddy's own shelf (no `spool_id`), or edit one: only the fields given change, and " +
      '`null` clears one. `archived: true` takes it off the shelf (used up), `false` puts it back. Only what ' +
      'the user says of the spool. 409 when another unarchived spool carries the RFID tag. Writes ScadBuddy only.',
    input: z.object({ spool_id: spoolId.optional(), ...fields, archived: z.boolean().optional() }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/native-spools', 'PATCH /api/v1/native-spools/{spool_id}'],
    summarize: ({ spool_id, material, name }) =>
      spool_id ? `Edit spool ${spool_id}` : `Add a spool ${[material, name].filter(Boolean).join(' ')}`.trim(),
    handler: async ({ spool_id, archived, ...given }, { backend }) => {
      const body = Object.fromEntries(Object.entries(given).filter(([, v]) => v !== undefined))
      if (spool_id === undefined) {
        if (archived !== undefined) throw new ToolError('archived needs a spool_id')
        return json(await ok(backend.POST('/api/v1/native-spools', { body: body as never }), 'add a spool'))
      }
      return json(
        await ok(
          backend.PATCH('/api/v1/native-spools/{spool_id}', {
            params: { path: { spool_id } },
            body: (archived === undefined ? body : { ...body, archived }) as never,
          }),
          `edit spool ${spool_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'native_spool_delete',
    label: "Forget one of ScadBuddy's own spools",
    description:
      "Delete a spool from ScadBuddy's own shelf for good. To keep its record (a used-up spool), archive it " +
      'with native_spool_upsert instead. Writes ScadBuddy only.',
    input: z.object({ spool_id: spoolId }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/native-spools/{spool_id}'],
    summarize: ({ spool_id }) => `Forget spool ${spool_id}`,
    handler: async ({ spool_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/native-spools/{spool_id}', { params: { path: { spool_id } } }),
        `forget spool ${spool_id}`,
      )
      return json({ deleted: spool_id })
    },
  }),
]

import { z } from 'zod'
import { ok } from './call.js'
import { LAN_SOURCE, printerCommand } from './printerCommand.js'
import { defineTool, json, type Tool } from './registry.js'

// A printer's AMS slots over ScadBuddy's LAN link (#7153, #7276 Task 8; routes
// backend/scadbuddy/api/printer_slots.py): the read, configure, reset, refresh, load, unload,
// a slot's saved preset and an AMS unit's label. A command (configure, reset, refresh, load,
// unload) reaches the printer, which may move, so it is outward (R-7153-10); it is sent with
// the call's Idempotency-Key and its 202 followed (`printerCommand`, P41, P51). The backend
// reads the printer's state first and refuses with a 409 and a `code` while it prints
// (R-7153-3). The saved presets and the unit labels are ScadBuddy's own, so they are write
// (R-7276-13).

const printerId = z.string().uuid().describe("A registry printer's id (the `id` get_printers_now lists)")
const amsId = z
  .number()
  .int()
  .min(0)
  .max(255)
  .describe('The unit: an AMS 0-15, an AMS-HT 128-135, or 255 for the external holders')
const trayId = z.number().int().min(0).max(3).describe('The slot in the unit, from 0; an AMS-HT has only 0')
const slotInput = z.object({ printer_id: printerId, ams_id: amsId, tray_id: trayId })

const SOURCE = LAN_SOURCE
const STATE =
  " The backend reads the printer's latest LAN report first and refuses with 409 and a `code`: `not_lan_linked` " +
  '(the printer is not linked over the LAN), `printer_offline` (not heard from lately), `printer_busy` (a print ' +
  'is active), `ams_busy`, `no_slot`; `printer_refused` when the printer refuses the command.'
const ASK =
  " Sent once under this call's key: a lost answer is sent again under the same key and reaches the printer " +
  'once. A slow printer answers `status: "running"` with a `command_id`: read it with printer_command_get, ' +
  'never send it again. Only what the user asks. Moves the printer; approved first.'

const PRESET_ID = /^[A-Za-z0-9_.+-]{0,64}$/

type Action = 'reset' | 'refresh' | 'load' | 'unload'

function slotCommand(action: Action, label: string, description: string): Tool {
  return defineTool({
    name: `printer_slot_${action}`,
    label,
    description: description + STATE + ASK,
    input: slotInput,
    risk: 'outward',
    source: SOURCE,
    routes: [`POST /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/${action}`],
    summarize: ({ printer_id, ams_id, tray_id }) => `${label}: slot ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id }, ctx) => {
      const { backend, signal } = ctx
      const what = `${action} slot ${ams_id}/${tray_id}`
      return json(
        await printerCommand(ctx, printer_id, what, (headers) => {
          const params = { params: { path: { printer_id, ams_id, tray_id } }, headers, signal }
          switch (action) {
            case 'reset':
              return backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/reset', params)
            case 'refresh':
              return backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/refresh', params)
            case 'load':
              return backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/load', params)
            case 'unload':
              return backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/unload', params)
          }
        }),
      )
    },
  })
}

export const printerSlotTools: Tool[] = [
  defineTool({
    name: 'printer_ams_get',
    label: "Read a printer's AMS slots",
    description:
      "A LAN-linked printer's AMS units and external holders from its latest LAN report: each slot's material, " +
      "colour, preset, temperatures, `filled`, `loaded` (the printer feeds from it), ScadBuddy's `saved_preset`, " +
      "each unit's `label` and ScadBuddy place, and `connected`, `printing` and `ams_busy`, which say whether a " +
      'slot command would be refused now. Sends no command. 409 `not_lan_linked` for a printer not linked over ' +
      'the LAN.',
    input: z.object({ printer_id: printerId }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers/{printer_id}/ams'],
    summarize: ({ printer_id }) => `Read the AMS of printer ${printer_id}`,
    handler: async ({ printer_id }, { backend }) =>
      json(await ok(backend.GET('/api/v1/printers/{printer_id}/ams', { params: { path: { printer_id } } }), 'read the AMS')),
  }),

  defineTool({
    name: 'printer_slot_configure',
    label: 'Configure an AMS slot',
    description:
      "Set a slot's material, colour, preset and nozzle temperatures (and K) on the printer. Send " +
      "`spool_id` (any ScadBuddy spool: its values, its type's preset for the slot's nozzle and its K value), the " +
      "values, or both: sent values replace the spool's. `colour` is `#RRGGBB` or `#RRGGBBAA`; `preset_id` the " +
      "filament id the slot reports (GFA00). Answers what was sent. 422 names what is missing; 409 `no_nozzle` when " +
      'the slot\'s nozzle is not known (send `nozzle_diameter`). It links no spool to the tray.' +
      STATE +
      ASK,
    input: slotInput.extend({
      spool_id: z.string().uuid().optional().describe("A ScadBuddy spool's id"),
      material: z.string().min(1).max(32).optional(),
      colour: z
        .string()
        .regex(/^#?[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$/)
        .optional(),
      preset_id: z.string().regex(PRESET_ID).optional(),
      preset_name: z.string().max(64).optional(),
      nozzle_temp_min: z.number().int().min(0).max(500).optional(),
      nozzle_temp_max: z.number().int().min(0).max(500).optional(),
      nozzle_diameter: z.enum(['0.2', '0.4', '0.6', '0.8']).optional(),
      cali_idx: z.number().int().min(-1).max(10_000).optional(),
      setting_id: z.string().regex(PRESET_ID).optional(),
      k_value: z.number().min(0).lt(10).optional(),
    }),
    risk: 'outward',
    source: SOURCE,
    routes: ['POST /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/configure'],
    summarize: ({ printer_id, ams_id, tray_id }) => `Configure slot ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id, ...body }, ctx) =>
      json(
        await printerCommand(ctx, printer_id, `configure slot ${ams_id}/${tray_id}`, (headers) =>
          ctx.backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/configure', {
            params: { path: { printer_id, ams_id, tray_id } },
            body,
            headers,
            signal: ctx.signal,
          }),
        ),
      ),
  }),

  slotCommand('reset', 'Reset an AMS slot', "Clear a slot's filament setting on the printer and ScadBuddy's saved slot preset."),
  slotCommand(
    'refresh',
    "Read an AMS slot's tag again",
    "Make the AMS read a slot's RFID tag again. Not for an external holder (422); 409 `filament_loaded` while " +
      'filament is loaded.',
  ),
  slotCommand(
    'load',
    'Load filament from an AMS slot',
    "Feed a slot's filament to the hotend. AMS 0-3, an AMS-HT or an external holder. Also 409 `tray_empty`, " +
      '`already_loaded`, `extruder_unknown`.',
  ),
  slotCommand(
    'unload',
    'Unload filament fed from an AMS slot',
    'Pull back the filament the hotend takes from a slot. Also 409 `not_loaded` when no hotend is loaded from it.',
  ),

  defineTool({
    name: 'printer_slot_preset_set',
    label: "Save an AMS slot's preset",
    description:
      "Save the slicer preset ScadBuddy shows for a slot (kept by ScadBuddy, not on the printer). Send the " +
      '`tray_info_idx` the slot was configured with; `preset_source` is accepted and ignored. 404 for an unknown ' +
      'printer. Only what the user asks. Writes ScadBuddy only.',
    input: slotInput.extend({
      preset_id: z.string().min(1).max(64).regex(PRESET_ID),
      preset_name: z.string().min(1).max(128),
      preset_source: z.enum(['cloud', 'local', 'builtin', 'orca_cloud']).optional(),
      tray_info_idx: z.string().max(32).regex(PRESET_ID).optional(),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/preset'],
    summarize: ({ printer_id, ams_id, tray_id, preset_name }) =>
      `Save ${preset_name} as the preset of slot ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/preset', {
            params: { path: { printer_id, ams_id, tray_id } },
            body,
          }),
          `save the preset of slot ${ams_id}/${tray_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'printer_slot_preset_clear',
    label: "Clear an AMS slot's preset",
    description:
      "Clear the slicer preset ScadBuddy shows for a slot; one that is not there is cleared all the same. 404 for " +
      'an unknown printer. Only what the user asks. Writes ScadBuddy only.',
    input: slotInput,
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/preset'],
    summarize: ({ printer_id, ams_id, tray_id }) => `Clear the preset of slot ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/preset', {
          params: { path: { printer_id, ams_id, tray_id } },
        }),
        `clear the preset of slot ${ams_id}/${tray_id}`,
      )
      return json({ cleared: { printer_id, ams_id, tray_id } })
    },
  }),

  defineTool({
    name: 'printer_ams_label_set',
    label: 'Label an AMS unit',
    description:
      "Label an AMS unit (0-15, or an AMS-HT 128-135): the label is the name of ScadBuddy's place of the unit, " +
      'made if missing. `place_problem` `no_ams_kind` says the place was not named (no location kind is marked ' +
      'AMS), `no_printer` that the printer was deleted meanwhile. 1-100 printable characters. Nothing is sent to ' +
      'the printer. Only what the user asks. Writes ScadBuddy only.',
    input: z.object({
      printer_id: printerId,
      ams_id: amsId,
      label: z.string().min(1).max(100),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/printers/{printer_id}/ams/{ams_id}/label'],
    summarize: ({ printer_id, ams_id, label }) => `Label AMS ${ams_id} of printer ${printer_id} "${label}"`,
    handler: async ({ printer_id, ams_id, label }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/printers/{printer_id}/ams/{ams_id}/label', {
            params: { path: { printer_id, ams_id } },
            body: { label },
          }),
          `label AMS ${ams_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'printer_ams_label_clear',
    label: "Clear an AMS unit's label",
    description:
      "Clear an AMS unit's label: ScadBuddy's place of the unit is named `<printer> <unit>` again. Nothing is " +
      'sent to the printer. Only what the user asks. Writes ScadBuddy only.',
    input: z.object({ printer_id: printerId, ams_id: amsId }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/ams/{ams_id}/label'],
    summarize: ({ printer_id, ams_id }) => `Clear the label of AMS ${ams_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/printers/{printer_id}/ams/{ams_id}/label', { params: { path: { printer_id, ams_id } } }),
          `clear the label of AMS ${ams_id}`,
        ),
      ),
  }),
]

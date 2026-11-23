import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'

// A printer's AMS slots through Bambuddy (#7153, plan 2026-11-23-ams-slot-control; routes
// backend/scadbuddy/api/printer_slots.py): the read, configure, reset, refresh, load, unload,
// a slot's saved preset and an AMS unit's label. Every write is outward: it reaches the
// printer or Bambuddy, and the printer may move (R-7153-10). The backend reads the printer's
// state first and refuses with a 409 and a `code` while it prints (R-7153-3).

const printerId = z.string().uuid().describe("A registry printer's id (the `id` get_printers_now lists)")
const amsId = z
  .number()
  .int()
  .min(0)
  .max(255)
  .describe('The unit: an AMS 0-15, an AMS-HT 128-135, or 255 for the external holders')
const trayId = z.number().int().min(0).max(3).describe('The slot in the unit, from 0; an AMS-HT has only 0')
const slotInput = z.object({ printer_id: printerId, ams_id: amsId, tray_id: trayId })

const SOURCE = "Bambuddy's control of the printer's AMS"
const STATE =
  ' The backend reads the printer first and refuses with 409 and a `code`: `printer_offline`, `printer_busy` (a ' +
  'print is active), `ams_busy`, `no_slot`.'
const ASK = ' Only what the user asks. Moves the printer; approved first.'

const PRESET_ID = /^[A-Za-z0-9_.+-]{0,64}$/

type Action = 'reset' | 'refresh' | 'load' | 'unload'

function command(action: Action, label: string, description: string): Tool {
  return defineTool({
    name: `printer_slot_${action}`,
    label,
    description: description + STATE + ASK,
    input: slotInput,
    risk: 'outward',
    source: SOURCE,
    routes: [`POST /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/${action}`],
    summarize: ({ printer_id, ams_id, tray_id }) => `${label}: slot ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id }, { backend }) => {
      const params = { params: { path: { printer_id, ams_id, tray_id } } }
      const what = `${action} slot ${ams_id}/${tray_id}`
      switch (action) {
        case 'reset':
          return json(await ok(backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/reset', params), what))
        case 'refresh':
          return json(await ok(backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/refresh', params), what))
        case 'load':
          return json(await ok(backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/load', params), what))
        case 'unload':
          return json(await ok(backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/unload', params), what))
      }
    },
  })
}

export const printerSlotTools: Tool[] = [
  defineTool({
    name: 'printer_ams_get',
    label: "Read a printer's AMS slots",
    description:
      "A Bambuddy-linked printer's AMS units and external holder from Bambuddy's live status: each slot's material, " +
      "colour, preset, temperatures, `filled`, `loaded` (the printer feeds from it), Bambuddy's `saved_preset`, " +
      "each unit's Bambuddy `label` and ScadBuddy place, and `printing` and `ams_busy`, which say whether a slot " +
      'write would be refused now. 409 for a printer not linked to Bambuddy.',
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
      "Set a slot's material, colour, preset and nozzle temperatures (and K) on the printer through Bambuddy. Send " +
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
    handler: async ({ printer_id, ams_id, tray_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/configure', {
            params: { path: { printer_id, ams_id, tray_id } },
            body,
          }),
          `configure slot ${ams_id}/${tray_id}`,
        ),
      ),
  }),

  command('reset', 'Reset an AMS slot', "Clear a slot's filament setting on the printer and Bambuddy's saved slot preset."),
  command(
    'refresh',
    "Read an AMS slot's tag again",
    "Make the AMS read a slot's RFID tag again. Not for an external holder (422); 409 `filament_loaded` while " +
      'filament is loaded.',
  ),
  command(
    'load',
    'Load filament from an AMS slot',
    "Feed a slot's filament to the hotend. AMS 0-3, an AMS-HT or an external holder. Also 409 `tray_empty`, " +
      '`already_loaded`, `extruder_unknown`.',
  ),
  command(
    'unload',
    'Unload filament fed from an AMS slot',
    "Pull back the filament the hotend takes from a slot; Bambuddy's 409 when no hotend is loaded from it.",
  ),

  defineTool({
    name: 'printer_slot_preset_set',
    label: "Save an AMS slot's preset",
    description:
      "Save the slicer preset Bambuddy shows for a slot (kept in Bambuddy, not on the printer). Send the " +
      "`tray_info_idx` the slot was configured with. Bambuddy refuses it to every API key while its auth is on (the " +
      'scope problem naming `Printers (admin only)`). Only what the user asks. Writes Bambuddy; approved first.',
    input: slotInput.extend({
      preset_id: z.string().min(1).max(64).regex(PRESET_ID),
      preset_name: z.string().min(1).max(128),
      preset_source: z.enum(['cloud', 'local', 'builtin', 'orca_cloud']).optional(),
      tray_info_idx: z.string().max(32).regex(PRESET_ID).optional(),
    }),
    risk: 'outward',
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
      "Clear the slicer preset Bambuddy shows for a slot; one that is not there is cleared all the same. Bambuddy " +
      'refuses it to every API key while its auth is on. Only what the user asks. Writes Bambuddy; approved first.',
    input: slotInput,
    risk: 'outward',
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
      "Write Bambuddy's label of an AMS unit (0-15, or an AMS-HT 128-135), then name ScadBuddy's place of the unit " +
      "the same. A refused Bambuddy write changes nothing; `place_problem` `no_ams_kind` says the place was not " +
      'named (no location kind is marked AMS). 1-100 printable characters. Bambuddy refuses it to every API key while its auth ' +
      'is on. Only what the user asks. Writes Bambuddy and ScadBuddy; approved first.',
    input: z.object({
      printer_id: printerId,
      ams_id: amsId,
      label: z.string().min(1).max(100),
    }),
    risk: 'outward',
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
      "Delete Bambuddy's label of an AMS unit, then name ScadBuddy's place of the unit `<printer> <unit>` again. " +
      'Only what the user asks. Writes Bambuddy and ScadBuddy; approved first.',
    input: z.object({ printer_id: printerId, ams_id: amsId }),
    risk: 'outward',
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

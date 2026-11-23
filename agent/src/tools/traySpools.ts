import { z } from 'zod'
import { ok } from './call.js'
import { linkingTools } from './filamentLinks.js'
import { defineTool, idempotencyKey, json, type Tool } from './registry.js'

// Which SB spool is in a registry printer's tray, on the native print path (#3818 tier A,
// #6262 Task 4): the manual link a person makes for an untagged tray. The spool moves to
// the tray's place (backend/scadbuddy/filament/inventory.py) and holds the tray until its
// material or colour changes. Write tier: nothing leaves ScadBuddy (not Bambuddy).
// Routes: backend/scadbuddy/api/tray_spools.py. Not the Bambuddy path's question
// (`POST /print/printers/{id}/trays/.../spool`), which writes Bambuddy's assignment.

const printerId = z.string().uuid().describe("A registry printer's id (the `id` get_printers_now lists)")
const amsId = z
  .number()
  .int()
  .min(0)
  .max(255)
  .describe('The unit: an AMS 0-15, an AMS-HT 128-135, or 255 for the external holders')
const trayId = z.number().int().min(0).max(3).describe('The slot in the unit, from 0; an AMS-HT has only 0')

const SOURCE = "ScadBuddy's own spool inventory (SB spools and their places)"

const tools: Tool[] = [
  defineTool({
    name: 'link_tray_spool',
    label: 'Link a spool to a tray',
    description:
      'Say which ScadBuddy spool is in an untagged tray of a LAN-linked printer, for a print on the native path: ' +
      '`spool_id` is the spool\'s uuid (the `id` filament_spool_list lists). Only what the user says is in the ' +
      "tray. The spool moves to the tray's place and the tray's previous spool to no place, each with a moved " +
      "ledger event; the spool holds the tray until the tray's material or colour (alpha included) changes. An " +
      "RFID tray's spool is the one its tag names; a tagged tray's tag is bound to the spool (one on no spool or " +
      "on an archived spool is taken), and a spool that held another tag is rebound to the tray's. 409 when the " +
      'printer has not reported that tray filled, its tag is on another spool, or its two keys name two different ' +
      'tags; 404 for an unknown printer or spool. Writes ScadBuddy; with tag sync set to push (`sb_to_bb` or `two_way`), the tag change is then pushed to Bambuddy.',
    input: z.object({
      printer_id: printerId,
      ams_id: amsId,
      tray_id: trayId,
      spool_id: z.string().uuid().describe("The ScadBuddy spool's id"),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool'],
    summarize: ({ printer_id, ams_id, tray_id }) => `Link the spool in tray ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id, ...body }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool', {
            params: { path: { printer_id, ams_id, tray_id } },
            body,
          }),
          `link the spool in tray ${ams_id}/${tray_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'create_tray_spool',
    label: 'Make a spool from a tray',
    description:
      'Create from slot (#7150): make a new ScadBuddy spool from what a LAN-linked printer last reported in a ' +
      "tray (its material, matched by name or alias and added to the catalog when none has it, its RGBA colour, " +
      "and a tagged tray's tag) and place it in the tray; the tray's previous spool moves to no place. The spool " +
      'has no type, label weight or grams yet (it needs a type: filament_type_proposals). Only when the user asks ' +
      'for a new spool record for a tray. A resend of the same call makes no second spool. 409 when the printer ' +
      "has not reported that tray filled, or the tray's tag is already on a spool (that spool is the tray's: use " +
      'it); 404 for an unknown printer. Writes ScadBuddy only; nothing reaches Bambuddy.',
    input: z.object({ printer_id: printerId, ams_id: amsId, tray_id: trayId }),
    risk: 'write',
    source: SOURCE,
    routes: ['POST /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/new-spool'],
    summarize: ({ printer_id, ams_id, tray_id }) => `Make a spool from tray ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id }, ctx) =>
      json(
        await ok(
          ctx.backend.POST('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/new-spool', {
            params: { path: { printer_id, ams_id, tray_id } },
            // One key per call: a resend after a lost answer answers the first spool.
            headers: { 'Idempotency-Key': idempotencyKey(ctx) },
          }),
          `make a spool from tray ${ams_id}/${tray_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'unlink_tray_spool',
    label: 'Unlink a spool from a tray',
    description:
      'Forget which spool is in a tray of a LAN-linked printer (the link link_tray_spool made): the spool moves ' +
      'to no place, with a moved ledger event, and itself stays. Writes ScadBuddy only.',
    input: z.object({ printer_id: printerId, ams_id: amsId, tray_id: trayId }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool'],
    summarize: ({ printer_id, ams_id, tray_id }) =>
      `Forget the spool in tray ${ams_id}/${tray_id} of printer ${printer_id}`,
    handler: async ({ printer_id, ams_id, tray_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool', {
          params: { path: { printer_id, ams_id, tray_id } },
        }),
        `forget the spool in tray ${ams_id}/${tray_id}`,
      )
      return json({ unlinked: { printer_id, ams_id, tray_id } })
    },
  }),
]

/** #6869: the answers carry the spools' `ui_path`s (`filamentLinks.ts`). */
export const traySpoolTools: Tool[] = linkingTools(tools, {})

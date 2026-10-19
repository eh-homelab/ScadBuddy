import { z } from 'zod'
import { ok } from './call.js'
import { defineTool, json, type Tool } from './registry.js'

// Which spool is in a registry printer's tray, on the native print path (#3818 tier A,
// plan 2026-10-18 D1, Task 6c): the manual link a person makes for an untagged tray,
// kept in ScadBuddy's `tray_spool_links` and dropped when the tray's material or colour
// changes. Write tier: nothing leaves ScadBuddy (not Bambuddy, not any spool provider).
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

const SOURCE = "ScadBuddy's spool overlay: provider records copied from Bambuddy's inventory"

export const traySpoolTools: Tool[] = [
  defineTool({
    name: 'link_tray_spool',
    description:
      'Say which spool is in an untagged tray of a LAN-linked printer, for a print on the native path: a ' +
      "physical spool (`physical_spool_id`) or one spool provider's record of it (`provider` and `record_id`, " +
      'e.g. Bambuddy\'s spool 16 is provider "bambuddy", record_id "16"). Name exactly one. Only what the user ' +
      'says is in the tray: the link is kept until the tray\'s material or colour changes. A record moves to ' +
      "the tray's spool, and a spool from any other tray. An RFID tray's spool is the one its tag names. 409 " +
      'when the printer has not reported that tray filled, or its tag names another spool; 404 for an unknown ' +
      'printer, spool or record. Writes ScadBuddy only.',
    input: z.object({
      printer_id: printerId,
      ams_id: amsId,
      tray_id: trayId,
      physical_spool_id: z.string().uuid().optional(),
      provider: z.string().min(1).max(64).optional(),
      record_id: z.string().min(1).max(128).optional(),
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
    name: 'unlink_tray_spool',
    description:
      'Forget which spool is in a tray of a LAN-linked printer (the link link_tray_spool made). The spool and ' +
      "its providers' records stay. Writes ScadBuddy only.",
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

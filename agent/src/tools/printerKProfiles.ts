import { z } from 'zod'
import { ok } from './call.js'
import { LAN_SOURCE, printerCommand, printerCommandGet } from './printerCommand.js'
import { defineTool, json, type Tool } from './registry.js'

// A printer's K-profiles (pressure advance) over ScadBuddy's LAN link (#7162, #7276 Task 7;
// routes backend/scadbuddy/api/printer_kprofiles.py, bodies printers/kprofiles.py). The list
// and the writes are printer commands (`printerCommand`): a write is keyed by the call's
// Idempotency-Key, so a lost answer sent again reaches the printer once (P38), and a 202 is
// followed, else handed back for printer_command_get (P51). The notes are ScadBuddy's own.

export const SOURCE = LAN_SOURCE

const printerId = z.string().uuid().describe("A registry printer's id (the `id` get_printers_now lists)")
const nozzleDiameter = z.enum(['0.2', '0.4', '0.6', '0.8'])

// The backend's bounds (printers/kprofiles.py). A string's `max` counts code points, as pydantic's
// does: zod 4 measures a string over the bound by `util.codePointLength` (zod/v4/core/checks.js).
const NOZZLE_ID = /^([A-Z]{2}[0-9]{2}(-[0-9]\.[0-9])?)?$/
const FILAMENT_ID = /^[A-Za-z0-9_.+-]{1,64}$/
const SETTING_ID = /^[A-Za-z0-9_.+-]{0,64}$/
const N_COEF = /^[0-9]{1,2}(\.[0-9]{1,6})?$/
/** A decimal below 10 with at most 6 places (`K_MAX`, `K_PLACES`). */
const K_VALUE = /^[0-9](\.[0-9]{1,6})?$/
/** Printable: no control, format, separator other than the space, or unassigned character. */
const PRINTABLE = /^(?:[^\p{C}\p{Z}]| )+$/u
const BATCH_MAX = 16
const NOTE_MAX = 2000
const NOTE_KEY_MAX = 256

const STATE =
  ' ScadBuddy reaches the printer over its LAN link: 409 `not_lan_linked` for a printer not linked over the ' +
  'LAN, `printer_offline` when it has not been heard from lately, `printer_refused` when it refuses the ' +
  'command, `printer_cert_changed` when it presents another certificate (re-link the printer in Settings > ' +
  'Printers); 502 `printer_unreachable`; 503 `lan_unavailable` when ScadBuddy cannot use its LAN link now; 404 ' +
  'for an unknown printer. A slow printer answers `status: "running"` ' +
  'with a `command_id`: read it with printer_command_get.'
const ONCE =
  ' Sent once under this call\'s key: a lost answer is sent again under the same key and reaches the printer ' +
  'once; it is never sent again under a new key. `confirmed` false: the printer did not acknowledge it in ' +
  'time, and it may have been applied. Read the list (printer_kprofiles_get) before a new try. Only what the ' +
  'user asks. Writes the printer; approved first.'

const profile = z.object({
  slot_id: z.number().int().min(0).max(10_000).optional().describe('0 (the default) adds a profile; above 0 edits the profile in that slot'),
  extruder_id: z.number().int().min(0).max(1).optional(),
  nozzle_id: z.string().regex(NOZZLE_ID).describe('`HS00-0.4` (standard flow) or `HH00-0.4` (high flow), as the list answers it'),
  nozzle_diameter: nozzleDiameter,
  filament_id: z.string().regex(FILAMENT_ID).describe('The filament id, such as `GFA00`'),
  name: z.string().min(1).max(64).regex(PRINTABLE),
  k_value: z.string().regex(K_VALUE).describe('A decimal string below 10 with at most 6 places, such as `0.020`'),
  setting_id: z.string().regex(SETTING_ID).optional(),
  n_coef: z.string().regex(N_COEF).optional().describe('Accepted and ignored: the printer gets `0.000000`'),
})

const noteKey = z
  .string()
  .min(1)
  .max(NOTE_KEY_MAX)
  .regex(PRINTABLE)
  .refine((key) => !key.includes('/'), 'cannot hold a /')
  // A path segment of `.` or `..` is normalised away before the DELETE reaches its route, so
  // a note under one could never be deleted; the backend refuses both (printers/kprofiles.py).
  .refine((key) => key !== '.' && key !== '..', 'cannot be . or ..')
  .describe("The note's key: the profile's `setting_id`, else `slot_<slot>_<filament>_<extruder>`, else `name_<name>_<filament>`")

export const printerKProfileTools: Tool[] = [
  defineTool({
    name: 'printer_kprofiles_get',
    label: "Read a printer's K-profiles",
    description:
      "The K-profiles (pressure advance) the printer holds for one nozzle diameter (default 0.4), read from the " +
      'printer. 502 `printer_no_answer` when it does not answer.' +
      STATE,
    input: z.object({ printer_id: printerId, nozzle_diameter: nozzleDiameter.default('0.4') }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers/{printer_id}/kprofiles'],
    summarize: ({ printer_id, nozzle_diameter }) => `Read the ${nozzle_diameter} mm K-profiles of printer ${printer_id}`,
    handler: async ({ printer_id, nozzle_diameter }, ctx) =>
      json(
        await printerCommand(ctx, printer_id, 'read the K-profiles', () =>
          ctx.backend.GET('/api/v1/printers/{printer_id}/kprofiles', {
            params: { path: { printer_id }, query: { nozzle_diameter } },
            signal: ctx.signal,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_notes_get',
    label: "Read the notes on a printer's K-profiles",
    description:
      "ScadBuddy's notes on a printer's K-profiles, each by its key (the profile's `setting_id`, else " +
      '`slot_<slot>_<filament>_<extruder>`, else `name_<name>_<filament>`). Not read from the printer. 404 for an ' +
      'unknown printer.',
    input: z.object({ printer_id: printerId }),
    risk: 'read',
    source: SOURCE,
    routes: ['GET /api/v1/printers/{printer_id}/kprofiles/notes'],
    summarize: ({ printer_id }) => `Read the K-profile notes of printer ${printer_id}`,
    handler: async ({ printer_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/printers/{printer_id}/kprofiles/notes', { params: { path: { printer_id } } }),
          'read the K-profile notes',
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_set',
    label: "Add or edit a printer's K-profile",
    description: 'Add a K-profile to the printer (`slot_id` 0), or edit the profile in a slot.' + ONCE + STATE,
    input: profile.extend({ printer_id: printerId }),
    risk: 'outward',
    keyed: true,
    source: SOURCE,
    routes: ['POST /api/v1/printers/{printer_id}/kprofiles'],
    summarize: ({ printer_id, name, k_value, nozzle_diameter, slot_id }) =>
      `${slot_id ? `Edit K-profile ${slot_id}` : 'Add a K-profile'} "${name}" K ${k_value} (${nozzle_diameter} mm) on printer ${printer_id}`,
    handler: async ({ printer_id, ...body }, ctx) =>
      json(
        await printerCommand(ctx, printer_id, 'set the K-profile', (headers) =>
          ctx.backend.POST('/api/v1/printers/{printer_id}/kprofiles', { params: { path: { printer_id } }, body, headers, signal: ctx.signal }),
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_batch_set',
    label: 'Add several K-profiles to a printer',
    description:
      `1 to ${BATCH_MAX} K-profiles of one nozzle diameter in one printer command (a new profile on both extruders ` +
      'of a dual-nozzle printer is two rows).' +
      ONCE +
      STATE,
    input: z.object({
      printer_id: printerId,
      profiles: z
        .array(profile)
        .min(1)
        .max(BATCH_MAX)
        .refine((rows) => new Set(rows.map((row) => row.nozzle_diameter)).size === 1, 'every profile has the same nozzle_diameter'),
    }),
    risk: 'outward',
    keyed: true,
    source: SOURCE,
    routes: ['POST /api/v1/printers/{printer_id}/kprofiles/batch'],
    summarize: ({ printer_id, profiles }) =>
      `Add ${profiles.length} K-profiles (${profiles.map((p) => `"${p.name}" K ${p.k_value}`).join(', ')}) on printer ${printer_id}`,
    handler: async ({ printer_id, profiles }, ctx) =>
      json(
        await printerCommand(ctx, printer_id, 'set the K-profiles', (headers) =>
          ctx.backend.POST('/api/v1/printers/{printer_id}/kprofiles/batch', {
            params: { path: { printer_id } },
            body: { profiles },
            headers,
            signal: ctx.signal,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_delete',
    label: "Delete a printer's K-profile",
    description: 'Delete the K-profile in a slot, named as the list answers it.' + ONCE + STATE,
    input: z.object({
      printer_id: printerId,
      slot_id: z.number().int().min(0).max(10_000),
      extruder_id: z.number().int().min(0).max(1).optional(),
      nozzle_id: z.string().regex(NOZZLE_ID),
      nozzle_diameter: nozzleDiameter,
      filament_id: z.string().regex(FILAMENT_ID),
      setting_id: z.string().regex(SETTING_ID).optional(),
    }),
    risk: 'outward',
    keyed: true,
    source: SOURCE,
    routes: ['POST /api/v1/printers/{printer_id}/kprofiles/delete'],
    summarize: ({ printer_id, slot_id, filament_id, nozzle_diameter }) =>
      `Delete K-profile ${slot_id} (${filament_id}, ${nozzle_diameter} mm) from printer ${printer_id}`,
    handler: async ({ printer_id, ...body }, ctx) =>
      json(
        await printerCommand(ctx, printer_id, 'delete the K-profile', (headers) =>
          ctx.backend.POST('/api/v1/printers/{printer_id}/kprofiles/delete', {
            params: { path: { printer_id } },
            body,
            headers,
            signal: ctx.signal,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_note_set',
    label: "Set the note on a printer's K-profile",
    description:
      `ScadBuddy's note on one of a printer's K-profiles, at most ${NOTE_MAX} characters; a blank note deletes it. ` +
      'Writes ScadBuddy only, not the printer. 404 for an unknown printer.',
    input: z.object({
      printer_id: printerId,
      setting_id: noteKey,
      note: z
        .string()
        .max(NOTE_MAX)
        .refine((note) => !note.includes('\0'), 'cannot hold a NUL character'),
    }),
    risk: 'write',
    source: SOURCE,
    routes: ['PUT /api/v1/printers/{printer_id}/kprofiles/notes'],
    summarize: ({ printer_id, setting_id }) => `Set the note on K-profile ${setting_id} of printer ${printer_id}`,
    handler: async ({ printer_id, setting_id, note }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/printers/{printer_id}/kprofiles/notes', { params: { path: { printer_id } }, body: { setting_id, note } }),
          'set the K-profile note',
        ),
      ),
  }),

  defineTool({
    name: 'printer_kprofile_note_delete',
    label: "Delete the note on a printer's K-profile",
    description:
      "Delete ScadBuddy's note on one of a printer's K-profiles; one that is not there is deleted all the same. " +
      'Writes ScadBuddy only, not the printer. 404 for an unknown printer.',
    input: z.object({ printer_id: printerId, setting_id: noteKey }),
    risk: 'write',
    source: SOURCE,
    routes: ['DELETE /api/v1/printers/{printer_id}/kprofiles/notes/{setting_id}'],
    summarize: ({ printer_id, setting_id }) => `Delete the note on K-profile ${setting_id} of printer ${printer_id}`,
    handler: async ({ printer_id, setting_id }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/printers/{printer_id}/kprofiles/notes/{setting_id}', {
            params: { path: { printer_id, setting_id } },
          }),
          'delete the K-profile note',
        ),
      ),
  }),

  printerCommandGet,
]

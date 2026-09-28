import { z } from 'zod'
import { ok } from './call.js'
import { printOptions } from './print.js'
import { defineTool, json, type Tool } from './registry.js'

// Settings (issue #251): read settings with secrets redacted, test the
// Bambuddy connection, and read targets and remembered print options.
// Writing the connection (URL, API key) and registering the sidebar link stay
// in the Settings UI; see the coverage allowlist in coverage.ts.
// Routes: backend/scadbuddy/api/settings.py.

const SECRET_KEY = /key|secret|token|password|credential/i
/** `SettingsView`'s per-field maps: keyed by field name, valued by a label. */
const LABEL_MAPS = new Set(['sources', 'applies'])
/** A source or applies label (`env`, `stored`, `live`, …), never secret-shaped. */
const LABEL = /^[a-z]{1,16}$/

/**
 * The backend's `SettingsView` already leaves the API key out ("What the
 * browser may see. The API key itself never appears here", openapi.json).
 * This is a second guard, so a secret field added later cannot reach an agent
 * by accident: any key that looks like one is replaced, except the booleans
 * that only say whether a secret is set. In `sources` and `applies` the keys
 * are field names (`bambuddy_api_key`) and the values labels, so there a value
 * is kept when it is a label and replaced otherwise.
 */
export function redact(value: unknown, labels = false): unknown {
  if (Array.isArray(value)) return value.map((v) => redact(v))
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => {
        if (labels && typeof v === 'string' && LABEL.test(v)) return [k, v]
        if (SECRET_KEY.test(k) && !k.startsWith('has_') && v !== null && typeof v !== 'boolean') return [k, '[redacted]']
        return [k, redact(v, LABEL_MAPS.has(k))]
      }),
    )
  }
  return value
}

export const settingsTools: Tool[] = [
  defineTool({
    name: 'get_settings',
    description:
      'ScadBuddy settings: the Bambuddy URL, whether an API key is set (never the key), default printer, ' +
      'plate and display unit.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/settings'],
    handler: async (_args, { backend }) => json(redact(await ok(backend.GET('/api/v1/settings'), 'get settings'))),
  }),

  defineTool({
    name: 'test_bambuddy_connection',
    description:
      'Check that Bambuddy is reachable and accepts the configured API key; lists the printers it can see ' +
      'and each Bambuddy scope ScadBuddy uses. Only Read Status is checked; the write scopes are ' +
      'reported as not checked, because telling them apart would take a write. Nothing is changed.',
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status'],
    routes: ['POST /api/v1/settings/test'],
    handler: async (_args, { backend }) => json(redact(await ok(backend.POST('/api/v1/settings/test'), 'test connection'))),
  }),

  defineTool({
    name: 'get_bambuddy_status',
    description:
      "Bambuddy's version, and whether it photographs a print when it finishes (capture_finish_photo). " +
      "Read-only: ScadBuddy never changes Bambuddy's settings; the user turns it on in Bambuddy.",
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/settings/bambuddy'],
    handler: async (_args, { backend }) =>
      json(redact(await ok(backend.GET('/api/v1/settings/bambuddy'), 'get Bambuddy status'))),
  }),

  defineTool({
    name: 'get_remembered_choices',
    description:
      'What the print dialog remembers (#322): per-model printer and spool choices, per-printer plates, ' +
      'and the print options at each scope. Forget one with remember_model_print_choices, ' +
      'remember_printer_bed_type or set_print_options.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/settings/remembered'],
    handler: async (_args, { backend }) =>
      json(redact(await ok(backend.GET('/api/v1/settings/remembered'), 'get remembered choices'))),
  }),

  defineTool({
    name: 'get_print_targets',
    description:
      "Bambuddy's printers (with model and live status), and library folders: the farm " +
      'context a print is planned against.',
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['GET /api/v1/settings/targets'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/settings/targets'), 'get print targets')),
  }),

  defineTool({
    name: 'get_print_options',
    description:
      'Remembered print options (timelapse, bed levelling, AMS, …) at each scope: global, per printer and ' +
      'per model, and the printer the per-printer scope keys on (the Settings printer).',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/settings/print-options'],
    handler: async (_args, { backend }) =>
      json(await ok(backend.GET('/api/v1/settings/print-options'), 'get print options')),
  }),

  // `outward`, deliberately, unlike the `remember_*` tools in print.ts (`write`).
  // Spec §8.1 puts "settings or credential writes" in `outward`, and this is one:
  // PUT /settings/print-options (backend/scadbuddy/api/settings.py
  // `put_print_options` → settings_store `save_print_options`) sets options that
  // bambuddy/send.py `resolve_print_options` then applies, global → per-printer
  // → per-model, to EVERY later print at that scope, by anyone, without being
  // chosen again: bed levelling, flow calibration, timelapse, preheat, manual
  // start, queue position.
  //
  // The `remember_*` tools write a different kind of state: which printer,
  // spools, nozzles, quality and plate type the print dialog pre-selects for
  // one model or printer (settings_store `set_model_choices`,
  // `set_printer_bed_type`; spool-first spec §7, #335). Those are starting
  // choices a person or agent picks again per print, not settings that
  // silently change every print, so they stay `write`.
  defineTool({
    name: 'set_print_options',
    description:
      'Remember print options for one scope (global, a printer, or a model). A settings write, so it needs a ' +
      'human approval.',
    input: z.object({
      scope: z.enum(['global', 'printer', 'model']),
      key: z.string().optional().describe('The printer id or model slug; omitted for global'),
      options: printOptions,
    }),
    risk: 'outward',
    routes: ['PUT /api/v1/settings/print-options'],
    summarize: ({ scope, key, options }) =>
      `Remember print options for ${scope}${key ? ` ${key}` : ''}: ${JSON.stringify(options)}`,
    handler: async ({ scope, key, options }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/settings/print-options', { body: { scope, key: key ?? null, options } }),
          'set print options',
        ),
      ),
  }),
]

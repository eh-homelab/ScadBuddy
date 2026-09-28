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

/**
 * The backend's `SettingsView` already leaves the API key out ("What the
 * browser may see. The API key itself never appears here", openapi.json).
 * This is a second guard, so a secret field added later cannot reach an agent
 * by accident: any key that looks like one is replaced, except the booleans
 * that only say whether a secret is set.
 */
export function redact(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redact)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [
        k,
        SECRET_KEY.test(k) && !k.startsWith('has_') && v !== null && typeof v !== 'boolean' ? '[redacted]' : redact(v),
      ]),
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
      'Check that Bambuddy is reachable and accepts the configured API key; lists the printers it can see.',
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Read Status'],
    routes: ['POST /api/v1/settings/test'],
    handler: async (_args, { backend }) => json(redact(await ok(backend.POST('/api/v1/settings/test'), 'test connection'))),
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
    input: z.object({ slug: z.string().optional() }),
    risk: 'read',
    routes: ['GET /api/v1/settings/print-options'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/settings/print-options', { params: { query: { slug } } }), 'get print options')),
  }),

  // `outward`, deliberately, unlike the `remember_*` tools in print.ts (`write`).
  // Spec §8.1 puts "settings or credential writes" in `outward`, and this is one:
  // PUT /settings/print-options (backend/scadbuddy/api/settings.py
  // `put_print_options` → settings_store `save_print_options`) sets options that
  // bambuddy/send.py `resolve_print_options` then applies, global → per-printer
  // → per-model, to EVERY later send and print at that scope, by anyone, without
  // being chosen again: bed levelling, flow calibration, timelapse, preheat,
  // manual start, queue position. On the send bar, a remembered option the
  // pipeline run cannot carry also changes the dispatch route to slice-and-queue
  // (bambuddy/send.py `_queue_send`, #124).
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

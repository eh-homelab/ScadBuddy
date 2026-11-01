import { z } from 'zod'
import { ok } from './call.js'
import { outputId, params, slug } from './common.js'
import { nozzles, presetRef, printOptions, slotChoice, tier } from './print.js'
import { defineTool, json, type Tool } from './registry.js'

// The print analyzers (#284, #4966): the bundled checks run over an output, a
// configuration or a library file against the print request it would print with, the
// preview of one of their fixes, and the decisions (accept, ignore, suppress) recorded
// at a scope. Routes: backend/scadbuddy/api/analyzers.py. Listing, running and
// previewing read; applying a fix and recording or removing a decision write only
// ScadBuddy's own `analyzer_decisions`, reversible by removing the row (AI spec §8.1,
// `write`). Nothing sends an accepted diff yet; the send that does goes through the
// outward approval (§8.2), so `confirm` on apply is not that approval.

const SOURCE = "the analyzers' findings over the template's or library file's geometry, the spools and Bambuddy's presets"

const SUBJECT =
  'Name one subject: `output_id` (an output), `slug` with `params` (a configuration, judged on its newest ' +
  'matching output, else without the checks that need a render), or `library_file_id` (a file in ' +
  "Bambuddy's library)."

const subject = {
  output_id: outputId.optional(),
  slug: slug.optional(),
  params: params.optional(),
  library_file_id: z.number().int().min(1).optional(),
}

/** `AnalysisRequest`: the base print request, every field optional (the dialog's own defaults). */
const request = {
  printer_id: z.number().int().optional().describe('A Bambuddy printer id; omitted is the remembered one'),
  printer: z.string().uuid().optional().describe("A registry printer's `id` (list_printers), in place of `printer_id`"),
  plate_id: z.number().int().min(1).optional(),
  all_plates: z.boolean().optional(),
  plate_ids: z.array(z.number().int().min(1)).min(1).optional(),
  copies: z.number().int().min(1).max(1000).optional(),
  filament_plan: z.object({ slots: z.array(slotChoice), force_colour_match: z.boolean().default(false) }).optional(),
  choices: z
    .object({
      nozzles,
      tier: tier.optional(),
      process_name: z.string().max(200).optional(),
      bed_type: z.string().max(64).optional(),
      filament_overrides: z.object({}).catchall(presetRef).optional(),
    })
    .optional()
    .describe("The print's choices; omitted is what the print dialog reopens with for this model"),
  options: printOptions,
}

const scopeKind = z.enum(['global', 'material', 'printer', 'template', 'template_version', 'configuration', 'print'])
const scope = z
  .object({ kind: scopeKind, key: z.string().max(200).default('') })
  .describe(
    'Keys: "" for global; `pla` or `pla/silk` for a material; `id:<printer id>` or `model:<model>` for a ' +
      'printer; the slug for a template; `<slug>@<version>`; `<slug>#<params hash>` for a configuration; the ' +
      'output id for a print. A diagnostic lists the scopes it falls in.',
  )

const fixShape = {
  ...subject,
  ...request,
  diagnostic_key: z.string().max(100).describe('A diagnostic\'s `key` from run_analyzers (`SB2001`, `SB3002:slot-2`)'),
  fix_id: z.string().max(100).describe("One of that diagnostic's fixes, by id"),
  scope: scope.optional().describe('Where the acceptance is stored; omitted is the narrowest (this print)'),
}

const oneSubject = (o: { output_id?: unknown; slug?: unknown; library_file_id?: unknown }) =>
  [o.output_id, o.slug, o.library_file_id].filter((v) => v !== undefined).length === 1

type SubjectArgs = { output_id?: string; slug?: string; params?: Record<string, unknown>; library_file_id?: number }
type RequestArgs = { [K in keyof typeof request]?: unknown }

/** The `target` and `request` halves of a run or fix body, with only what was given. */
function body(args: SubjectArgs & RequestArgs) {
  const { output_id, slug, params, library_file_id, ...rest } = args
  const target = Object.fromEntries(
    Object.entries({ output_id, slug, params, library_file_id }).filter(([, v]) => v !== undefined),
  )
  const asked = Object.fromEntries(
    Object.keys(request)
      .map((key) => [key, rest[key as keyof RequestArgs]])
      // `options` defaults to `{}`, which is the backend's own default: left out.
      .filter(([key, v]) => v !== undefined && !(key === 'options' && Object.keys(v as object).length === 0)),
  )
  return { target, request: asked }
}

const fixInput = z.object(fixShape).refine(oneSubject, { message: SUBJECT })

export const analyzerTools: Tool[] = [
  defineTool({
    name: 'list_analyzers',
    label: 'List the print analyzers',
    description:
      'The bundled print analyzers: id (SB####), title, severity, category, the inputs each needs, the fixes it ' +
      'can offer, and the sources it cites (URL and quoted line).',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/analyzers'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/analyzers'), 'list the analyzers')),
  }),

  defineTool({
    name: 'run_analyzers',
    label: 'Run the print analyzers',
    description:
      'Run every analyzer over a print before it runs, against its base print request (printer, spools, ' +
      'nozzles, quality, plate and options; each omitted is what the print dialog would use). Returns located, ' +
      "cited diagnostics, each with its `key`, the scopes it falls in, the decisions that apply to it, and fixes " +
      'to preview with preview_analyzer_fix. ' +
      SUBJECT +
      " `detail` 'advanced' also lists info-level findings.",
    input: z
      .object({ ...subject, ...request, detail: z.enum(['simple', 'advanced']).default('simple') })
      .refine(oneSubject, { message: SUBJECT }),
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/analyzers/run'],
    handler: async ({ detail, ...args }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/analyzers/run', { body: { ...body(args), detail } }),
          'run the analyzers',
        ),
      ),
  }),

  defineTool({
    name: 'preview_analyzer_fix',
    label: 'Preview an analyzer fix',
    description:
      "Preview one fix of a diagnostic from run_analyzers: the diff, the scope it would be stored at, whether " +
      'it is applicable (`blockers` names what it waits on), a summary for the user, and the `fingerprint` ' +
      'apply_analyzer_fix needs. Send the same subject and request as the run. ' +
      SUBJECT,
    input: fixInput,
    risk: 'read',
    source: SOURCE,
    routes: ['POST /api/v1/analyzers/fixes/preview'],
    handler: async ({ diagnostic_key, fix_id, scope, ...args }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/analyzers/fixes/preview', {
            body: { ...body(args), diagnostic_key, fix_id, ...(scope ? { scope } : {}) },
          }),
          'preview the fix',
        ),
      ),
  }),

  defineTool({
    name: 'apply_analyzer_fix',
    label: 'Apply an analyzer fix',
    description:
      "Record a previewed fix as an `accept` decision at its scope. Show the user the preview's summary first. " +
      'Send exactly what preview_analyzer_fix was sent, with its `fingerprint`: a fingerprint that no longer ' +
      'matches (the diff, scope, print or base moved) is refused, so preview again. A fix with blockers cannot ' +
      'be applied. It sends nothing: it writes only ScadBuddy, reversible with remove_analyzer_decision; a ' +
      'print that later uses the diff asks its own approval.',
    input: z
      .object({
        ...fixShape,
        fingerprint: z.string().length(64).describe("The preview's `fingerprint`"),
        reason: z.string().max(500).optional(),
      })
      .refine(oneSubject, { message: SUBJECT }),
    risk: 'write',
    routes: ['POST /api/v1/analyzers/fixes/apply'],
    handler: async ({ diagnostic_key, fix_id, scope, fingerprint, reason, ...args }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/analyzers/fixes/apply', {
            // The call is the confirmation of the diff the fingerprint binds (analyzers.py `apply_fix`).
            body: {
              ...body(args),
              diagnostic_key,
              fix_id,
              ...(scope ? { scope } : {}),
              fingerprint,
              confirm: true,
              ...(reason === undefined ? {} : { reason }),
            },
          }),
          'apply the fix',
        ),
      ),
  }),

  defineTool({
    name: 'list_analyzer_decisions',
    label: 'List analyzer decisions',
    description:
      'Recorded analyzer decisions (accept, ignore, suppress), broadest scope first: every one, or those at one ' +
      'scope kind (and key), or about one rule.',
    input: z.object({
      scope: scopeKind.optional(),
      scope_key: z.string().max(200).optional().describe('Needs `scope`'),
      diagnostic_id: z.string().regex(/^SB[0-9]{4}$/).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/analyzers/decisions'],
    handler: async (query, { backend }) =>
      json(await ok(backend.GET('/api/v1/analyzers/decisions', { params: { query } }), 'list the decisions')),
  }),

  defineTool({
    name: 'record_analyzer_decision',
    label: 'Ignore or suppress an analyzer finding',
    description:
      'Ignore or suppress a diagnostic at a scope, replacing an earlier decision about the same rule and ' +
      "instance there. `instance` is one diagnostic's `key`; omitted is every instance of the rule. A " +
      'suppression needs a `reason`. `enforced` makes it win over narrower scopes; enforcing the suppression of ' +
      'an error rule also needs `confirm` true, after the user agreed to hide it from every print in the scope. ' +
      'Accepting a fix is apply_analyzer_fix. Reversible with remove_analyzer_decision.',
    input: z.object({
      diagnostic_id: z.string().regex(/^SB[0-9]{4}$/),
      instance: z.string().min(1).max(100).optional(),
      kind: z.enum(['ignore', 'suppress']),
      scope,
      reason: z.string().max(500).optional(),
      enforced: z.boolean().default(false),
      confirm: z.boolean().default(false),
    }),
    risk: 'write',
    routes: ['POST /api/v1/analyzers/decisions'],
    handler: async ({ instance, reason, ...rest }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/analyzers/decisions', {
            body: { ...rest, instance: instance ?? null, ...(reason === undefined ? {} : { reason }) },
          }),
          'record the decision',
        ),
      ),
  }),

  defineTool({
    name: 'remove_analyzer_decision',
    label: 'Remove an analyzer decision',
    description: 'Remove a recorded analyzer decision by its id (list_analyzer_decisions), accept, ignore or suppress.',
    input: z.object({ decision_id: z.string().regex(/^[0-9a-f]{32}$/) }),
    risk: 'write',
    routes: ['DELETE /api/v1/analyzers/decisions/{decision_id}'],
    handler: async ({ decision_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/analyzers/decisions/{decision_id}', { params: { path: { decision_id } } }),
        `remove decision ${decision_id}`,
      )
      return json({ removed: decision_id })
    },
  }),
]

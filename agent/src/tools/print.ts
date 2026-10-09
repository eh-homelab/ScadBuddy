import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ACCEPTING_MS, command, reattach } from './command.js'
import { binary } from './binary.js'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { defineTool, image, json, type Tool, type ToolContext, ToolError } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'

// Bambuddy (issue #251, spec D8): ScadBuddy's own tools over its backend's
// Bambuddy client, so the API key stays server-side and the backend's
// scope-aware errors (backend/scadbuddy/bambuddy/errors.py) reach the agent
// unchanged. Routes: backend/scadbuddy/api/printing.py and outputs.py, and
// library_print.py for a file already in Bambuddy's library (#1756).
//
// - Print flow, outward: anything that uploads to, creates in, or queues on
//   Bambuddy goes through the approval gate (spec §8.2).
// - The print flow is spool-first (#335, docs/superpowers/specs/
//   2026-09-27-spool-first-print-design.md): spools, nozzles, quality and plate
//   are chosen, and the backend's resolver derives Bambu's printer, process and
//   filament presets from them. There are no pipelines, presets or eligibility
//   tools any more: their routes went with the pipeline picker (that spec §7,
//   "Removed from the dialog"). send_to_bambuddy only uploads to the library
//   (#312).
// - Farm context, read: printers and live status (get_print_targets in
//   settings.ts), the print dialog's choices (get_print_choices), spools with
//   per-slot remaining grams (get_print_filaments), print progress, and a
//   printer's current camera frame (get_printer_camera, #796). The
//   queue, the print archive and aggregate stats have no backend route yet, so
//   they have no tool yet.
// - Printer control (pause/stop/lights/motion/G-code) is out of scope.

const nullable = <T extends z.ZodType>(schema: T) => schema.nullable().optional()

/** Print run ids are 32 lowercase hex digits (`run_id` in backend/openapi.json). */
const runId = z
  .string()
  .regex(/^[0-9a-f]{32}$/, 'must be a print run id: 32 lowercase hex digits, as print_output returns it')
  .describe('Print run id, as print_output returns it')

type PrintRun = Awaited<ReturnType<typeof getRun>>

export { RUN_REATTEMPTS } from './command.js'

// What a print is of (#1749, #1756): an output, or a file already in Bambuddy's library.
// A library-file print is an ordinary print; only the 3MF it slices comes from elsewhere,
// so every print tool takes either, and only the route differs.

export const libraryFileId = z
  .number()
  .int()
  .min(1)
  .describe("A Bambuddy library file's `id` (list_library), in place of output_id")

/** The two sources a print tool takes, exactly one of them per call. */
export const sourceShape = { output_id: outputId.optional(), library_file_id: libraryFileId.optional() }

export const ONE_SOURCE = 'name one source: output_id or library_file_id'

/** A print tool's input: `shape` plus its source, refused at parse time unless it names exactly one. */
export function withSource<S extends z.ZodRawShape>(shape: S) {
  return z
    .object({ ...sourceShape, ...shape })
    .refine(
      (o) => {
        // `o` is generic over `shape`; only the two source fields matter here.
        const { output_id, library_file_id } = o as { output_id?: unknown; library_file_id?: unknown }
        return (output_id === undefined) !== (library_file_id === undefined)
      },
      { message: ONE_SOURCE },
    )
}

export type Source = { kind: 'output'; id: string } | { kind: 'library'; id: number }

export function sourceOf(args: { output_id?: string | undefined; library_file_id?: number | undefined }): Source {
  if (args.library_file_id !== undefined) return { kind: 'library', id: args.library_file_id }
  if (args.output_id !== undefined) return { kind: 'output', id: args.output_id }
  throw new ToolError(ONE_SOURCE)
}

/** How a source is named in an approval line, a title or an error. */
export function sourceName(args: { output_id?: string | undefined; library_file_id?: number | undefined }): string {
  return args.library_file_id !== undefined ? `library file ${args.library_file_id}` : `output ${args.output_id ?? ''}`
}

type FilamentQuery = { printer_id?: number | undefined; plate_id?: number | undefined; all_plates?: boolean | undefined }

function getChoices(ctx: ToolContext, source: Source, printer_id: number | undefined) {
  const query = { printer_id }
  return source.kind === 'library'
    ? ok(
        ctx.backend.GET('/api/v1/print/library/{file_id}/choices', { params: { path: { file_id: source.id }, query } }),
        `get print choices for library file ${source.id}`,
      )
    : ok(
        ctx.backend.GET('/api/v1/print/outputs/{output_id}/choices', { params: { path: { output_id: source.id }, query } }),
        `get print choices for ${source.id}`,
      )
}

/** The source's plates: for an output its 3MF's, for a library file what Bambuddy reads from it. */
export function getPlates(ctx: Pick<ToolContext, 'backend'>, source: Source) {
  return source.kind === 'library'
    ? ok(
        ctx.backend.GET('/api/v1/print/library/{file_id}/plates', { params: { path: { file_id: source.id } } }),
        `get plates of library file ${source.id}`,
      )
    : ok(
        ctx.backend.GET('/api/v1/outputs/{output_id}/plates', { params: { path: { output_id: source.id } } }),
        `get plates of ${source.id}`,
      )
}

function getFilaments(ctx: ToolContext, source: Source, query: FilamentQuery) {
  return source.kind === 'library'
    ? ok(
        ctx.backend.GET('/api/v1/print/library/{file_id}/filaments', { params: { path: { file_id: source.id }, query } }),
        `get filaments for library file ${source.id}`,
      )
    : ok(
        ctx.backend.GET('/api/v1/print/outputs/{output_id}/filaments', { params: { path: { output_id: source.id }, query } }),
        `get filaments for ${source.id}`,
      )
}

async function getRun(ctx: ToolContext, id: string) {
  return ok(
    ctx.backend.GET('/api/v1/print/runs/{run_id}', { params: { path: { run_id: id } }, signal: ctx.signal }),
    `get print run ${id}`,
  )
}

/**
 * `POST .../run` answers 202 and slices in the background (#470): follow the
 * run until it ends or `renderWaitMs` passes, as render_model follows a render.
 * A read that stays unanswered names the run, so it is followed, not printed again.
 */
async function waitForRun(ctx: ToolContext, run: PrintRun): Promise<PrintRun> {
  const deadline = performance.now() + ctx.renderWaitMs
  for (let step = 1; run.status === 'running' && performance.now() < deadline; step++) {
    await ctx.progress(step, undefined, 'print run: slicing and queueing')
    await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
    const id = run.id
    try {
      run = await reattach(
        ctx,
        () => ctx.backend.GET('/api/v1/print/runs/{run_id}', { params: { path: { run_id: id } }, signal: ctx.signal }),
        `get print run ${id}`,
      )
    } catch (caught) {
      if (ctx.signal.aborted) throw caught
      const reason = caught instanceof Error ? caught.message : String(caught)
      throw new ToolError(
        `print run ${id} was started, but reading it failed: ${reason}. ` +
          'Follow it with get_print_run; calling print_output again would be a second print.',
      )
    }
  }
  return run
}

/** A failed run is the tool's error, in the backend's own words; a running one says how to follow it. */
function runOutcome(run: PrintRun, ctx: ToolContext) {
  if (run.status === 'failed') {
    const error = run.error
    const failed = `print ${run.output_id} failed${error ? ` (HTTP ${error.status}): ${error.detail}` : ''}`
    if (!run.may_have_queued) throw new ToolError(failed)
    // Failed after it had tried to queue: the print may be on Bambuddy's queue anyway, and
    // another print_output call is a new print (its own request_id), so check first. The
    // answer is the run itself, so the session records it (#1017, sessions/touched.ts).
    const note = "The print may still have been queued: check Bambuddy's queue before printing again."
    ctx.report?.({ detail: `${failed}. ${note}` })
    return { ...json({ ...run, note }), isError: true }
  }
  if (run.status === 'running') return json({ ...run, note: 'still slicing; poll get_print_run with this id' })
  return json(run)
}
const calibration = z.enum(['off', 'on', 'auto'])

/** `PrintOptions` in backend/openapi.json: sparse, and the backend refuses unknown fields. */
export const printOptions = z
  .object({
    auto_off_after: nullable(z.boolean()),
    bed_levelling: nullable(calibration),
    flow_cali: nullable(calibration),
    insert_at_top: nullable(z.boolean()),
    layer_inspect: nullable(z.boolean()),
    manual_start: nullable(z.boolean()),
    nozzle_offset_cali: nullable(calibration),
    preheat_chamber_target_override: nullable(z.number().int().min(0).max(65)),
    preheat_override: nullable(z.enum(['inherit', 'on', 'off'])),
    project_id: nullable(z.number().int()),
    quantity: nullable(z.number().int().min(1).max(1000)),
    timelapse: nullable(z.boolean()),
    use_ams: nullable(z.boolean()),
    vibration_cali: nullable(z.boolean()),
  })
  .strict()
  .default({})

export const slotChoice = z.object({ slot_id: z.number().int(), spool_id: z.number().int() })
const presetRef = z.object({
  source: z.enum(['orca_cloud', 'cloud', 'local', 'standard']),
  id: z.string().min(1),
})
/** `NozzleChoice`: one extruder's nozzle. One size for the job (spool-first spec §2 step 3). */
const nozzleChoice = z.object({
  size: z.enum(['0.2', '0.4', '0.6', '0.8']),
  flow: z.enum(['standard', 'high_flow']).default('standard'),
})
const nozzles = z.array(nozzleChoice).min(1).max(2)
const tier = z.enum(['fine', 'standard', 'draft'])

type NozzleChoice = z.infer<typeof nozzleChoice>
type SlotChoice = z.infer<typeof slotChoice>

/** 0.4 mm standard on both sides, when the choices name no defaults (frontend `DEFAULT_NOZZLES`). */
const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

/**
 * The model's remembered spool per slot where that spool still fits the slot's
 * colour (`colour_matches`, #933), else the backend's suggestion:
 * frontend/src/lib/filaments.ts `seedPlan`, so an agent's print starts from
 * what the dialog would show.
 */
function seedPlan(
  options: { slots?: { slot_id: number; colour_matches?: number[] }[]; suggested?: SlotChoice[] },
  remembered: SlotChoice[],
): SlotChoice[] {
  return (options.slots ?? []).flatMap((slot) => {
    const fits = new Set(slot.colour_matches ?? [])
    const kept = remembered.find((choice) => choice.slot_id === slot.slot_id && fits.has(choice.spool_id))
    const choice = kept ?? options.suggested?.find((entry) => entry.slot_id === slot.slot_id)
    return choice ? [{ slot_id: choice.slot_id, spool_id: choice.spool_id }] : []
  })
}

export const printTools: Tool[] = [
  // ── read: farm context and planning ─────────────────────────────────────
  defineTool({
    name: 'get_print_choices',
    description:
      'Everything the print dialog offers for an output, or a Bambuddy library file (`library_file_id`), in ' +
      'one read: printers (and the one chosen), the installed nozzles, quality tiers and Bambu processes per ' +
      'nozzle size, filament presets per size, plate types with the one last printed on, the filament step ' +
      "(as get_print_filaments), the model's (or the file's) remembered choices, and its plates by index and " +
      'name (what each holds; `null` when nothing names it), which print_output takes as `plate_id`. What ' +
      'print_output fills omitted choices from.',
    input: withSource({ printer_id: z.number().int().optional() }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    // Printers, status and archives (Read Status); slicer presets and the 3MF's
    // filament requirements (Manage Library). backend/scadbuddy/bambuddy/choices.py.
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: [
      'GET /api/v1/print/outputs/{output_id}/choices',
      'GET /api/v1/print/library/{file_id}/choices',
      'GET /api/v1/outputs/{output_id}/plates',
      'GET /api/v1/print/library/{file_id}/plates',
    ],
    handler: async (args, ctx) => {
      const source = sourceOf(args)
      const [choices, plates] = await Promise.all([getChoices(ctx, source, args.printer_id), getPlates(ctx, source)])
      // #986 — a plate by what it holds, so the agent can say "the lid", not "plate 2".
      return json({ ...choices, plates: plates.map(({ index, name }) => ({ index, name: name ?? null })) })
    },
  }),

  defineTool({
    name: 'get_print_filaments',
    description:
      "Bambuddy's spool inventory joined to where each spool is loaded and, with `printer_id`, the " +
      'remaining grams per slot and the mounted nozzles — plus what this output\'s (or library file\'s) plate ' +
      '(or, with `all_plates`, every plate) needs and a suggested spool per slot.',
    input: withSource({
      printer_id: z.number().int().optional(),
      plate_id: z.number().int().min(1).optional(),
      all_plates: z.boolean().optional(),
    }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['GET /api/v1/print/outputs/{output_id}/filaments', 'GET /api/v1/print/library/{file_id}/filaments'],
    handler: async ({ printer_id, plate_id, all_plates, ...source }, ctx) =>
      json(await getFilaments(ctx, sourceOf(source), { printer_id, plate_id, all_plates })),
  }),

  defineTool({
    name: 'get_print_progress',
    description:
      'How the last print of an output, or of a Bambuddy library file (`library_file_id`), is going, per copy; ' +
      'null when it was never printed. Poll until `settled` is true.',
    input: withSource({}),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Read Status', 'Manage Queue'],
    routes: ['GET /api/v1/print/outputs/{output_id}/progress', 'GET /api/v1/print/library/{file_id}/progress'],
    handler: async (args, { backend }) => {
      const source = sourceOf(args)
      return json(
        source.kind === 'library'
          ? await ok(
              backend.GET('/api/v1/print/library/{file_id}/progress', { params: { path: { file_id: source.id } } }),
              `get print progress of library file ${source.id}`,
            )
          : await ok(
              backend.GET('/api/v1/print/outputs/{output_id}/progress', { params: { path: { output_id: source.id } } }),
              `get print progress of ${source.id}`,
            ),
      )
    },
  }),

  defineTool({
    name: 'get_printer_camera',
    description:
      "A printer's current camera frame as a JPEG: what is on the bed now, including prints ScadBuddy did " +
      'not start. printer_id is a Bambuddy printer id, as get_print_targets lists them.',
    input: z.object({ printer_id: z.number().int().nonnegative() }),
    risk: 'read',
    source: "a printer's camera: whatever is in view, including anything written on it",
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/print/printers/{printer_id}/camera'],
    handler: async ({ printer_id }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/print/printers/{printer_id}/camera', {
          params: { path: { printer_id } },
          parseAs: 'stream',
        }),
        `capture the camera of printer ${printer_id}`,
        ctx,
        { path: `/api/v1/print/printers/${printer_id}/camera`, name: `printer-${printer_id}-camera.jpg`, fallbackType: 'image/jpeg' },
        image,
      ),
  }),

  defineTool({
    name: 'get_print_run',
    description:
      'A print run print_output started: `running` while it slices and queues, then `succeeded` with its ' +
      '`result` (warnings, queue item ids, the Bambuddy URL) or `failed` with the `error` that stopped it.',
    input: z.object({ run_id: runId }),
    risk: 'read',
    routes: ['GET /api/v1/print/runs/{run_id}'],
    handler: async ({ run_id }, ctx) => json(await getRun(ctx, run_id)),
  }),

  defineTool({
    name: 'list_print_projects',
    description: "Bambuddy's projects, to file prints under, and the one last used." + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Manage Projects'],
    routes: ['GET /api/v1/print/projects'],
    handler: async (args, { backend }) => {
      const { last_project_id, projects } = await ok(backend.GET('/api/v1/print/projects'), 'list projects')
      const { items, ...rest } = page(projects ?? [], args, (p) => String(p.id), 'list_print_projects')
      return json({ last_project_id, projects: items, ...rest })
    },
  }),

  defineTool({
    name: 'list_library',
    description:
      "Bambuddy's library, one folder at a time: the folder tree, and the files of `folder_id` (the top level " +
      'without it). Without `all` only unsliced 3MFs, the files print_output takes as `library_file_id`; with ' +
      'it every file, each flagged `printable`. A file ScadBuddy uploaded names its `output_id`.' +
      PAGED,
    input: z.object({ folder_id: z.number().int().optional(), all: z.boolean().optional(), ...pageInput }),
    risk: 'read',
    source: 'Bambuddy data (folder and file names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/print/library'],
    handler: async (args, { backend }) => {
      const { files, ...listing } = await ok(
        backend.GET('/api/v1/print/library', { params: { query: { folder_id: args.folder_id, all: args.all } } }),
        'list the library',
      )
      const { items, ...rest } = page(files ?? [], args, (file) => String(file.id), 'list_library')
      return json({ ...listing, files: items, ...rest })
    },
  }),

  // ── write: preferences the print dialog remembers (ScadBuddy-local, re-settable) ──
  defineTool({
    name: 'remember_model_print_choices',
    description:
      "Remember what a model's (or a Bambuddy library file's, `library_file_id`) print dialog opens on next " +
      'time: the printer, per-slot spools, nozzles, and the quality tier or named process. Replaces the ' +
      "entry whole: an omitted part is forgotten, and passing nothing forgets them all (get_print_choices " +
      'shows the current `model_choices`).',
    input: z
      .object({
        slug: slug.optional(),
        library_file_id: libraryFileId.optional(),
        printer_id: z.number().int().nullable().optional(),
        filament_plan: z.array(slotChoice).optional().describe('Only spools moved off the suggestion'),
        nozzles: z.array(nozzleChoice).max(2).optional(),
        tier: tier.nullable().optional(),
        process_name: z.string().max(200).nullable().optional().describe('An Advanced-mode Bambu process, in place of a tier'),
      })
      .refine((o) => (o.slug === undefined) !== (o.library_file_id === undefined), {
        message: 'name one: slug or library_file_id',
      }),
    risk: 'write',
    routes: ['PUT /api/v1/print/models/{slug}/choices', 'PUT /api/v1/print/library/{file_id}/choices'],
    handler: async ({ slug, library_file_id, printer_id, filament_plan, nozzles, tier, process_name }, { backend }) => {
      const body = {
        printer_id: printer_id ?? null,
        filament_plan: filament_plan ?? [],
        nozzles: nozzles ?? [],
        tier: tier ?? null,
        process_name: process_name ?? null,
      }
      if (library_file_id !== undefined) {
        return json(
          await ok(
            backend.PUT('/api/v1/print/library/{file_id}/choices', { params: { path: { file_id: library_file_id } }, body }),
            `remember choices for library file ${library_file_id}`,
          ),
        )
      }
      if (slug === undefined) throw new ToolError('name one: slug or library_file_id')
      return json(
        await ok(
          backend.PUT('/api/v1/print/models/{slug}/choices', { params: { path: { slug } }, body }),
          `remember choices for ${slug}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'remember_last_project',
    description:
      'Choose the Bambuddy project the Customize page and the print dialog open on (null for "No project"). ' +
      'Generate files its 3MF there; list_print_projects shows the current `last_project_id`.',
    input: z.object({ project_id: z.number().int().nullable() }),
    risk: 'write',
    routes: ['PUT /api/v1/print/projects/last'],
    handler: async ({ project_id }, { backend }) =>
      json(
        await ok(backend.PUT('/api/v1/print/projects/last', { body: { project_id } }), 'remember the project'),
      ),
  }),

  defineTool({
    name: 'remember_printer_bed_type',
    description: 'Remember which build plate type is on a printer (null to forget it).',
    input: z.object({ printer_id: z.number().int(), bed_type: z.string().max(64).nullable() }),
    risk: 'write',
    routes: ['PUT /api/v1/print/printers/{printer_id}/bed-type'],
    handler: async ({ printer_id, bed_type }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/print/printers/{printer_id}/bed-type', { params: { path: { printer_id } }, body: { bed_type } }),
          `remember bed type of printer ${printer_id}`,
        ),
      ),
  }),

  // ── outward: everything that uploads to, creates in or queues on Bambuddy ──
  defineTool({
    name: 'send_to_bambuddy',
    description:
      "Upload an output's 3MF to Bambuddy's library folder. Nothing is sliced or queued: printing is " +
      'print_output.',
    // Strict, so an older client still asking for `mode: 'queue'` or `copies` is refused
    // rather than silently given a library upload (the HTTP route 422s the same, #312).
    input: z.object({ output_id: outputId }).strict(),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/outputs/{output_id}/send'],
    title: ({ output_id }) => `Send output ${output_id} to Bambuddy`,
    summarize: ({ output_id }) => `Send output ${output_id} to Bambuddy's library`,
    handler: async ({ output_id }, ctx) =>
      json(
        await command(ctx, `send ${output_id}`, (headers) =>
          ctx.backend.POST('/api/v1/outputs/{output_id}/send', {
            params: { path: { output_id } },
            body: { mode: 'library' },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'print_output',
    description:
      'Print an output, or a file already in Bambuddy\'s library (`library_file_id`, from list_library): the ' +
      'same print, only the 3MF it slices comes from the library. Spool-first: slice with presets the backend ' +
      'derives from the chosen spools, nozzles, quality and plate, then queue it on one printer, behind one ' +
      'approval. Any choice left out is filled ' +
      "the way the print dialog opens: the chosen printer, the model's (or file's) remembered nozzles, tier or " +
      "process and spools (else 0.4 mm, High Flow on each side with a High Flow nozzle of that size and Standard " +
      "elsewhere, the Standard tier and the suggested spools), and the printer's " +
      'preselected plate type. A choice the backend cannot resolve (mixed nozzle sizes, a slot with no ' +
      'spool or preset) is refused before anything is sliced. `project_id` files the print under a Bambuddy ' +
      'project: omit it for the remembered project (`last_project_id`), or pass null for "No project". ' +
      'The run slices and queues in the background: this waits for it and answers with the run and its ' +
      '`result` (warnings, queue item ids), or hands back the still-running run to poll with ' +
      'get_print_run. Then follow the print with get_print_progress.',
    input: withSource({
      printer_id: z.number().int().optional(),
      copies: z.number().int().min(1).max(1000).optional(),
      plate_id: z.number().int().min(1).default(1),
      all_plates: z.boolean().default(false),
      filament_plan: z
        .object({ slots: z.array(slotChoice), force_colour_match: z.boolean().default(false) })
        .optional(),
      nozzles: nozzles.optional().describe('One size for the job; a second entry is the other extruder'),
      tier: tier.optional(),
      process_name: z.string().max(200).optional().describe('A Bambu process by name, in place of `tier`'),
      bed_type: z.string().max(64).optional(),
      // `catchall`, not `z.record`: see `params` in common.ts.
      filament_overrides: z
        .object({})
        .catchall(presetRef)
        .optional()
        .describe('A filament preset per slot id, in place of the spool\'s own'),
      project_id: nullable(z.number().int()).describe('Omit for the remembered project; null for "No project"'),
      options: printOptions,
      print_sequence: z
        .enum(['by layer', 'by object'])
        .optional()
        .describe("'by object' finishes each object before the next starts; omit for the template's own"),
    }),
    risk: 'outward',
    bambuddyScope: ['Read Status', 'Manage Library', 'Manage Queue'],
    routes: [
      'POST /api/v1/print/outputs/{output_id}/run',
      'POST /api/v1/print/library/{file_id}/run',
      'GET /api/v1/print/runs/{run_id}',
    ],
    title: (args) => `Print ${sourceName(args)}${args.copies && args.copies > 1 ? ` × ${args.copies}` : ''}`,
    summarize: (args) => {
      const { printer_id, copies, plate_id, all_plates, nozzles, tier, process_name } = args
      const defaulted =
        nozzles === undefined ||
        (tier === undefined && process_name === undefined) ||
        args.filament_plan === undefined ||
        args.bed_type === undefined
      return (
        `Print ${sourceName(args)}: ${copies ?? 1} cop${(copies ?? 1) === 1 ? 'y' : 'ies'} of ${
          all_plates ? 'every plate' : `plate ${plate_id}`
        }` +
        `${nozzles?.[0] ? ` with a ${nozzles[0].size} mm nozzle` : ''}` +
        `${process_name ? `, process "${process_name}"` : tier ? `, ${tier} quality` : ''}` +
        `${printer_id !== undefined ? ` on printer ${printer_id}` : ''}` +
        `${defaulted ? ' (other choices as the print dialog opens)' : ''}`
      )
    },
    // Its own wait for the print run, after the backend has accepted it.
    waitsMs: (_, ctx) => ACCEPTING_MS + ctx.renderWaitMs,
    handler: async (args, ctx) => {
      const { backend } = ctx
      const source = sourceOf(args)
      let { printer_id: printerId, nozzles: chosenNozzles, bed_type: bedType } = args
      let slots = args.filament_plan?.slots
      let chosenTier: z.infer<typeof tier> | null | undefined = args.tier
      let processName: string | null | undefined = args.process_name
      if (processName !== undefined) chosenTier = null
      // Fill what was left out the way the dialog does (frontend PrintPicker
      // `seedDialog`, spool-first spec §7), from the one read the dialog opens
      // on: GET /print/{outputs|library}/{id}/choices. /run takes no defaults of its own
      // for nozzles, spools or plate, so an omitted choice must be made here.
      if (
        printerId === undefined ||
        chosenNozzles === undefined ||
        bedType === undefined ||
        slots === undefined ||
        (chosenTier === undefined && processName === undefined)
      ) {
        const view = await getChoices(ctx, source, printerId)
        printerId ??= view.printer_id ?? undefined
        bedType ??= view.bed_type
        const last = view.model_choices
        const remembered = last?.nozzles ?? []
        // With nothing remembered, the printer's defaults: High Flow on each side that has
        // a High Flow nozzle of the size (#1895).
        const defaults = view.default_nozzles ?? []
        if (chosenNozzles === undefined)
          chosenNozzles = remembered.length > 0 ? remembered : defaults.length > 0 ? defaults : DEFAULT_NOZZLES
        if (chosenTier === undefined && processName === undefined) {
          // A remembered process belongs to the remembered nozzle size.
          processName = remembered.length > 0 && args.nozzles === undefined ? (last?.process_name ?? null) : null
          chosenTier = processName ? null : (last?.tier ?? 'standard')
        }
        if (slots === undefined) {
          // The choices read carries plate 1's filament step; any other plate,
          // or all of them, is read for itself (PrintPicker does the same).
          const filaments =
            args.plate_id === 1 && !args.all_plates
              ? view.filaments
              : await getFilaments(
                  ctx,
                  source,
                  args.all_plates
                    ? { printer_id: printerId, all_plates: true }
                    : { printer_id: printerId, plate_id: args.plate_id },
                )
          slots = seedPlan(filaments, last?.filament_plan ?? [])
        }
      }
      // One per call (#470): a call is a deliberate print, so the same choices again are
      // a new one rather than the last call's run. Every re-send below reuses it, so a
      // POST whose answer was lost re-attaches to its run instead of printing twice.
      const requestId = randomUUID()
      const body = {
        printer_id: printerId ?? null,
        copies: args.copies ?? null,
        plate_id: args.plate_id,
        all_plates: args.all_plates,
        filament_plan: { slots, force_colour_match: args.filament_plan?.force_colour_match ?? false },
        choices: {
          nozzles: chosenNozzles,
          tier: chosenTier ?? null,
          process_name: processName ?? null,
          bed_type: bedType,
          filament_overrides: args.filament_overrides ?? {},
        },
        // Omitted stays omitted (the remembered project); null is "No project" (#317).
        ...(args.project_id === undefined ? {} : { project_id: args.project_id }),
        options: args.options,
        // The same for a library file as for an output (#907, #1756).
        ...(args.print_sequence === undefined ? {} : { print_sequence: args.print_sequence }),
        request_id: requestId,
      }
      const started = await reattach(
        ctx,
        () =>
          source.kind === 'library'
            ? backend.POST('/api/v1/print/library/{file_id}/run', {
                params: { path: { file_id: source.id } },
                signal: ctx.signal,
                body,
              })
            : backend.POST('/api/v1/print/outputs/{output_id}/run', {
                params: { path: { output_id: source.id } },
                signal: ctx.signal,
                body,
              }),
        `print ${sourceName(args)}`,
        " The print may still have started: check Bambuddy's queue before printing again.",
      )
      return runOutcome(await waitForRun(ctx, started), ctx)
    },
  }),

  defineTool({
    name: 'create_print_project',
    description:
      'Create a Bambuddy project (with its library folder), or link an existing one by `project_id`. ' +
      '`parent_id` nests a new project under an existing one, and its folder under the parent\'s folder; ' +
      'it applies only to a new project, and sent with `project_id` or `folder_id` it is refused (400), never ignored.',
    input: z.object({
      name: z.string().optional(),
      description: z.string().optional(),
      colour: z.string().optional(),
      tags: z.string().optional(),
      url: z.string().optional(),
      folder_id: z.number().int().optional(),
      project_id: z.number().int().optional(),
      parent_id: z.number().int().optional(),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Projects', 'Manage Library'],
    routes: ['POST /api/v1/print/projects'],
    summarize: ({ name, project_id }) =>
      project_id !== undefined ? `Link Bambuddy project ${project_id}` : `Create the Bambuddy project "${name ?? ''}"`,
    handler: async (args, ctx) =>
      json(
        await command(ctx, 'create project', (headers) =>
          ctx.backend.POST('/api/v1/print/projects', {
            headers,
            body: {
              name: args.name ?? null,
              description: args.description ?? null,
              colour: args.colour ?? null,
              tags: args.tags ?? null,
              url: args.url ?? null,
              folder_id: args.folder_id ?? null,
              project_id: args.project_id ?? null,
              parent_id: args.parent_id ?? null,
            },
          }),
        ),
      ),
  }),

  defineTool({
    name: 'file_output_in_project_folder',
    description:
      "Upload an output's editable 3MF into a Bambuddy project's library folder, as Generate does with a " +
      'project chosen. Idempotent: the same project again reuses the file already there (`created: false`), ' +
      'and a later print on the same printer reuses it too.',
    input: z.object({ output_id: outputId, project_id: z.number().int() }),
    risk: 'outward',
    bambuddyScope: ['Manage Library', 'Manage Projects'],
    routes: ['POST /api/v1/outputs/{output_id}/project-file'],
    summarize: ({ output_id, project_id }) =>
      `Upload output ${output_id}'s 3MF into Bambuddy project ${project_id}'s folder`,
    handler: async ({ output_id, project_id }, ctx) =>
      json(
        await command(ctx, `file ${output_id} in project ${project_id}`, (headers) =>
          ctx.backend.POST('/api/v1/outputs/{output_id}/project-file', {
            params: { path: { output_id } },
            body: { project_id },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'file_output_under_project',
    description:
      "File an output's, or a Bambuddy library file's (`library_file_id`), queue entries (and any finished " +
      "prints' archives) under a Bambuddy project: the entries named, else those of its newest print.",
    input: withSource({
      project_id: z.number().int().optional(),
      queue_item_ids: z.array(z.number().int()).default([]),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Projects'],
    routes: ['POST /api/v1/print/outputs/{output_id}/project', 'POST /api/v1/print/library/{file_id}/project'],
    summarize: (args) =>
      `File ${sourceName(args)}'s prints under Bambuddy project ${args.project_id ?? '(its own)'}`,
    handler: async ({ project_id, queue_item_ids, ...rest }, ctx) => {
      const source = sourceOf(rest)
      const body = { ...(project_id === undefined ? {} : { project_id }), queue_item_ids }
      return json(
        await command(ctx, `file ${sourceName(rest)} under a project`, (headers) =>
          source.kind === 'library'
            ? ctx.backend.POST('/api/v1/print/library/{file_id}/project', {
                params: { path: { file_id: source.id } },
                body,
                headers,
              })
            : ctx.backend.POST('/api/v1/print/outputs/{output_id}/project', {
                params: { path: { output_id: source.id } },
                body,
                headers,
              }),
        ),
      )
    },
  }),

]

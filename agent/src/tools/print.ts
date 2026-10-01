import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { defineTool, json, type Tool, type ToolContext, ToolError } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'

// Bambuddy (issue #251, spec D8): ScadBuddy's own tools over its backend's
// Bambuddy client, so the API key stays server-side and the backend's
// scope-aware errors (backend/scadbuddy/bambuddy/errors.py) reach the agent
// unchanged. Routes: backend/scadbuddy/api/printing.py and outputs.py.
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
//   per-slot remaining grams (get_print_filaments), and print progress. The
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

type FetchResult<T> = { data?: T; error?: unknown; response: Response }

/** How many more times a print run request no ScadBuddy answer described is sent (#470). */
export const RUN_REATTEMPTS = 3

/**
 * The request never got the backend's own answer: a 502/503/504, or Cloudflare's 524,
 * from something in between, whose body is not one of the backend's problems (they
 * always carry a `detail`). The same list as the browser client's `unanswered`.
 */
function unanswered(result: FetchResult<unknown>): boolean {
  const { error, response } = result
  const detail = typeof error === 'object' && error !== null && typeof (error as { detail?: unknown }).detail === 'string'
  return [502, 503, 504, 524].includes(response.status) && !detail
}

/**
 * `send`, again while it goes unanswered (a dropped connection, fetch's `TypeError`, or
 * a proxy's own 502/503/504/524), as the browser client's `reattach` does. Safe only for a
 * request keyed to its run: the POST's `request_id` makes a re-send the same run, never a
 * second print, and the GET only reads. A problem the backend wrote is never re-sent.
 */
async function reattach<T>(
  ctx: ToolContext,
  send: () => Promise<FetchResult<T>>,
  what: string,
  gaveUp = '',
): Promise<T> {
  for (let tries = 0; ; tries++) {
    let result: FetchResult<T>
    try {
      result = await send()
    } catch (caught) {
      if (ctx.signal.aborted || !(caught instanceof TypeError)) throw caught
      if (tries >= RUN_REATTEMPTS) throw new ToolError(`${what}: ScadBuddy did not answer (${caught.message}).${gaveUp}`)
      await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
      continue
    }
    if (unanswered(result)) {
      if (tries >= RUN_REATTEMPTS) {
        throw new ToolError(`${what}: ScadBuddy did not answer (HTTP ${result.response.status}).${gaveUp}`)
      }
      await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
      continue
    }
    return ok(Promise.resolve(result), what)
  }
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
  const deadline = Date.now() + ctx.renderWaitMs
  for (let step = 1; run.status === 'running' && Date.now() < deadline; step++) {
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
function runOutcome(run: PrintRun) {
  if (run.status === 'failed') {
    const error = run.error
    // Failed after it had tried to queue: the print may be on Bambuddy's queue anyway, and
    // another print_output call is a new print (its own request_id), so check first.
    const queued = run.may_have_queued
      ? " The print may still have been queued: check Bambuddy's queue before printing again."
      : ''
    throw new ToolError(
      `print ${run.output_id} failed${error ? ` (HTTP ${error.status}): ${error.detail}` : ''}${queued}`,
    )
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

const slotChoice = z.object({ slot_id: z.number().int(), spool_id: z.number().int() })
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

/** The dialog's own default: 0.4 mm standard on both sides (frontend PrintPicker `DEFAULT_NOZZLES`). */
const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

/**
 * The model's remembered spool per slot where that spool is still in the
 * inventory, else the backend's suggestion: frontend/src/lib/filaments.ts
 * `seedPlan`, so an agent's print starts from what the dialog would show.
 */
function seedPlan(
  options: { slots?: { slot_id: number }[]; spools?: { spool_id: number }[]; suggested?: SlotChoice[] },
  remembered: SlotChoice[],
): SlotChoice[] {
  const inventory = new Set((options.spools ?? []).map((spool) => spool.spool_id))
  return (options.slots ?? []).flatMap((slot) => {
    const kept = remembered.find((choice) => choice.slot_id === slot.slot_id && inventory.has(choice.spool_id))
    const choice = kept ?? options.suggested?.find((entry) => entry.slot_id === slot.slot_id)
    return choice ? [{ slot_id: choice.slot_id, spool_id: choice.spool_id }] : []
  })
}

export const printTools: Tool[] = [
  // ── read: farm context and planning ─────────────────────────────────────
  defineTool({
    name: 'get_print_choices',
    description:
      'Everything the print dialog offers for an output, in one read: printers (and the one chosen), the ' +
      'installed nozzles, quality tiers and Bambu processes per nozzle size, filament presets per size, plate ' +
      "types with the one last printed on, the filament step (as get_print_filaments), and this model's " +
      'remembered choices. What print_output fills omitted choices from.',
    input: z.object({ output_id: outputId, printer_id: z.number().int().optional() }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    // Printers, status and archives (Read Status); slicer presets and the 3MF's
    // filament requirements (Manage Library). backend/scadbuddy/bambuddy/choices.py.
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['GET /api/v1/print/outputs/{output_id}/choices'],
    handler: async ({ output_id, printer_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/outputs/{output_id}/choices', {
            params: { path: { output_id }, query: { printer_id } },
          }),
          `get print choices for ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'get_print_filaments',
    description:
      "Bambuddy's spool inventory joined to where each spool is loaded and, with `printer_id`, the " +
      'remaining grams per slot and the mounted nozzles — plus what this output\'s plate (or, with ' +
      '`all_plates`, every plate) needs and a suggested spool per slot.',
    input: z.object({
      output_id: outputId,
      printer_id: z.number().int().optional(),
      plate_id: z.number().int().min(1).optional(),
      all_plates: z.boolean().optional(),
    }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['GET /api/v1/print/outputs/{output_id}/filaments'],
    handler: async ({ output_id, printer_id, plate_id, all_plates }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/outputs/{output_id}/filaments', {
            params: { path: { output_id }, query: { printer_id, plate_id, all_plates } },
          }),
          `get filaments for ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'get_print_progress',
    description:
      "How the last print of an output is going, per copy; null when it was never printed. Poll until " +
      '`settled` is true.',
    input: z.object({ output_id: outputId }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Read Status', 'Manage Queue'],
    routes: ['GET /api/v1/print/outputs/{output_id}/progress'],
    handler: async ({ output_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/outputs/{output_id}/progress', { params: { path: { output_id } } }),
          `get print progress of ${output_id}`,
        ),
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

  // ── write: preferences the print dialog remembers (ScadBuddy-local, re-settable) ──
  defineTool({
    name: 'remember_model_print_choices',
    description:
      "Remember what a model's print dialog opens on next time: the printer, per-slot spools, nozzles, and " +
      'the quality tier or named process. Replaces the model\'s entry whole: an omitted part is forgotten, ' +
      'and passing nothing forgets them all (get_print_choices shows the current `model_choices`).',
    input: z.object({
      slug,
      printer_id: z.number().int().nullable().optional(),
      filament_plan: z.array(slotChoice).optional().describe('Only spools moved off the suggestion'),
      nozzles: z.array(nozzleChoice).max(2).optional(),
      tier: tier.nullable().optional(),
      process_name: z.string().max(200).nullable().optional().describe('An Advanced-mode Bambu process, in place of a tier'),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/print/models/{slug}/choices'],
    handler: async ({ slug, printer_id, filament_plan, nozzles, tier, process_name }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/print/models/{slug}/choices', {
            params: { path: { slug } },
            body: {
              printer_id: printer_id ?? null,
              filament_plan: filament_plan ?? [],
              nozzles: nozzles ?? [],
              tier: tier ?? null,
              process_name: process_name ?? null,
            },
          }),
          `remember choices for ${slug}`,
        ),
      ),
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
    summarize: ({ output_id }) => `Send output ${output_id} to Bambuddy's library`,
    handler: async ({ output_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/outputs/{output_id}/send', {
            params: { path: { output_id } },
            body: { mode: 'library' },
          }),
          `send ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'print_output',
    description:
      'Print an output, spool-first: slice with presets the backend derives from the chosen spools, nozzles, ' +
      'quality and plate, then queue it on one printer, behind one approval. Any choice left out is filled ' +
      "the way the print dialog opens: the chosen printer, this model's remembered nozzles, tier or process " +
      "and spools (else 0.4 mm standard, the Standard tier and the suggested spools), and the printer's " +
      'preselected plate type. A choice the backend cannot resolve (mixed nozzle sizes, a slot with no ' +
      'spool or preset) is refused before anything is sliced. `project_id` files the print under a Bambuddy ' +
      'project: omit it for the remembered project (`last_project_id`), or pass null for "No project". ' +
      'The run slices and queues in the background: this waits for it and answers with the run and its ' +
      '`result` (warnings, queue item ids), or hands back the still-running run to poll with ' +
      'get_print_run. Then follow the print with get_print_progress.',
    input: z.object({
      output_id: outputId,
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
    }),
    risk: 'outward',
    bambuddyScope: ['Read Status', 'Manage Library', 'Manage Queue'],
    routes: ['POST /api/v1/print/outputs/{output_id}/run', 'GET /api/v1/print/runs/{run_id}'],
    summarize: (args) => {
      const { output_id, printer_id, copies, plate_id, all_plates, nozzles, tier, process_name } = args
      const defaulted =
        nozzles === undefined ||
        (tier === undefined && process_name === undefined) ||
        args.filament_plan === undefined ||
        args.bed_type === undefined
      return (
        `Print output ${output_id}: ${copies ?? 1} cop${(copies ?? 1) === 1 ? 'y' : 'ies'} of ${
          all_plates ? 'every plate' : `plate ${plate_id}`
        }` +
        `${nozzles?.[0] ? ` with a ${nozzles[0].size} mm nozzle` : ''}` +
        `${process_name ? `, process "${process_name}"` : tier ? `, ${tier} quality` : ''}` +
        `${printer_id !== undefined ? ` on printer ${printer_id}` : ''}` +
        `${defaulted ? ' (other choices as the print dialog opens)' : ''}`
      )
    },
    handler: async (args, ctx) => {
      const { backend } = ctx
      const path = { output_id: args.output_id }
      let { printer_id: printerId, nozzles: chosenNozzles, bed_type: bedType } = args
      let slots = args.filament_plan?.slots
      let chosenTier: z.infer<typeof tier> | null | undefined = args.tier
      let processName: string | null | undefined = args.process_name
      if (processName !== undefined) chosenTier = null
      // Fill what was left out the way the dialog does (frontend PrintPicker
      // `seedDialog`, spool-first spec §7), from the one read the dialog opens
      // on: GET /print/outputs/{id}/choices. /run takes no defaults of its own
      // for nozzles, spools or plate, so an omitted choice must be made here.
      if (
        printerId === undefined ||
        chosenNozzles === undefined ||
        bedType === undefined ||
        slots === undefined ||
        (chosenTier === undefined && processName === undefined)
      ) {
        const view = await ok(
          backend.GET('/api/v1/print/outputs/{output_id}/choices', { params: { path, query: { printer_id: printerId } } }),
          `get print choices for ${args.output_id}`,
        )
        printerId ??= view.printer_id ?? undefined
        bedType ??= view.bed_type
        const last = view.model_choices
        const remembered = last?.nozzles ?? []
        if (chosenNozzles === undefined) chosenNozzles = remembered.length > 0 ? remembered : DEFAULT_NOZZLES
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
              : await ok(
                  backend.GET('/api/v1/print/outputs/{output_id}/filaments', {
                    params: {
                      path,
                      query: args.all_plates
                        ? { printer_id: printerId, all_plates: true }
                        : { printer_id: printerId, plate_id: args.plate_id },
                    },
                  }),
                  `get filaments for ${args.output_id}`,
                )
          slots = seedPlan(filaments, last?.filament_plan ?? [])
        }
      }
      // One per call (#470): a call is a deliberate print, so the same choices again are
      // a new one rather than the last call's run. Every re-send below reuses it, so a
      // POST whose answer was lost re-attaches to its run instead of printing twice.
      const requestId = randomUUID()
      const started = await reattach(
          ctx,
          () => backend.POST('/api/v1/print/outputs/{output_id}/run', {
            params: { path },
            signal: ctx.signal,
            body: {
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
              request_id: requestId,
            },
          }),
          `print ${args.output_id}`,
          " The print may still have started: check Bambuddy's queue before printing again.",
        )
      return runOutcome(await waitForRun(ctx, started))
    },
  }),

  defineTool({
    name: 'create_print_project',
    description:
      'Create a Bambuddy project (with its library folder), or link an existing one by `project_id`. ' +
      '`parent_id` nests a new project under an existing one, and its folder under the parent\'s folder.',
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
    handler: async (args, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print/projects', {
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
          'create project',
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
    handler: async ({ output_id, project_id }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/outputs/{output_id}/project-file', {
            params: { path: { output_id } },
            body: { project_id },
          }),
          `file ${output_id} in project ${project_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'file_output_under_project',
    description:
      "File an output's queue entries (and any finished prints' archives) under a Bambuddy project.",
    input: z.object({
      output_id: outputId,
      project_id: z.number().int().optional(),
      queue_item_ids: z.array(z.number().int()).default([]),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Projects'],
    routes: ['POST /api/v1/print/outputs/{output_id}/project'],
    summarize: ({ output_id, project_id }) =>
      `File output ${output_id}'s prints under Bambuddy project ${project_id ?? '(its own)'}`,
    handler: async ({ output_id, project_id, queue_item_ids }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print/outputs/{output_id}/project', {
            params: { path: { output_id } },
            body: { ...(project_id === undefined ? {} : { project_id }), queue_item_ids },
          }),
          `file ${output_id} under a project`,
        ),
      ),
  }),

]

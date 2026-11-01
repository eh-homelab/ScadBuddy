import { randomUUID } from 'node:crypto'
import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { ACCEPTING_MS, command, reattach, reattachFollowing, RESPOND_ASYNC } from './command.js'
import { binary } from './binary.js'
import type { BackendClient } from '../api/backend.js'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { type ApprovalNeeds, defineTool, idempotencyKey, image, json, type Tool, type ToolContext, ToolError, toolErrorText } from './registry.js'
import { page, PAGED, pageInput } from './pagination.js'
import { uploadInit, uploadName, withSent, workspaceFile, workspacePathInput, workspaceShaInput } from './workspaceUploads.js'
import { errorName } from '../telemetry/scrub.js'

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
//   printer's current camera frame (get_printer_camera, #796). The queue, the
//   print archive, aggregate stats and the whole spool inventory are farm.ts
//   (#1912).
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

/** #1899 — a print's supports, as its approval line names them. */
function supportsPhrase(
  supports: { enabled: boolean; type?: string; threshold_angle?: number; on_build_plate_only?: boolean } | undefined,
): string {
  if (supports === undefined) return ''
  if (!supports.enabled) return ', without supports'
  return (
    (supports.type === undefined ? ', with supports' : `, with ${supports.type} supports`) +
    `${supports.threshold_angle === undefined ? '' : ` at ${supports.threshold_angle}°`}` +
    `${supports.on_build_plate_only === true ? ' on the build plate only' : supports.on_build_plate_only === false ? ', from the model too' : ''}`
  )
}

function towerPhrase(tower: { enabled: boolean; no_sparse_layers?: boolean } | undefined): string {
  if (tower === undefined) return ''
  if (!tower.enabled) return ', without a prime tower'
  return (
    ', with a prime tower' +
    (tower.no_sparse_layers === true ? ' (no sparse layers)' : tower.no_sparse_layers === false ? ' (sparse layers too)' : '')
  )
}

/** The folder a library move or copy names, in its approval line (#2670). */
function folderName(folderId: number | undefined, folderPath: string | undefined): string {
  if (folderId !== undefined) return `library folder ${folderId}`
  if (folderPath?.replaceAll('/', '').trim()) return `library folder ${JSON.stringify(folderPath)}`
  return "the library's top level"
}

type PrinterQuery = { printer_id?: number | undefined; printer?: string | undefined }
type FilamentQuery = PrinterQuery & { plate_id?: number | undefined; all_plates?: boolean | undefined }

/**
 * A registry printer's `id` (list_printers, get_printers_now), as every dialog read and the
 * run take it (#3823): on a row a run reaches over the LAN (#4646) the reads are the
 * native path's, its trays for spools, and ask Bambuddy nothing.
 */
const registryPrinter = z.string().uuid()

function getChoices(ctx: ToolContext, source: Source, query: PrinterQuery) {
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

/**
 * #4660 — whether a linked registry row prints over the LAN, as the backend decides it
 * (`lan_printers` in the choices, the server's `hot_path_native`), never the tool's guess.
 */
async function linkedNative(ctx: ToolContext, source: Source, printer: string, printerId: number | undefined): Promise<boolean> {
  const view = await getChoices(ctx, source, { printer_id: printerId, printer })
  return view.lan_printers?.includes(printer) ?? false
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

/** #4660 — what a `follow` handle's names are, said where a tool returns one. */
const FOLLOW_NAMES =
  "A `follow` handle's `subtask_names` are the names the printer started the plates under, as its screen " +
  'shows them, for telling the plates apart: they are not an input of get_print_progress, which takes only ' +
  'the `output_id` or `library_file_id`, and filter nothing.'

/**
 * #4646 — a run that printed over the LAN (`result.path` `native`) queued nothing on
 * Bambuddy: its result carries no queue item ids or Bambuddy URL, and the print is
 * followed by its subject's progress, which reads the LAN report (`FollowPrint`), the
 * plates by the `subtask_name` each started under. A Bambuddy run is returned as it is.
 */
export function followed(run: PrintRun) {
  const result = run.result
  if (run.status !== 'succeeded' || result?.path !== 'native') return run
  const { queue_item_ids: _queued, bambuddy_url: _url, ...rest } = result
  // `output_id` is the run's subject as clients read it: an output's id, or `library:<file id>`.
  const library = run.output_id.startsWith('library:')
  return {
    ...run,
    result: rest,
    path: 'native' as const,
    follow: {
      tool: 'get_print_progress',
      ...(library ? { library_file_id: Number(run.output_id.slice('library:'.length)) } : { output_id: run.output_id }),
      subtask_names: (rest.lan ?? []).map((plate) => plate.subtask_name),
    },
  }
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
  return json(followed(run))
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
export const presetRef = z.object({
  source: z.enum(['orca_cloud', 'cloud', 'local', 'standard']),
  id: z.string().min(1),
})
/** `NozzleChoice`: one extruder's nozzle. One size for the job (spool-first spec §2 step 3). */
const nozzleChoice = z.object({
  size: z.enum(['0.2', '0.4', '0.6', '0.8']),
  flow: z.enum(['standard', 'high_flow']).default('standard'),
})
export const nozzles = z.array(nozzleChoice).min(1).max(2)
export const tier = z.enum(['fine', 'standard', 'draft'])

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

const PRINT_OUTPUT_INPUT = withSource({
  printer_id: z.number().int().optional(),
  printer: registryPrinter
    .optional()
    .describe(
      "A registry printer's `id` (list_printers, get_printers_now), in place of `printer_id`: the run prints " +
        'on the Bambuddy printer it links and records the printer by this id. A row that links no Bambuddy ' +
        'printer prints over the LAN (#4646); send no `printer_id` beside it.',
    ),
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
  project_id: nullable(z.number().int()).describe('Omit for the default project; null for "No project"'),
  folder_id: z.number().int().optional().describe('A library folder of no project, in place of a project'),
  options: printOptions,
  print_sequence: z
    .enum(['by layer', 'by object'])
    .optional()
    .describe("'by object' finishes each object before the next starts; omit for the template's own"),
  filament_profiles: z
    .array(z.string().max(64))
    .max(8)
    .optional()
    .describe(
      'Filament profiles to apply, by id (get_filament_profiles), e.g. silk-pla for a silk PLA spool. ' +
        "Never applied unless named here. One that cannot apply to this print is skipped, never refused: " +
        "the run's warnings say so (profile-not-applied) and it prints with the preset as it is.",
    ),
  supports: z
    .object({
      enabled: z
        .boolean()
        .describe('false prints with no supports, and the other fields are then not sent; true adds them over the template'),
      type: z.enum(['tree', 'normal']).optional().describe("'tree' or 'normal'; omit for the template's, else tree"),
      threshold_angle: z
        .number()
        .int()
        .min(0)
        .max(90)
        .optional()
        .describe('Degrees from vertical; omit for the template\'s, else the preset\'s'),
      on_build_plate_only: z.boolean().optional().describe("Omit for the template's, else the preset's"),
    })
    .optional()
    .describe(
      'Support structures for this print (#1899), over what the template declares. Omit to keep the ' +
        "template's. A setting the slicer's preset cannot take is left out with a warning on the result; never a refusal.",
    ),
  prime_tower: z
    .object({
      enabled: z.boolean().describe('false prints with no prime (purge) tower; true prints one when the plate changes filament'),
      no_sparse_layers: z
        .boolean()
        .optional()
        .describe(
          "With the tower on, print it only on layers that change filament (Bambu Studio's wipe_tower_no_sparse_layers); omit for the template's, else off",
        ),
    })
    .optional()
    .describe(
      "The prime tower for this print (#3628), over what the template declares. Omit to keep the template's, " +
        'else the process (on whenever a plate uses more than one filament).',
    ),
})
type PrintOutputArgs = z.infer<typeof PRINT_OUTPUT_INPUT>

/** #4646 — a print over the LAN is filed in a native library folder, never a Bambuddy project. */
function refuseProject(name: string, args: PrintOutputArgs): void {
  if (args.project_id === undefined || args.project_id === null) return
  throw new ToolError(
    `${name} prints over the LAN, so the print has no Bambuddy project: omit project_id. ` +
      "folder_id may name one of ScadBuddy's own library folders (list_print_projects with native: true).",
  )
}

/**
 * The Bambuddy printer a print_output call prints on, and its name, resolved as the backend
 * resolves it (#4442). A registry `printer` must still link the `printer_id` sent beside it,
 * or the call is refused: the backend would print on `printer_id`'s printer instead. A row
 * that links no Bambuddy printer is `lan` (#4646): it prints over the LAN with `printer_id`
 * null, as the dialog prints it, so a `printer_id` or a project beside it is refused.
 */
async function printerOf(
  args: PrintOutputArgs,
  backend: BackendClient,
  signal: AbortSignal,
  needs: ApprovalNeeds = { summary: true },
): Promise<{ printerId?: number; name?: string; lan?: true }> {
  if (args.printer !== undefined) {
    const row = await ok(
      backend.GET('/api/v1/printers/{printer_id}', { params: { path: { printer_id: args.printer } }, signal }),
      'read the printer',
    )
    if (row.bambuddy_printer_id == null) {
      if (args.printer_id !== undefined) {
        throw new ToolError(
          `${row.name} links no Bambuddy printer, so it is not printer_id ${args.printer_id}: it prints over ` +
            'the LAN. Send only `printer`.',
        )
      }
      refuseProject(row.name, args)
      return { name: row.name, lan: true }
    }
    if (args.printer_id !== undefined && args.printer_id !== row.bambuddy_printer_id) {
      throw new ToolError(
        `${row.name} is Bambuddy printer ${row.bambuddy_printer_id}, not printer_id ${args.printer_id}. ` +
          'Send only one of them, or the pair list_printers shows now.',
      )
    }
    return { printerId: row.bambuddy_printer_id, name: row.name }
  }
  if (args.printer_id === undefined) return {}
  // Only for the approval's line (#4442 review: not read when only the input is wanted): a
  // printer the registry does not name yet is said by its id.
  if (!needs.summary) return { printerId: args.printer_id }
  const { data } = await backend.GET('/api/v1/printers', { signal }).catch(() => ({ data: undefined }))
  return { printerId: args.printer_id, name: data?.find((row) => row.bambuddy_printer_id === args.printer_id)?.name }
}

/** print_output's approval line; `printerName` is the name of the printer that prints, when read. */
function printSummary(args: PrintOutputArgs, printerName?: string, lan = false): string {
  const { printer_id, printer, copies, plate_id, all_plates, nozzles, tier, process_name } = args
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
    `${printerName !== undefined ? ` on printer ${JSON.stringify(printerName)}` : printer_id !== undefined ? ` on printer ${printer_id}` : printer !== undefined ? ` on printer ${printer}` : ''}` +
    `${lan ? ' over the LAN' : ''}` +
    `${args.filament_profiles?.length ? `, with the ${args.filament_profiles.join(', ')} profile` : ''}` +
    `${supportsPhrase(args.supports)}` +
    `${towerPhrase(args.prime_tower)}` +
    `${defaulted ? ' (other choices as the print dialog opens)' : ''}`
  )
}

export const printTools: Tool[] = [
  // ── read: farm context and planning ─────────────────────────────────────
  defineTool({
    name: 'get_print_choices',
    label: 'Read what the print dialog offers',
    description:
      'Everything the print dialog offers for an output, or a Bambuddy library file (`library_file_id`), in ' +
      'one read: printers (and the one chosen), the installed nozzles, quality tiers and Bambu processes per ' +
      'nozzle size, filament presets per size, plate types with the one last printed on, the filament step ' +
      "(as get_print_filaments), the model's (or the file's) remembered choices, and its plates by index and " +
      'name (what each holds; `null` when nothing names it; empty, with `plates_error` saying why, when ' +
      'they cannot be read: then report that rather than assume plate 1), which ' +
      'print_output takes as `plate_id`. What ' +
      'print_output fills omitted choices from.',
    input: withSource({
      printer_id: z.number().int().optional(),
      printer: registryPrinter
        .optional()
        .describe(
          "A registry printer's `id` (list_printers), the one way to name a printer only the LAN reaches: " +
            'on a printer a run reaches over the LAN (`lan_printers` lists them) the filament step is its ' +
            'loaded trays, each a negative `spool_id` print_output takes in `filament_plan`.',
        ),
    }),
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
      // #986 — a plate by what it holds, so the agent can say "the lid", not "plate 2". The plates
      // are extra: an unreadable list is empty, as in the dialog, and never fails the choices. But
      // it says why (#2107): "no plates" and "a missing Bambuddy scope" are not the same answer.
      let platesError: string | undefined
      const [choices, plates] = await Promise.all([
        getChoices(ctx, source, { printer_id: args.printer_id, printer: args.printer }),
        getPlates(ctx, source).catch((err: unknown) => {
          platesError = err instanceof ToolError ? toolErrorText(err, 'get_print_choices') : String(err)
          // #1814: the error's name and status, never its message, which can quote a connection string (telemetry/scrub.ts).
          console.warn(`get_print_choices: the plates could not be read (${errorName(err)})`)
          return []
        }),
      ])
      return json({
        ...choices,
        plates: plates.map(({ index, name }) => ({ index, name: name || null })),
        ...(platesError === undefined ? {} : { plates_error: platesError }),
      })
    },
  }),

  defineTool({
    name: 'get_print_filaments',
    label: "Read the print dialog's filaments",
    description:
      "Bambuddy's spool inventory joined to where each spool is loaded and, with `printer_id`, the " +
      'remaining grams per slot and the mounted nozzles — plus what this output\'s (or library file\'s) plate ' +
      '(or, with `all_plates`, every plate) needs and a suggested spool per slot.',
    input: withSource({
      printer_id: z.number().int().optional(),
      printer: registryPrinter
        .optional()
        .describe(
          "A registry printer's `id` (list_printers): on a printer a run reaches over the LAN the spools are " +
            "its loaded trays, each a negative `spool_id`, with the spool providers' records over them.",
        ),
      plate_id: z.number().int().min(1).optional(),
      all_plates: z.boolean().optional(),
    }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Read Status', 'Manage Library'],
    routes: ['GET /api/v1/print/outputs/{output_id}/filaments', 'GET /api/v1/print/library/{file_id}/filaments'],
    handler: async ({ printer_id, printer, plate_id, all_plates, ...source }, ctx) =>
      json(await getFilaments(ctx, sourceOf(source), { printer_id, printer, plate_id, all_plates })),
  }),

  defineTool({
    name: 'get_filament_profiles',
    label: 'List filament profiles',
    description:
      'Filament profiles (#717): named process changes the print dialog suggests for a spool, e.g. Silk PLA\'s ' +
      'slower outer wall. Each lists which spools it is for (`match`: the material, and a word in the spool\'s ' +
      'subtype or slicer preset), the process settings it changes, each with its cited sources, the filament ' +
      'settings it sets in the matching slots\' filament presets (`filament_settings`, e.g. Silk\'s 235 °C ' +
      'nozzle; applied by the native slicer only, and on Bambuddy\'s the run warns profile-not-applied), and ' +
      'advice it cannot apply (`notes`). Apply one with print_output\'s `filament_profiles`, only when the ' +
      'user wants it.',
    input: z.object({}),
    risk: 'read',
    source: 'ScadBuddy\'s built-in profiles, quoting the pages they cite',
    routes: ['GET /api/v1/print/filament-profiles'],
    handler: async (_, { backend }) =>
      json(await ok(backend.GET('/api/v1/print/filament-profiles'), 'list the filament profiles')),
  }),

  defineTool({
    name: 'get_print_progress',
    label: "Check a print's progress",
    description:
      'How the last print of an output, or of a Bambuddy library file (`library_file_id`), is going, per copy; ' +
      'null when it was never printed. Poll until `settled` is true. A run over the LAN with prints still to ' +
      'start names them in `lan_run` (`run_id`, `waiting`, and `awaiting_clear` while it waits for the plate ' +
      'to be cleared): stop_print_run stops them, confirm_plate_clear starts the next.',
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
    label: "See a printer's camera",
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
    label: 'Check a print run',
    description:
      'A print run print_output started: `running` while it slices and queues, then `succeeded` with its ' +
      '`result` (warnings, queue item ids, the Bambuddy URL) or `failed` with the `error` that stopped it. ' +
      'A run that printed over the LAN (`path: "native"`) has no queue item ids: its `follow` names the ' +
      'get_print_progress call that follows it. ' +
      FOLLOW_NAMES,
    input: z.object({ run_id: runId }),
    risk: 'read',
    routes: ['GET /api/v1/print/runs/{run_id}'],
    handler: async ({ run_id }, ctx) => json(followed(await getRun(ctx, run_id))),
  }),

  // #4942, #4939: a native run's prints still to start. get_print_progress names them
  // (`lan_run`), as the dialog's panel and the Prints page card do.
  defineTool({
    name: 'stop_print_run',
    label: 'Stop the rest of a LAN print run',
    description:
      'Stops the prints a run over the LAN (`path: "native"`) has not started yet: a run of several plates or ' +
      "copies starts each once the printer has ended the one before (`lan_waiting`). The print on the printer " +
      'carries on; one being started as the stop arrives still starts. The run then records why the rest never ' +
      'start (`lan_stopped`). get_print_progress names a run that has prints still to start (`lan_run`). 409 when ' +
      'it has none left to stop.',
    input: z.object({ run_id: runId }),
    risk: 'outward',
    routes: ['POST /api/v1/print/runs/{run_id}/stop'],
    summarize: ({ run_id }) => `Stop the prints print run ${run_id} has not started yet`,
    handler: async ({ run_id }, { backend }) =>
      json(
        followed(
          await ok(
            backend.POST('/api/v1/print/runs/{run_id}/stop', { params: { path: { run_id } } }),
            `stop the rest of print run ${run_id}`,
          ),
        ),
      ),
  }),

  defineTool({
    name: 'confirm_plate_clear',
    label: 'Confirm the plate is clear',
    description:
      'Tells a run over the LAN that the printer\'s plate is clear, so it starts its next print. With "Wait for ' +
      'a cleared plate" on in Settings, such a run of several prints waits after each until someone says so ' +
      '(`lan_awaiting_clear`, and get_print_progress `lan_run.awaiting_clear`). Only call it once the user has ' +
      'said the plate is clear: the next print starts on whatever is on the bed. 409 when the run is not waiting.',
    input: z.object({ run_id: runId }),
    risk: 'outward',
    routes: ['POST /api/v1/print/runs/{run_id}/plate-cleared'],
    summarize: ({ run_id }) => `Say the plate is clear, so print run ${run_id} starts its next print`,
    handler: async ({ run_id }, { backend }) =>
      json(
        followed(
          await ok(
            backend.POST('/api/v1/print/runs/{run_id}/plate-cleared', { params: { path: { run_id } } }),
            `confirm the plate is clear for print run ${run_id}`,
          ),
        ),
      ),
  }),

  defineTool({
    name: 'list_print_projects',
    label: 'List print projects',
    description:
      "Bambuddy's projects, to file prints under, and the default project (`last_project_id`), which only " +
      'the user changes, in Settings; also `folders`, the library folders of no project a print or ' +
      'file_output_in_project_folder may go to instead (`folder_id`), each with its `path`; and `sort`, how ' +
      'the picker orders them. get_default_print_project says where some content goes by default. With ' +
      "`native`, the destinations of a print over the LAN: ScadBuddy's own library folders, no projects, and " +
      'Bambuddy is not asked.' +
      PAGED,
    input: z.object({ native: z.boolean().optional(), ...pageInput }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Manage Projects'],
    routes: ['GET /api/v1/print/projects'],
    handler: async (args, { backend }) => {
      const { last_project_id, projects, folders, sort } = await ok(
        backend.GET('/api/v1/print/projects', { params: { query: { native: args.native } } }),
        'list projects',
      )
      const { items, ...rest } = page(projects ?? [], args, (p) => String(p.id), 'list_print_projects')
      return json({ last_project_id, sort, projects: items, folders: folders ?? [], ...rest })
    },
  }),

  defineTool({
    name: 'get_default_print_project',
    label: 'Read the default print project',
    description:
      'Where a print (or Arrange, or Generate) of some outputs and library files is filed by default: the one ' +
      'project they are all in already (`reason: "content"`), else the Settings default (`"settings"`), else ' +
      'none (`"none"`). Pass it to print_output as `project_id` to file ' +
      'the print as the print dialog would.',
    input: z.object({
      output_ids: z.array(outputId).max(100).optional(),
      library_file_ids: z.array(libraryFileId).max(100).optional(),
    }),
    risk: 'read',
    source:
      'Bambuddy data (printer, project, spool and archive names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Manage Projects', 'Manage Library'],
    routes: ['GET /api/v1/print/projects/default'],
    handler: async ({ output_ids, library_file_ids }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/projects/default', {
            params: { query: { output_id: output_ids, library_file_id: library_file_ids } },
          }),
          'read the default project',
        ),
      ),
  }),

  defineTool({
    name: 'list_library',
    label: 'Browse the Bambuddy library',
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

  // #3630 — a file found by name, without walking the folders one at a time.
  defineTool({
    name: 'search_library',
    label: 'Search the Bambuddy library',
    description:
      "Find files in Bambuddy's library by name, in every folder at once: each word of `query` must appear (as " +
      'a substring) in ' +
      "the file's name or its folder path, whatever the case or separator. Each file names its `folder_path`. " +
      'Without `all` only unsliced 3MFs (`hidden` counts the other matches); with it every file. At most 100 ' +
      'files; `total` says how many matched. The library is read whole and kept for 30 s: after changing it ' +
      '(delete_library_files, a move, copy or rename) pass `fresh: true` so the search sees the change.',
    input: z.object({ query: z.string().min(1).max(200), all: z.boolean().optional(), fresh: z.boolean().optional() }),
    risk: 'read',
    source: 'Bambuddy data (folder and file names) that anyone with access to Bambuddy can write',
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/print/library/search'],
    handler: async ({ query, all, fresh }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/library/search', { params: { query: { q: query, all, fresh } } }),
          'search the library',
        ),
      ),
  }),

  // #2167 — Bambuddy's library writes: a delete to its trash, and the restore that undoes it.
  defineTool({
    name: 'delete_library_files',
    label: 'Delete Bambuddy library files',
    description:
      "Delete files from Bambuddy's library (ids from list_library), moving them to Bambuddy's trash. A file " +
      'linked from an external folder (`is_external`) is removed for good. Bambuddy may skip a file its API ' +
      "key's user did not add: those are listed in `skipped`, not deleted. A file a print waiting in " +
      "Bambuddy's queue names is refused. restore_library_files undoes a delete.",
    input: z.object({ file_ids: z.array(libraryFileId).min(1).max(500) }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/delete'],
    summarize: ({ file_ids }) =>
      `Delete ${file_ids.length === 1 ? `library file ${file_ids[0]}` : `${file_ids.length} library files`} to Bambuddy's trash`,
    handler: async ({ file_ids }, ctx) =>
      json(
        await command(ctx, 'delete library files', (headers) =>
          ctx.backend.POST('/api/v1/print/library/delete', { body: { file_ids }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'restore_library_files',
    label: 'Restore Bambuddy library files',
    description:
      "Restore deleted files from Bambuddy's trash, under their own ids: a delete_library_files undone. A file " +
      'not in the trash (an external one, or one emptied from it) is listed in `skipped`.',
    input: z.object({ file_ids: z.array(libraryFileId).min(1).max(500) }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/restore'],
    summarize: ({ file_ids }) =>
      `Restore ${file_ids.length === 1 ? `library file ${file_ids[0]}` : `${file_ids.length} library files`} from Bambuddy's trash`,
    handler: async ({ file_ids }, ctx) =>
      json(
        await command(ctx, 'restore library files', (headers) =>
          ctx.backend.POST('/api/v1/print/library/restore', { body: { file_ids }, headers }),
        ),
      ),
  }),

  // #2686: the writes the Bambuddy filesystem mount makes.
  defineTool({
    name: 'upload_library_file',
    label: 'Upload a file to the Bambuddy library',
    description:
      "Upload a file from your workspace into Bambuddy's library, at the top level or in `folder_id`. Give " +
      '`workspace_path` with its `workspace_sha256`; save a file there first with workspace_save, http_request ' +
      '`save_to_workspace` or workspace_write. The library name is `filename`, by default the workspace file\'s own ' +
      'name. Returns the file\'s id, name and folder.',
    input: z.object({
      folder_id: z.number().int().positive().optional().describe('A folder id from list_library; omitted is the top level'),
      filename: z.string().min(1).optional().describe('e.g. "cube.3mf"'),
      workspace_path: workspacePathInput,
      workspace_sha256: workspaceShaInput,
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/files'],
    summarize: ({ filename, workspace_path, folder_id }) =>
      `Upload ${filename ?? workspace_path} to Bambuddy's library folder ${folder_id ?? 'top level'}`,
    handler: async ({ folder_id, filename, workspace_path, workspace_sha256 }, ctx) => {
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'upload_library_file')
      const name = filename ?? uploadName(info.path)
      const answer = await command(ctx, `upload ${name}`, (headers) =>
        ctx.backend.POST('/api/v1/print/library/files', {
          body: { file: name, folder_id: folder_id ?? null },
          ...uploadInit(
            ctx,
            info,
            { name: 'file', filename: name, type: 'application/octet-stream' },
            headers,
            folder_id === undefined ? {} : { folder_id: String(folder_id) },
          ),
        }),
      )
      return json(withSent(answer, info))
    },
  }),

  defineTool({
    name: 'update_library_file',
    label: 'Rename or move a Bambuddy library file',
    description:
      "Rename a file in Bambuddy's library or move it to another folder (ids from list_library). Give " +
      '`filename`, `folder_id` or both; `folder_id` 0 moves it to the top level.',
    input: z.object({
      file_id: libraryFileId,
      filename: z.string().min(1).optional(),
      folder_id: z.number().int().min(0).optional().describe('0 is the top level'),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['PATCH /api/v1/print/library/{file_id}'],
    summarize: ({ file_id }) => `Rename/move library file ${file_id}`,
    handler: async ({ file_id, filename, folder_id }, ctx) =>
      json(
        await command(ctx, `update library file ${file_id}`, (headers) =>
          ctx.backend.PATCH('/api/v1/print/library/{file_id}', {
            params: { path: { file_id } },
            body: { filename, folder_id },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'create_library_folder',
    label: 'Create a Bambuddy library folder',
    description: "Make a folder in Bambuddy's library, at the top level or inside `parent_id`.",
    input: z.object({
      name: z.string().min(1),
      parent_id: z.number().int().positive().optional().describe('A folder id from list_library; omitted is the top level'),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/folders'],
    summarize: ({ name }) => `Create library folder ${name}`,
    handler: async ({ name, parent_id }, ctx) =>
      json(
        await command(ctx, `create library folder ${name}`, (headers) =>
          ctx.backend.POST('/api/v1/print/library/folders', { body: { name, parent_id }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'update_library_folder',
    label: 'Rename or move a Bambuddy library folder',
    description:
      "Rename a folder in Bambuddy's library or move it under another folder. Give `name`, `parent_id` or both; " +
      '`parent_id` 0 moves it to the top level.',
    input: z.object({
      folder_id: z.number().int().positive(),
      name: z.string().min(1).optional(),
      parent_id: z.number().int().min(0).optional().describe('0 is the top level'),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['PATCH /api/v1/print/library/folders/{folder_id}'],
    summarize: ({ folder_id }) => `Rename/move library folder ${folder_id}`,
    handler: async ({ folder_id, name, parent_id }, ctx) =>
      json(
        await command(ctx, `update library folder ${folder_id}`, (headers) =>
          ctx.backend.PATCH('/api/v1/print/library/folders/{folder_id}', {
            params: { path: { folder_id } },
            body: { name, parent_id },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'delete_library_folder',
    label: 'Delete a Bambuddy library folder',
    description:
      "Delete a folder from Bambuddy's library along with its files and subfolders. Managed files go to " +
      "Bambuddy's trash (restore_library_files undoes that); a file linked from an external folder " +
      '(`is_external`) is deleted for good. Answers 409, before anything is deleted, if a print waiting in ' +
      "Bambuddy's queue names a file or Bambuddy's trash holds one of the folder's files, and 403 unless " +
      "the API key's user holds `library:delete_all` (without it the key cannot see other users' trash). " +
      'A file that could not be deleted is listed in `skipped`, and its ' +
      'folder and that folder\'s parents stay.',
    input: z.object({ folder_id: z.number().int().positive() }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/folders/{folder_id}/delete'],
    summarize: ({ folder_id }) => `Delete library folder ${folder_id} and everything in it`,
    handler: async ({ folder_id }, ctx) =>
      json(
        await command(ctx, `delete library folder ${folder_id}`, (headers) =>
          ctx.backend.POST('/api/v1/print/library/folders/{folder_id}/delete', {
            params: { path: { folder_id } },
            headers,
          }),
        ),
      ),
  }),

  // #2670 — reorganising the library: a move and a rename keep each file's id; a copy is a new file.
  defineTool({
    name: 'move_library_file',
    label: 'Move Bambuddy library files',
    description:
      "Move files in Bambuddy's library (ids from list_library) into a folder: `folder_id`, or `folder_path` " +
      "(folder names from the top, joined by '/'), or neither for the top level. Each keeps its id, so its " +
      'print history, slices and remembered print choices stay with it. A file that leaves a project\'s folder ' +
      'names that project in `left_project`: the project no longer lists it, so say so. Bambuddy may skip a ' +
      "file its API key's user did not add: those are listed in `skipped`, not moved. A file moved into the " +
      "inbox (Settings' library folder) stops being tracked as an output's copy, so ScadBuddy never deletes it; " +
      'the next send uploads a fresh copy.',
    input: z.object({
      file_ids: z.array(libraryFileId).min(1).max(500),
      folder_id: z.number().int().positive().optional().describe('The folder to move into (list_library)'),
      folder_path: z.string().max(1024).optional().describe("The folder by path, e.g. 'Projects/Lamp', in place of folder_id"),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/move'],
    summarize: ({ file_ids, folder_id, folder_path }) =>
      `Move ${file_ids.length === 1 ? `library file ${file_ids[0]}` : `${file_ids.length} library files`} to ${folderName(folder_id, folder_path)}`,
    handler: async ({ file_ids, folder_id, folder_path }, ctx) =>
      json(
        await command(ctx, 'move library files', (headers) =>
          ctx.backend.POST('/api/v1/print/library/move', { body: { file_ids, folder_id, folder_path }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'copy_library_file',
    label: 'Copy Bambuddy library files',
    description:
      "Copy files in Bambuddy's library (ids from list_library) into a folder: `folder_id`, or `folder_path` " +
      "(folder names from the top, joined by '/'), or neither for the top level. Each copy is a new file with " +
      'its own id (`copied[].id`), the same name, bytes and notes. A file too large to copy, or one Bambuddy ' +
      'failed on (and any after it), is listed in `skipped`; the copies made before it are still in `copied`.',
    input: z.object({
      file_ids: z.array(libraryFileId).min(1).max(500),
      folder_id: z.number().int().positive().optional().describe('The folder to copy into (list_library)'),
      folder_path: z.string().max(1024).optional().describe("The folder by path, e.g. 'Projects/Lamp', in place of folder_id"),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/copy'],
    summarize: ({ file_ids, folder_id, folder_path }) =>
      `Copy ${file_ids.length === 1 ? `library file ${file_ids[0]}` : `${file_ids.length} library files`} to ${folderName(folder_id, folder_path)}`,
    handler: async ({ file_ids, folder_id, folder_path }, ctx) =>
      json(
        await command(ctx, 'copy library files', (headers) =>
          ctx.backend.POST('/api/v1/print/library/copy', { body: { file_ids, folder_id, folder_path }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'rename_library_file',
    label: 'Rename a Bambuddy library file',
    description:
      "Rename a file in Bambuddy's library (an id from list_library), keeping its id. The new name keeps the " +
      "file's extension ('.3mf', or '.gcode.3mf' for a sliced file); Bambuddy refuses a name a printer's SD card " +
      'would not take.',
    input: z.object({ file_id: libraryFileId, filename: z.string().min(1).max(255) }),
    risk: 'outward',
    bambuddyScope: ['Manage Library'],
    routes: ['POST /api/v1/print/library/rename'],
    summarize: ({ file_id, filename }) => `Rename library file ${file_id} to ${JSON.stringify(filename)}`,
    handler: async ({ file_id, filename }, ctx) =>
      json(
        await command(ctx, 'rename a library file', (headers) =>
          ctx.backend.POST('/api/v1/print/library/rename', { body: { file_id, filename }, headers }),
        ),
      ),
  }),

  // ── write: preferences the print dialog remembers (ScadBuddy-local, re-settable) ──
  defineTool({
    name: 'remember_model_print_choices',
    label: "Remember a model's print choices",
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
    name: 'remember_printer_bed_type',
    label: "Remember a printer's bed type",
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

  // #4969: the print dialog's Advanced rack algorithm, a per-printer starting choice
  // like the plate (rack spec §4). print_output takes it for every print on that printer.
  defineTool({
    name: 'remember_printer_rack_algorithm',
    label: "Remember a printer's rack algorithm",
    description:
      "Remember how a printer's nozzle rack is ranked when a print leaves the hotend to ScadBuddy (#836): " +
      '`least_used` (the default), `oldest_first`, `newest_first`, or `bambuddy` (the printer picks; no ' +
      'nozzle mapping is sent). null forgets it. Every later print on that printer, the dialog\'s and ' +
      "print_output's, ranks by it. Answers the algorithm now in force.",
    input: z.object({
      printer_id: z.number().int(),
      algorithm: z.enum(['least_used', 'oldest_first', 'newest_first', 'bambuddy']).nullable(),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/print/printers/{printer_id}/rack-algorithm'],
    // No `version` (#1216): an unordered save, which leaves the dialog's ordering alone.
    handler: async ({ printer_id, algorithm }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/print/printers/{printer_id}/rack-algorithm', {
            params: { path: { printer_id } },
            body: { algorithm },
          }),
          `remember rack algorithm of printer ${printer_id}`,
        ),
      ),
  }),

  // ── outward: everything that uploads to, creates in or queues on Bambuddy ──
  defineTool({
    name: 'send_to_bambuddy',
    label: 'Send to Bambuddy',
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
    label: 'Print an output',
    description:
      'Print an output, or a file already in Bambuddy\'s library (`library_file_id`, from list_library): the ' +
      'same print, only the 3MF it slices comes from the library. Spool-first: slice with presets the backend ' +
      'derives from the chosen spools, nozzles, quality and plate, then queue it on one printer, behind one ' +
      'approval. Any choice left out is filled ' +
      "the way the print dialog opens: the chosen printer, the model's (or file's) remembered nozzles, tier or " +
      "process and spools (else 0.4 mm with one flow for both sides, High Flow when either side has a High Flow " +
      "nozzle of that size and Standard otherwise, the Standard tier and the suggested spools), and the printer's " +
      'preselected plate type. A choice the backend cannot resolve (mixed nozzle sizes, a slot with no ' +
      'spool or preset) is refused before anything is sliced. `project_id` files the print under a Bambuddy ' +
      'project: omit it for the default project (`last_project_id`), or pass null for "No project". ' +
      '`folder_id` (from list_print_projects\' `folders`) puts the 3MF in a library folder of no project ' +
      'instead; it cannot be sent with a `project_id`. ' +
      '`printer` names a registry printer by its id (list_printers) in place of `printer_id`. ' +
      'On a printer a run reaches over the LAN (a row that links no Bambuddy printer, or one ' +
      "get_print_choices lists in `lan_printers` for that `printer`) the print goes to the printer with no " +
      'Bambuddy call, as the print dialog sends it: spools are its loaded trays, by the negative `spool_id`s ' +
      'get_print_filaments gives them, there is no project (a `project_id` is refused; `folder_id` names one ' +
      "of ScadBuddy's own folders, list_print_projects with `native`) and no queue item. " +
      'The run slices and queues in the background: this waits for it and answers with the run and its ' +
      '`result` (warnings, queue item ids; over the LAN, `path: "native"` and a `follow` handle instead), or ' +
      'hands back the still-running run to poll with get_print_run. Then follow the print with ' +
      'get_print_progress. ' +
      "A `printer` that links a Bambuddy printer is checked against the print's choices (as get_print_choices " +
      'reads them) before the approval and again before the run, to learn whether it prints over the LAN: ' +
      'if that read fails, the call fails with it and nothing is approved or sent. ' +
      FOLLOW_NAMES,
    input: PRINT_OUTPUT_INPUT,
    risk: 'outward',
    bambuddyScope: ['Read Status', 'Manage Library', 'Manage Queue'],
    routes: [
      'POST /api/v1/print/outputs/{output_id}/run',
      'POST /api/v1/print/library/{file_id}/run',
      'GET /api/v1/print/runs/{run_id}',
      'GET /api/v1/printers/{printer_id}',
    ],
    title: (args) => `Print ${sourceName(args)}${args.copies && args.copies > 1 ? ` × ${args.copies}` : ''}`,
    summarize: (args) => printSummary(args),
    // #4442: the approval names the printer that prints, and approves the pair it is: a
    // registry `printer` is sent with the `printer_id` it links, so confirm_action runs
    // exactly what the person saw.
    // A row only the LAN reaches (#4646) is approved as sent: it links no printer_id to add.
    // A linked row the backend prints natively (#4660) is approved as a print over the LAN,
    // and a project its run would refuse is refused before anyone approves it. So every
    // linked row's line reads the choices, with or without a project_id: the "over the
    // LAN" wording needs them too. Only for the line: the run reads the choices again, so
    // an input-only call skips the read. A failed read fails the approval, as a failed
    // printer read does: a line worded for Bambuddy's path could approve a print that then
    // goes over the LAN. The run's own read decides: if the settings change between the
    // two reads, a project the line approved can still be refused when the run starts.
    prepareApproval: async (args, ctx, needs) => {
      const { printerId, name, lan } = await printerOf(args, ctx.backend, ctx.signal, needs)
      const native =
        lan === true ||
        (needs.summary && args.printer !== undefined && (await linkedNative(ctx, sourceOf(args), args.printer, printerId)))
      if (native && !lan) refuseProject(name ?? `printer ${args.printer}`, args)
      const input = args.printer === undefined || lan ? args : { ...args, printer_id: printerId }
      return { input, summary: printSummary(input, name, native) }
    },
    // Its own wait for the print run, after the backend has accepted it.
    waitsMs: (_, ctx) => ACCEPTING_MS + ctx.renderWaitMs,
    handler: async (args, ctx) => {
      const { backend } = ctx
      const source = sourceOf(args)
      let { printer_id: printerId, nozzles: chosenNozzles, bed_type: bedType } = args
      // #3823 — a registry printer prints on the Bambuddy printer it links; both go to the
      // backend. A pair the registry no longer links is refused here (#4442 security
      // review), so the backend never falls back to a printer nobody approved. A row that
      // links none prints over the LAN with `printer_id` null (#4646), as the dialog sends it.
      let lanOnly = false
      let printerName: string | undefined
      if (args.printer !== undefined) {
        const resolved = await printerOf(args, backend, ctx.signal)
        printerId = resolved.printerId
        printerName = resolved.name
        lanOnly = resolved.lan === true
      }
      // Whether the run goes over the LAN: always for a LAN-only row, else as the choices
      // say (`lan_printers`, the server's `hot_path_native`), never the tool's own guess.
      let native = lanOnly
      let slots = args.filament_plan?.slots
      let chosenTier: z.infer<typeof tier> | null | undefined = args.tier
      let processName: string | null | undefined = args.process_name
      if (processName !== undefined) chosenTier = null
      // Fill what was left out the way the dialog does (frontend PrintPicker
      // `seedDialog`, spool-first spec §7), from the one read the dialog opens
      // on: GET /print/{outputs|library}/{id}/choices. /run takes no defaults of its own
      // for nozzles, spools or plate, so an omitted choice must be made here. A linked row
      // reads them even with every choice supplied: only they say whether it prints over
      // the LAN (#4660), and so whether a project is refused here rather than by /run.
      if (
        (args.printer !== undefined && !lanOnly) ||
        (printerId === undefined && !lanOnly) ||
        chosenNozzles === undefined ||
        bedType === undefined ||
        slots === undefined ||
        (chosenTier === undefined && processName === undefined)
      ) {
        const view = await getChoices(ctx, source, { printer_id: printerId, printer: args.printer })
        if (args.printer !== undefined && view.lan_printers?.includes(args.printer)) native = true
        // A LAN-only row's view names no Bambuddy printer, and the run must send none.
        if (!lanOnly) printerId ??= view.printer_id ?? undefined
        bedType ??= view.bed_type
        const last = view.model_choices
        const remembered = last?.nozzles ?? []
        // With nothing remembered, the printer's defaults: one flow for both sides, High
        // Flow when either side has a High Flow nozzle of the size (#1895, #4272). A
        // remembered pair comes back with one flow too.
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
                    ? { printer_id: printerId, printer: args.printer, all_plates: true }
                    : { printer_id: printerId, printer: args.printer, plate_id: args.plate_id },
                )
          slots = seedPlan(filaments, last?.filament_plan ?? [])
        }
      }
      // A linked row the choices say prints natively refuses a project as a LAN-only row does.
      if (native && !lanOnly) refuseProject(printerName ?? `printer ${args.printer ?? printerId}`, args)
      // One per call (#470): a call is a deliberate print, so the same choices again are
      // a new one rather than the last call's run. Every re-send below reuses it, so a
      // POST whose answer was lost re-attaches to its run instead of printing twice. A call
      // with a callKey (a harness tool use, a released turn's decided call) keys it on that,
      // so the same call run again (a re-placed continuation) re-attaches too (spec §7).
      const requestId = ctx.callKey ? idempotencyKey(ctx) : randomUUID()
      const body = {
        printer_id: printerId ?? null,
        ...(args.printer === undefined ? {} : { printer: args.printer }),
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
        // Omitted stays omitted (the remembered project); null is "No project" (#317). Over
        // the LAN there is no project to name (#4646, plan 2026-10-18 D6), so none is sent.
        ...(args.project_id === undefined || native ? {} : { project_id: args.project_id }),
        // #3651 — a library folder in place of a project; the backend refuses both.
        ...(args.folder_id === undefined ? {} : { folder_id: args.folder_id }),
        options: args.options,
        // The same for a library file as for an output (#907, #1756).
        ...(args.print_sequence === undefined ? {} : { print_sequence: args.print_sequence }),
        ...(args.filament_profiles?.length ? { filament_profiles: args.filament_profiles } : {}),
        ...(args.supports === undefined
          ? {}
          : {
              // Only what was asked for: a field left out keeps the template's (#2835 review).
              // With supports off, only that: the backend applies nothing else (#3179).
              supports: !args.supports.enabled
                ? { enabled: false }
                : {
                    enabled: true,
                    ...(args.supports.type === undefined ? {} : { type: args.supports.type }),
                    ...(args.supports.threshold_angle === undefined
                      ? {}
                      : { threshold_angle: args.supports.threshold_angle }),
                    ...(args.supports.on_build_plate_only === undefined
                      ? {}
                      : { on_build_plate_only: args.supports.on_build_plate_only }),
                  },
            }),
        // As given: the backend drops No sparse layers with the tower off (#3648 review).
        ...(args.prime_tower === undefined ? {} : { prime_tower: args.prime_tower }),
        request_id: requestId,
      }
      // A run no worker has picked up yet is a 202 followed to the run, not a 503
      // re-sent (#2795): each re-send of a print is a chance to print twice.
      const started = await reattachFollowing(
        ctx,
        () =>
          // Only the preference, not `CommandHeaders`: a run keys on the body's
          // `request_id`, never an `Idempotency-Key` (#3086).
          source.kind === 'library'
            ? backend.POST('/api/v1/print/library/{file_id}/run', {
                params: { path: { file_id: source.id } },
                headers: RESPOND_ASYNC,
                signal: ctx.signal,
                body,
              })
            : backend.POST('/api/v1/print/outputs/{output_id}/run', {
                params: { path: { output_id: source.id } },
                headers: RESPOND_ASYNC,
                signal: ctx.signal,
                body,
              }),
        `print ${sourceName(args)}`,
        ` The print may still have started (request ${requestId}): it has no run id yet for get_print_run. ` +
          `get_print_progress for this ${source.kind === 'library' ? 'library file' : 'output'} is safe to repeat and ` +
          "shows the print once it starts; check it and Bambuddy's queue before printing again, as calling " +
          'print_output again would be a second print.',
      )
      return runOutcome(await waitForRun(ctx, started), ctx)
    },
  }),

  defineTool({
    name: 'create_print_project',
    label: 'Create a Bambuddy print project',
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
    label: 'File an output in a project folder',
    description:
      "Upload an output's editable 3MF into a Bambuddy project's library folder, as Generate does with a " +
      'project chosen, or into a library folder of no project (`folder_id`, from list_print_projects\' ' +
      '`folders`) in its place: name one. Idempotent: the same destination again reuses the file already ' +
      'there (`created: false`), and a later print on the same printer reuses it too. On ScadBuddy\'s own ' +
      'library (`library_backend` native) a `folder_id` is one of its folders and the file is filed there with ' +
      'no Bambuddy: the answer\'s `bambuddy_url` is null, so offer no Bambuddy link; the file is in ScadBuddy\'s Library.',
    input: z
      .object({ output_id: outputId, project_id: z.number().int().optional(), folder_id: z.number().int().optional() })
      .refine((o) => (o.project_id === undefined) !== (o.folder_id === undefined), {
        message: 'name one: project_id or folder_id',
      }),
    risk: 'outward',
    bambuddyScope: ['Manage Library', 'Manage Projects'],
    routes: ['POST /api/v1/outputs/{output_id}/project-file'],
    summarize: ({ output_id, project_id, folder_id }) =>
      project_id !== undefined
        ? `Upload output ${output_id}'s 3MF into Bambuddy project ${project_id}'s folder`
        : `Upload output ${output_id}'s 3MF into Bambuddy library folder ${folder_id ?? ''}`,
    handler: async ({ output_id, project_id, folder_id }, ctx) =>
      json(
        await command(
          ctx,
          project_id !== undefined ? `file ${output_id} in project ${project_id}` : `file ${output_id} in folder ${folder_id ?? ''}`,
          (headers) =>
            ctx.backend.POST('/api/v1/outputs/{output_id}/project-file', {
              params: { path: { output_id } },
              body: project_id !== undefined ? { project_id } : { folder_id },
              headers,
            }),
        ),
      ),
  }),

  defineTool({
    name: 'file_output_under_project',
    label: 'File an output under a project',
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

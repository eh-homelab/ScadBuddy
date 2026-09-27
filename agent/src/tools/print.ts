import { z } from 'zod'
import { ok } from './call.js'
import { outputId, slug } from './common.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'

// Bambuddy (issue #251, spec D8): ScadBuddy's own tools over its backend's
// Bambuddy client, so the API key stays server-side and the backend's
// scope-aware errors (backend/scadbuddy/bambuddy/errors.py) reach the agent
// unchanged. Routes: backend/scadbuddy/api/printing.py and outputs.py.
//
// - Print flow, outward: anything that uploads to, creates in, or queues on
//   Bambuddy goes through the approval gate (spec §8.2).
// - Farm context, read: printers and live status (get_print_targets in
//   settings.ts), spools with per-slot remaining grams (get_print_filaments),
//   and print progress. The queue, the print archive and aggregate stats have
//   no backend route yet, so they have no tool yet.
// - Printer control (pause/stop/lights/motion/G-code) is out of scope.

const nullable = <T extends z.ZodType>(schema: T) => schema.nullable().optional()
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

export const printTools: Tool[] = [
  // ── read: farm context and planning ─────────────────────────────────────
  defineTool({
    name: 'get_print_filaments',
    description:
      "Bambuddy's spool inventory joined to where each spool is loaded and, with `printer_id`, the " +
      "remaining grams per slot and the mounted nozzles — plus what this output's plates need.",
    input: z.object({
      output_id: outputId,
      printer_id: z.number().int().optional(),
      nozzle_diameter: z.string().max(16).optional().describe('e.g. "0.4"'),
      plate_id: z.number().int().min(1).optional(),
    }),
    risk: 'read',
    bambuddyScope: ['Read Status'],
    routes: ['GET /api/v1/print/outputs/{output_id}/filaments'],
    handler: async ({ output_id, printer_id, nozzle_diameter, plate_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/outputs/{output_id}/filaments', {
            params: { path: { output_id }, query: { printer_id, nozzle_diameter, plate_id } },
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
    name: 'list_pipelines',
    description: "Bambuddy's slicer pipelines, with this model's remembered default.",
    input: z.object({ slug }),
    risk: 'read',
    bambuddyScope: ['Manage Queue'],
    routes: ['GET /api/v1/print/models/{slug}/pipelines'],
    handler: async ({ slug }, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/print/models/{slug}/pipelines', { params: { path: { slug } } }), `list pipelines for ${slug}`),
      ),
  }),

  defineTool({
    name: 'list_print_presets',
    description:
      'Printer presets and bed types and, once `printer_preset_id` is given, the process and filament ' +
      'presets compatible with it: what a new pipeline is built from.',
    input: z.object({
      printer_preset_source: z.enum(['orca_cloud', 'cloud', 'local', 'standard']).optional(),
      printer_preset_id: z.string().optional(),
    }),
    risk: 'read',
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/print/presets'],
    handler: async ({ printer_preset_source, printer_preset_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/print/presets', { params: { query: { printer_preset_source, printer_preset_id } } }),
          'list print presets',
        ),
      ),
  }),

  defineTool({
    name: 'list_print_projects',
    description: "Bambuddy's projects, to file prints under.",
    input: z.object({}),
    risk: 'read',
    bambuddyScope: ['Manage Projects'],
    routes: ['GET /api/v1/print/projects'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/print/projects'), 'list projects')),
  }),

  // ── write: preferences the print dialog remembers (ScadBuddy-local, re-settable) ──
  defineTool({
    name: 'remember_model_print_choices',
    description:
      "Remember a model's printer, pipeline and per-slot spools for next time. Any omitted part is left " +
      'as it is.',
    input: z.object({
      slug,
      printer_id: z.number().int().nullable().optional(),
      filament_plan: z.array(slotChoice).optional(),
      pipeline_id: z.number().int().nullable().optional(),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/print/models/{slug}/choices', 'PUT /api/v1/print/models/{slug}/pipeline'],
    handler: async ({ slug, printer_id, filament_plan, pipeline_id }, { backend }) => {
      const path = { slug }
      const result: Record<string, unknown> = {}
      if (printer_id !== undefined || filament_plan !== undefined) {
        result.choices = await ok(
          backend.PUT('/api/v1/print/models/{slug}/choices', {
            params: { path },
            body: { printer_id: printer_id ?? null, filament_plan: filament_plan ?? [] },
          }),
          `remember choices for ${slug}`,
        )
      }
      if (pipeline_id !== undefined) {
        result.pipeline = await ok(
          backend.PUT('/api/v1/print/models/{slug}/pipeline', { params: { path }, body: { pipeline_id } }),
          `remember pipeline for ${slug}`,
        )
      }
      if (Object.keys(result).length === 0) throw new ToolError('nothing to remember: pass printer_id, filament_plan or pipeline_id')
      return json(result)
    },
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
    name: 'check_print_eligibility',
    description:
      "Ask Bambuddy's pipelines whether they would accept this output. Bambuddy judges a library file, so " +
      'this uploads the 3MF first if Bambuddy does not have it: that is why it needs an approval.',
    input: z.object({ output_id: outputId, pipeline_ids: z.array(z.number().int()).optional() }),
    risk: 'outward',
    bambuddyScope: ['Manage Library', 'Manage Queue'],
    routes: ['POST /api/v1/print/outputs/{output_id}/eligibility'],
    summarize: ({ output_id, pipeline_ids }) =>
      `Upload output ${output_id} to Bambuddy (if needed) and check it against ${
        pipeline_ids ? `pipelines ${pipeline_ids.join(', ')}` : 'every pipeline'
      }`,
    handler: async ({ output_id, pipeline_ids }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print/outputs/{output_id}/eligibility', {
            params: { path: { output_id } },
            body: { pipeline_ids: pipeline_ids ?? null },
          }),
          `check eligibility of ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'send_to_bambuddy',
    description:
      "Send an output's 3MF to Bambuddy's library folder, or in `queue` mode also slice and queue it.",
    input: z.object({
      output_id: outputId,
      mode: z.enum(['library', 'queue']).default('library'),
      copies: z.number().int().min(1).max(1000).optional(),
      options: printOptions,
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library', 'Manage Queue'],
    routes: ['POST /api/v1/outputs/{output_id}/send'],
    summarize: ({ output_id, mode, copies }) =>
      mode === 'queue'
        ? `Send output ${output_id} to Bambuddy and queue ${copies ?? 1} cop${copies === 1 || !copies ? 'y' : 'ies'}`
        : `Send output ${output_id} to Bambuddy's library`,
    handler: async ({ output_id, mode, copies, options }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/outputs/{output_id}/send', {
            params: { path: { output_id } },
            body: { mode, copies: copies ?? null, options },
          }),
          `send ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'print_output',
    description:
      "Print an output: resolves the pipeline (the given one, else the model's, else the global default), " +
      'checks its eligibility, then runs it (or slices and queues when a filament plan, plate or remembered ' +
      'option needs it), behind one approval. Refuses on a blocking eligibility issue unless `force` is ' +
      'true, and when no pipeline resolves. Follow it with get_print_progress.',
    input: z.object({
      output_id: outputId,
      pipeline_id: z.number().int().optional().describe("Defaults to the model's pipeline, then the global one"),
      printer_id: z.number().int().optional(),
      copies: z.number().int().min(1).max(1000).optional(),
      plate_id: z.number().int().min(1).default(1),
      all_plates: z.boolean().default(false),
      bed_type: z.string().max(64).optional(),
      filament_plan: z
        .object({ slots: z.array(slotChoice), force_colour_match: z.boolean().default(false) })
        .optional(),
      project_id: z.number().int().optional(),
      options: printOptions,
      force: z.boolean().default(false),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Library', 'Manage Queue'],
    routes: ['POST /api/v1/print/outputs/{output_id}/run'],
    summarize: ({ output_id, pipeline_id, printer_id, copies, plate_id, all_plates }) =>
      `Print output ${output_id}: ${copies ?? 1} cop${(copies ?? 1) === 1 ? 'y' : 'ies'} of ${
        all_plates ? 'every plate' : `plate ${plate_id}`
      }` +
      `${pipeline_id !== undefined ? ` via pipeline ${pipeline_id}` : ' via the default pipeline'}` +
      `${printer_id !== undefined ? ` on printer ${printer_id}` : ''}`,
    handler: async (args, { backend }) => {
      const path = { output_id: args.output_id }
      // Resolve the pipeline the way /run would, so the one checked is the one
      // run. backend/scadbuddy/bambuddy/pipelines.py `run_for_output`:
      // `request.pipeline_id or settings.pipeline_for(meta.slug)` (the model's
      // pipeline, then the global one; library/settings_store.py), refusing
      // when neither is set. The eligibility route does NOT do this: with no
      // `pipeline_ids` it checks every pipeline (`check_pipelines`). So the
      // default is read from GET /print/models/{slug}/pipelines, whose
      // `default_pipeline_id` is that same `settings.pipeline_for(slug)`
      // (`describe_pipelines`), and passed to /run explicitly.
      let pipelineId = args.pipeline_id
      if (pipelineId === undefined) {
        const output = await ok(
          backend.GET('/api/v1/outputs/{output_id}', { params: { path } }),
          `get output ${args.output_id}`,
        )
        const choices = await ok(
          backend.GET('/api/v1/print/models/{slug}/pipelines', { params: { path: { slug: output.slug } } }),
          `resolve the default pipeline of ${output.slug}`,
        )
        if (choices.default_pipeline_id === null || choices.default_pipeline_id === undefined) {
          throw new ToolError(
            `no slicer pipeline is set for model ${output.slug} and there is no global default: pass ` +
              'pipeline_id (see list_pipelines) or remember one with remember_model_print_choices',
          )
        }
        pipelineId = choices.default_pipeline_id
      }
      if (!args.force) {
        const overview = await ok(
          backend.POST('/api/v1/print/outputs/{output_id}/eligibility', {
            params: { path },
            body: { pipeline_ids: [pipelineId] },
          }),
          `check eligibility of ${args.output_id}`,
        )
        // A report with `error` could not be judged; the run decides then (Bambuddy's 409).
        const blocked = (overview.reports ?? []).filter((r) => r.report?.ok === false)
        if (blocked.length > 0) {
          return { ...json({ status: 'ineligible', pipeline_id: pipelineId, reports: blocked }), isError: true }
        }
      }
      return json(
        await ok(
          backend.POST('/api/v1/print/outputs/{output_id}/run', {
            params: { path },
            body: {
              pipeline_id: pipelineId,
              printer_id: args.printer_id ?? null,
              copies: args.copies ?? null,
              plate_id: args.plate_id,
              all_plates: args.all_plates,
              bed_type: args.bed_type ?? null,
              filament_plan: args.filament_plan ?? null,
              project_id: args.project_id ?? null,
              options: args.options,
              force: args.force,
            },
          }),
          `print ${args.output_id}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'create_print_project',
    description: 'Create a Bambuddy project (with its library folder), or link an existing one by `project_id`.',
    input: z.object({
      name: z.string().optional(),
      description: z.string().optional(),
      colour: z.string().optional(),
      tags: z.string().optional(),
      url: z.string().optional(),
      folder_id: z.number().int().optional(),
      project_id: z.number().int().optional(),
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
            },
          }),
          'create project',
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
            body: { project_id: project_id ?? null, queue_item_ids },
          }),
          `file ${output_id} under a project`,
        ),
      ),
  }),

  defineTool({
    name: 'create_pipeline',
    description: 'Create a Bambuddy slicer pipeline from a printer, process and filament presets.',
    input: z.object({
      name: z.string().min(1),
      description: z.string().optional(),
      bed_type: z.string().optional(),
      printer_preset: presetRef,
      process_preset: presetRef,
      filament_presets: z.array(presetRef).min(1),
    }),
    risk: 'outward',
    bambuddyScope: ['Manage Queue'],
    routes: ['POST /api/v1/print/pipelines'],
    summarize: ({ name }) => `Create the Bambuddy pipeline "${name}"`,
    handler: async (args, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/print/pipelines', {
            body: { ...args, description: args.description ?? null, bed_type: args.bed_type ?? null },
          }),
          `create pipeline ${args.name}`,
        ),
      ),
  }),
]

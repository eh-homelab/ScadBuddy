import { z } from 'zod'
import { ok } from './call.js'
import { ACCEPTING_MS, command } from './command.js'
import { outputId, slug } from './common.js'
import { waitForJob } from './customizer.js'
import { slotChoice } from './print.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'

// Arrange (#1864, spec 2026-09-27 §7): objects from saved outputs of any template,
// and Bambuddy library files (#1863: any 3MF or STL), laid out together on shared plates
// for a goal, then saved as one output. Route: backend/scadbuddy/api/outputs.py
// `arrange_outputs`.

const arrangeObject = z
  .object({
    output_id: outputId.optional(),
    library_file_id: z
      .number()
      .int()
      .min(1)
      .optional()
      .describe(
        "A Bambuddy library file's `id` (a 3MF or STL), in place of output_id",
      ),
    part: z
      .string()
      .min(1)
      .optional()
      .describe("A `manifest` entry's `part` (get_output) of an output; omit for every object of the source"),
    count: z
      .number()
      .int()
      .min(0)
      .max(500)
      .optional()
      .describe('Copies (of each object, with part omitted); omit for the count the source has'),
    group: z.string().max(100).optional().describe('With goal keep_together: objects of one group share a plate'),
  })
  .refine((o) => (o.output_id === undefined) !== (o.library_file_id === undefined), {
    message: 'name one source: output_id or library_file_id',
  })

const goal = z.enum(['fewest_plates', 'fewest_swaps', 'by_colour', 'keep_together'])

/** The arrange's refusals the agent acts on differently, by their problem `code`. */
type ArrangeProblem = { code?: string; output_ids?: string[]; library_file_ids?: number[] }

export const arrangeTools: Tool[] = [
  defineTool({
    name: 'arrange',
    description:
      'Lay objects out again on shared plates, with no re-render, and save the result as a new output. ' +
      'Sources mix freely: outputs of any template (`output_id`), and Bambuddy library files ' +
      '(`library_file_id`): one ScadBuddy uploaded stands for its output, any other 3MF or STL is read ' +
      'from the file (each build item an object, with its count). Each object is one ' +
      "`part` of an output's manifest, or the whole source with part omitted; objects naming the same thing " +
      'twice (an output and its library file, say) are placed twice. `goal` picks the layout; ' +
      '`printer_id` packs for that printer\'s plate (omit for the configured one); `filament_plan` is the ' +
      "same spool-per-slot plan print_output takes, slots numbered by `colours` (omit for the first source's " +
      "colours, then any the others add). The result is filed under `slug`, one of the outputs' templates " +
      "(omit for the first object's); with library files alone, slug is required and may be any template. Waits for the job; if it outlasts the wait, the still-running job " +
      'is returned: poll it with get_render_job, then save_output under its slug. An output saved before ' +
      'Arrange existed is refused: the user re-renders it from Arrange in History. A sliced library file, ' +
      'or one painted in several colours, cannot be arranged.',
    input: z.object({
      objects: z.array(arrangeObject).min(1).max(200),
      goal: goal.default('fewest_plates'),
      printer_id: z.number().int().optional(),
      filament_plan: z
        .object({ slots: z.array(slotChoice), force_colour_match: z.boolean().default(false) })
        .optional(),
      colours: z.array(z.string().regex(/^#[0-9A-Fa-f]{6}$/)).max(32).optional(),
      name: z.string().max(200).optional(),
      slug: slug
        .optional()
        .describe("The template the result is filed under: one of the outputs'; required with library files alone"),
    }),
    risk: 'write',
    bambuddyScope: ['Read Status'],
    routes: ['POST /api/v1/outputs/arrange'],
    summarize: ({ objects, goal }) =>
      `Arrange ${objects.length} object${objects.length === 1 ? '' : 's'} (${goal.replaceAll('_', ' ')}) and save the result`,
    // Its own wait for the render, after the backend has accepted it.
    waitsMs: (_, ctx) => ACCEPTING_MS + ctx.renderWaitMs,
    handler: async ({ objects, goal, printer_id, filament_plan, colours, name, slug }, ctx) => {
      const sent = await ctx.backend.POST('/api/v1/outputs/arrange', {
        body: {
          objects,
          goal,
          printer_id: printer_id ?? null,
          filament_plan: filament_plan ?? null,
          colours: colours ?? null,
          name: name ?? null,
          slug: slug ?? null,
        },
        signal: ctx.signal,
      })
      const problem = (sent.error ?? {}) as ArrangeProblem
      if (problem.code === 'needs_backfill') {
        throw new ToolError(
          `not arranged: ${(problem.output_ids ?? []).join(', ')} were saved before Arrange existed, so ` +
            'nothing records their objects. Ask the user to open Arrange on them in History, which offers ' +
            'to re-render them first, or leave them out.',
        )
      }
      if (problem.code === 'library_file_not_arrangeable') {
        throw new ToolError(
          `not arranged: library file(s) ${(problem.library_file_ids ?? []).join(', ')} cannot be arranged ` +
            `(${(sent.error as { detail?: string }).detail ?? 'their objects could not be read'}). Leave them out.`,
        )
      }
      const started = await ok(Promise.resolve(sent), 'arrange')
      await ctx.progress(0, undefined, `arrange queued as ${started.id}`)
      const job = await waitForJob(ctx, started.id)
      if (job.status === 'failed' || job.status === 'cancelled') {
        throw new ToolError(`the arrange was ${job.status}${job.error ? `: ${job.error}` : ''}`)
      }
      if (job.status !== 'done') {
        return json({
          job_id: job.id,
          slug: job.slug,
          status: job.status,
          note: 'still arranging; poll get_render_job with this job_id, then save_output under this slug',
        })
      }
      const output = await command(ctx, `save arranged output of ${job.id}`, (headers) =>
        ctx.backend.POST('/api/v1/models/{slug}/outputs', {
          params: { path: { slug: job.slug } },
          body: { job_id: job.id, name: name ?? null },
          headers,
        }),
      )
      return json({ job_id: job.id, slug: job.slug, plates: Math.max(1, (job.plates ?? []).length), output })
    },
  }),
]

import { setTimeout as sleep } from 'node:timers/promises'
import { z } from 'zod'
import { binary } from './binary.js'
import { ok } from './call.js'
import { decodeBase64, fileForm, params, slug, VIEW, VIEW_SIZE } from './common.js'
import { blob, defineTool, image, json, type Tool, type ToolContext, ToolError } from './registry.js'
import { validateParams } from './validate.js'

// Customizer (issue #251): the schema (with the `// color` and `// font`
// overlays), validating a parameter set, rendering and waiting with progress,
// the job and its preview GLB, presets, and the uploaded files and samples a
// `// file` parameter uses. Routes: backend/scadbuddy/api/{models,jobs,presets,assets}.py.

type JobStatus = Awaited<ReturnType<typeof getJob>>

/** Job ids are 32 lowercase hex digits (every `job_id` path parameter in backend/openapi.json). */
const jobId = z
  .string()
  .regex(/^[0-9a-f]{32}$/, 'must be a render job id: 32 lowercase hex digits, as render_model returns it')
  .describe('Render job id, as render_model returns it')
const presetId = z.string().regex(/^[a-z0-9-]{1,64}$/).describe('Preset id, as list_presets returns it')
async function fetchSchema(ctx: ToolContext, slug: string, version?: string) {
  return version
    ? ok(
        ctx.backend.GET('/api/v1/models/{slug}/versions/{commit}/schema', {
          params: { path: { slug, commit: version } },
        }),
        `get schema of ${slug} at ${version}`,
      )
    : ok(ctx.backend.GET('/api/v1/models/{slug}/schema', { params: { path: { slug } } }), `get schema of ${slug}`)
}

async function getJob(ctx: ToolContext, id: string) {
  return ok(
    ctx.backend.GET('/api/v1/jobs/{job_id}', { params: { path: { job_id: id } }, signal: ctx.signal }),
    `get render job ${id}`,
  )
}

/**
 * Polls a render until it settles or `renderWaitMs` passes, reporting each
 * poll as progress so a streaming client sees the job move (spec §5.1: the
 * tool "submits a render and streams progress until it settles").
 */
export async function waitForJob(ctx: ToolContext, id: string): Promise<JobStatus> {
  const deadline = Date.now() + ctx.renderWaitMs
  for (let step = 1; ; step++) {
    const job = await getJob(ctx, id)
    const lastLine = job.log_tail?.at(-1)
    await ctx.progress(step, undefined, `render ${job.status}${lastLine ? `: ${lastLine}` : ''}`)
    if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled' || Date.now() >= deadline)
      return job
    await sleep(ctx.pollIntervalMs, undefined, { signal: ctx.signal })
  }
}

function jobSummary(job: JobStatus) {
  return {
    job_id: job.id,
    status: job.status,
    error: job.error ?? null,
    warnings: job.warnings ?? [],
    bbox_mm: job.bbox_mm ?? null,
    colors: job.colors ?? null,
    parts: job.parts ?? null,
    model_version: job.model_version ?? null,
    log_tail: job.log_tail ?? [],
  }
}

export const customizerTools: Tool[] = [
  defineTool({
    name: 'get_schema',
    description:
      "A model's customizer schema: every parameter with its type (number, integer, slider, string, " +
      'boolean, select, color, font, file), default, range, step, options and group. Pass `version` for ' +
      'the schema at an earlier revision.',
    input: z.object({ slug, version: z.string().regex(/^[0-9a-f]{7,40}$/).optional() }),
    risk: 'read',
    source:
      "the customizer schema OpenSCAD derives from the model's source, with the parameter names and comments its author wrote",
    routes: ['GET /api/v1/models/{slug}/schema', 'GET /api/v1/models/{slug}/versions/{commit}/schema'],
    handler: async ({ slug, version }, ctx) => json(await fetchSchema(ctx, slug, version)),
  }),

  defineTool({
    name: 'validate_params',
    description:
      "Check a parameter set against a model's customizer schema without rendering: unknown names, wrong " +
      'types, out-of-range numbers, values not among a select\'s options. Returns the issues and every ' +
      "parameter's effective value.",
    input: z.object({ slug, params }),
    risk: 'read',
    routes: [],
    handler: async ({ slug, params }, ctx) => json(validateParams(await fetchSchema(ctx, slug), params)),
  }),

  defineTool({
    name: 'render_model',
    description:
      'Render a model with the given parameters and wait for it to finish, reporting progress. Returns the ' +
      "job's outcome (bounding box, colours, parts, warnings, errors). Parameters are validated first; " +
      'set `save_output` to keep the result as an output (needed before plates, 3MF download or printing). ' +
      'If the render outlasts the wait, the still-running job id is returned: poll it with get_render_job.',
    input: z.object({
      slug,
      params: params.default({}),
      version: z.string().regex(/^[0-9a-f]{7,40}$/).optional().describe('Render an earlier revision'),
      save_output: z.boolean().default(false),
      output_name: z.string().optional(),
    }),
    risk: 'write',
    source:
      "OpenSCAD's output for a model, including echo() text and other messages the model's source controls",
    routes: ['POST /api/v1/models/{slug}/render', 'GET /api/v1/jobs/{job_id}'],
    handler: async ({ slug, params, version, save_output, output_name }, ctx) => {
      const report = validateParams(await fetchSchema(ctx, slug, version), params)
      if (!report.valid) {
        throw new ToolError(
          `not rendered: ${report.issues.map((i) => `${i.param} ${i.problem}`).join('; ')}`,
        )
      }
      const accepted = await ok(
        ctx.backend.POST('/api/v1/models/{slug}/render', {
          params: { path: { slug } },
          body: { params, version: version ?? null },
          signal: ctx.signal,
        }),
        `render ${slug}`,
      )
      await ctx.progress(0, undefined, `render queued as ${accepted.job_id}`)
      const job = await waitForJob(ctx, accepted.job_id)
      const summary = jobSummary(job)
      if (job.status === 'failed' || job.status === 'cancelled') {
        return { ...json(summary), isError: true }
      }
      if (job.status !== 'done') {
        return json({ ...summary, note: 'still rendering; poll get_render_job with this job_id' })
      }
      if (!save_output) return json(summary)
      const output = await ok(
        ctx.backend.POST('/api/v1/models/{slug}/outputs', {
          params: { path: { slug } },
          body: { job_id: job.id, name: output_name ?? null },
        }),
        `save output of ${job.id}`,
      )
      return json({ ...summary, output })
    },
  }),

  defineTool({
    name: 'get_render_job',
    description: "A render job's current state: status, error, warnings, bounding box, colours, parts and log tail.",
    input: z.object({ job_id: jobId }),
    risk: 'read',
    source:
      "OpenSCAD's output for a model, including echo() text and other messages the model's source controls",
    routes: [],
    handler: async ({ job_id }, ctx) => json(jobSummary(await getJob(ctx, job_id))),
  }),

  defineTool({
    name: 'get_render_preview',
    description:
      "A finished render's preview mesh as a binary glTF (model/gltf-binary), embedded as a base64 resource; " +
      'when it is too large to inline, a link to fetch it instead.',
    input: z.object({ job_id: jobId }),
    risk: 'read',
    routes: ['GET /api/v1/jobs/{job_id}/preview.glb'],
    handler: async ({ job_id }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/jobs/{job_id}/preview.glb', { params: { path: { job_id } }, parseAs: 'stream' }),
        `get preview of ${job_id}`,
        ctx,
        { path: `/api/v1/jobs/${job_id}/preview.glb`, name: `preview-${job_id}.glb`, fallbackType: 'model/gltf-binary' },
        (bytes, mimeType) => blob(`scadbuddy://jobs/${job_id}/preview.glb`, bytes, mimeType),
      ),
  }),

  defineTool({
    name: 'get_render_view',
    description:
      "A render job's preview mesh drawn from a named view (iso, front, back, left, right, top, bottom) as a " +
      'shaded PNG, to check the geometry without a 3D viewer.',
    input: z.object({ job_id: jobId, view: VIEW, size: VIEW_SIZE }),
    risk: 'read',
    routes: ['GET /api/v1/jobs/{job_id}/views/{view}.png'],
    handler: async ({ job_id, view, size }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/jobs/{job_id}/views/{view}.png', {
          params: { path: { job_id, view }, query: { size } },
          parseAs: 'stream',
        }),
        `draw ${view} view of ${job_id}`,
        ctx,
        {
          path: `/api/v1/jobs/${job_id}/views/${view}.png${size ? `?size=${size}` : ''}`,
          name: `${job_id}-${view}.png`,
          fallbackType: 'image/png',
        },
        image,
      ),
  }),

  defineTool({
    name: 'get_render_diagnostics',
    description:
      "OpenSCAD's warnings and errors, with the file and line each names, from the model's most recently " +
      'settled render (done or failed). Use it to fix a failing or warning-laden model.',
    input: z.object({ slug }),
    risk: 'read',
    source:
      "OpenSCAD's output for a model, including echo() text and other messages the model's source controls",
    routes: ['GET /api/v1/models/{slug}/diagnostics'],
    handler: async ({ slug }, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/models/{slug}/diagnostics', { params: { path: { slug } } }), `get diagnostics of ${slug}`),
      ),
  }),

  defineTool({
    name: 'get_asset_usage',
    description:
      'How much the upload store for `// file` parameters holds (count, bytes) and the caps past which an ' +
      'upload is refused (0 is no limit).',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/assets/usage'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/assets/usage'), 'get asset usage')),
  }),

  defineTool({
    name: 'list_presets',
    description: "A model's saved parameter presets, including read-only ones a template ships.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      'preset names and values written by model authors or users',
    routes: ['GET /api/v1/models/{slug}/presets'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/presets', { params: { path: { slug } } }), `list presets of ${slug}`)),
  }),

  defineTool({
    name: 'save_preset',
    description: 'Save a parameter set as a named preset of a model.',
    input: z.object({ slug, name: z.string().min(1).max(80), params: params.default({}) }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/presets'],
    handler: async ({ slug, name, params }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/{slug}/presets', { params: { path: { slug } }, body: { name, params } }),
          `save preset ${name}`,
        ),
      ),
  }),

  defineTool({
    name: 'update_preset',
    description: "Rename a preset or replace its values. Omitted fields are unchanged.",
    input: z.object({ slug, preset_id: presetId, name: z.string().min(1).max(80).optional(), params: params.optional() }),
    risk: 'write',
    routes: ['PATCH /api/v1/models/{slug}/presets/{preset_id}'],
    handler: async ({ slug, preset_id, name, params }, { backend }) =>
      json(
        await ok(
          backend.PATCH('/api/v1/models/{slug}/presets/{preset_id}', {
            params: { path: { slug, preset_id } },
            body: { name: name ?? null, params: params ?? null },
          }),
          `update preset ${preset_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'duplicate_preset',
    description: 'Copy a preset (including a read-only one a template ships) under a new name.',
    input: z.object({ slug, preset_id: presetId, name: z.string().min(1).max(80) }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/presets/{preset_id}/duplicate'],
    handler: async ({ slug, preset_id, name }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/{slug}/presets/{preset_id}/duplicate', {
            params: { path: { slug, preset_id } },
            body: { name },
          }),
          `duplicate preset ${preset_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'delete_preset',
    description: 'Delete a saved preset. Presets have no history, so this needs a human approval.',
    input: z.object({ slug, preset_id: presetId }),
    risk: 'outward',
    routes: ['DELETE /api/v1/models/{slug}/presets/{preset_id}'],
    summarize: ({ slug, preset_id }) => `Delete preset "${preset_id}" of model "${slug}"`,
    handler: async ({ slug, preset_id }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/models/{slug}/presets/{preset_id}', { params: { path: { slug, preset_id } } }),
        `delete preset ${preset_id}`,
      )
      return json({ deleted: preset_id })
    },
  }),

  defineTool({
    name: 'upload_asset',
    description:
      'Upload an SVG or PNG (base64) for a `// file` parameter. Returns its asset id, the value to pass ' +
      'for that parameter. SVGs are sanitised and PNGs downscaled by the backend.',
    input: z.object({
      slug,
      filename: z.string().min(1).describe('e.g. "logo.svg"; the content is sniffed, not trusted by name'),
      content_base64: z.string().min(1),
    }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/assets'],
    handler: async ({ slug, filename, content_base64 }, { backend }) => {
      const type = filename.toLowerCase().endsWith('.svg') ? 'image/svg+xml' : 'image/png'
      const form = fileForm(decodeBase64(content_base64, 'content_base64'), filename, type)
      return json(
        await ok(
          backend.POST('/api/v1/models/{slug}/assets', {
            params: { path: { slug } },
            body: { file: filename },
            bodySerializer: () => form,
          }),
          `upload ${filename}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'get_asset',
    description: "An uploaded file's metadata and, with `include_content`, the image itself.",
    input: z.object({
      slug,
      asset_id: z.string().regex(/^[0-9a-f]{64}$/),
      include_content: z.boolean().default(false),
    }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/assets/{asset_id}', 'GET /api/v1/models/{slug}/assets/{asset_id}/content'],
    handler: async ({ slug, asset_id, include_content }, ctx) => {
      const path = { slug, asset_id }
      const meta = json(
        await ok(ctx.backend.GET('/api/v1/models/{slug}/assets/{asset_id}', { params: { path } }), `get asset ${asset_id}`),
      )
      if (!include_content) return meta
      const content = await binary(
        ctx.backend.GET('/api/v1/models/{slug}/assets/{asset_id}/content', { params: { path }, parseAs: 'stream' }),
        `get asset content ${asset_id}`,
        ctx,
        { path: `/api/v1/models/${slug}/assets/${asset_id}/content`, name: `asset-${asset_id}`, fallbackType: 'image/png' },
        imageOrText,
      )
      return { content: [...meta.content, ...content.content] }
    },
  }),

  defineTool({
    name: 'get_sample',
    description: "A sample file a template ships for one of its `// file` parameters (listed in the schema's `samples`).",
    input: z.object({ slug, name: z.string().min(1), version: z.string().optional() }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/samples/{name}'],
    handler: async ({ slug, name, version }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/models/{slug}/samples/{name}', {
          params: { path: { slug, name }, query: { version } },
          parseAs: 'stream',
        }),
        `get sample ${name}`,
        ctx,
        {
          path: `/api/v1/models/${slug}/samples/${encodeURIComponent(name)}${version ? `?version=${encodeURIComponent(version)}` : ''}`,
          name,
          fallbackType: 'image/png',
        },
        imageOrText,
      ),
  }),
]

/** MCP image content is raster; an SVG is more useful to a model as its text. */
function imageOrText(bytes: ArrayBuffer, mimeType: string) {
  return mimeType === 'image/svg+xml'
    ? { content: [{ type: 'text' as const, text: Buffer.from(bytes).toString('utf8') }] }
    : image(bytes, mimeType)
}

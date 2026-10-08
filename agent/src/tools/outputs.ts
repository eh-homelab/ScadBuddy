import { z } from 'zod'
import { ok } from './call.js'
import { command } from './command.js'
import { binary } from './binary.js'
import { outputId, slug, VIEW, VIEW_SIZE } from './common.js'
import { blob, defineTool, image, json, type Tool } from './registry.js'
import { compositeKey, page, PAGED, pageInput } from './pagination.js'

// Outputs & plates (issue #251): list and get outputs, their plates and plate
// images, plate fit, and the 3MF. Routes: backend/scadbuddy/api/{outputs,plates}.py.

export const outputTools: Tool[] = [
  defineTool({
    name: 'list_outputs',
    description: "A model's saved outputs (finished renders kept as 3MFs), newest first." + PAGED,
    input: z.object({ slug, ...pageInput }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/outputs'],
    handler: async ({ slug, ...args }, { backend }) =>
      json(
        page(
          await ok(backend.GET('/api/v1/models/{slug}/outputs', { params: { path: { slug } } }), `list outputs of ${slug}`),
          { slug, ...args },
          (o) => o.id,
          'list_outputs',
        ),
      ),
  }),

  defineTool({
    name: 'save_output',
    description: 'Keep a finished render job as an output. render_model does this itself with `save_output: true`.',
    input: z.object({ slug, job_id: z.string().min(1), name: z.string().optional() }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/outputs'],
    handler: async ({ slug, job_id, name }, ctx) =>
      json(
        await command(ctx, `save output of ${job_id}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/outputs', {
            params: { path: { slug } },
            body: { job_id, name: name ?? null },
            headers,
          }),
        ),
      ),
  }),

  defineTool({
    name: 'get_output',
    description: "One output's detail: parameters, model version, parts, colours, plates, and where it was sent or printed.",
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}'],
    handler: async ({ output_id }, { backend }) =>
      json(await ok(backend.GET('/api/v1/outputs/{output_id}', { params: { path: { output_id } } }), `get output ${output_id}`)),
  }),

  defineTool({
    name: 'get_output_edit_target',
    description: 'What re-opening an output in the customizer loads: its model, revision and parameters.',
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/edit'],
    handler: async ({ output_id }, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/outputs/{output_id}/edit', { params: { path: { output_id } } }), `resolve edit of ${output_id}`),
      ),
  }),

  defineTool({
    name: 'delete_output',
    description: 'Delete an output and its 3MF. Irreversible, so it needs a human approval.',
    input: z.object({ output_id: outputId }),
    risk: 'outward',
    routes: ['DELETE /api/v1/outputs/{output_id}'],
    summarize: ({ output_id }) => `Delete output ${output_id} and its 3MF`,
    handler: async ({ output_id }, ctx) => {
      await command(ctx, `delete output ${output_id}`, (headers) =>
        ctx.backend.DELETE('/api/v1/outputs/{output_id}', { params: { path: { output_id } }, headers }),
      )
      return json({ deleted: output_id })
    },
  }),

  defineTool({
    name: 'download_3mf',
    description:
      "An output's multi-colour 3MF, embedded as a base64 resource (model/3mf); when it is too large to " +
      'inline, a link to fetch it instead.',
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/model.3mf'],
    handler: async ({ output_id }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/model.3mf', { params: { path: { output_id } }, parseAs: 'stream' }),
        `download 3MF of ${output_id}`,
        ctx,
        { path: `/api/v1/outputs/${output_id}/model.3mf`, name: `${output_id}.3mf`, fallbackType: 'model/3mf' },
        (bytes, mimeType) => blob(`scadbuddy://outputs/${output_id}/model.3mf`, bytes, mimeType),
      ),
  }),

  defineTool({
    name: 'get_output_preview',
    description:
      "An output's preview mesh as a binary glTF (model/gltf-binary), embedded as a base64 resource; " +
      'when it is too large to inline, a link to fetch it instead.',
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/preview.glb'],
    handler: async ({ output_id }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/preview.glb', { params: { path: { output_id } }, parseAs: 'stream' }),
        `get preview of ${output_id}`,
        ctx,
        { path: `/api/v1/outputs/${output_id}/preview.glb`, name: `${output_id}.glb`, fallbackType: 'model/gltf-binary' },
        (bytes, mimeType) => blob(`scadbuddy://outputs/${output_id}/preview.glb`, bytes, mimeType),
      ),
  }),

  defineTool({
    name: 'get_output_plates',
    description: "The plates in an output's 3MF: objects, colours and slots per plate.",
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/plates'],
    handler: async ({ output_id }, { backend }) =>
      json(
        await ok(backend.GET('/api/v1/outputs/{output_id}/plates', { params: { path: { output_id } } }), `get plates of ${output_id}`),
      ),
  }),

  defineTool({
    name: 'get_output_view',
    description:
      "A saved output's preview mesh drawn from a named view (iso, front, back, left, right, top, bottom) as " +
      'a shaded PNG.',
    input: z.object({ output_id: outputId, view: VIEW, size: VIEW_SIZE }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/views/{view}.png'],
    handler: async ({ output_id, view, size }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/views/{view}.png', {
          params: { path: { output_id, view }, query: { size } },
          parseAs: 'stream',
        }),
        `draw ${view} view of ${output_id}`,
        ctx,
        {
          path: `/api/v1/outputs/${output_id}/views/${view}.png${size ? `?size=${size}` : ''}`,
          name: `${output_id}-${view}.png`,
          fallbackType: 'image/png',
        },
        image,
      ),
  }),

  defineTool({
    name: 'get_output_image',
    description:
      "An output's thumbnail, or with `plate` the cover image of that plate (1-based), as a PNG.",
    input: z.object({ output_id: outputId, plate: z.number().int().min(1).optional() }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/thumbnail', 'GET /api/v1/outputs/{output_id}/plates/{index}/thumbnail'],
    handler: async ({ output_id, plate }, ctx) =>
      plate === undefined
        ? binary(
            ctx.backend.GET('/api/v1/outputs/{output_id}/thumbnail', { params: { path: { output_id } }, parseAs: 'stream' }),
            `get thumbnail of ${output_id}`,
            ctx,
            { path: `/api/v1/outputs/${output_id}/thumbnail`, name: `${output_id}-thumbnail.png`, fallbackType: 'image/png' },
            image,
          )
        : binary(
            ctx.backend.GET('/api/v1/outputs/{output_id}/plates/{index}/thumbnail', {
              params: { path: { output_id, index: plate } },
              parseAs: 'stream',
            }),
            `get plate ${plate} image of ${output_id}`,
            ctx,
            {
              path: `/api/v1/outputs/${output_id}/plates/${plate}/thumbnail`,
              name: `${output_id}-plate-${plate}.png`,
              fallbackType: 'image/png',
            },
            image,
          ),
  }),

  defineTool({
    name: 'analyze_geometry',
    description:
      "Printability measurements of an output's closed per-colour solids: open and non-manifold edges with " +
      'their locations, bounding box, bed contact, height-to-base ratio, overhang area by angle, and ' +
      'estimates of the thinnest wall and smallest feature (mm, Z up, model coordinates).',
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/geometry'],
    handler: async ({ output_id }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/outputs/{output_id}/geometry', { params: { path: { output_id } } }),
          `analyze geometry of ${output_id}`,
        ),
      ),
  }),

  defineTool({
    name: 'list_plates',
    description: 'The build plates ScadBuddy knows, by printer model, and the configured default.' + PAGED,
    input: z.object({ ...pageInput }),
    risk: 'read',
    routes: ['GET /api/v1/plates'],
    handler: async (args, { backend }) => {
      const { default: fallback, plates } = await ok(backend.GET('/api/v1/plates'), 'list plates')
      const { items, ...rest } = page(plates, args, (p) => compositeKey(p.model, p.name), 'list_plates')
      return json({ default: fallback, plates: items, ...rest })
    },
  }),

  defineTool({
    name: 'get_plate',
    description: "The build plate for a printer model (e.g. \"X1C\"), or the configured default's.",
    input: z.object({ model: z.string().optional() }),
    risk: 'read',
    routes: ['GET /api/v1/plate'],
    handler: async ({ model }, { backend }) =>
      json(await ok(backend.GET('/api/v1/plate', { params: { query: { model } } }), 'get plate')),
  }),

  defineTool({
    name: 'check_plate_fit',
    description:
      'Whether a part of the given size in mm (e.g. a render\'s bbox_mm.size) fits a printer\'s plate, and by ' +
      'how much it overshoots if not.',
    input: z.object({
      x: z.number().positive(),
      y: z.number().positive(),
      z: z.number().positive(),
      model: z.string().optional().describe('Printer model; the configured default when omitted'),
      colours: z.number().int().min(1).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/plate/fit'],
    handler: async ({ x, y, z: height, model, colours }, { backend }) =>
      json(await ok(backend.GET('/api/v1/plate/fit', { params: { query: { x, y, z: height, model, colours } } }), 'check plate fit')),
  }),
]

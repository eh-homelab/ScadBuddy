import { z } from 'zod'
import { ok } from './call.js'
import { MAX_INLINE_BYTES, outputId, slug } from './common.js'
import { blob, defineTool, image, json, type Tool } from './registry.js'

// Outputs & plates (issue #251): list and get outputs, their plates and plate
// images, plate fit, and the 3MF. Routes: backend/scadbuddy/api/{outputs,plates}.py.

export const outputTools: Tool[] = [
  defineTool({
    name: 'list_outputs',
    description: "A model's saved outputs (finished renders kept as 3MFs), newest first.",
    input: z.object({ slug }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/outputs'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/outputs', { params: { path: { slug } } }), `list outputs of ${slug}`)),
  }),

  defineTool({
    name: 'save_output',
    description: 'Keep a finished render job as an output. render_model does this itself with `save_output: true`.',
    input: z.object({ slug, job_id: z.string().min(1), name: z.string().optional() }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/outputs'],
    handler: async ({ slug, job_id, name }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/{slug}/outputs', { params: { path: { slug } }, body: { job_id, name: name ?? null } }),
          `save output of ${job_id}`,
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
    handler: async ({ output_id }, { backend }) => {
      await ok(backend.DELETE('/api/v1/outputs/{output_id}', { params: { path: { output_id } } }), `delete output ${output_id}`)
      return json({ deleted: output_id })
    },
  }),

  defineTool({
    name: 'download_3mf',
    description: "An output's multi-colour 3MF, embedded as a base64 resource (model/3mf).",
    input: z.object({ output_id: outputId }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/model.3mf'],
    handler: async ({ output_id }, { backend }) =>
      blob(
        `scadbuddy://outputs/${output_id}/model.3mf`,
        await ok(
          backend.GET('/api/v1/outputs/{output_id}/model.3mf', { params: { path: { output_id } }, parseAs: 'arrayBuffer' }),
          `download 3MF of ${output_id}`,
        ),
        'model/3mf',
        MAX_INLINE_BYTES,
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
    name: 'get_output_image',
    description:
      "An output's thumbnail, or with `plate` the cover image of that plate (1-based), as a PNG.",
    input: z.object({ output_id: outputId, plate: z.number().int().min(1).optional() }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/thumbnail', 'GET /api/v1/outputs/{output_id}/plates/{index}/thumbnail'],
    handler: async ({ output_id, plate }, { backend }) =>
      image(
        plate === undefined
          ? await ok(
              backend.GET('/api/v1/outputs/{output_id}/thumbnail', { params: { path: { output_id } }, parseAs: 'arrayBuffer' }),
              `get thumbnail of ${output_id}`,
            )
          : await ok(
              backend.GET('/api/v1/outputs/{output_id}/plates/{index}/thumbnail', {
                params: { path: { output_id, index: plate } },
                parseAs: 'arrayBuffer',
              }),
              `get plate ${plate} image of ${output_id}`,
            ),
        'image/png',
      ),
  }),

  defineTool({
    name: 'list_plates',
    description: 'Every build plate ScadBuddy knows, by printer model.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/plates'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/plates'), 'list plates')),
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

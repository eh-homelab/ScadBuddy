import { z } from 'zod'
import { ok } from './call.js'
import { command } from './command.js'
import { binary } from './binary.js'
import { CAMERA, cameraQuery, outputId, slug, VIEW, VIEW_SIZE, withQuery } from './common.js'
import { blob, defineTool, image, json, type Tool } from './registry.js'
import { backendPage, compositeKey, page, PAGED, pageInput, totalCount } from './pagination.js'
import { sourceOf, withSource } from './print.js'

// Outputs & plates (issue #251): list and get outputs, their plates and plate
// images, plate fit, and the 3MF. Routes: backend/scadbuddy/api/{outputs,plates}.py.

export const outputTools: Tool[] = [
  defineTool({
    name: 'list_outputs',
    description: "A model's saved outputs (finished renders kept as 3MFs), newest first." + PAGED,
    input: z.object({ slug, ...pageInput }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/outputs'],
    // The backend pages (#843): it builds only this page's output details.
    handler: async ({ slug, ...args }, { backend }) =>
      json(
        await backendPage({ slug, ...args }, (o: { id: string }) => o.id, 'list_outputs', async (query) => {
          const pending = backend.GET('/api/v1/models/{slug}/outputs', { params: { path: { slug }, query } })
          return { items: await ok(pending, `list outputs of ${slug}`), total: totalCount((await pending).response) }
        }),
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
      // Its model, read first: once deleted the output can no longer say (#1071). A read
      // that fails leaves it unnamed; the delete answers for whether the output exists.
      const { data } = await ctx.backend
        .GET('/api/v1/outputs/{output_id}', { params: { path: { output_id } }, signal: ctx.signal })
        .catch(() => ({ data: undefined }))
      await command(ctx, `delete output ${output_id}`, (headers) =>
        ctx.backend.DELETE('/api/v1/outputs/{output_id}', { params: { path: { output_id } }, headers }),
      )
      return json({ deleted: output_id, slug: data?.slug ?? null })
    },
  }),

  defineTool({
    name: 'download_3mf',
    description:
      "An output's multi-colour 3MF (model/3mf), or a Bambuddy library file as the library holds it " +
      '(`library_file_id`: what a print of it slices, a 3MF or an STL), embedded as a base64 resource; when it ' +
      'is too large to inline, a link to fetch it instead.',
    input: withSource({}),
    risk: 'read',
    // A library file is read from Bambuddy; an output never is.
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/outputs/{output_id}/model.3mf', 'GET /api/v1/print/library/{file_id}/file'],
    handler: async (args, ctx) => {
      const source = sourceOf(args)
      if (source.kind === 'library') {
        const id = source.id
        return binary(
          ctx.backend.GET('/api/v1/print/library/{file_id}/file', { params: { path: { file_id: id } }, parseAs: 'stream' }),
          `download library file ${id}`,
          ctx,
          { path: `/api/v1/print/library/${id}/file`, name: `library-${id}`, fallbackType: 'application/octet-stream' },
          (bytes, mimeType) => blob(`scadbuddy://print/library/${id}/file`, bytes, mimeType),
        )
      }
      const output_id = source.id
      return binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/model.3mf', { params: { path: { output_id } }, parseAs: 'stream' }),
        `download 3MF of ${output_id}`,
        ctx,
        { path: `/api/v1/outputs/${output_id}/model.3mf`, name: `${output_id}.3mf`, fallbackType: 'model/3mf' },
        (bytes, mimeType) => blob(`scadbuddy://outputs/${output_id}/model.3mf`, bytes, mimeType),
      )
    },
  }),

  defineTool({
    name: 'get_output_preview',
    description:
      "An output's preview mesh, or one plate of a Bambuddy library file (`library_file_id`, `plate` from 1) " +
      'read from the 3MF a print of it slices, as a binary glTF (model/gltf-binary), embedded as a base64 ' +
      'resource; when it is too large to inline, a link to fetch it instead.',
    input: withSource({ plate: z.number().int().min(1).optional().describe("A library file's plate; 1 by default") }),
    risk: 'read',
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/outputs/{output_id}/preview.glb', 'GET /api/v1/print/library/{file_id}/preview.glb'],
    handler: async ({ plate, ...rest }, ctx) => {
      const source = sourceOf(rest)
      if (source.kind === 'library') {
        const id = source.id
        return binary(
          ctx.backend.GET('/api/v1/print/library/{file_id}/preview.glb', {
            params: { path: { file_id: id }, query: { plate } },
            parseAs: 'stream',
          }),
          `get preview of library file ${id}`,
          ctx,
          {
            path: `/api/v1/print/library/${id}/preview.glb${plate === undefined ? '' : `?plate=${plate}`}`,
            name: `library-${id}.glb`,
            fallbackType: 'model/gltf-binary',
          },
          (bytes, mimeType) => blob(`scadbuddy://print/library/${id}/preview.glb`, bytes, mimeType),
        )
      }
      const output_id = source.id
      return binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/preview.glb', { params: { path: { output_id } }, parseAs: 'stream' }),
        `get preview of ${output_id}`,
        ctx,
        { path: `/api/v1/outputs/${output_id}/preview.glb`, name: `${output_id}.glb`, fallbackType: 'model/gltf-binary' },
        (bytes, mimeType) => blob(`scadbuddy://outputs/${output_id}/preview.glb`, bytes, mimeType),
      )
    },
  }),

  defineTool({
    name: 'get_output_plates',
    description:
      "The plates in an output's 3MF: objects, colours and slots per plate. For a Bambuddy library file " +
      '(`library_file_id`), the plates Bambuddy reads from it (index and name), what print_output takes as ' +
      '`plate_id`; empty when it reads none, which prints as plate 1.',
    input: withSource({}),
    risk: 'read',
    bambuddyScope: ['Manage Library'],
    routes: ['GET /api/v1/outputs/{output_id}/plates', 'GET /api/v1/print/library/{file_id}/plates'],
    handler: async (args, { backend }) => {
      const source = sourceOf(args)
      return json(
        source.kind === 'library'
          ? await ok(
              backend.GET('/api/v1/print/library/{file_id}/plates', { params: { path: { file_id: source.id } } }),
              `get plates of library file ${source.id}`,
            )
          : await ok(
              backend.GET('/api/v1/outputs/{output_id}/plates', { params: { path: { output_id: source.id } } }),
              `get plates of ${source.id}`,
            ),
      )
    },
  }),

  defineTool({
    name: 'get_output_view',
    description:
      "A saved output's preview mesh drawn from a named view (iso, front, back, left, right, top, bottom) as " +
      'a shaded PNG. The camera arguments are get_render_view\'s.',
    input: z.object({ output_id: outputId, view: VIEW, size: VIEW_SIZE, ...CAMERA }),
    risk: 'read',
    routes: ['GET /api/v1/outputs/{output_id}/views/{view}.png'],
    handler: async ({ output_id, view, size, ...camera }, ctx) =>
      binary(
        ctx.backend.GET('/api/v1/outputs/{output_id}/views/{view}.png', {
          params: { path: { output_id, view }, query: { size, ...cameraQuery(camera) } },
          parseAs: 'stream',
        }),
        `draw ${view} view of ${output_id}`,
        ctx,
        {
          path: withQuery(`/api/v1/outputs/${output_id}/views/${view}.png`, { size, ...cameraQuery(camera) }),
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
    title: ({ output_id, plate }) => `Get the picture of output ${output_id}${plate ? `, plate ${plate}` : ''}`,
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

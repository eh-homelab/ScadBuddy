// agent/src/tools/meshModels.ts
import { z } from 'zod'
import { command } from './command.js'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'
import { BYTES_NEVER_AN_ARGUMENT, uploadInit, uploadName, withSent, workspaceFile, workspacePathInput, workspaceShaInput } from './workspaceUploads.js'

// Mesh models (#2146, spec docs/superpowers/specs/2026-10-11-mesh-models-design.md):
// a 3MF/STL opened as a model whose scene.json compiles to its model.scad. Routes:
// backend/scadbuddy/api/mesh_models.py. Nothing here writes to Bambuddy.

const vec3 = z.tuple([z.number(), z.number(), z.number()])
// An exposed parameter's name: the backend's IDENT (meshmodel/scene.py). Not an object id (o1, o2_x), an sb_ name or an OpenSCAD keyword.
const KEYWORDS = ['if', 'else', 'for', 'let', 'each', 'module', 'function', 'include', 'use', 'assert', 'echo', 'intersection_for', 'true', 'false', 'undef', 'PI']
const ident = z
  .string()
  .max(64)
  .regex(new RegExp(`^(?!o[0-9]+(?:_|$))(?!sb_)(?!(?:${KEYWORDS.join('|')})$)[A-Za-z_][A-Za-z0-9_]*$`), 'not a usable parameter name (o<digits>, sb_…, an OpenSCAD keyword and PI are reserved)')
const num = z.union([z.number(), z.object({ expose: ident, value: z.number() })])
const nvec3 = z.tuple([num, num, num])
const object = z.string().regex(/^o[0-9]+$/)

const edit = z.discriminatedUnion('edit', [
  z.object({ edit: z.literal('transform'), object, translate: nvec3.optional(), rotate: nvec3.optional(), scale: nvec3.optional() }),
  z.object({
    edit: z.literal('split_at_plane'),
    object,
    point: vec3.describe('A point on the cutting plane, world mm'),
    normal: vec3.describe('The plane normal, world; the new object is the side it points to'),
    joint: z
      .object({
        diameter: z.number().positive().max(100).optional().describe('Peg diameter; default 60% of the room at the section centre'),
        depth: z.number().positive().max(100).default(6),
        clearance: z.number().min(0).max(2).default(0.2),
        key: z.enum(['none', 'flat', 'hex']).default('flat'),
        peg_on: z.enum(['above', 'below']).default('above'),
      })
      .optional(),
  }),
  z.object({ edit: z.literal('lay_flat'), object, normal: z.union([vec3, z.literal('largest_flat')]) }),
  z.object({ edit: z.literal('add'), object, primitive: z.enum(['cube', 'cylinder', 'sphere']), size: nvec3, at: vec3.optional(), rotate: nvec3.default([0, 0, 0]), colour: z.string().regex(/^#[0-9A-Fa-f]{6}$/).optional() }),
  z.object({ edit: z.literal('subtract'), object, primitive: z.enum(['cube', 'cylinder', 'sphere']), size: nvec3, at: vec3.optional(), rotate: nvec3.default([0, 0, 0]) }),
  z.object({ edit: z.literal('custom'), object, scad: z.string().min(1).max(20_000) }),
  z.object({ edit: z.literal('set_colour'), object, part: z.string().regex(/^p[0-9]+$/), colour: z.string().regex(/^#[0-9A-Fa-f]{6}$/) }),
  z.object({ edit: z.literal('delete'), object }),
  z.object({ edit: z.literal('duplicate'), object, offset: vec3.default([20, 0, 0]) }),
  z.object({ edit: z.literal('rename'), object, name: z.string().min(1).max(200) }),
  z.object({ edit: z.literal('remove_op'), object, op: z.number().int().min(0) }),
  z.object({ edit: z.literal('expose'), object, op: z.number().int().min(0).nullable().describe('The op index, or null for the object\'s transform'), field: z.string().regex(/^[a-z_]+(\.[0-2])?$/).describe('e.g. diameter, or point.2 for a tuple element'), name: ident }),
  z.object({ edit: z.literal('unexpose'), object, op: z.number().int().min(0).nullable(), field: z.string().regex(/^[a-z_]+(\.[0-2])?$/) }),
])

export const meshModelTools: Tool[] = [
  defineTool({
    name: 'create_model_from_mesh',
    label: 'Open a mesh as an editable model',
    description:
      'Open a mesh as an editable model: a Bambuddy library file (library_file_id, from list_library), an output ' +
      '(output_id), or a 3MF/STL in your workspace (workspace_path with its workspace_sha256). Each object becomes a ' +
      'scene object you can cut, join with a peg and socket, lay flat, add to or subtract from with edit_scene, then ' +
      'render and print like any template. Nothing is written to Bambuddy. ' + BYTES_NEVER_AN_ARGUMENT,
    input: z
      .object({
        name: z.string().min(1).max(200),
        description: z.string().max(10_000).optional(),
        library_file_id: z.number().int().positive().optional(),
        output_id: z.string().regex(/^[0-9a-f]{32}$/).optional(),
        filename: z.string().min(1).max(255).optional().describe('The upload\'s name, ending .3mf or .stl; the workspace file\'s own name by default'),
        workspace_path: workspacePathInput.optional(),
        workspace_sha256: workspaceShaInput.optional(),
      })
      .strict(),
    risk: 'write',
    routes: ['POST /api/v1/models/from-mesh', 'POST /api/v1/models/from-mesh/upload'],
    title: ({ name }) => `Open a mesh as the model "${name}"`,
    handler: async ({ name, description, library_file_id, output_id, filename, workspace_path, workspace_sha256 }, ctx) => {
      if ([library_file_id !== undefined, output_id !== undefined, workspace_path !== undefined].filter(Boolean).length !== 1) {
        throw new ToolError('give exactly one source: library_file_id, output_id, or workspace_path')
      }
      if (workspace_path === undefined) {
        if (workspace_sha256 !== undefined) throw new ToolError('`workspace_sha256` goes with `workspace_path` only')
        const body =
          library_file_id !== undefined
            ? { name, description: description ?? '', library_file_id }
            : { name, description: description ?? '', output_id: output_id! }
        return json(await command(ctx, `open ${name}`, (headers) => ctx.backend.POST('/api/v1/models/from-mesh', { body, headers })))
      }
      if (workspace_sha256 === undefined) {
        throw new ToolError('`workspace_path` needs `workspace_sha256`: the sha256 workspace_list or workspace_read gave for the file')
      }
      const info = await workspaceFile(ctx, { path: workspace_path, sha256: workspace_sha256 }, 'create_model_from_mesh')
      const file = filename ?? uploadName(info.path)
      // The extension says whether the bytes are an STL or a 3MF.
      if (!/\.(3mf|stl)$/i.test(file)) throw new ToolError('give a `filename` ending .3mf or .stl')
      const answer = await command(ctx, `open ${file}`, (headers) =>
        ctx.backend.POST('/api/v1/models/from-mesh/upload', {
          body: { file: file, name, description: description ?? null },
          ...uploadInit(ctx, info, { name: 'file', filename: file, type: 'application/octet-stream' }, headers, { name, ...(description === undefined ? {} : { description }) }),
        }),
      )
      return json(withSent(answer, info))
    },
  }),

  defineTool({
    name: 'get_scene',
    label: "Read a mesh model's scene",
    description:
      "A mesh model's scene: its objects (ids o1, o2…), each object's colour parts, its ops in order (cut, joint, " +
      'add, subtract, custom) in the object\'s own frame, and its transform (translate, rotate in degrees X then Y ' +
      'then Z, scale). notes say what reading the file dropped and where a painted object\'s painting is not carried whole ' +
      '(a mirrored object). hand_edited true means model.scad was edited directly; edit_scene is refused until eject_scene.',
    input: z.object({ slug }),
    risk: 'read',
    source: 'scene data derived from a mesh file the user supplied or Bambuddy holds',
    routes: ['GET /api/v1/models/{slug}/scene'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/scene', { params: { path: { slug } } }), `get scene ${slug}`)),
  }),

  defineTool({
    name: 'edit_scene',
    label: "Edit a mesh model's scene",
    description:
      'Edit a mesh model as one revision (undo with restore_version). Painting done in Bambu Studio is carried into a model\'s one-plate renders (not multi-plate ones; a hand-edited model.scad or unreadable paint file skips it with a render warning). Edits apply in order, so a later edit may name an ' +
      'object an earlier one made (split_at_plane makes the top half the next id). Planes and `at` are world mm. ' +
      'split_at_plane with joint adds a keyed peg to one half and a socket with clearance to the other, sized from ' +
      'the section and refused when it would not fit. lay_flat turns a world direction (or the largest flat face, often ' +
      'the cut face) onto the bed. add with a colour fills its space in that colour, carved out of every other colour (an ' +
      'add without one is in the object\'s first colour); to print a part in place into another (a carrot\'s leaves into ' +
      'its body), add a cylinder in the leaves\' colour through the neck. expose turns a number into a customizer parameter; removing one a saved preset sets ' +
      'needs drop_from_presets. Render after editing to check.',
    input: z.object({ slug, edits: z.array(edit).min(1).max(50), drop_from_presets: z.boolean().default(false), message: z.string().max(200).optional().describe('The revision\'s message') }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/scene/edits'],
    handler: async ({ slug, edits, drop_from_presets, message }, ctx) =>
      json(
        await command(ctx, `edit scene ${slug}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/scene/edits', { params: { path: { slug } }, body: { edits, drop_from_presets, ...(message === undefined ? {} : { message }) }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'eject_scene',
    label: 'Turn a mesh model back into OpenSCAD',
    description:
      "Stop editing a mesh model as a scene: scene.json is removed and model.scad (which imports the meshes) is the " +
      'source from then on, edited with update_source. One way; restore_version undoes it.',
    input: z.object({ slug }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/scene/eject'],
    handler: async ({ slug }, ctx) =>
      json(
        await command(ctx, `eject scene ${slug}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/scene/eject', { params: { path: { slug } }, headers }),
        ),
      ),
  }),
]

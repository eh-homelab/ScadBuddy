import { z } from 'zod'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, text, type Tool } from './registry.js'

/** The backend's MAX_SOURCE_CHARS and MAX_SUBJECT (`SourceFileUpdate.content` and
 * `.message` in backend/scadbuddy/api/model_files.py); test/sourceFiles.test.ts checks
 * both against the OpenAPI spec (PR #752 review). */
export const MAX_SOURCE_CHARS = 1_000_000
export const MAX_MESSAGE_CHARS = 200

/** Characters as Pydantic's `max_length` counts them: code points, where a JS string's
 * `length` (and so Zod's `.max`) counts UTF-16 units and an astral character twice. */
function codePoints(text: string): number {
  let count = 0
  for (const _ of text) count++
  return count
}

// Multi-file models (issue #252: "Multi-file models are supported (includes
// inside the model folder)"): the `.scad` files beside model.scad that it
// `include`s or `use`s. Each write or removal is one revision in the model's
// history, so `write` (spec §8.1). model.scad itself is get_source /
// update_source. Routes: backend/scadbuddy/api/model_files.py.

const fileName = z
  .string()
  .regex(/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\.scad$/, 'must be a bare .scad file name, e.g. "parts.scad"')
  .describe('A .scad file at the top of the model\'s directory, e.g. "parts.scad"')

const SOURCE = "OpenSCAD source (code and comments) written by the model's author, imported from the web or pulled from an upstream"

export const sourceFileTools: Tool[] = [
  defineTool({
    name: 'list_source_files',
    description:
      "A model's .scad files, model.scad first: the files it can `include <name.scad>` or `use <name.scad>`.",
    input: z.object({ slug }),
    risk: 'read',
    source: 'file names in a model directory, chosen by its author',
    routes: ['GET /api/v1/models/{slug}/files'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/files', { params: { path: { slug } } }), `list files of ${slug}`)),
  }),

  defineTool({
    name: 'get_source_file',
    description: "One of a model's .scad files, model.scad included.",
    input: z.object({ slug, name: fileName }),
    risk: 'read',
    source: SOURCE,
    // The editor's reader (#707, backend api/lsp.py) serves every text file in the model's directory.
    routes: ['GET /api/v1/models/{slug}/files/{path}'],
    handler: async ({ slug, name }, { backend }) =>
      text(
        await ok(
          backend.GET('/api/v1/models/{slug}/files/{path}', { params: { path: { slug, path: name } }, parseAs: 'text' }),
          `get ${slug}/${name}`,
        ),
      ),
  }),

  defineTool({
    name: 'write_source_file',
    description:
      'Create or replace a .scad file beside model.scad (a module library it `use`s, a part it `include`s), ' +
      'as one revision in its history. Not model.scad itself: that is update_source or apply_patch. The file ' +
      'is not parse-checked on its own; check the model afterwards with check_source (with `slug`) or a render.',
    input: z.object({
      slug,
      name: fileName,
      content: z
        .string()
        .refine((text) => codePoints(text) <= MAX_SOURCE_CHARS, `at most ${MAX_SOURCE_CHARS} characters`),
      message: z.string().max(MAX_MESSAGE_CHARS).optional().describe("What the revision is called in the history: the user's instruction, in short"),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/files/{name}'],
    handler: async ({ slug, name, content, message }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/models/{slug}/files/{name}', {
            params: { path: { slug, name } },
            body: { content, message: message ?? null },
          }),
          `write ${slug}/${name}`,
        ),
      ),
  }),

  defineTool({
    name: 'delete_source_file',
    description:
      'Remove a .scad file beside model.scad, as one revision (restore_version brings it back). Not model.scad.',
    input: z.object({ slug, name: fileName }),
    risk: 'write',
    routes: ['DELETE /api/v1/models/{slug}/files/{name}'],
    handler: async ({ slug, name }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/models/{slug}/files/{name}', { params: { path: { slug, name } } }),
          `remove ${slug}/${name}`,
        ),
      ),
  }),
]

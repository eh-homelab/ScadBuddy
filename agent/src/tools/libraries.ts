import { z } from 'zod'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, type Tool } from './registry.js'

// Libraries & fonts (issue #251): the library catalogue, pinning a library to a
// model (a revision of the model's `libraries` list), installed fonts, the
// Google Fonts catalogue, and installing a family. Routes:
// backend/scadbuddy/api/{libraries,models,fonts}.py.

const libraryName = z.string().min(1).describe('Library name, as list_libraries returns it')

export const libraryTools: Tool[] = [
  defineTool({
    name: 'list_libraries',
    description: 'The OpenSCAD library catalogue (e.g. BOSL2): name, git URL and default ref.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/libraries'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/libraries'), 'list libraries')),
  }),

  defineTool({
    name: 'pin_library',
    description:
      'Add a library to a model, or re-pin it at another tag or branch, so `include <…>`/`use <…>` resolve. ' +
      "Defaults to the catalogue's URL and ref.",
    input: z.object({
      slug,
      name: libraryName,
      ref: z.string().optional().describe('A tag or branch'),
      url: z.string().url().optional().describe('An https git URL'),
    }),
    risk: 'write',
    routes: ['PUT /api/v1/models/{slug}/libraries/{name}'],
    handler: async ({ slug, name, ref, url }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/models/{slug}/libraries/{name}', {
            params: { path: { slug, name } },
            body: { ref: ref ?? null, url: url ?? null },
          }),
          `pin ${name} to ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'unpin_library',
    description: 'Remove a library from a model, as a revision in its history.',
    input: z.object({ slug, name: libraryName }),
    risk: 'write',
    routes: ['DELETE /api/v1/models/{slug}/libraries/{name}'],
    handler: async ({ slug, name }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/models/{slug}/libraries/{name}', { params: { path: { slug, name } } }),
          `unpin ${name} from ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'list_fonts',
    description:
      'Font families installed for rendering. A `// font` parameter must name one exactly: a missing family ' +
      'silently falls back to DejaVu and changes the geometry.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/fonts'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/fonts'), 'list fonts')),
  }),

  defineTool({
    name: 'search_fonts',
    description: 'Search the Google Fonts catalogue for families that can be installed.',
    input: z.object({
      q: z.string().optional(),
      category: z.string().optional().describe('e.g. "serif", "sans-serif", "display", "handwriting", "monospace"'),
      limit: z.number().int().min(1).max(200).optional(),
    }),
    risk: 'read',
    routes: ['GET /api/v1/fonts/catalogue'],
    handler: async ({ q, category, limit }, { backend }) =>
      json(await ok(backend.GET('/api/v1/fonts/catalogue', { params: { query: { q, category, limit } } }), 'search fonts')),
  }),

  defineTool({
    name: 'install_font',
    description: 'Install a Google Fonts family onto the data volume so renders can use it.',
    input: z.object({ family: z.string().min(1).max(100), force: z.boolean().default(false) }),
    risk: 'write',
    routes: ['POST /api/v1/fonts/install'],
    handler: async ({ family, force }, { backend }) =>
      json(await ok(backend.POST('/api/v1/fonts/install', { body: { family, force } }), `install font ${family}`)),
  }),
]

import { z } from 'zod'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'

// Libraries & fonts (issue #251): the library catalogue, pinning a library to a
// model (a revision of the model's `libraries` list), installed fonts, the
// Google Fonts catalogue, and installing a family. Routes:
// backend/scadbuddy/api/{libraries,models,fonts}.py.

const libraryName = z.string().min(1).describe('Library name, as list_libraries returns it')

/**
 * The backend's `_same_repository` (backend/scadbuddy/library/libraries.py):
 * `https://host/o/r`, `.../r.git` and `.../r/` name one repository, whatever the
 * case of the scheme and host; the path's case is significant.
 */
export function sameRepository(first: string, second: string): boolean {
  const bare = (url: string): string => {
    const trimmed = url.replace(/\/+$/, '').replace(/\.git$/, '').replace(/\/+$/, '')
    const match = /^([^:]+):\/\/([^/?#]*)(.*)$/.exec(trimmed)
    return match ? `${match[1]!.toLowerCase()}://${match[2]!.toLowerCase()}${match[3]}` : trimmed
  }
  return bare(first) === bare(second)
}

export const libraryTools: Tool[] = [
  defineTool({
    name: 'list_libraries',
    description: 'The OpenSCAD library catalogue (e.g. BOSL2): name, git URL and default ref.',
    input: z.object({}),
    risk: 'read',
    routes: ['GET /api/v1/libraries'],
    handler: async (_args, { backend }) => json(await ok(backend.GET('/api/v1/libraries'), 'list libraries')),
  }),

  // Pinning is split by WHAT THE BACKEND FETCHES, because a tool's tier is static:
  //
  // - pin_library (write): only the catalogue's own repository for that name
  //   (no url, or one naming the same repository). Nothing a caller chose is fetched.
  // - pin_library_from_url (outward): any other https git URL, behind a human approval.
  //
  // SSRF into the cluster is already blocked by the backend for any URL
  // (backend/scadbuddy/library/libraries.py, module docstring and
  // `LibraryStore.resolve`: GIT_ALLOW_PROTOCOL limited to https; a URL that is
  // not the catalogue's vetted by url_import.py `public_addresses()` so every
  // resolved address must be public; the clone pinned to those addresses with
  // `http.curloptResolve`; no redirects followed). The `outward` tier is about
  // EXFILTRATION: a prompt-injected agent could encode data into a URL on a
  // public host it controls, so a human approves that fetch first (spec §8.2).
  defineTool({
    name: 'pin_library',
    description:
      'Add a catalogue library (see list_libraries) to a model, or re-pin it at another tag or branch, so ' +
      "`include <…>`/`use <…>` resolve. Uses the catalogue's repository; for any other git URL use " +
      'pin_library_from_url.',
    input: z.object({
      slug,
      name: libraryName,
      ref: z.string().optional().describe("A tag or branch; the catalogue's default when omitted"),
      url: z
        .string()
        .url()
        .optional()
        .describe("Only the catalogue's own URL for this library is accepted here"),
    }),
    risk: 'write',
    // Also reads GET /api/v1/libraries (list_libraries) when `url` is given.
    routes: ['PUT /api/v1/models/{slug}/libraries/{name}'],
    handler: async ({ slug, name, ref, url }, { backend }) => {
      if (url !== undefined) {
        // The backend's catalogue is the source of truth for "the catalogue's
        // URL", compared the way the backend does (libraries.py `_same_repository`).
        const catalogue = await ok(backend.GET('/api/v1/libraries'), 'list libraries')
        const known = catalogue.find((entry) => entry.name === name)
        if (!known || !sameRepository(url, known.url)) {
          throw new ToolError(
            `${url} is not the catalogue's repository for "${name}". Pinning from any other URL makes the ` +
              'backend fetch it, which needs a human approval: use pin_library_from_url.',
          )
        }
      }
      return json(
        await ok(
          backend.PUT('/api/v1/models/{slug}/libraries/{name}', {
            params: { path: { slug, name } },
            // The catalogue's URL is the backend's default; not sending it keeps
            // the request identical to a plain catalogue pin.
            body: { ref: ref ?? null, url: null },
          }),
          `pin ${name} to ${slug}`,
        ),
      )
    },
  }),

  defineTool({
    name: 'pin_library_from_url',
    description:
      'Pin a library to a model from an https git URL that is not the catalogue\'s. The backend clones that ' +
      'URL, so this needs a human approval. A non-catalogue URL needs a `ref`.',
    input: z.object({
      slug,
      name: z
        .string()
        .regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/)
        .describe('The directory `use <NAME/...>` names'),
      url: z.string().url().max(500).describe('An https git URL'),
      ref: z.string().min(1).describe('A tag or branch'),
    }),
    risk: 'outward',
    routes: ['PUT /api/v1/models/{slug}/libraries/{name}'],
    summarize: ({ slug, name, url, ref }) => `Clone ${url} at ${ref} and pin it to model "${slug}" as ${name}`,
    handler: async ({ slug, name, url, ref }, { backend }) =>
      json(
        await ok(
          backend.PUT('/api/v1/models/{slug}/libraries/{name}', {
            params: { path: { slug, name } },
            body: { ref, url },
          }),
          `pin ${name} from ${url} to ${slug}`,
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

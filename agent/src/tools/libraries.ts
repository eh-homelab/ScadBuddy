import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { ok } from './call.js'
import { slug } from './common.js'
import { defineTool, json, type Tool, ToolError } from './registry.js'

// Libraries & fonts (issue #251): the library catalogue, pinning a library to a
// model (a revision of the model's `libraries` list), installed fonts, the
// Google Fonts catalogue, and installing a family; include/use resolution and
// the font checks are #253. Routes: backend/scadbuddy/api/{libraries,models,fonts}.py.

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

function repin(backend: BackendClient, slug: string, name: string, ref: string | undefined) {
  return ok(
    backend.PATCH('/api/v1/models/{slug}/libraries/{name}', {
      params: { path: { slug, name } },
      body: { ref: ref ?? null },
    }),
    `re-pin ${name} of ${slug}`,
  )
}

export const libraryTools: Tool[] = [
  defineTool({
    name: 'list_libraries',
    description: 'The OpenSCAD library catalogue (e.g. BOSL2): name, git URL and default ref.',
    input: z.object({}),
    risk: 'read',
    source:
      'upstream OpenSCAD libraries fetched from third-party git repositories',
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
    description:
      'Remove a library from a model, as a revision in its history: every entry of that name, or with ' +
      "`index` only the invalid entry at that position (the model record's `invalid_libraries[].index`).",
    input: z.object({
      slug,
      name: libraryName,
      index: z
        .number()
        .int()
        .min(0)
        .optional()
        .describe('Only the invalid entry at this position of `libraries`; a 409 if it is no longer one of that name'),
    }),
    risk: 'write',
    routes: ['DELETE /api/v1/models/{slug}/libraries/{name}'],
    handler: async ({ slug, name, index }, { backend }) =>
      json(
        await ok(
          backend.DELETE('/api/v1/models/{slug}/libraries/{name}', {
            params: { path: { slug, name }, ...(index === undefined ? {} : { query: { index } }) },
          }),
          `unpin ${name} from ${slug}`,
        ),
      ),
  }),

  // Re-pinning clones again from the URL the model's pin already records, at a
  // ref the caller chooses (backend/scadbuddy/api/libraries.py, PATCH). Tiered
  // like pinning, by WHAT IS FETCHED: when the recorded URL is the catalogue's
  // repository it is `write` (nothing a caller chose leaves the pod); when it is
  // any other URL (a fork, a private repo) it is `outward`, because the ref
  // name reaches that server during the fetch and could carry data to a host
  // an injected agent controls. SSRF is the backend's (see the pin tools above).
  defineTool({
    name: 'repin_library',
    description:
      "Re-pin a model's catalogue library from the repository it already pins, at `ref` or at the ref " +
      'already pinned (moving a branch pin to the branch\'s current commit), as a revision in its history. ' +
      'For a library pinned from a non-catalogue URL use repin_library_from_pinned_url.',
    input: z.object({ slug, name: libraryName, ref: z.string().optional().describe('A tag or branch') }),
    risk: 'write',
    // Also reads GET /models/{slug} (get_model) and GET /libraries (list_libraries).
    routes: ['PATCH /api/v1/models/{slug}/libraries/{name}'],
    handler: async ({ slug, name, ref }, { backend }) => {
      const [model, catalogue] = await Promise.all([
        ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug } } }), `get model ${slug}`),
        ok(backend.GET('/api/v1/libraries'), 'list libraries'),
      ])
      const pinned = (model.libraries ?? []).find((l) => l.name === name)
      if (!pinned) throw new ToolError(`model ${slug} does not pin a library named "${name}"`)
      const known = catalogue.find((entry) => entry.name === name)
      if (!known || !sameRepository(pinned.url, known.url)) {
        throw new ToolError(
          `"${name}" is pinned from ${pinned.url}, not the catalogue's repository. Re-pinning it fetches from ` +
            'that URL, which needs a human approval: use repin_library_from_pinned_url.',
        )
      }
      return json(await repin(backend, slug, name, ref))
    },
  }),

  defineTool({
    name: 'repin_library_from_pinned_url',
    description:
      "Re-pin a model's library from the non-catalogue URL its pin already records (a fork, another repo), " +
      'at `ref` or the ref already pinned. The backend clones that URL again, so this needs a human approval.',
    input: z.object({ slug, name: libraryName, ref: z.string().optional().describe('A tag or branch') }),
    risk: 'outward',
    routes: ['PATCH /api/v1/models/{slug}/libraries/{name}'],
    summarize: ({ slug, name, ref }) =>
      `Clone library ${name} of model "${slug}" again from the URL its pin records, at ${ref ?? 'the pinned ref'}`,
    handler: async ({ slug, name, ref }, { backend }) => json(await repin(backend, slug, name, ref)),
  }),

  defineTool({
    name: 'list_installed_libraries',
    description: 'Every library checkout on the data volume, with the models whose live pins read it.',
    input: z.object({}),
    risk: 'read',
    source:
      'upstream OpenSCAD libraries fetched from third-party git repositories',
    routes: ['GET /api/v1/libraries/installed'],
    handler: async (_args, { backend }) =>
      json(await ok(backend.GET('/api/v1/libraries/installed'), 'list installed libraries')),
  }),

  defineTool({
    name: 'remove_library_checkout',
    description:
      "Delete a library's checkout at `commit`, or all of its checkouts, from the volume. Refused while a " +
      'model pins it or a render reads it. Irreversible (a later render of an old revision needs a re-pin), ' +
      'so it needs a human approval.',
    input: z.object({
      name: libraryName,
      commit: z.string().regex(/^[0-9a-f]{40}([0-9a-f]{24})?$/).optional(),
    }),
    risk: 'outward',
    routes: ['DELETE /api/v1/libraries/{name}'],
    summarize: ({ name, commit }) =>
      commit ? `Delete the ${name} checkout at ${commit}` : `Delete every checkout of library ${name}`,
    handler: async ({ name, commit }, { backend }) => {
      await ok(
        backend.DELETE('/api/v1/libraries/{name}', { params: { path: { name }, query: { commit: commit ?? null } } }),
        `remove ${name} checkouts`,
      )
      return json({ removed: name, commit: commit ?? 'all' })
    },
  }),

  // Include/use resolution (#253): the backend resolves each target the way
  // OpenSCAD's find_valid_path does (beside the file, then each pinned checkout
  // on OPENSCADPATH), in backend/scadbuddy/library/includes.py. Read-only; the
  // route is a POST only because it takes an unsaved source.
  defineTool({
    name: 'check_dependencies',
    description:
      "Resolve a model's `include <…>`/`use <…>` targets against its own files and pinned libraries, as a " +
      'render would, without rendering or fetching anything. Each target is `resolved` (with the file and ' +
      'library it resolved to) or `unresolved` with the reason and, when one exists, a `suggestion`: the ' +
      'catalogue library to pin (pin_library), or one another model pins from its own URL ' +
      '(pin_library_from_url). Also lists every `font = "…"` literal with the families not installed. Pass ' +
      '`source` to check an unsaved edit. OpenSCAD only warns on a missing include and renders without it.',
    input: z.object({
      slug,
      source: z
        .string()
        .max(1_000_000)
        .optional()
        .describe("Unsaved OpenSCAD source to check in place of the model's saved model.scad"),
    }),
    risk: 'read',
    source: "the model's own source and file names, and library data fetched from third-party git repositories",
    routes: ['POST /api/v1/models/{slug}/dependencies'],
    handler: async ({ slug, source }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/{slug}/dependencies', {
            params: { path: { slug } },
            body: { source: source ?? null },
          }),
          `check the dependencies of ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'list_fonts',
    description:
      'Font families installed for rendering. A `// font` value must name one of them (case and spaces do not ' +
      'matter): OpenSCAD itself would silently draw a missing family in DejaVu Sans with other geometry, so ' +
      'render_model and save_preset refuse one with an error naming it. install_font adds a family.',
    input: z.object({}),
    risk: 'read',
    source:
      'installed font names, some fetched from the web',
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
    source:
      'font metadata from the Google Fonts catalogue on the web',
    routes: ['GET /api/v1/fonts/catalogue'],
    handler: async ({ q, category, limit }, { backend }) =>
      json(await ok(backend.GET('/api/v1/fonts/catalogue', { params: { query: { q, category, limit } } }), 'search fonts')),
  }),

  defineTool({
    name: 'install_font',
    description:
      'Install a Google Fonts family onto the data volume so renders can use it. The backend then checks that ' +
      'fontconfig resolves the family where renders run, and answers an error if it does not (the files may ' +
      'name another family: see list_fonts).',
    input: z.object({ family: z.string().min(1).max(100), force: z.boolean().default(false) }),
    risk: 'write',
    routes: ['POST /api/v1/fonts/install'],
    handler: async ({ family, force }, { backend }) =>
      json(await ok(backend.POST('/api/v1/fonts/install', { body: { family, force } }), `install font ${family}`)),
  }),
]

import { z } from 'zod'
import { command } from './command.js'
import { ok } from './call.js'
import { commit, slug } from './common.js'
import { defineTool, json, text, type Tool } from './registry.js'
import { cursorPosition, DEFAULT_PAGE_SIZE, page, PAGED, pageInput, StaleCursorError } from './pagination.js'

// History (issue #251): versions, diff, source at a commit, restore, and a
// duplicate's upstream status/merge/dismiss/detach. Every write here is itself
// a revision, so it is `write`, reversible through history (spec §8.1).
// Routes: backend/scadbuddy/api/{versions,upstream}.py over library/history.py.

/** The backend's cap on one versions read (`limit` ≤ 500 in backend/openapi.json). */
const HISTORY_WINDOW = 500

export const historyTools: Tool[] = [
  defineTool({
    name: 'list_versions',
    description:
      "A model's revision history, newest first: commit id, message and time. Only the newest " +
      `${HISTORY_WINDOW} revisions can be paged to, and \`total\` is null while more may follow.` + PAGED,
    input: z.object({ slug, ...pageInput }),
    risk: 'read',
    source:
      'revision messages written by model authors or upstreams',
    routes: ['GET /api/v1/models/{slug}/versions'],
    handler: async ({ slug, ...args }, { backend }) => {
      // The backend takes a limit, not a cursor: read only as far as this page, plus a
      // page of slack for revisions made since the cursor was issued (they push its
      // item down) and one more to know whether there is a next; not the whole window.
      // More revisions than the slack push the item past that read, which is not
      // staleness: read the whole window before calling the cursor stale (#841 review).
      const size = args.limit ?? DEFAULT_PAGE_SIZE
      const read = (limit: number) =>
        ok(backend.GET('/api/v1/models/{slug}/versions', { params: { path: { slug }, query: { limit } } }), `list versions of ${slug}`)
      const listed = (versions: { commit: string }[], limit: number) =>
        page(versions, { slug, ...args }, (v) => v.commit, 'list_versions', { complete: versions.length < limit })
      const limit = Math.min(HISTORY_WINDOW, cursorPosition(args.cursor, 'list_versions') + 2 * size + 1)
      const versions = await read(limit)
      try {
        return json(listed(versions, limit))
      } catch (err) {
        if (!(err instanceof StaleCursorError) || versions.length < limit || limit === HISTORY_WINDOW) throw err
        return json(listed(await read(HISTORY_WINDOW), HISTORY_WINDOW))
      }
    },
  }),

  defineTool({
    name: 'diff_version',
    description: 'The unified diff of a revision against its parent, or against `base` when given.',
    input: z.object({ slug, commit, base: commit.optional() }),
    risk: 'read',
    source:
      "OpenSCAD source (code and comments) written by the model's author, imported from the web or pulled from an upstream",
    routes: ['GET /api/v1/models/{slug}/versions/{commit}/diff'],
    handler: async ({ slug, commit, base }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/models/{slug}/versions/{commit}/diff', {
            params: { path: { slug, commit }, query: { base } },
          }),
          `diff ${slug}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'get_version_source',
    description: "A model's OpenSCAD source at an earlier revision.",
    input: z.object({ slug, commit }),
    risk: 'read',
    source:
      "OpenSCAD source (code and comments) written by the model's author, imported from the web or pulled from an upstream",
    routes: ['GET /api/v1/models/{slug}/versions/{commit}/source'],
    handler: async ({ slug, commit }, { backend }) =>
      text(
        await ok(
          backend.GET('/api/v1/models/{slug}/versions/{commit}/source', {
            params: { path: { slug, commit } },
            parseAs: 'text',
          }),
          `get source of ${slug}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'restore_version',
    description: 'Restore an earlier revision as a new revision on top of the history (nothing is lost).',
    input: z.object({ slug, commit }),
    risk: 'write',
    routes: ['POST /api/v1/models/{slug}/versions/{commit}/restore'],
    handler: async ({ slug, commit }, ctx) =>
      json(
        await command(ctx, `restore ${slug}@${commit}`, (headers) =>
          ctx.backend.POST('/api/v1/models/{slug}/versions/{commit}/restore', { params: { path: { slug, commit } }, headers }),
        ),
      ),
  }),

  defineTool({
    name: 'get_upstream',
    description:
      "A duplicate's upstream template: whether it has moved on, and a preview of merging its current revision.",
    input: z.object({ slug }),
    risk: 'read',
    source:
      "upstream metadata fetched from the model's upstream",
    routes: ['GET /api/v1/models/{slug}/upstream'],
    handler: async ({ slug }, { backend }) =>
      json(await ok(backend.GET('/api/v1/models/{slug}/upstream', { params: { path: { slug } } }), `get upstream of ${slug}`)),
  }),

  defineTool({
    name: 'update_from_upstream',
    description:
      "Act on a duplicate's upstream: `merge` its current revision, `dismiss` that revision (stop offering " +
      'it), or `detach` from an upstream that no longer exists.',
    input: z.object({ slug, action: z.enum(['merge', 'dismiss', 'detach']) }),
    risk: 'write',
    routes: [
      'POST /api/v1/models/{slug}/upstream/merge',
      'POST /api/v1/models/{slug}/upstream/dismiss',
      'POST /api/v1/models/{slug}/upstream/detach',
    ],
    handler: async ({ slug, action }, ctx) => {
      const what = `${action} upstream of ${slug}`
      const params = { path: { slug } }
      switch (action) {
        case 'merge':
          return json(await command(ctx, what, (headers) => ctx.backend.POST('/api/v1/models/{slug}/upstream/merge', { params, headers })))
        case 'dismiss':
          return json(await command(ctx, what, (headers) => ctx.backend.POST('/api/v1/models/{slug}/upstream/dismiss', { params, headers })))
        case 'detach':
          return json(await command(ctx, what, (headers) => ctx.backend.POST('/api/v1/models/{slug}/upstream/detach', { params, headers })))
      }
    },
  }),
]

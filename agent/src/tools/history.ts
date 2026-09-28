import { z } from 'zod'
import { ok } from './call.js'
import { commit, slug } from './common.js'
import { defineTool, json, text, type Tool } from './registry.js'

// History (issue #251): versions, diff, source at a commit, restore, and a
// duplicate's upstream status/merge/dismiss/detach. Every write here is itself
// a revision, so it is `write`, reversible through history (spec §8.1).
// Routes: backend/scadbuddy/api/{versions,upstream}.py over library/history.py.

export const historyTools: Tool[] = [
  defineTool({
    name: 'list_versions',
    description: "A model's revision history, newest first: commit id, message and time.",
    input: z.object({ slug, limit: z.number().int().min(1).max(500).optional() }),
    risk: 'read',
    routes: ['GET /api/v1/models/{slug}/versions'],
    handler: async ({ slug, limit }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/models/{slug}/versions', { params: { path: { slug }, query: { limit } } }),
          `list versions of ${slug}`,
        ),
      ),
  }),

  defineTool({
    name: 'diff_version',
    description: 'The unified diff of a revision against its parent, or against `base` when given.',
    input: z.object({ slug, commit, base: commit.optional() }),
    risk: 'read',
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
    handler: async ({ slug, commit }, { backend }) =>
      json(
        await ok(
          backend.POST('/api/v1/models/{slug}/versions/{commit}/restore', { params: { path: { slug, commit } } }),
          `restore ${slug}@${commit}`,
        ),
      ),
  }),

  defineTool({
    name: 'get_upstream',
    description:
      "A duplicate's upstream template: whether it has moved on, and a preview of merging its current revision.",
    input: z.object({ slug }),
    risk: 'read',
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
    handler: async ({ slug, action }, { backend }) => {
      const options = { params: { path: { slug } } }
      const what = `${action} upstream of ${slug}`
      switch (action) {
        case 'merge':
          return json(await ok(backend.POST('/api/v1/models/{slug}/upstream/merge', options), what))
        case 'dismiss':
          return json(await ok(backend.POST('/api/v1/models/{slug}/upstream/dismiss', options), what))
        case 'detach':
          return json(await ok(backend.POST('/api/v1/models/{slug}/upstream/detach', options), what))
      }
    },
  }),
]

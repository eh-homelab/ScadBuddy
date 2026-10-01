import { z } from 'zod'
import { ok } from './call.js'
import { commit, slug } from './common.js'
import { defineTool, json, ToolError, type Tool } from './registry.js'

// The agent's edit loop (issue #252): patch a model's source against the
// revision it read, and checkpoint before a try that may not work out. Every
// accepted edit is one revision in the model's history, authored as the agent
// (authorship.ts), so `restore_version` undoes any of them (spec §8.1: `write`
// is "reversible through history"). Routes: backend/scadbuddy/api/models.py.

const COMMIT_ID = /^[0-9a-f]{7,64}$/

/** A backend 409's `current` (RFC 9457 extension member), when it is a commit id. */
function currentOf(error: unknown): string | null {
  const current = (error as { current?: unknown } | undefined)?.current
  return typeof current === 'string' && COMMIT_ID.test(current) ? current : null
}

export const authoringTools: Tool[] = [
  defineTool({
    name: 'apply_patch',
    description:
      "Change part of a model's OpenSCAD source without resending all of it: a unified diff (`patch`, as " +
      '`diff -u` or `git diff` writes it) or search/replace `edits` (each search must occur exactly once). ' +
      "`base` is the revision you read the source at (get_model's `version`, or checkpoint's). Saved as one " +
      'revision, like update_source, and parse-checked unless `force`. If the model has moved on since ' +
      '`base`, nothing is written and the result names the `current` revision: read the source again and ' +
      'rebuild the patch. A hunk or edit that does not apply is refused by number, with nothing written.',
    input: z.object({
      slug,
      base: commit.describe("The revision the patch was made against: get_model's `version`"),
      patch: z.string().max(1_000_000).optional().describe('A unified diff of the source'),
      edits: z
        .array(z.object({ search: z.string().min(1), replace: z.string() }))
        .min(1)
        .max(100)
        .optional()
        .describe('Search/replace edits, applied in order'),
      message: z
        .string()
        .max(200)
        .optional()
        .describe("What the revision is called in the history: the user's instruction, in short"),
      force: z.boolean().default(false),
    }),
    risk: 'write',
    source:
      "the backend's account of the edit, whose refusal can quote the model's OpenSCAD source",
    routes: ['POST /api/v1/models/{slug}/source/patch'],
    handler: async ({ slug, base, patch, edits, message, force }, { backend }) => {
      if ((patch === undefined) === (edits === undefined)) {
        throw new ToolError('give exactly one of `patch` and `edits`')
      }
      const answered = await backend.POST('/api/v1/models/{slug}/source/patch', {
        params: { path: { slug } },
        body: { base, patch: patch ?? null, edits: edits ?? null, message: message ?? null, force },
      })
      if (answered.response.status === 409) {
        const current = currentOf(answered.error)
        if (current !== null) {
          return {
            ...json({
              status: 'conflict',
              base,
              current,
              next:
                'Nothing was written: the model changed after you read it. Call get_source (and get_model ' +
                'for its version) again, rebuild the patch against that, and pass `current` as `base`.',
            }),
            isError: true,
          }
        }
      }
      return json(await ok(Promise.resolve(answered), `patch source of ${slug}`))
    },
  }),

  defineTool({
    name: 'checkpoint',
    description:
      'Mark where a model is now, before trying something that may not work out. Returns its current ' +
      'revision: every edit after it is a revision of its own, so restore_version with this commit abandons ' +
      'them all at once (the history keeps them). The revision is also the `base` for apply_patch.',
    input: z.object({
      slug,
      label: z.string().max(200).optional().describe('What you are about to try; echoed back'),
    }),
    risk: 'read',
    source: 'model metadata (names, descriptions, tags) written by model authors or imported from the web',
    routes: ['GET /api/v1/models/{slug}'],
    handler: async ({ slug, label }, { backend }) => {
      const model = await ok(backend.GET('/api/v1/models/{slug}', { params: { path: { slug } } }), `get model ${slug}`)
      if (!model.version) {
        throw new ToolError(`${slug} has no revision history on this instance, so there is nothing to return to`)
      }
      return json({
        slug,
        checkpoint: model.version,
        ...(label ? { label } : {}),
        restore: { tool: 'restore_version', arguments: { slug, commit: model.version } },
      })
    },
  }),
]

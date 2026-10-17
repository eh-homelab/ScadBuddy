import { z } from 'zod'
import { ok } from './call.js'
import { answered } from './command.js'
import { defineTool, json, type Tool } from './registry.js'

// Blob store migration (spec 2026-10-16 §4.3). Routes: backend/scadbuddy/api/store.py
// `migrate_store` and `get_store_migration`. The start answers 202 with its workflow id
// (not an operation), so it is not `command()`, which would follow a 202 as one, and the
// route takes no Idempotency-Key: a second start joins the running migration, so `answered`'s
// re-send after a lost answer is harmless.

const backend = z.enum(['bambuddy', 's3'])

export const storeTools: Tool[] = [
  defineTool({
    name: 'store_migrate',
    description:
      'Copy every recorded blob from the `source` store backend to `target`, `batch` at a time, as a ' +
      'background workflow. Save the target backend\'s settings first. Each row switches to its copy when ' +
      'ScadBuddy next starts with store_backend set to `target`. A second call while it runs joins it; one ' +
      'after it ended retries what failed. Refused (409) once store_backend is already `target`. Follow it ' +
      'with store_migrate_status.',
    input: z.object({
      source: backend.default('bambuddy'),
      target: backend.default('s3'),
      batch: z.number().int().min(1).max(1000).default(50).describe('Rows copied per batch'),
    }),
    risk: 'outward',
    routes: ['POST /api/v1/store/migrate'],
    summarize: ({ source, target }) => `Migrate the blob store from ${source} to ${target}`,
    handler: async ({ source, target, batch }, ctx) =>
      json(
        await ok(
          answered(ctx, () => ctx.backend.POST('/api/v1/store/migrate', { body: { source, target, batch } }), 'start store migration'),
          'start store migration',
        ),
      ),
  }),

  defineTool({
    name: 'store_migrate_status',
    description:
      "A store migration's progress: rows still to copy (`remaining`), `copied`, the ones whose copy " +
      'failed and why (`failed`, up to 200 of `failed_count`), and whether it `running`. Refused (404) when ' +
      'none ever ran for the pair.',
    input: z.object({ source: backend.default('bambuddy'), target: backend.default('s3') }),
    risk: 'read',
    routes: ['GET /api/v1/store/migrate'],
    handler: async ({ source, target }, { backend }) =>
      json(
        await ok(
          backend.GET('/api/v1/store/migrate', { params: { query: { source, target } } }),
          'get store migration',
        ),
      ),
  }),
]

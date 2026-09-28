import type { Operation } from './registry.js'

// The openapi coverage allowlist (spec §5.1: "A CI check fails when an
// operation in backend/openapi.json has neither a tool nor an explicit
// allowlist entry"). test/coverage.test.ts enforces it.

/** Backend operations deliberately left without a tool, each with the reason. */
export const NOT_A_TOOL: readonly { operation: Operation; reason: string }[] = [
  {
    operation: 'PUT /api/v1/settings',
    reason:
      'Writes the Bambuddy URL and API key. Credentials are entered in the Settings UI only and never pass ' +
      'through an agent (spec §8.6, credential leakage).',
  },
  {
    operation: 'POST /api/v1/settings/register-sidebar',
    reason: "One-time setup that edits Bambuddy's own sidebar; an operator action in Settings, not an agent task.",
  },
  {
    operation: 'PUT /api/v1/outputs/{output_id}/thumbnail',
    reason:
      "Uploads the 3D viewer's canvas capture. An agent has no canvas; a browser_* tool driving the open tab " +
      'can (#254, #266).',
  },
  // #274: template media. An agent reads `media` (ids, captions, order) from the model record.
  ...(
    [
      'GET /api/v1/models/{slug}/media/{item_id}',
      'GET /api/v1/models/{slug}/media/{item_id}/poster',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      'Serves an image or video file to the browser; an agent gets the item list from the model record ' +
      'and has no use for the bytes.',
  })),
  {
    operation: 'POST /api/v1/models/{slug}/media',
    reason: 'A multipart upload of an image or video from the user; an agent has no file to send.',
  },
  ...(
    [
      'PATCH /api/v1/models/{slug}/media/{item_id}',
      'PUT /api/v1/models/{slug}/media/order',
      'DELETE /api/v1/models/{slug}/media/{item_id}',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      "Captioning, reordering and removing a template's media happen on the edit page (#279); the plan " +
      'adds no agent tools in the gallery epic (#273, decision 7).',
  })),
  // #477: Bambuddy archive files, proxied for the print history (#305).
  ...(
    [
      'GET /api/v1/prints/{archive_id}/files/sliced',
      'GET /api/v1/prints/{archive_id}/files/source',
      'GET /api/v1/prints/{archive_id}/photos/{filename}',
      'GET /api/v1/prints/{archive_id}/plates/{index}/thumbnail',
      'GET /api/v1/prints/{archive_id}/thumbnail',
      'GET /api/v1/prints/{archive_id}/timelapse',
    ] as const
  ).map((operation) => ({
    operation,
    reason:
      "A Range-passing proxy of a Bambuddy archive's files, photos and timelapse for the browser's print " +
      'history; binary content, not an agent task.',
  })),
]

/**
 * Tools whose backend routes are in open PRs, so they are not in
 * backend/openapi.json on main yet. Listed so the gap is visible. An entry
 * must not outlive its route landing: test/coverage.test.ts fails as soon as
 * one of these operations appears in the spec, until it gets its tool (or a
 * NOT_A_TOOL entry) and leaves this list.
 */
export const PENDING_ROUTES: readonly { operation: string; pr: number; tool: string; reason: string }[] = [
  // Empty: #320's and #324's routes have merged and have their tools.
]

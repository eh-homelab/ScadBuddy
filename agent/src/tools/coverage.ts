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
]

/**
 * Tools whose backend routes are in open PRs, so they are not in
 * backend/openapi.json on main yet. Listed so the gap is visible, and so a PR
 * that adds one of these routes does not fail this check before its tool is
 * written; each becomes a tool once its PR merges.
 */
export const PENDING_ROUTES: readonly { operation: string; pr: number; tool: string; reason: string }[] = [
  {
    operation: 'GET /api/v1/outputs/{output_id}/geometry',
    pr: 320,
    tool: 'get_output_geometry',
    reason: 'Geometry facts about an output (volume, overhangs, walls) for analyzers; route added by #320.',
  },
  {
    operation: 'GET /api/v1/models/{slug}/diagnostics',
    pr: 324,
    tool: 'get_render_diagnostics',
    reason: "OpenSCAD's warnings and errors with file and line, from the latest settled render; route added by #324.",
  },
  {
    operation: 'GET /api/v1/jobs/{job_id}/views/{view}.png',
    pr: 324,
    tool: 'get_render_view',
    reason: 'Rendered views (front, top, …) of a job for visual checks; route added by #324.',
  },
  {
    operation: 'GET /api/v1/outputs/{output_id}/views/{view}.png',
    pr: 324,
    tool: 'get_output_view',
    reason: 'Rendered views of a saved output; route added by #324.',
  },
  {
    operation: 'GET /api/v1/libraries/installed',
    pr: 324,
    tool: 'list_installed_libraries',
    reason: 'Library checkouts on the volume and the models that use them; route added by #324.',
  },
  {
    operation: 'DELETE /api/v1/libraries/{name}',
    pr: 324,
    tool: 'remove_library_checkout',
    reason: 'Deletes library checkouts no model uses (outward: irreversible); route added by #324.',
  },
]

import type { Operation } from './registry.js'

// The openapi coverage allowlist (spec §5.1: "A CI check fails when an
// operation in backend/openapi.json has neither a tool nor an explicit
// allowlist entry"). test/coverage.test.ts enforces it.

const ANALYZERS_LATER =
  'The print analyzers (#284) land API-first; their agent tools are a follow-up. Applying a fix or ' +
  'recording a decision is outward and would need the approval flow (AI spec §8.1).'

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
  {
    operation: 'GET /api/v1/analyzers',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/run',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/fixes/preview',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/fixes/apply',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'GET /api/v1/analyzers/decisions',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'POST /api/v1/analyzers/decisions',
    reason: ANALYZERS_LATER,
  },
  {
    operation: 'DELETE /api/v1/analyzers/decisions/{decision_id}',
    reason: ANALYZERS_LATER,
  },
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

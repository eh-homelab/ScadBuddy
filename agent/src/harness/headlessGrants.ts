import { randomUUID } from 'node:crypto'
import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { z } from 'zod'
import { AUTHORIZE_TOOL, AUTHORIZE_TOOL_NAME, GRANT_SERVER } from './headlessBrowser.js'

// How the headless browser gets past the backend's agent-actor gate ONCE, with
// a human's approval (#349, spec §5.3: "unless an approved, unconsumed outward
// action for that session authorises that request; the backend consumes it
// once").
//
// A click in the headless UI is a `write`-tier tool call; the request the page
// then makes may be outward (send, print, delete, a settings write). The
// backend refuses such a request when it carries the agent-actor marker
// (backend/scadbuddy/api/agent_actor.py). To make it, the model first calls
// `mcp__scadbuddy_browser__authorize_request` with that request's method and
// exact path. That tool is OUTWARD tier, so it parks for a human approval in
// the ScadBuddy UI like every other outward call (approvals/service.ts, #258),
// bound to that exact input. Only once it is approved does the handler below
// run: it finds the approval it ran under (same session, same turn, same tool,
// same input hash, approved and consumed) and writes one row to
// `ai_headless_grants` naming that approval, the turn, the method and the path.
//
// The backend lets a marked request through for a grant only when (its
// GRANT_SQL, checked against this schema by test/headlessGrants.pg.test.ts):
//   - session, method and exact path match;
//   - the grant is unused and not expired (GRANT_TTL_SECONDS);
//   - its turn is still the session's live turn (so an interrupt, a handoff, a
//     new turn or the end of the turn voids it);
//   - its approval is approved and consumed for this session;
// and it marks the grant used in the same statement: one request per grant.

/** How long a grant waits to be used; the model clicks right after. */
export const GRANT_TTL_SECONDS = 120

const METHODS = ['POST', 'PUT', 'PATCH', 'DELETE'] as const
/** Same rule as the migration's CHECK: an /api/v1 path, no query, no dot segments. */
const PATH = /^\/api\/v1\/[A-Za-z0-9._~%/-]+$/
const DOT_SEGMENT = /\/\.\.?(\/|$)/

export const AuthorizeInput = {
  method: z.enum(METHODS).describe('The HTTP method of the request the page will make'),
  path: z
    .string()
    .max(512)
    .regex(PATH, 'an /api/v1/... path with no query string')
    .refine((p) => !DOT_SEGMENT.test(p), 'no . or .. segments')
    .describe('The exact request path, e.g. /api/v1/print/outputs/<id>/run (no query string)'),
}

export type GrantContext = {
  sql: Sql
  sessionId: string
  turnId: string
  /** The approval input hash (ApprovalService.hash), so the grant binds to the approved input. */
  hash: (tool: string, input: Record<string, unknown>) => string
  ttlSeconds?: number
}

export type GrantResult = { ok: true; id: string; expiresAt: string } | { ok: false; reason: string }

/** Writes the grant for an approved, consumed call of the authorize tool in this turn. */
export async function recordGrant(
  context: GrantContext,
  input: { method: (typeof METHODS)[number]; path: string },
): Promise<GrantResult> {
  const hash = context.hash(AUTHORIZE_TOOL_NAME, input)
  const [approval] = await context.sql<{ id: string }[]>`
    SELECT a.id FROM ai_approvals a
    WHERE a.session_id = ${context.sessionId}
      AND (a.turn_id = ${context.turnId} OR a.resume_turn_id = ${context.turnId})
      AND a.tool = ${AUTHORIZE_TOOL_NAME} AND a.input_hash = ${hash}
      AND a.decision = 'approved' AND a.consumed_at IS NOT NULL AND a.revoked_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM ai_headless_grants g WHERE g.approval_id = a.id)
    ORDER BY a.consumed_at DESC LIMIT 1`
  if (!approval) {
    return { ok: false, reason: 'no approved, unused approval of this exact request was found in this turn' }
  }
  const id = randomUUID()
  const ttl = context.ttlSeconds ?? GRANT_TTL_SECONDS
  const [row] = await context.sql<{ expires_at: Date }[]>`
    INSERT INTO ai_headless_grants (id, session_id, turn_id, approval_id, method, path, expires_at)
    VALUES (${id}, ${context.sessionId}, ${context.turnId}, ${approval.id}, ${input.method}, ${input.path},
            now() + (${ttl} * interval '1 second'))
    ON CONFLICT (approval_id) DO NOTHING
    RETURNING expires_at`
  if (!row) return { ok: false, reason: 'that approval has already been turned into a grant' }
  return { ok: true, id, expiresAt: row.expires_at.toISOString() }
}

/** The in-process MCP server a turn with the headless browser gets. */
export function headlessGrantServer(context: GrantContext): McpSdkServerConfigWithInstance {
  const authorize = tool(
    AUTHORIZE_TOOL,
    'Ask the human to allow ONE outward request from the headless browser: send, print, delete, or a ' +
      'settings change. The backend refuses such requests from the headless browser (403 "Needs ' +
      'approval"). Give the method and exact path the page uses (browser_network_requests shows them). ' +
      'When the human approves, repeat the click once, within two minutes and in this same turn.',
    AuthorizeInput,
    async (args) => {
      const result = await recordGrant(context, args)
      return result.ok
        ? {
            content: [
              {
                type: 'text' as const,
                text:
                  `Approved: the headless browser may now make ${args.method} ${args.path} once, ` +
                  `until ${result.expiresAt}, in this turn. Repeat the click now.`,
              },
            ],
          }
        : { content: [{ type: 'text' as const, text: `Not granted: ${result.reason}.` }], isError: true }
    },
  )
  return createSdkMcpServer({ name: GRANT_SERVER, tools: [authorize] })
}

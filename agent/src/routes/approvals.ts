import type { Hono } from 'hono'
import { z } from 'zod'
import { ApprovalError, type ApprovalRecord, type ApprovalService } from '../approvals/service.js'
import type { OriginPolicy } from '../http/origins.js'
import type { Owner } from '../sessions/protocol.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'

// /api/v1/ai/approvals (#258): the panel's and the tests' way to see and decide
// approvals of outward tool calls until #266's socket carries the panel's
// `approval.decision` (which calls ApprovalService.decision directly).
//
//   GET  /api/v1/ai/approvals?session=<id>[&pending=true]  a session's approvals
//   GET  /api/v1/ai/approvals                              every pending one (browser view)
//   POST /api/v1/ai/approvals/:id/approve                  body: optional {"input_hash": "…"}
//   POST /api/v1/ai/approvals/:id/deny
//
// The writes pass guard.ts first (the UI's origin, through the HTTPS ingress);
// a request that passes is the browser user (spec §8.1: the browser user
// "approves in the UI"). Reads pass guard.ts `uiReadProblem` (transport, and
// the UI's origin as far as a same-origin GET shows it); they return the
// scrubbed summary only, never a tool's full input. Error bodies use
// `{ detail }`, the backend's FastAPI shape.

/** The principal an HTTP request from the UI acts as. */
export const BROWSER_USER: Owner = { kind: 'browser', id: 'browser', label: 'You' }

export type ApprovalRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  approvals: ApprovalService | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type ApprovalView = {
  id: string
  session_id: string | null
  tool_use_id: string
  tool: string
  input_summary: string
  input_hash: string
  tier: string
  requested_by: Owner
  created_at: string
  expires_at: string
  decision: ApprovalRecord['decision']
  decided_by: Owner | null
  decided_at: string | null
  reason: string | null
  used: boolean
  /** Approved but withdrawn before it was used. */
  voided: boolean
}

export function approvalView(a: ApprovalRecord): ApprovalView {
  return {
    id: a.id,
    session_id: a.sessionId,
    tool_use_id: a.toolUseId,
    tool: a.tool,
    input_summary: a.inputSummary,
    input_hash: a.inputHash,
    tier: a.tier,
    requested_by: a.requestedBy,
    created_at: a.createdAt,
    expires_at: a.expiresAt,
    decision: a.decision,
    decided_by: a.decidedBy,
    decided_at: a.decidedAt,
    reason: a.reason,
    used: a.consumedAt !== null,
    voided: a.revokedAt !== null,
  }
}

const DecisionBody = z.strictObject({ input_hash: z.string().regex(/^[0-9a-f]{64}$/).optional() })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

export function registerApprovalRoutes(app: Hono, deps: ApprovalRouteDeps): void {
  const base = '/api/v1/ai/approvals'

  async function service(): Promise<ApprovalService | string> {
    if (!deps.approvals) return NO_DATABASE
    return (await deps.ready()) ? deps.approvals : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'approval reads')
    if (problem) return c.json({ detail: problem }, 403)
    const approvals = await service()
    if (typeof approvals === 'string') return c.json({ detail: approvals }, 503)
    const session = c.req.query('session')
    const pending = c.req.query('pending') === 'true'
    try {
      const list = await approvals.list(BROWSER_USER, { ...(session ? { sessionId: session } : {}), pending })
      return c.json({ approvals: list.map(approvalView) })
    } catch (err) {
      if (err instanceof ApprovalError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })

  // The body is at most {"input_hash": "<64 hex>"}; anything over the cap is 413 unread.
  app.post(`${base}/:id/:verb{approve|deny}`, jsonBodyLimit(), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'approval decisions')
    if (problem) return c.json({ detail: problem }, 403)
    const approvals = await service()
    if (typeof approvals === 'string') return c.json({ detail: approvals }, 503)

    let body: z.infer<typeof DecisionBody> = {}
    const raw = await c.req.text()
    if (raw.trim() !== '') {
      const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
      if (type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 415)
      try {
        body = DecisionBody.parse(JSON.parse(raw))
      } catch (err) {
        const detail =
          err instanceof z.ZodError
            ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
            : 'body is not valid JSON'
        return c.json({ detail }, 400)
      }
    }
    try {
      const decided = await approvals.decide(BROWSER_USER, c.req.param('id'), c.req.param('verb') === 'approve', {
        ...(body.input_hash === undefined ? {} : { inputHash: body.input_hash }),
        clientIp: deps.remoteAddress(c),
      })
      return c.json(approvalView(decided))
    } catch (err) {
      if (err instanceof ApprovalError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  })
}

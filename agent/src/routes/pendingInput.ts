import type { Hono } from 'hono'
import type { OriginPolicy } from '../http/origins.js'
import type { SessionManager } from '../sessions/manager.js'
import type { Owner } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiReadProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// GET /api/v1/ai/pending-input (#815; durable-agents spec §6.6, PR #1070): every
// tool call parked on a person that the browser user may answer, in one shape,
// for the Assistant badge. Classic sessions only so far: the pending rows of
// `ai_approvals` (kind `approval`, session-less MCP prepares included) and of
// `ai_questions` (kind `answer`: questions, #940, and attention requests,
// #815). The durable projection (`ai_pending_input`) joins this union when
// durable sessions land.
//
// A read, behind guard.ts `uiReadProblem` like the approvals list. An approval
// carries its scrubbed summary and input hash, never its input; an answer
// carries its prompt (the question or the attention message), which the gate
// stored with the turn's secrets redacted. Answering stays where it is today:
// approvals through /api/v1/ai/approvals, answers through the panel's
// `question.answer`; spec §6.6's single `respond` route is later work.

export type PendingInputEntry = {
  /** Opaque; the prefix names the store (`approval:` or `question:`, then the row id). */
  id: string
  kind: 'approval' | 'answer'
  session_id: string | null
  tool: string
  /** An approval's scrubbed summary; empty for an answer. */
  summary: string
  input_hash: string | null
  /** An answer's question or attention message; empty for an approval. */
  prompt: string
  /** Who asked for the approval; null for an answer (the session's agent). */
  requested_by: Owner | null
  /** Who may answer: the browser user, and for an approval also a grant holder. */
  responders: ('browser' | 'grant')[]
  created_at: string
  /** When its timer fires: an approval's expiry, an attention request's; null for a question, which has none. */
  expires_at: string | null
  /**
   * Set on an attention request (#815) only. A `done` summary has no timer
   * (`on_timeout` null) and carries `summary`, ScadBuddy's record of what its
   * turn touched; it stays listed until the user dismisses it.
   */
  attention?: { reason: string; on_timeout: string | null; summary?: string }
}

export type PendingInputRouteDeps = {
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

export async function pendingInput(sessions: SessionManager): Promise<PendingInputEntry[]> {
  const [approvals, answers] = await Promise.all([sessions.approvals.list(BROWSER_USER, { pending: true }), sessions.questions.listPending()])
  const entries: PendingInputEntry[] = [
    ...approvals.map((a) => ({
      id: `approval:${a.id}`,
      kind: 'approval' as const,
      session_id: a.sessionId,
      tool: a.tool,
      summary: a.inputSummary,
      input_hash: a.inputHash,
      prompt: '',
      requested_by: a.requestedBy,
      responders: ['browser' as const, 'grant' as const],
      created_at: a.createdAt,
      expires_at: a.expiresAt,
    })),
    ...answers.map((q) => ({
      id: `question:${q.id}`,
      kind: 'answer' as const,
      session_id: q.sessionId,
      tool: q.tool,
      summary: '',
      input_hash: null,
      prompt: q.questions.map((v) => v.question).join('\n'),
      requested_by: null,
      responders: ['browser' as const],
      created_at: q.createdAt,
      expires_at: q.expiresAt,
      ...(q.kind === 'attention' && q.attentionReason
        ? {
            attention: {
              reason: q.attentionReason,
              on_timeout: q.onTimeout,
              ...(q.summary === null ? {} : { summary: q.summary }),
            },
          }
        : {}),
    })),
  ]
  return entries.sort((a, b) => a.created_at.localeCompare(b.created_at))
}

export function registerPendingInputRoutes(app: Hono, deps: PendingInputRouteDeps): void {
  app.get('/api/v1/ai/pending-input', async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'pending input reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    return c.json({ entries: await pendingInput(deps.sessions) })
  })
}

/** The pending-input read (#815). */
export const route: RouteModule = {
  register(app, deps) {
    registerPendingInputRoutes(app, {
      sessions: deps.sessions,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}

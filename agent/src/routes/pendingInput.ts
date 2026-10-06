import type { Hono } from 'hono'
import { z } from 'zod'
import { ApprovalError } from '../approvals/service.js'
import { ANSWER_MAX, QUESTION_TEXT_MAX, QUESTIONS_MAX } from '../harness/questions.js'
import type { OriginPolicy } from '../http/origins.js'
import { QuestionError } from '../questions/service.js'
import type { SessionManager } from '../sessions/manager.js'
import { type Owner, PROTOCOL_VERSION } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
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
// stored with the turn's secrets redacted.
//
// POST /api/v1/ai/pending-input/{request_id} is §6.6's `respond`: one route
// that answers any entry the read lists, dispatching on the id's prefix to its
// store. The body names the entry's kind, and the route refuses one that does
// not match the entry, an entry no longer pending, and an unknown id (stale):
//
//   approval  {"kind": "approval", "decision": "approve" | "deny", "input_hash"?: "<64 hex>"}
//   question  {"kind": "answer", "answers": {"<question>": "…" | ["…", …]}}
//   attention {"kind": "answer", "choice": "<one of its options>"} or {"kind": "answer", "text": "…"}
//
// It is a write from the UI, so it passes guard.ts `uiRequestProblem` and acts
// as the browser user, like the approval decisions. Those routes
// (/api/v1/ai/approvals/:id/approve|deny) and `sessions_approve` /
// `sessions_deny` stay as aliases for the `approval` kind (§6.6, "Classic
// sessions"), as do the chat socket's `approval.decision` and
// `question.answer`, which a panel loaded before this route existed still sends.

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

/**
 * A response's cap, derived from the largest answer the panel can send rather
 * than §6.6's flat 16 KiB, which is smaller than one ANSWER_MAX answer and would
 * refuse answers the socket's `question.answer` takes. The largest valid body is
 * a question's: QUESTIONS_MAX answers of ANSWER_MAX, each keyed by a question
 * text of QUESTION_TEXT_MAX (an attention request's one `choice` or `text`, and
 * a multi-select's picks, which join to one ANSWER_MAX answer, are smaller).
 * Those bounds count UTF-16 code units, and a code unit is at most 6 bytes of
 * JSON (a `\uXXXX` escape), as for guard.ts JSON_BODY_MAX; 1 KiB covers the
 * rest of the body. The panel's copies of these bounds are pinned to the agent's
 * (test/sessions.protocol.test.ts), so the cap follows them.
 */
export const RESPONSE_MAX = QUESTIONS_MAX * (QUESTION_TEXT_MAX + ANSWER_MAX) * 6 + 1024

const answerText = z.string().min(1).max(ANSWER_MAX)

/** The body of a respond; which answer shape applies is the entry's, checked in `respond`. */
export const RespondBody = z.discriminatedUnion('kind', [
  z.strictObject({
    kind: z.literal('approval'),
    decision: z.enum(['approve', 'deny']),
    input_hash: z
      .string()
      .regex(/^[0-9a-f]{64}$/)
      .optional(),
  }),
  z.strictObject({
    kind: z.literal('answer'),
    answers: z.record(z.string(), z.union([answerText, z.array(answerText).min(1)])).optional(),
    choice: answerText.optional(),
    text: answerText.optional(),
  }),
])
export type RespondBody = z.infer<typeof RespondBody>

export type RespondResult = { id: string; kind: PendingInputEntry['kind']; outcome: 'approved' | 'denied' | 'answered' }

/**
 * A refused respond, with the HTTP status the route answers. A 409's `reason` says
 * how the entry ended, as a clause the panel shows ("it was already answered"), so it
 * never has to guess one (#1400).
 */
export class RespondError extends Error {
  override name = 'RespondError'
  readonly status: 400 | 403 | 404 | 409 | 410
  readonly reason: string | undefined
  constructor(status: 400 | 403 | 404 | 409 | 410, message: string, reason?: string) {
    super(message)
    this.status = status
    this.reason = reason
  }
}

/** How a question that is no longer pending ended, as a clause. */
function endedReason(entry: { outcome: 'answered' | 'cancelled' | 'timed_out' | 'reconnected' | null; reason: string | null }): string {
  if (entry.outcome === 'answered') return 'it was already answered'
  if (entry.outcome === 'reconnected') return entry.reason ?? 'the ScadBuddy tab is connected again'
  return entry.reason ?? (entry.outcome === 'timed_out' ? 'nobody replied in time' : 'it was cancelled')
}

const QUESTION_STATUS = { not_found: 404, forbidden: 403, conflict: 409, invalid: 400 } as const

/**
 * §6.6 `respond` for the classic stores: answers the entry `requestId` names as
 * `principal`. Refuses (RespondError) an unknown or stale id, an entry no longer
 * pending, a body whose kind is not the entry's, and an answer that does not fit
 * the entry: a question needs one answer per question, keyed by its text; an
 * attention request one of its options as `choice`, or its own words as `text`.
 */
export async function respond(
  sessions: SessionManager,
  principal: Owner,
  requestId: string,
  body: RespondBody,
  where: { clientIp?: string | undefined } = {},
): Promise<RespondResult> {
  const match = /^(approval|question):(.+)$/.exec(requestId)
  const stale = new RespondError(404, `no pending input ${requestId}: it is stale or was never asked`)
  if (!match?.[1] || !match[2]) throw stale
  const store = match[1] === 'approval' ? 'approval' : 'question'
  const rowId = match[2]

  if (store === 'approval') {
    if (body.kind !== 'approval') {
      throw new RespondError(400, `${requestId} is an approval: respond with {"kind": "approval", "decision": …}`)
    }
    try {
      const decided = await sessions.approvals.decide(principal, rowId, body.decision === 'approve', {
        ...(body.input_hash === undefined ? {} : { inputHash: body.input_hash }),
        clientIp: where.clientIp,
      })
      return { id: requestId, kind: 'approval', outcome: decided.decision === 'approved' ? 'approved' : 'denied' }
    } catch (err) {
      if (err instanceof ApprovalError) throw new RespondError(err.status, err.message, err.reason)
      throw err
    }
  }

  const entry = await sessions.questions.entry(rowId)
  if (!entry) throw stale
  if (body.kind !== 'answer') {
    throw new RespondError(400, `${requestId} asks for an answer: respond with {"kind": "answer", …}`)
  }
  const ended = (now: NonNullable<typeof entry>) => new RespondError(409, `${requestId} is no longer waiting for an answer`, endedReason(now))
  if (!entry.pending) throw ended(entry)
  let answers: string[]
  if (entry.kind === 'attention') {
    if (body.answers !== undefined || (body.choice === undefined) === (body.text === undefined)) {
      throw new RespondError(400, `${requestId} is an attention request: respond with exactly one of "choice" or "text"`)
    }
    const options = entry.questions[0]?.options.map((o) => o.label) ?? []
    if (body.choice !== undefined && !options.includes(body.choice)) {
      throw new RespondError(400, `"choice" must be one of ${JSON.stringify(options)}; use "text" for your own words`)
    }
    answers = [body.choice ?? body.text ?? '']
  } else {
    const given = body.answers
    if (!given || body.choice !== undefined || body.text !== undefined) {
      throw new RespondError(400, `${requestId} is a question: respond with "answers", one per question, keyed by its text`)
    }
    const asked = entry.questions.map((q) => q.question)
    const keys = Object.keys(given)
    if (keys.length !== asked.length || !asked.every((q) => Object.hasOwn(given, q))) {
      throw new RespondError(400, `"answers" must answer exactly these questions: ${JSON.stringify(asked)}`)
    }
    // A multi-select's picks join as the panel's card joins them (FeedItemView).
    answers = asked.map((q) => {
      const a = given[q] ?? ''
      return typeof a === 'string' ? a : a.join(', ')
    })
    // The socket caps each answer at ANSWER_MAX; joined picks must fit it too.
    if (answers.some((a) => a.length > ANSWER_MAX)) {
      throw new RespondError(400, `each answer must be at most ${ANSWER_MAX} characters, a multi-select's picks joined with ", "`)
    }
  }
  try {
    await sessions.questions.answer(
      principal,
      { v: PROTOCOL_VERSION, type: 'question.answer', sessionId: entry.sessionId, id: rowId, answers },
      { clientIp: where.clientIp },
    )
  } catch (err) {
    if (err instanceof QuestionError && err.code === 'conflict') {
      // It ended between the read above and the answer: say how.
      // A failed re-read must not turn the 409 into a 500: fall through without a reason.
      const now = await sessions.questions.entry(rowId).catch(() => undefined)
      if (now && !now.pending) throw ended(now)
    }
    if (err instanceof QuestionError) throw new RespondError(QUESTION_STATUS[err.code], err.message)
    throw err
  }
  return { id: requestId, kind: 'answer', outcome: 'answered' }
}

export function registerPendingInputRoutes(app: Hono, deps: PendingInputRouteDeps): void {
  app.get('/api/v1/ai/pending-input', async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'pending input reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    return c.json({ entries: await pendingInput(deps.sessions) })
  })

  // Anything over the cap is 413 unread.
  app.post('/api/v1/ai/pending-input/:id', jsonBodyLimit(RESPONSE_MAX), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'responses to the assistant')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 415)
    let body: RespondBody
    try {
      body = RespondBody.parse(JSON.parse(await c.req.text()))
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    try {
      return c.json(await respond(deps.sessions, BROWSER_USER, c.req.param('id'), body, { clientIp: deps.remoteAddress(c) }))
    } catch (err) {
      if (err instanceof RespondError) {
        // `stale` marks the agent's own 404, so the panel can tell it from one a proxy or
        // an older replica without this route answers: only this one closes the card.
        return c.json(
          { detail: err.message, ...(err.reason === undefined ? {} : { reason: err.reason }), ...(err.status === 404 ? { stale: true } : {}) },
          err.status,
        )
      }
      throw err
    }
  })
}

/** The pending-input read and its respond route (#815). */
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

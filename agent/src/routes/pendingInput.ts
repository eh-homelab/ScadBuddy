import type { Hono } from 'hono'
import { z } from 'zod'
import { ApprovalError, conflictReason } from '../approvals/service.js'
import { BACK_REPLIES } from '../harness/attention.js'
import { ANSWER_MAX } from '../harness/questions.js'
import { parseRequestId, type RequestId } from '../gate/ids.js'
import { WorkflowNotFoundError } from '@temporalio/common'
import { REFUSAL_STATUS, RESPONSE_MAX, RespondRefusal, validateRespond } from '../gate/validate.js'
import type { OriginPolicy } from '../http/origins.js'
import { QuestionError } from '../questions/service.js'
import type { SessionManager } from '../sessions/manager.js'
import { canSee, type InputOutcome, type Owner, PROTOCOL_VERSION } from '../sessions/protocol.js'
import { approvalEntry, questionEntry } from '../gate/classic.js'
import { DurableUnavailable } from '../gate/durable.js'
import type { PendingInputEntry } from '../gate/projection.js'
import { roleOf } from '../gate/role.js'
import { isUuid } from '../harness/stateDirs.js'
import { BROWSER_USER } from './approvals.js'
import { jsonBodyLimit, type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// GET /api/v1/ai/pending-input (#815; durable-agents spec §6.6, PR #1070): every
// tool call parked on a person that the browser user may answer, in one shape,
// for the Assistant badge. One Postgres read of the union of the pending rows of
// `ai_approvals` (kind `approval`, session-less MCP prepares included), of
// `ai_questions` (kind `answer`: questions, #940, and attention requests, #815),
// and of `ai_pending_input`, durable sessions' projection, never a Query per
// workflow. A durable entry past its timer whose worker has not resolved it yet is
// listed `expiring`, and the badge does not count it.
//
// GET /api/v1/ai/sessions/{id}/pending-input answers for one session: a classic
// one from those tables, a durable one by its workflow's `pending_input` Query,
// the source of truth (503 when its worker does not answer; plan 5b ruling 11).
//
// Both are reads, behind guard.ts `uiReadProblem` like the approvals list. An
// approval carries its scrubbed summary and input hash, never its input; an answer
// carries its prompt (the question or the attention message), which the gate
// stored with the turn's secrets redacted. Other principals read through the
// `pending_input_list` and `sessions_pending_input` tools (tools/pendingInput.ts).
//
// POST /api/v1/ai/pending-input/{request_id} is §6.6's `respond`: one route
// that answers any entry the reads list, dispatching on the id's prefix: an
// `approval:` or `question:` id to its store, after the gate's shared validator
// (gate/validate.ts); a `durable:` id to the session's workflow as the `respond`
// Update, whose validator is the same. A `flow:` id, or any other, is stale until
// flows exist (plan 5b ruling 2). The body names the entry's kind, and the route
// refuses, in this order, an unknown id (404, stale), an entry no longer pending
// (409, whatever the body's kind), and a body that does not match the entry (400):
//
//   approval  {"kind": "approval", "decision": "approve" | "deny", "input_hash"?: "<64 hex>"}
//   question  {"kind": "answer", "answers": {"<question>": "…" | ["…", …]}}
//             (a list only for a multi-select, its picks joined with ", ", so no pick may contain ", ")
//   attention {"kind": "answer", "choice": "<one of its options>"} or {"kind": "answer", "text": "…"}
//
// It is a write from the UI, so it passes guard.ts `uiRequestProblem` and acts
// as the browser user, like the approval decisions. Those routes
// (/api/v1/ai/approvals/:id/approve|deny, `ai_approvals` ids only) and
// `sessions_approve` / `sessions_deny` (any approval id, `durable:` included) stay
// as aliases for the `approval` kind (§6.6, "Classic sessions"), as do the chat
// socket's `approval.decision` and `question.answer`, which a panel loaded before
// this route existed still sends.

export type { PendingInputEntry }

export type PendingInputRouteDeps = {
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

/**
 * The response body. `summaries_truncated`: more undismissed `done` summaries
 * are pending than are listed (questions/service.ts PENDING_CAP), so a count
 * of them is a lower bound.
 */
export type PendingInputPage = { entries: PendingInputEntry[]; summaries_truncated: boolean }

/**
 * Every entry `principal` may see (plan 5b ruling 10): for the browser user, all of
 * them. For anyone else, the approvals it may see (all of them with the approval
 * grant; else its own sessions' and its own MCP prepares), and the answers of the
 * sessions it owns. `sessionId` narrows it to one session.
 */
export async function pendingInput(
  sessions: SessionManager,
  principal: Owner = BROWSER_USER,
  filter: { sessionId?: string } = {},
): Promise<PendingInputPage> {
  const browser = principal.kind === 'browser'
  const grant = !browser && (await sessions.approvals.holdsGrant(principal))
  const [approvals, { questions: answers, summariesTruncated }, durable] = await Promise.all([
    sessions.approvals.listVisiblePending(principal),
    sessions.questions.listPending(principal),
    sessions.projection.entries({
      ...(filter.sessionId === undefined ? {} : { sessionId: filter.sessionId }),
      ...(browser ? {} : { ownedBy: principal, allApprovals: grant }),
    }),
  ])
  const inSession = (e: PendingInputEntry) => filter.sessionId === undefined || e.session_id === filter.sessionId
  const entries: PendingInputEntry[] = [
    ...approvals.map(approvalEntry).filter(inSession),
    ...answers.map(questionEntry).filter(inSession),
    ...durable,
  ]
  return { entries: entries.sort((a, b) => a.created_at.localeCompare(b.created_at)), summaries_truncated: summariesTruncated }
}

/**
 * One session's entries, as `principal` may see them: a durable session's from its
 * workflow (DurableUnavailable when its worker does not answer), a classic one's
 * from its stores. Undefined when there is no such session for the principal.
 */
export async function sessionPendingInput(
  sessions: SessionManager,
  principal: Owner,
  sessionId: string,
): Promise<PendingInputPage | undefined> {
  if (!isUuid(sessionId)) return undefined
  const session = await sessions.projection.session(sessionId)
  if (!session) return undefined
  const grant = principal.kind !== 'browser' && (await sessions.approvals.holdsGrant(principal))
  const own = canSee(principal, { owner: session.owner, creator: session.creator, offer: null })
  if (principal.kind !== 'browser' && !grant && !own) return undefined
  if (session.mode === 'durable') {
    if (!sessions.durable) throw new DurableUnavailable("durable sessions need Temporal, and this agent service has none configured")
    const entries = await sessions.durable.pendingInput(sessionId)
    // A grant holder that does not own the session sees its approvals only, as in the aggregate.
    return { entries: principal.kind === 'browser' || own ? entries : entries.filter((e) => e.kind === 'approval'), summaries_truncated: false }
  }
  return pendingInput(sessions, principal, { sessionId })
}

/** A response's cap (gate/validate.ts, plan 5b ruling 1). */
export { RESPONSE_MAX }

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

export type RespondResult = { id: string; kind: PendingInputEntry['kind']; outcome: InputOutcome }

/**
 * A refused respond, with the HTTP status the route answers. A 409's `reason` says
 * how the entry ended, as a clause the panel shows ("it was already answered"), so it
 * never has to guess one (#1400).
 */
export class RespondError extends Error {
  override name = 'RespondError'
  readonly status: 400 | 403 | 404 | 409 | 410 | 413 | 503
  readonly reason: string | undefined
  constructor(status: 400 | 403 | 404 | 409 | 410 | 413 | 503, message: string, reason?: string) {
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

/** A validator refusal as the route answers it; `reason` says how an ended entry ended. */
function refused(err: RespondRefusal, reason?: string): RespondError {
  return new RespondError(REFUSAL_STATUS[err.code], err.message, err.code === 'resolved' ? reason : undefined)
}

/**
 * §6.6 `respond` for the classic stores: answers the entry `requestId` names as
 * `principal`. The entry is read, then checked with the gate's shared validator
 * (gate/validate.ts), which refuses an entry no longer pending, a body whose kind is
 * not the entry's, and an answer that does not fit it: a question needs one answer
 * per question, keyed by its text; an attention request one of its options as
 * `choice`, or its own words as `text`. An unknown or stale id is refused first.
 */
export async function respond(
  sessions: SessionManager,
  principal: Owner,
  requestId: string,
  body: RespondBody,
  where: { clientIp?: string | undefined } = {},
): Promise<RespondResult> {
  const parsed = parseRequestId(requestId)
  const stale = new RespondError(404, `no pending input ${requestId}: it is stale or was never asked`)
  if (parsed?.store === 'durable') return respondDurable(sessions, principal, parsed, requestId, body, stale)
  if (parsed?.store !== 'approval' && parsed?.store !== 'question') throw stale
  // The classic stores authorize other principals themselves (sessions_approve calls them directly).
  if (principal.kind !== 'browser') throw new RespondError(403, 'only the user in the ScadBuddy panel responds through this route')
  const rowId = parsed.rowId
  const request = { requestId, response: body, responder: principal, role: 'browser' as const }

  if (parsed.store === 'approval') {
    try {
      // Read first, as a question is: an unknown id is stale whatever the body names
      // (#1358), and one already decided is a 409 before its kind is checked (#1479).
      const approval = await sessions.approvals.get(rowId, principal)
      let decision: 'approve' | 'deny'
      try {
        const valid = validateRespond(
          {
            id: requestId,
            kind: 'approval',
            state: approval.decision === null ? 'pending' : 'resolved',
            inputHash: approval.inputHash,
            requestedBy: approval.requestedBy,
            sessionOwner: null,
            sessionCreator: null,
          },
          request,
        )
        decision = 'decision' in valid ? valid.decision : 'deny'
      } catch (err) {
        if (err instanceof RespondRefusal) throw refused(err, conflictReason(approval))
        throw err
      }
      const decided = await sessions.approvals.decide(principal, rowId, decision === 'approve', {
        ...(body.kind === 'approval' && body.input_hash !== undefined ? { inputHash: body.input_hash } : {}),
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
  const ended = (now: NonNullable<typeof entry>) => new RespondError(409, `${requestId} is no longer waiting for an answer`, endedReason(now))
  // #815 §2: "I'm back" to a request the tab's return already ended is the service's
  // no-op success, not a conflict: let it through for answer() to take (#1538).
  const backAfterReconnect =
    body.kind === 'answer' &&
    entry.kind === 'attention' &&
    entry.outcome === 'reconnected' &&
    BACK_REPLIES.includes(body.choice ?? body.text ?? '')
  let answers: string[]
  try {
    const valid = validateRespond(
      {
        id: requestId,
        kind: 'answer',
        state: entry.pending || backAfterReconnect ? 'pending' : 'resolved',
        inputHash: null,
        requestedBy: null,
        sessionOwner: null,
        sessionCreator: null,
        ...(entry.kind === 'attention'
          ? { options: entry.questions[0]?.options.map((o) => o.label) ?? [] }
          : { questions: entry.questions.map((q) => ({ question: q.question, multiSelect: q.multiSelect })) }),
      },
      request,
    )
    answers = 'answers' in valid ? valid.answers : []
  } catch (err) {
    if (err instanceof RespondRefusal) throw err.code === 'resolved' ? ended(entry) : refused(err)
    throw err
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

/**
 * `respond` for a durable session's entry: the session must be durable; the route
 * decides the role (gate/role.ts) and the workflow's `respond` Update validates and
 * resolves (spec §6.6). Its refusals keep their status; a workflow that is gone is
 * stale, and one whose worker does not answer is a 503 the caller may retry.
 */
async function respondDurable(
  sessions: SessionManager,
  principal: Owner,
  parsed: Extract<RequestId, { store: 'durable' }>,
  requestId: string,
  body: RespondBody,
  stale: RespondError,
): Promise<RespondResult> {
  const session = await sessions.projection.session(parsed.sessionId)
  if (!session || session.mode !== 'durable') throw stale
  if (!sessions.durable) throw new RespondError(503, 'durable sessions need Temporal, and this agent service has none configured')
  const grant = principal.kind !== 'browser' && (await sessions.approvals.holdsGrant(principal))
  const role = roleOf(principal, session, grant)
  if (!role) {
    throw new RespondError(
      403,
      `${principal.label} may not respond: outward actions need a human approval in the ScadBuddy UI, ` +
        'and another agent needs a per-token approval grant (spec §6, §8.2)',
    )
  }
  // No kind accepts `owner` (gate/validate.ts), so it is refused here, with no Update sent.
  if (role === 'owner') {
    throw new RespondError(
      403,
      "an approval grant is for approving another agent's outward actions, never your own; ask the user in the ScadBuddy UI",
    )
  }
  try {
    const out = await sessions.durable.respond(parsed.sessionId, { request_id: requestId, response: body, responder: principal, role })
    return { id: requestId, kind: out.kind, outcome: out.outcome }
  } catch (err) {
    if (err instanceof RespondRefusal) throw refused(err)
    if (err instanceof WorkflowNotFoundError) throw stale
    if (err instanceof DurableUnavailable) throw new RespondError(503, err.message)
    throw err
  }
}

export function registerPendingInputRoutes(app: Hono, deps: PendingInputRouteDeps): void {
  app.get('/api/v1/ai/pending-input', async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'pending input reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    return c.json(await pendingInput(deps.sessions))
  })

  app.get('/api/v1/ai/sessions/:id/pending-input', async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'pending input reads')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    try {
      const page = await sessionPendingInput(deps.sessions, BROWSER_USER, c.req.param('id'))
      return page ? c.json(page) : c.json({ detail: `no session ${c.req.param('id')}` }, 404)
    } catch (err) {
      if (err instanceof DurableUnavailable) return c.json({ detail: err.message, retry: true }, 503)
      if (err instanceof WorkflowNotFoundError) return c.json({ detail: `session ${c.req.param('id')} has no running workflow` }, 404)
      throw err
    }
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
          {
            detail: err.message,
            ...(err.reason === undefined ? {} : { reason: err.reason }),
            ...(err.status === 404 ? { stale: true } : {}),
            ...(err.status === 503 ? { retry: true } : {}),
          },
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

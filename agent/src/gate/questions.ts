import { randomUUID } from 'node:crypto'
import { type AuditEntry, type AuditLog, type AuditSurface, safeDetail, SYSTEM_ACTOR } from '../audit/log.js'
import type { AttentionReason, OnTimeout } from '../harness/attention.js'
import { parseQuestions, type QuestionGate, type QuestionRequest, type QuestionVerdict, type UserQuestion } from '../harness/questions.js'
import { isUuid } from '../harness/stateDirs.js'
import { redact } from '../secrets.js'
import type { EventLog } from '../sessions/eventLog.js'
import { type Owner, PROTOCOL_VERSION, type QuestionAnswerMessage, type QuestionView } from '../sessions/protocol.js'
import type { Logged } from './crudStore.js'
import { QuestionRefused, type QuestionRoutes } from './crudQuestions.js'
import { doneSummary } from './doneSummary.js'
import { parseRequestId } from './ids.js'

// The gate's questions outside the workflow (#940, #815), folded here from
// agent/src/questions/ (unified runtime plan Steps 8.2 and 9.7). Every session is an
// AgentSession (plan Step 6.3): a question, an attention request and a tab wait park as the
// workflow's entries (temporal/session/gate.ts, ai_pending_input), never here. What stays:
//
//   - `done` summaries (#815 §4), rows of `ai_questions` (`kind = 'attention'`,
//     `attention_reason = 'done'`, no timer). A summary is posted, never waited on: the call
//     returns at once, the session never shows `waiting_input` for it, and it OUTLIVES ITS
//     TURN. It stays pending, and on the badge, until the user dismisses it on its card
//     (`answer`) or the session posts a newer one. Its row carries `summary`, ScadBuddy's
//     record of what the turn touched (doneSummary.ts). At most DONE_POSTS_PER_TURN a turn.
//   - #815 §5's rate limits on an AgentSession's attention requests and tab waits
//     (`attentionLimit`), which count the gate's own `wait_for_user` entries.
//   - The `question` audit row of an answered entry (`auditGateAnswer`, #1075).
//
// The rows live on the API (gate/crudQuestions.ts, area `questions`, #5299 step 10).
//
// The question timer's settings (`question_expiry_seconds`) are the gate's
// (temporal/session/gateActivities.ts).
//
// WHO ANSWERS. Only the user in the ScadBuddy panel (the browser principal).

/** ai_settings key: seconds a question waits for an answer before it is cancelled. */
export const SETTING_QUESTION_EXPIRY_SECONDS = 'question_expiry_seconds'
export const DEFAULT_QUESTION_EXPIRY_SECONDS = 3600
export const MIN_QUESTION_EXPIRY_SECONDS = 10
export const MAX_QUESTION_EXPIRY_SECONDS = 86_400

/** How many undismissed `done` summaries listPending returns. */
export const PENDING_CAP = 500
/**
 * #815 §5's per-user rate limit on attention requests: this many per window. A
 * `done` summary neither counts nor is limited: a timeout tells the model to
 * post one, so a turn that timed out often must still be able to.
 */
export const ATTENTION_RATE_LIMIT = 10
export const ATTENTION_RATE_WINDOW_S = 600
/**
 * #1344: browser_* calls' tab waits (sessions/manager.ts waitForTab) across the
 * user's sessions, per ATTENTION_RATE_WINDOW_S. TAB_WAITS_PER_TURN bounds one
 * turn only; this bounds sessions or turns looping on a dead tab. Apart from the
 * model's own limit, which tab waits never use up.
 */
export const TAB_WAIT_RATE_LIMIT = 10
/**
 * #1383: the most `done` summaries one turn posts. Outside the rate limit, so a
 * model looping on done is bounded here, before each post scans the turn's touches.
 */
export const DONE_POSTS_PER_TURN = 3
/** How long `attentionLimit` holds its lease for the entry's open (a workflow Update round trip). */
const OPEN_WAIT_MS = 10_000
/** How long `attentionLimit` waits before it tries the lease again, at most. */
const RATE_LOCK_RETRY_MS = 50
/** How long `attentionLimit` tries the lease before it refuses: past one holder's OPEN_WAIT_MS, twice over (#5053). */
const RATE_LOCK_WAIT_MS = 2 * OPEN_WAIT_MS + 5_000

export type QuestionErrorCode = 'not_found' | 'forbidden' | 'conflict' | 'invalid'

/** A refused answer; `code` is what the panel's `error` event carries. */
export class QuestionError extends Error {
  override name = 'QuestionError'
  readonly code: QuestionErrorCode
  constructor(code: QuestionErrorCode, message: string) {
    super(message)
    this.code = code
  }
}

export type QuestionServiceDeps = {
  /** The API's routes, area `questions` (gate/crudQuestions.ts). */
  routes: QuestionRoutes
  /** The session event log the question events go to. */
  events: EventLog
  /** The AI audit log (#1075): one `question` row per answer. */
  audit?: Pick<AuditLog, 'record' | 'hash'>
  /** Tests only: how long `attentionLimit` tries the rate lease before it refuses (default RATE_LOCK_WAIT_MS). */
  rateLockWaitMs?: number
}

/** What `gate()` needs to know about the turn that posts. */
export type QuestionGateContext = {
  sessionId: string
  turnId: string
  /** Redacted from the stored card and the events (the turn's credential). */
  secrets: () => readonly string[]
  /** The turn's own abort signal (interrupt, shutdown). */
  signal: AbortSignal
  /**
   * When the turn was claimed, by the database's clock (the claim's `now()`): a
   * `done` summary covers the touches since, and `ai_session_resources.at` is
   * stamped by the same clock. The agent's own clock may disagree with it.
   * Postgres's text for it, microseconds kept: a JS Date would round it down to
   * the millisecond and let in a touch made just before the claim (#1383).
   */
  turnStartedAt: string
  /**
   * The turn's placement (`turn_attempt`): a post lands only while it and `turnId` still hold
   * the session. Without it no post is made.
   */
  attempt?: string
}

type Outcome = 'answered' | 'cancelled' | 'timed_out' | 'reconnected' | null

/** An undismissed `done` summary, as GET /api/v1/ai/pending-input lists it (spec §6.6). */
export type PendingQuestion = {
  id: string
  sessionId: string
  kind: 'question' | 'attention'
  tool: string
  toolUseId: string
  questions: QuestionView[]
  attentionReason: AttentionReason | null
  /** Null for a `done` request, which has no timer. */
  onTimeout: OnTimeout | null
  /** A `done` request's record of what its turn touched (doneSummary.ts). */
  summary: string | null
  createdAt: string
  expiresAt: string | null
}

/** What a post that was not made tells the model. */
const NOT_ASKED = 'The question was not asked: the session is no longer the user’s, or its turn ended.'

/** Runs `open`, waiting for it at most OPEN_WAIT_MS: the rate lease is every session's. */
async function openBounded(open: () => Promise<void>): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined
  await Promise.race([open(), new Promise<void>((resolve) => (timer = setTimeout(resolve, OPEN_WAIT_MS)))]).finally(() => clearTimeout(timer))
}

/** The questions with every string redacted of `secrets`. */
function redactQuestions(questions: readonly UserQuestion[], secrets: readonly string[]): QuestionView[] {
  const r = (text: string) => redact(text, secrets)
  return questions.map((q) => ({
    question: r(q.question),
    header: r(q.header),
    multiSelect: q.multiSelect,
    options: q.options.map((o) => ({
      label: r(o.label),
      description: r(o.description),
      ...(o.preview === undefined ? {} : { preview: r(o.preview) }),
    })),
  }))
}

export class QuestionService {
  private readonly deps: QuestionServiceDeps

  constructor(deps: QuestionServiceDeps) {
    this.deps = deps
  }

  /** Hands the batches the API appended to the event log's followers. */
  private committed(logged: readonly Logged[]): void {
    for (const l of logged) this.deps.events.committed(l.session_id, l.events, l.seqs)
  }

  /**
   * One row by id, pending or not, for the respond route (routes/pendingInput.ts):
   * what it checks a response against before `answer()` takes it.
   */
  async entry(
    id: string,
  ): Promise<{ sessionId: string; kind: 'question' | 'attention'; questions: QuestionView[]; pending: boolean; outcome: Outcome; reason: string | null } | undefined> {
    if (!isUuid(id)) return undefined
    const row = await this.deps.routes.entry(id)
    return row && { sessionId: row.session_id, kind: row.kind, questions: row.questions, pending: row.outcome === null, outcome: row.outcome, reason: row.reason }
  }

  /**
   * The undismissed `done` summaries of the sessions `principal` owns (#1218), newest first, at
   * most PENDING_CAP. Nothing expires them, so `summariesTruncated` says there were more and the
   * oldest are not listed. A row nothing parks on any more (a classic question, from before
   * every session was an AgentSession) is not listed: nobody would read its answer.
   */
  async listPending(principal: Owner): Promise<{ questions: PendingQuestion[]; summariesTruncated: boolean }> {
    const done = (await this.deps.routes.pending(principal)).map((r) => ({ ...r, created_at: new Date(r.created_at), expires_at: r.expires_at === null ? null : new Date(r.expires_at) }))
    const questions = done.slice(0, PENDING_CAP).map((r) => ({
      id: r.id,
      sessionId: r.session_id,
      kind: r.kind,
      tool: r.tool,
      toolUseId: r.tool_use_id,
      questions: r.questions,
      attentionReason: r.attention_reason,
      onTimeout: r.on_timeout,
      summary: r.summary,
      createdAt: r.created_at.toISOString(),
      expiresAt: r.expires_at?.toISOString() ?? null,
    }))
    return { questions, summariesTruncated: done.length > PENDING_CAP }
  }

  /**
   * The `question` audit row of an AgentSession's answered entry (`requestId`, a gate id) (#1075):
   * who answered, from where, the call, and the answers as a hash, never their text. Read from
   * the entry's response row; nothing when there is no answered row or no audit log.
   */
  async auditGateAnswer(principal: Owner, requestId: string, where: { clientIp?: string | undefined } = {}): Promise<void> {
    if (!this.deps.audit) return
    const row = await this.deps.routes.gateAnswer(requestId)
    if (!row) return
    const parsed = parseRequestId(requestId)
    const tool = row.tool ?? 'question'
    const answers = Array.isArray(row.response?.answers) ? row.response.answers.filter((a): a is string => typeof a === 'string') : []
    await this.audited([
      {
        kind: 'question',
        action: 'answered',
        surface: 'http',
        actor: principal,
        clientIp: where.clientIp,
        sessionId: row.session_id,
        ...(parsed?.store === 'durable' ? { toolUseId: parsed.toolUseId } : {}),
        tier: 'read',
        inputHash: this.deps.audit.hash(tool, { answers }),
        outcome: 'ok',
        detail: `${tool} question ${requestId}: ${answers.length} answer${answers.length === 1 ? '' : 's'}`,
        startedAt: row.requested_at ?? new Date(),
        finishedAt: new Date(),
      },
    ])
  }

  /**
   * The panel's `question.answer` to a summary: its dismissal, from the user in the panel. `where`
   * is for the audit log: the answerer's address and the surface ('http', the panel, when omitted).
   */
  async answer(principal: Owner, message: QuestionAnswerMessage, where: { clientIp?: string | undefined; surface?: AuditSurface } = {}): Promise<void> {
    if (message.v !== PROTOCOL_VERSION || message.type !== 'question.answer') {
      throw new QuestionError('invalid', 'not a question.answer message')
    }
    if (principal.kind !== 'browser') {
      throw new QuestionError('forbidden', "only the user in the ScadBuddy panel answers the agent's questions")
    }
    const { sessionId, id, answers } = message
    if (!isUuid(id) || !isUuid(sessionId)) throw new QuestionError('not_found', `no question ${id} in this session`)
    // The tool keys the answers' hash; the API checks the row again under its lock, and
    // writes the dismissal, its events and its audit row in one transaction.
    const asked = await this.deps.routes.entry(id)
    if (!asked || asked.session_id !== sessionId) throw new QuestionError('not_found', `no question ${id} in this session`)
    try {
      const logged = await this.deps.routes.answer({
        id,
        sessionId,
        answers,
        by: principal,
        inputHash: this.deps.audit?.hash(asked.tool, { answers }),
        surface: where.surface ?? 'http',
        clientIp: where.clientIp,
      })
      this.committed(logged)
    } catch (err) {
      if (err instanceof QuestionRefused) throw new QuestionError(err.code, err.message)
      throw err
    }
  }

  /**
   * The audit rows (#1075), after the state they report is committed: AuditLog never throws,
   * but a sink that did must not cost the other rows theirs.
   */
  private async audited(entries: AuditEntry[]): Promise<void> {
    const audit = this.deps.audit
    if (!audit) return
    await Promise.allSettled(entries.map((entry) => audit.record(entry)))
  }

  /**
   * Runs `open` under the lease `attention-rate` (#5053): the count read while this process holds
   * it, and `open` run before it is released, so two requests raised at once cannot both pass at
   * the limit. The lease is tried, not waited on: a busy one is tried again until `rateLockWaitMs`.
   * Undefined, a refusal, when the lease was not had in time, the API did not answer, or the
   * lease was lost before the count: never a count it could not vouch for.
   */
  private async underRateLease(own: boolean, limit: number, open: () => Promise<void>): Promise<{ value: number } | undefined> {
    const until = performance.now() + (this.deps.rateLockWaitMs ?? RATE_LOCK_WAIT_MS)
    for (;;) {
      // An open that fails is the caller's error; the rest refuses.
      const opened: { failed: boolean; err?: unknown } = { failed: false }
      let done: { value: number } | 'busy'
      try {
        done = await this.deps.routes.rateLeased(async (count) => {
          const n = await count(own)
          if (n < limit) {
            await openBounded(open).catch((err: unknown) => {
              opened.failed = true
              opened.err = err
            })
          }
          return n
        })
      } catch (err) {
        console.error(`the attention limit was not checked: ${err instanceof Error ? err.message : String(err)}`)
        return undefined
      }
      if (opened.failed) throw opened.err
      if (done !== 'busy') return done
      if (performance.now() >= until) return undefined
      await new Promise((resolve) => setTimeout(resolve, Math.ceil(Math.random() * RATE_LOCK_RETRY_MS)))
    }
  }

  /**
   * #815 §5's limits for an AgentSession turn's attention request (#5003), whose entry its
   * workflow opens (sessions/turnHost.ts): the model's own request (`own`) against
   * ATTENTION_RATE_LIMIT, a browser call's tab wait against TAB_WAIT_RATE_LIMIT, each counting
   * the gate's `wait_for_user` entries, open (`ai_pending_input`) or resolved
   * (`ai_input_responses`), a tab wait's call id `tab:<tool_use id>` (TurnHost's; any other
   * entry is a model's own request, whatever its reason). Undefined when it was asked: `open`,
   * which the workflow's entry is written by, runs under the lease, so two requests raised at
   * once cannot both pass at the limit. Else the refusal the model reads, audited, and `open`
   * never runs. Best effort past OPEN_WAIT_MS: an open that takes longer goes on outside the
   * lease, so a request counted meanwhile does not see its entry. A lease not had within
   * RATE_LOCK_WAIT_MS refuses the request, as the limit does (#5053).
   */
  async attentionLimit(
    context: { sessionId: string; turnId: string; toolUseId: string; tool: string; reason: string; own: boolean },
    open: () => Promise<void>,
  ): Promise<string | undefined> {
    const { own } = context
    const limit = own ? ATTENTION_RATE_LIMIT : TAB_WAIT_RATE_LIMIT
    const counted = await this.underRateLease(own, limit, open)
    if (!counted) {
      return own
        ? 'The user was not asked: ScadBuddy could not check the attention request limit in time. Say what you need in your reply instead.'
        : 'The user was not asked for the tab: ScadBuddy could not check the tab wait limit in time. Carry on without the tab.'
    }
    if (counted.value < limit) return undefined
    const limited = own
      ? `The user was not asked: at most ${ATTENTION_RATE_LIMIT} attention requests are sent in ` +
        `${ATTENTION_RATE_WINDOW_S / 60} minutes. Say what you need in your reply instead.`
      : `The user was not asked for the tab: at most ${TAB_WAIT_RATE_LIMIT} tab waits are opened in ` +
        `${ATTENTION_RATE_WINDOW_S / 60} minutes. Carry on without the tab.`
    const at = new Date()
    await this.audited([
      {
        kind: 'question',
        action: 'rate_limited',
        surface: 'system',
        actor: SYSTEM_ACTOR,
        sessionId: context.sessionId,
        turnId: context.turnId,
        toolUseId: context.toolUseId,
        tier: 'read',
        outcome: 'refused',
        detail: safeDetail(`${context.tool} attention request (${context.reason}): ${limited}`),
        startedAt: at,
        finishedAt: at,
      },
    ])
    return limited
  }

  /**
   * `gate`'s post: the turn's facts read, the summary written here with the
   * turn's secrets redacted, then posted. The API writes it only while the turn's placement
   * (`attempt` and `turnId`) holds the session, with the cap, the replaced summaries, the events
   * and the audit rows in one transaction. The facts are read before that transaction, so a touch
   * committed between the two is not in the summary; the turn's own calls are done by then.
   */
  private async post(context: QuestionGateContext, request: QuestionRequest, id: string, questions: QuestionView[]): Promise<QuestionVerdict> {
    const { sessionId, turnId, attempt } = context
    if (attempt === undefined) return { answered: false, message: NOT_ASKED }
    const facts = await this.deps.routes.doneFacts(sessionId, turnId, context.turnStartedAt)
    const summary = doneSummary(facts.touches, facts.windows, context.secrets())
    const posted = await this.deps.routes.done({
      id,
      sessionId,
      turnId,
      attempt,
      tool: request.tool,
      toolUseId: request.toolUseId,
      questions,
      summary,
      unattended: facts.touches.some((t) => t.unattended),
    })
    this.committed(posted.logged)
    if (posted.limited !== null) return { answered: false, message: posted.limited }
    if (!posted.asked) return { answered: false, message: NOT_ASKED }
    return { answered: false, posted: true, message: 'posted' }
  }

  /**
   * The turn's `done` summary post (#815 §4, harness/questions.ts QuestionGate): the call returns
   * `posted` at once. Any other request is the workflow's to park (sessions/turnHost.ts), and is
   * refused here.
   */
  gate(context: QuestionGateContext): QuestionGate {
    return async (request: QuestionRequest): Promise<QuestionVerdict> => {
      if (request.attention?.reason !== 'done') return { answered: false, message: 'The question was not asked: only a done summary is posted outside the gate.' }
      if (context.signal.aborted) return { answered: false, message: 'The turn is stopping; the question was not asked.' }
      const id = randomUUID()
      const questions = redactQuestions(request.questions, context.secrets())
      // Redaction can merge two labels or lengthen a string past its bound; the panel would drop that frame.
      const shown = parseQuestions({ questions })
      if (!shown.ok) {
        return {
          answered: false,
          message: `The question could not be shown: with secrets redacted it is not a valid question (${shown.error}). Ask it without the secret.`,
        }
      }
      return await this.post(context, request, id, questions)
    }
  }
}

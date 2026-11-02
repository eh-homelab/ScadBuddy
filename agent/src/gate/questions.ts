import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { type AuditEntry, type AuditLog, type AuditSurface, safeDetail, SYSTEM_ACTOR } from '../audit/log.js'
import type { AttentionReason, OnTimeout } from '../harness/attention.js'
import { ATTENTION_TOOL, parseQuestions, type QuestionGate, type QuestionRequest, type QuestionVerdict, type UserQuestion } from '../harness/questions.js'
import { isUuid } from '../harness/stateDirs.js'
import { redact } from '../secrets.js'
import type { EventLog } from '../sessions/eventLog.js'
import { event, type Owner, PROTOCOL_VERSION, type QuestionAnswerMessage, type QuestionView, type ServerEvent } from '../sessions/protocol.js'
import { loadDoneSummary } from './doneSummary.js'
import { WAIT_FOR_USER } from './entry.js'
import { parseRequestId } from './ids.js'
import { inputRequested, inputResolved, questionEntry } from './projection.js'
import { isDoneSummary } from './waiting.js'

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
/** The advisory lock the rate limit's count and the entry's open are taken under. */
export const ATTENTION_RATE_LOCK = 'scadbuddy:attention-rate'
/** How long `attentionLimit` holds its lock for the entry's open (a workflow Update round trip). */
const OPEN_WAIT_MS = 10_000
/** How long `attentionLimit` waits before it tries the lock again, at most; it holds no connection meanwhile. */
const RATE_LOCK_RETRY_MS = 50
/** How long `attentionLimit` tries the lock before it refuses: past one holder's OPEN_WAIT_MS, twice over (#5053). */
const RATE_LOCK_WAIT_MS = 2 * OPEN_WAIT_MS + 5_000
/** Why a newer summary ended an older one. */
const REPLACED = 'replaced by a newer request for the same reason'

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
  sql: Sql
  /** The session event log the question events go to. */
  events: EventLog
  /** The AI audit log (#1075): one `question` row per answer. */
  audit?: Pick<AuditLog, 'record' | 'hash'>
  /** Tests only: how long `attentionLimit` tries the rate lock before it refuses (default RATE_LOCK_WAIT_MS). */
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
}

/** A row's columns the audit log needs. */
type Resolved = { id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }

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

  /**
   * Runs `change` in one transaction with the append of the events it returns, so a row and
   * the event that reports it commit together: a watcher never sees a summary the log does not
   * show. The session row is locked first, as every gate writer does (store.ts lockSessionOf).
   * Followers are woken once it has committed.
   */
  private async atomically<T>(sessionId: string, change: (tx: TransactionSql) => Promise<{ value: T; events: ServerEvent[] }>): Promise<T> {
    let logged: { events: ServerEvent[]; seqs: number[] } | undefined
    const value = await this.deps.sql.begin(async (tx) => {
      await tx`SELECT 1 FROM ai_sessions WHERE id = ${sessionId} FOR UPDATE`
      const { value, events } = await change(tx)
      if (events.length) logged = { events, seqs: await this.deps.events.append(sessionId, events, tx) }
      return value
    })
    if (logged) this.deps.events.committed(sessionId, logged.events, logged.seqs)
    return value as T
  }

  /**
   * One row by id, pending or not, for the respond route (routes/pendingInput.ts):
   * what it checks a response against before `answer()` takes it.
   */
  async entry(
    id: string,
  ): Promise<{ sessionId: string; kind: 'question' | 'attention'; questions: QuestionView[]; pending: boolean; outcome: Outcome; reason: string | null } | undefined> {
    if (!isUuid(id)) return undefined
    const [row] = await this.deps.sql<{ session_id: string; kind: 'question' | 'attention'; questions: QuestionView[]; outcome: Outcome; reason: string | null }[]>`
      SELECT session_id, kind, questions, outcome, reason FROM ai_questions WHERE id = ${id}`
    return row && { sessionId: row.session_id, kind: row.kind, questions: row.questions, pending: row.outcome === null, outcome: row.outcome, reason: row.reason }
  }

  /**
   * The undismissed `done` summaries of the sessions `principal` owns (#1218), newest first, at
   * most PENDING_CAP. Nothing expires them, so `summariesTruncated` says there were more and the
   * oldest are not listed. A row nothing parks on any more (a classic question, from before
   * every session was an AgentSession) is not listed: nobody would read its answer.
   */
  async listPending(principal: Owner): Promise<{ questions: PendingQuestion[]; summariesTruncated: boolean }> {
    const done = await this.deps.sql<
      {
        id: string
        session_id: string
        kind: 'question' | 'attention'
        tool: string
        tool_use_id: string
        questions: QuestionView[]
        attention_reason: AttentionReason | null
        on_timeout: OnTimeout | null
        summary: string | null
        created_at: Date
        expires_at: Date | null
      }[]
    >`
      SELECT q.id, q.session_id, q.kind, q.tool, q.tool_use_id, q.questions, q.attention_reason, q.on_timeout, q.summary,
             q.created_at, q.expires_at
      FROM ai_questions q JOIN ai_sessions s ON s.id = q.session_id
      WHERE s.owner_kind = ${principal.kind} AND s.owner_id = ${principal.id}
        AND q.outcome IS NULL AND ${isDoneSummary(this.deps.sql, 'q')}
      ORDER BY q.created_at DESC, q.id DESC LIMIT ${PENDING_CAP + 1}`
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
    const [row] = await this.deps.sql<{ session_id: string; tool: string | null; requested_at: Date | null; response: { answers?: unknown } | null }[]>`
      SELECT session_id, tool, requested_at, response FROM ai_input_responses
       WHERE request_id = ${requestId} AND kind = 'answer' AND outcome = 'answered'`
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
    const [asked] = await this.deps.sql<{ questions: QuestionView[]; outcome: Outcome }[]>`
      SELECT questions, outcome FROM ai_questions WHERE id = ${id} AND session_id = ${sessionId}`
    if (!asked) throw new QuestionError('not_found', `no question ${id} in this session`)
    if (asked.outcome !== null) throw new QuestionError('conflict', `question ${id} is no longer waiting for an answer`)
    if (answers.length !== asked.questions.length || answers.some((a) => !a.trim())) {
      throw new QuestionError('invalid', `question ${id} needs one answer for each of its ${asked.questions.length} questions`)
    }
    const answered = await this.atomically(sessionId, async (tx) => {
      const [row] = await tx<Resolved[]>`
        UPDATE ai_questions
        SET outcome = 'answered', answers = ${tx.json(answers)}, resolved_at = now(),
            answered_by_kind = ${principal.kind}, answered_by_id = ${principal.id}, answered_by_label = ${principal.label}
        WHERE id = ${id} AND session_id = ${sessionId} AND outcome IS NULL
        RETURNING id, turn_id, tool, tool_use_id, created_at`
      return row
        ? {
            value: row,
            events: [
              event({ type: 'question.resolved', sessionId, id, answered: true, answers, by: principal }),
              inputResolved(sessionId, `question:${id}`, 'answer', 'answered'),
            ],
          }
        : { value: undefined, events: [] }
    })
    if (!answered) throw new QuestionError('conflict', `question ${id} is no longer waiting for an answer`)
    // Hashed, never stored as text: an answer may be anything the user typed.
    await this.audited([
      {
        kind: 'question',
        action: 'answered',
        surface: where.surface ?? 'http',
        actor: principal,
        clientIp: where.clientIp,
        sessionId,
        turnId: answered.turn_id,
        toolUseId: answered.tool_use_id,
        tier: 'read',
        inputHash: this.deps.audit?.hash(answered.tool, { answers }),
        outcome: 'ok',
        detail: `${answered.tool} question ${id}: ${answers.length} answer${answers.length === 1 ? '' : 's'}`,
        startedAt: answered.created_at,
        finishedAt: new Date(),
      },
    ])
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
   * Runs `work` in a transaction that holds ATTENTION_RATE_LOCK. The lock is tried, not waited
   * on: its holder may wait up to OPEN_WAIT_MS on a workflow Update, whose activity needs a
   * connection of the same pool, and a waiter blocked in the lock would hold one all that time.
   * Undefined when the lock was not had within `rateLockWaitMs` (#5053).
   */
  private async underRateLock<T>(work: (tx: TransactionSql) => Promise<T>): Promise<{ value: T } | undefined> {
    const until = performance.now() + (this.deps.rateLockWaitMs ?? RATE_LOCK_WAIT_MS)
    for (;;) {
      const done = await this.deps.sql.begin(async (tx) => {
        const [lock] = await tx<{ held: boolean }[]>`SELECT pg_try_advisory_xact_lock(hashtextextended(${ATTENTION_RATE_LOCK}, 0)) AS held`
        return lock?.held ? { value: await work(tx) } : undefined
      })
      if (done) return done
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
   * which the workflow's entry is written by, runs under the lock, so two requests raised at once
   * cannot both pass at the limit. Else the refusal the model reads, audited, and `open` never
   * runs. Best effort past OPEN_WAIT_MS: an open that takes longer goes on outside the lock, so
   * a request counted meanwhile does not see its entry. A lock not had within RATE_LOCK_WAIT_MS
   * refuses the request, as the limit does (#5053).
   */
  async attentionLimit(
    context: { sessionId: string; turnId: string; toolUseId: string; tool: string; reason: string; own: boolean },
    open: () => Promise<void>,
  ): Promise<string | undefined> {
    const { own } = context
    const tab = '%:tab:%'
    const limit = own ? ATTENTION_RATE_LIMIT : TAB_WAIT_RATE_LIMIT
    const counted = await this.underRateLock(async (tx) => {
      const [row] = await tx<{ n: number }[]>`
        SELECT
          (SELECT count(*) FROM ai_pending_input
             WHERE tool = ${WAIT_FOR_USER} AND created_at > now() - make_interval(secs => ${ATTENTION_RATE_WINDOW_S})
               AND (request_id LIKE ${tab}) <> ${own})
          + (SELECT count(*) FROM ai_input_responses
             WHERE tool = ${WAIT_FOR_USER} AND requested_at > now() - make_interval(secs => ${ATTENTION_RATE_WINDOW_S})
               AND (request_id LIKE ${tab}) <> ${own}) AS n`
      const n = Number(row?.n ?? 0)
      // Bounded: the lock is every session's, and a slow or absent workflow must not hold it.
      // Past OPEN_WAIT_MS the entry opens outside it, so the limit may then let one more through.
      if (n < limit) {
        let timer: ReturnType<typeof setTimeout> | undefined
        await Promise.race([open(), new Promise<void>((resolve) => (timer = setTimeout(resolve, OPEN_WAIT_MS)))]).finally(() => clearTimeout(timer))
      }
      return n
    })
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
      const { sessionId, turnId } = context
      const startedAt = new Date()
      const posted = await this.atomically(sessionId, async (tx) => {
        const none = { asked: false, superseded: [] as Resolved[], limited: undefined as string | undefined }
        // Still this turn's, and still the user's: after a handoff mid-turn the new owner is not posted to.
        const [owner] = await tx<{ owner_kind: string }[]>`
          SELECT owner_kind FROM ai_sessions WHERE id = ${sessionId} AND durable_turn_id = ${turnId} FOR UPDATE`
        if (owner?.owner_kind !== 'browser') return { value: none, events: [] }
        // Capped per turn (DONE_POSTS_PER_TURN), a count the session row locked above serialises.
        const [count] = await tx<{ n: number }[]>`
          SELECT count(*)::int AS n FROM ai_questions
          WHERE session_id = ${sessionId} AND turn_id = ${turnId} AND kind = 'attention' AND attention_reason = 'done'`
        if ((count?.n ?? 0) >= DONE_POSTS_PER_TURN) {
          return { value: { ...none, limited: `The summary was not posted: this turn already posted ${DONE_POSTS_PER_TURN}, the most a turn may.` }, events: [] }
        }
        // A summary that recorded unattended actions (`unattended`) is not replaced by a later
        // turn's: that record is the user's check on what ran while nobody answered, and the later
        // summary covers only its own turn. It stays until dismissed. The same turn's earlier
        // summary is replaced as usual: the newer one covers the same windows.
        const superseded = await tx<Resolved[]>`
          UPDATE ai_questions SET outcome = 'cancelled', reason = ${REPLACED}, resolved_at = now()
          WHERE session_id = ${sessionId} AND kind = 'attention' AND attention_reason = 'done'
            AND outcome IS NULL AND (NOT unattended OR turn_id = ${turnId}) AND (tool = ${ATTENTION_TOOL}) = ${request.tool === ATTENTION_TOOL}
          RETURNING id, turn_id, tool, tool_use_id, created_at`
        const tail: ServerEvent[] = superseded.flatMap((r) => [
          event({ type: 'question.resolved', sessionId, id: r.id, answered: false, reason: REPLACED }),
          inputResolved(sessionId, `question:${r.id}`, 'answer', 'cancelled', REPLACED),
        ])
        const done = await loadDoneSummary(tx, sessionId, turnId, context.turnStartedAt, context.secrets())
        const [inserted] = await tx<{ created_at: Date }[]>`
          INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary, unattended)
          VALUES (${id}, ${sessionId}, ${turnId}, ${request.tool}, ${request.toolUseId}, ${tx.json(questions)},
                  'attention', 'done', ${done.summary}, ${done.unattended})
          RETURNING created_at`
        const createdAt = inserted?.created_at ?? new Date()
        tail.push(
          event({ type: 'question.asked', sessionId, id, tool: request.toolUseId, questions, attention: { reason: 'done' as const, summary: done.summary } }),
          inputRequested(
            sessionId,
            questionEntry({ id, sessionId, tool: request.tool, questions, attentionReason: 'done', onTimeout: null, summary: done.summary, createdAt: createdAt.toISOString(), expiresAt: null }),
          ),
        )
        // A done summary waits for nobody: the session goes on as it was.
        return { value: { ...none, asked: true, superseded }, events: tail }
      })
      await this.audited(
        posted.superseded.map((r) => ({
          kind: 'question',
          action: 'cancelled',
          surface: 'system',
          actor: SYSTEM_ACTOR,
          sessionId,
          turnId: r.turn_id,
          toolUseId: r.tool_use_id,
          tier: 'read',
          outcome: 'refused',
          detail: safeDetail(`${r.tool} attention request ${r.id}: ${REPLACED}`),
          startedAt: r.created_at,
          finishedAt: new Date(),
        })),
      )
      if (posted.limited !== undefined) {
        // #1205: a refused post leaves a trail, so a looping agent shows in the audit log. A done
        // post is the turn's summary, which waits for nothing: not an attention request (#3236).
        await this.audited([
          {
            kind: 'question',
            action: 'rate_limited',
            surface: 'system',
            actor: SYSTEM_ACTOR,
            sessionId,
            turnId,
            toolUseId: request.toolUseId,
            tier: 'read',
            outcome: 'refused',
            detail: safeDetail(`${request.tool} done summary refused: ${posted.limited}`),
            startedAt,
            finishedAt: new Date(),
          },
        ])
        return { answered: false, message: posted.limited }
      }
      if (!posted.asked) return { answered: false, message: 'The question was not asked: the session is no longer the user’s, or its turn ended.' }
      return { answered: false, posted: true, message: 'posted' }
    }
  }
}

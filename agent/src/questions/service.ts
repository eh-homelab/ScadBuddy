import { randomUUID } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { type AuditEntry, type AuditLog, type AuditSurface, safeDetail, SYSTEM_ACTOR } from '../audit/log.js'
import type { AttentionReason, OnTimeout } from '../harness/attention.js'
import { parseQuestions, type QuestionGate, type QuestionRequest, type QuestionVerdict, type UserQuestion } from '../harness/questions.js'
import { isUuid } from '../harness/stateDirs.js'
import { loadDoneSummary, UNATTENDED_HEADING } from './doneSummary.js'
import { redact } from '../secrets.js'
import type { EventLog } from '../sessions/eventLog.js'
import {
  event,
  type Owner,
  PROTOCOL_VERSION,
  type QuestionAnswerMessage,
  type QuestionView,
  type ServerEvent,
} from '../sessions/protocol.js'

// Questions the agent asks the user (#940, harness/questions.ts), in Postgres
// (`ai_questions`), so the panel may answer through any replica.
//
// HOW A QUESTION WAITS. The turn's QuestionGate (`gate()`) records the
// question, sets the session to `waiting_input`, emits `question.asked`, and
// parks the AskUserQuestion call until the row is resolved: the waiter wakes
// at once for an answer given on this replica and polls the row every
// `pollMs` for one given on another, as approvals do (approvals/service.ts).
// Answered, the call is allowed with the answers; anything else reaches the
// model as the tool's error.
//
// A QUESTION NEVER OUTLIVES ITS TURN. When the turn ends, for any reason
// (interrupt, shutdown, failure, the reaper), its pending questions are
// cancelled (`cancelPending`, from sessions/manager.ts). A question is asked
// again by the next turn if the model still needs it; there is no orphan to
// resume, unlike an approval. A question has no expiry of its own yet (spec
// §6.6 adds `question_expiry_seconds`, which must cancel, never answer).
//
// ATTENTION REQUESTS (#815, harness/attention.ts) are rows here too (`kind =
// 'attention'`): the same gate, card, answer and cancellation. Two things are
// theirs only. They have a timer, run by the parked waiter itself: a request
// never outlives its turn either, so the replica holding the turn is the one
// place it can fire. When it fires the row is `timed_out`, never answered, and
// `on_timeout` decides the rest (`proceed` returns, `wait` and `stop` end the
// turn). And they are throttled (#815 §5): one open request per session per
// reason (a new one supersedes the last), and at most ATTENTION_RATE_LIMIT
// created per ATTENTION_RATE_WINDOW_S across the user's sessions.
//
// A `done` ATTENTION REQUEST (#815 §4) is the exception to both rules above: it
// is posted, never waited on. The call returns at once, the session never shows
// `waiting_input` for it, and it OUTLIVES ITS TURN: it stays pending, and on the
// badge, until the user dismisses it on its card (`answer`) or the session posts
// a newer one. Its row carries `summary`, ScadBuddy's record of what the turn
// touched (doneSummary.ts). That is a done row WITHOUT a timer (`expires_at`
// NULL): one with a timer was inserted by a replica on an older image, whose
// turn parks on it, so it is treated as any other attention request.
//
// WHO ANSWERS. Only the user in the ScadBuddy panel (the browser principal):
// the question is the agent's own call, and an answer is a human's. No tool
// result, no MCP client and no other agent can answer one; content the model
// read can at most make it ASK, which is the model's call like any other.

export const DEFAULT_QUESTION_POLL_MS = 1000

/** How many rows listPending returns per group: waiting rows, then `done` summaries. */
export const PENDING_CAP = 500
/**
 * #815 §5's per-user rate limit on attention requests: this many per window. A
 * `done` summary neither counts nor is limited: a timeout tells the model to
 * post one, so a turn that timed out often must still be able to.
 */
export const ATTENTION_RATE_LIMIT = 10
export const ATTENTION_RATE_WINDOW_S = 600
/** The advisory lock the rate limit's count and insert are taken under. */
const ATTENTION_RATE_LOCK = 'scadbuddy:attention-rate'

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
  pollMs?: number
  /** The AI audit log (#1075): one `question` row per answer. */
  audit?: Pick<AuditLog, 'record' | 'hash'>
}

/** What `gate()` needs to know about the turn it parks. */
export type QuestionGateContext = {
  sessionId: string
  turnId: string
  /** Redacted from the stored questions and the events (the turn's credential). */
  secrets: () => readonly string[]
  /** The turn's own abort signal (interrupt, shutdown). */
  signal: AbortSignal
  /** Ends the turn as an interrupt does: an attention request's `stop` and `wait` timers (#815). */
  stopTurn?: (why: string) => void
  /**
   * When the turn was claimed, by the database's clock (the claim's `now()`): a
   * `done` summary covers the touches since, and `ai_session_resources.at` is
   * stamped by the same clock. The agent's own clock may disagree with it.
   */
  turnStartedAt: Date
}

type Row = {
  id: string
  session_id: string
  outcome: 'answered' | 'cancelled' | 'timed_out' | null
  answers: string[] | null
  reason: string | null
}

/** A resolved row's columns the audit log needs. */
type Resolved = { id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }

/** An `answer`-kind entry still waiting, as GET /api/v1/ai/pending-input lists it (spec §6.6). */
export type PendingQuestion = {
  id: string
  sessionId: string
  kind: 'question' | 'attention'
  tool: string
  toolUseId: string
  questions: QuestionView[]
  attentionReason: AttentionReason | null
  /** Null for a question, and for a `done` request, which has no timer. */
  onTimeout: OnTimeout | null
  /** A `done` request's record of what its turn touched (doneSummary.ts); null otherwise. */
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
  private readonly pollMs: number
  /** Parked waiters on this replica, by question id: wake them at once. */
  private readonly waiters = new Map<string, Set<() => void>>()

  constructor(deps: QuestionServiceDeps) {
    this.deps = deps
    this.pollMs = deps.pollMs ?? DEFAULT_QUESTION_POLL_MS
  }

  /**
   * Runs `change` in one transaction with the append of the events it returns,
   * so a row and the event that reports it commit together (as approvals do,
   * approvals/service.ts `create`/`settle`): a watcher never sees a question
   * resolved, or a session waiting, that the log does not show. Followers are
   * woken once it has committed.
   */
  private async atomically<T>(
    sessionId: string,
    change: (tx: TransactionSql) => Promise<{ value: T; events: ServerEvent[] }>,
  ): Promise<T> {
    let logged: { events: ServerEvent[]; seqs: number[] } | undefined
    const value = await this.deps.sql.begin(async (tx) => {
      const { value, events } = await change(tx)
      if (events.length) logged = { events, seqs: await this.deps.events.append(sessionId, events, tx) }
      return value
    })
    if (logged) this.deps.events.committed(sessionId, logged.events, logged.seqs)
    return value as T
  }

  private async row(id: string): Promise<Row | undefined> {
    const [row] = await this.deps.sql<Row[]>`
      SELECT id, session_id, outcome, answers, reason FROM ai_questions WHERE id = ${id}`
    return row
  }

  /**
   * Every question and attention request still waiting for the user, oldest
   * first (at most PENDING_CAP), then the undismissed `done` summaries, newest
   * first (at most PENDING_CAP more). The summaries have their own cap: nothing
   * expires them, so under one shared cap enough of them would push a question
   * a turn is parked on off the badge.
   */
  async listPending(): Promise<PendingQuestion[]> {
    type Pending = {
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
    }
    const waiting = await this.deps.sql<Pending[]>`
      SELECT id, session_id, kind, tool, tool_use_id, questions, attention_reason, on_timeout, summary, created_at, expires_at
      FROM ai_questions WHERE outcome IS NULL AND (attention_reason IS DISTINCT FROM 'done' OR expires_at IS NOT NULL)
      ORDER BY created_at, id LIMIT ${PENDING_CAP}`
    const done = await this.deps.sql<Pending[]>`
      SELECT id, session_id, kind, tool, tool_use_id, questions, attention_reason, on_timeout, summary, created_at, expires_at
      FROM ai_questions WHERE outcome IS NULL AND attention_reason = 'done' AND expires_at IS NULL
      ORDER BY created_at DESC, id DESC LIMIT ${PENDING_CAP}`
    return [...waiting, ...done].map((r) => ({
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
  }

  /**
   * Sets a session that no longer waits on a question back from
   * `waiting_input`: to `waiting_approval` while an approval is pending, to
   * `running` while its turn is live, else `idle`.
   */
  async refreshStatus(sessionId: string): Promise<void> {
    await this.atomically(sessionId, async (tx) => {
      const [row] = await tx<{ status: 'running' | 'idle' | 'waiting_approval' }[]>`
      UPDATE ai_sessions
      SET status = CASE
            WHEN EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = ${sessionId} AND decision IS NULL)
              THEN 'waiting_approval'
            WHEN turn_id IS NOT NULL AND lease_until > now() THEN 'running'
            ELSE 'idle'
          END,
          updated_at = now()
      WHERE id = ${sessionId} AND status = 'waiting_input'
        AND NOT EXISTS (SELECT 1 FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL
                        AND (attention_reason IS DISTINCT FROM 'done' OR expires_at IS NOT NULL))
      RETURNING status`
      return { value: undefined, events: row ? [event({ type: 'session.status', sessionId, status: row.status })] : [] }
    })
  }

  /**
   * The panel's `question.answer`, from the user in the panel. `where` is for
   * the audit log, as for an approval decision: the answerer's address and the
   * surface ('http', the panel, when omitted).
   */
  async answer(
    principal: Owner,
    message: QuestionAnswerMessage,
    where: { clientIp?: string | undefined; surface?: AuditSurface } = {},
  ): Promise<void> {
    if (message.v !== PROTOCOL_VERSION || message.type !== 'question.answer') {
      throw new QuestionError('invalid', 'not a question.answer message')
    }
    if (principal.kind !== 'browser') {
      throw new QuestionError('forbidden', "only the user in the ScadBuddy panel answers the agent's questions")
    }
    const { sessionId, id, answers } = message
    if (!isUuid(id) || !isUuid(sessionId)) throw new QuestionError('not_found', `no question ${id} in this session`)
    const [asked] = await this.deps.sql<{ questions: QuestionView[]; outcome: Row['outcome'] }[]>`
      SELECT questions, outcome FROM ai_questions WHERE id = ${id} AND session_id = ${sessionId}`
    if (!asked) throw new QuestionError('not_found', `no question ${id} in this session`)
    if (asked.outcome !== null) throw new QuestionError('conflict', `question ${id} is no longer waiting for an answer`)
    if (answers.length !== asked.questions.length || answers.some((a) => !a.trim())) {
      throw new QuestionError('invalid', `question ${id} needs one answer for each of its ${asked.questions.length} questions`)
    }
    const answered = await this.atomically(sessionId, async (tx) => {
      const [row] = await tx<{ turn_id: string; tool: string; tool_use_id: string; created_at: Date }[]>`
        UPDATE ai_questions
        SET outcome = 'answered', answers = ${tx.json(answers)}, resolved_at = now(),
            answered_by_kind = ${principal.kind}, answered_by_id = ${principal.id}, answered_by_label = ${principal.label}
        WHERE id = ${id} AND session_id = ${sessionId} AND outcome IS NULL
        RETURNING turn_id, tool, tool_use_id, created_at`
      return row
        ? { value: row, events: [event({ type: 'question.resolved', sessionId, id, answered: true, answers, by: principal })] }
        : { value: undefined, events: [] }
    })
    if (!answered) throw new QuestionError('conflict', `question ${id} is no longer waiting for an answer`)
    this.wake(id)
    try {
      await this.refreshStatus(sessionId)
    } finally {
      // The answer is committed: its row is written even if the status refresh failed.
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
  }

  /**
   * Cancels a session's pending questions (its turn ended, a handoff); returns
   * how many. `turnId`: only that turn's, so a turn that lost its claim never
   * cancels the newer turn's. A parked call is refused with `reason`.
   * `refresh: false`: the caller (a finishing turn) sets the status itself.
   * A `done` request is left alone unless named by `questionId`: nothing waits
   * on it, and it is meant to outlive its turn. A timed one (from a replica on
   * an older image) is cancelled like any other: only its turn's timer would
   * ever have resolved it.
   */
  async cancelPending(
    sessionId: string,
    reason: string,
    options: { refresh?: boolean; turnId?: string; questionId?: string } = {},
  ): Promise<number> {
    const turnId = options.turnId ?? null
    const questionId = options.questionId ?? null
    const rows = await this.atomically(sessionId, async (tx) => {
      const cancelled = await tx<{ id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }[]>`
        UPDATE ai_questions SET outcome = 'cancelled', reason = ${reason}, resolved_at = now()
        WHERE session_id = ${sessionId} AND outcome IS NULL
          AND (${turnId}::uuid IS NULL OR turn_id = ${turnId}::uuid)
          AND (${questionId}::uuid IS NULL OR id = ${questionId}::uuid)
          AND (${questionId}::uuid IS NOT NULL OR attention_reason IS DISTINCT FROM 'done' OR expires_at IS NOT NULL)
        RETURNING id, turn_id, tool, tool_use_id, created_at`
      return {
        value: cancelled,
        events: cancelled.map((r) => event({ type: 'question.resolved', sessionId, id: r.id, answered: false, reason })),
      }
    })
    if (rows.length === 0) return 0
    for (const r of rows) this.wake(r.id)
    try {
      if (options.refresh !== false) await this.refreshStatus(sessionId)
    } finally {
      // ScadBuddy cancelled it (the turn ended, a handoff), not a person: an
      // approval cancelled the same way is audited the same way.
      await this.audited(
        rows.map((r) => ({
          kind: 'question',
          action: 'cancelled',
          surface: 'system',
          actor: SYSTEM_ACTOR,
          sessionId,
          turnId: r.turn_id,
          toolUseId: r.tool_use_id,
          tier: 'read',
          outcome: 'refused',
          detail: safeDetail(`${r.tool} question ${r.id}: ${reason}`),
          startedAt: r.created_at,
          finishedAt: new Date(),
        })),
      )
    }
    return rows.length
  }

  /**
   * The audit rows of resolved questions (#1075), after the state they report
   * is committed and the session's status set: AuditLog never throws, but a
   * sink that did must not leave the session showing a wait that is over, nor
   * cost the other rows theirs.
   */
  private async audited(entries: AuditEntry[]): Promise<void> {
    const audit = this.deps.audit
    if (!audit) return
    await Promise.allSettled(entries.map((entry) => audit.record(entry)))
  }

  // -- waiting -----------------------------------------------------------------

  private wake(id: string): void {
    for (const wake of this.waiters.get(id) ?? []) wake()
  }

  private pause(id: string, signal: AbortSignal, ms: number): Promise<void> {
    return new Promise((resolve) => {
      const set = this.waiters.get(id) ?? new Set()
      this.waiters.set(id, set)
      const done = () => {
        clearTimeout(timer)
        set.delete(done)
        if (set.size === 0 && this.waiters.get(id) === set) this.waiters.delete(id)
        signal.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, ms)
      set.add(done)
      signal.addEventListener('abort', done, { once: true })
    })
  }

  /**
   * Waits until the question is resolved; undefined once `signal` aborts first,
   * 'due' once `deadline` (epoch ms, an attention request's timer) passes first.
   */
  private async waitFor(id: string, signal: AbortSignal, deadline?: number): Promise<Row | 'due' | undefined> {
    for (;;) {
      if (signal.aborted) return undefined
      const row = await this.row(id)
      if (!row) throw new Error(`question ${id} no longer exists`)
      if (row.outcome !== null) return row
      const left = deadline === undefined ? this.pollMs : deadline - Date.now()
      if (left <= 0) return 'due'
      await this.pause(id, signal, Math.min(this.pollMs, left))
    }
  }

  /**
   * An attention request's timer (#815): resolves the row as `timed_out`, never
   * answered, and returns it; or, when an answer or a cancellation won the race,
   * the row as that left it.
   */
  private async timeOut(sessionId: string, id: string, onTimeout: OnTimeout): Promise<Row> {
    const reason = `nobody replied in time (on_timeout: ${onTimeout})`
    const resolved = await this.atomically(sessionId, async (tx) => {
      const [row] = await tx<Resolved[]>`
        UPDATE ai_questions SET outcome = 'timed_out', reason = ${reason}, resolved_at = now()
        WHERE id = ${id} AND outcome IS NULL
        RETURNING id, turn_id, tool, tool_use_id, created_at`
      return row
        ? { value: row, events: [event({ type: 'question.resolved', sessionId, id, answered: false, reason })] }
        : { value: undefined, events: [] }
    })
    if (resolved) {
      this.wake(id)
      try {
        await this.refreshStatus(sessionId)
      } finally {
        await this.audited([
          {
            kind: 'question',
            action: 'timed_out',
            surface: 'system',
            actor: SYSTEM_ACTOR,
            sessionId,
            turnId: resolved.turn_id,
            toolUseId: resolved.tool_use_id,
            tier: 'read',
            outcome: 'refused',
            detail: safeDetail(`${resolved.tool} attention request ${id}: ${reason}`),
            startedAt: resolved.created_at,
            finishedAt: new Date(),
          },
        ])
      }
    }
    const row = await this.row(id)
    if (!row) throw new Error(`question ${id} no longer exists`)
    return row
  }

  /** The canUseTool question gate for one turn (harness/questions.ts QuestionGate). */
  gate(context: QuestionGateContext): QuestionGate {
    return async (request: QuestionRequest): Promise<QuestionVerdict> => {
      if (context.signal.aborted) return { answered: false, message: 'The turn is stopping; the question was not asked.' }
      const id = randomUUID()
      const questions = redactQuestions(request.questions, context.secrets())
      // Redaction can merge two labels or lengthen a string past its bound; the
      // panel would drop that frame and the turn would wait for nobody.
      const shown = parseQuestions({ questions })
      if (!shown.ok) {
        return {
          answered: false,
          message: `The question could not be shown: with secrets redacted it is not a valid question (${shown.error}). Ask it without the secret.`,
        }
      }
      const { sessionId, turnId } = context
      const { attention } = request
      const asked = await this.atomically(sessionId, async (tx) => {
        const none = { asked: false, superseded: [] as Resolved[] }
        // Still this turn's, and still the user's: after a handoff mid-turn
        // the new owner is not asked, so nothing parks for them.
        const [owner] = await tx<{ owner_kind: string }[]>`
          SELECT owner_kind FROM ai_sessions WHERE id = ${sessionId} AND turn_id = ${turnId} FOR UPDATE`
        if (owner?.owner_kind !== 'browser') return { value: none, events: [] }
        const tail: ServerEvent[] = []
        let superseded: Resolved[] = []
        let expiresAt: Date | null = null
        let summary: string | null = null
        if (attention) {
          // #815 §5: a per-user rate limit (every session is the browser user's,
          // or the request was refused above), then one open request per reason.
          // The limit spans sessions and replicas, so its count and insert hold one
          // lock that does too (to commit), or two turns could each read 9 and insert.
          await tx`SELECT pg_advisory_xact_lock(hashtextextended(${ATTENTION_RATE_LOCK}, 0))`
          const [recent] = await tx<{ n: number }[]>`
            SELECT count(*)::int AS n FROM ai_questions
            WHERE kind = 'attention' AND (attention_reason <> 'done' OR expires_at IS NOT NULL)
              AND created_at > now() - make_interval(secs => ${ATTENTION_RATE_WINDOW_S})`
          if (attention.reason !== 'done' && (recent?.n ?? 0) >= ATTENTION_RATE_LIMIT) {
            return { value: { ...none, limited: true }, events: [] }
          }
          // A done summary that recorded unattended actions is not replaced by a later
          // turn's: that record is the user's check on what ran while nobody answered,
          // and the later summary covers only its own turn. It stays until dismissed.
          // The same turn's earlier summary is replaced as usual: the newer one covers
          // the same windows, and keeping it would let one turn post done rows unbounded
          // (done is outside the rate limit).
          const why = 'replaced by a newer request for the same reason'
          superseded = await tx<Resolved[]>`
            UPDATE ai_questions SET outcome = 'cancelled', reason = ${why}, resolved_at = now()
            WHERE session_id = ${sessionId} AND kind = 'attention' AND attention_reason = ${attention.reason}
              AND outcome IS NULL AND ((summary LIKE ${`${UNATTENDED_HEADING}%`}) IS NOT TRUE OR turn_id = ${turnId})
            RETURNING id, turn_id, tool, tool_use_id, created_at`
          for (const r of superseded) {
            tail.push(event({ type: 'question.resolved', sessionId, id: r.id, answered: false, reason: why }))
          }
          if (attention.reason === 'done') {
            summary = redact(await loadDoneSummary(tx, sessionId, turnId, context.turnStartedAt), context.secrets())
            await tx`
              INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary)
              VALUES (${id}, ${sessionId}, ${turnId}, ${request.tool}, ${request.toolUseId}, ${tx.json(questions)},
                      'attention', 'done', ${summary})`
          } else {
            const [inserted] = await tx<{ expires_at: Date }[]>`
              INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions,
                                        kind, attention_reason, on_timeout, expires_at)
              VALUES (${id}, ${sessionId}, ${turnId}, ${request.tool}, ${request.toolUseId}, ${tx.json(questions)},
                      'attention', ${attention.reason}, ${attention.onTimeout},
                      now() + make_interval(secs => ${attention.timeoutS}))
              RETURNING expires_at`
            expiresAt = inserted?.expires_at ?? null
          }
        } else {
          await tx`
            INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions)
            VALUES (${id}, ${sessionId}, ${turnId}, ${request.tool}, ${request.toolUseId}, ${tx.json(questions)})`
        }
        tail.push(
          event({
            type: 'question.asked',
            sessionId,
            id,
            tool: request.toolUseId,
            questions,
            ...(attention?.reason === 'done'
              ? { attention: { reason: 'done' as const, summary: summary ?? '' } }
              : attention && expiresAt
                ? { attention: { reason: attention.reason, onTimeout: attention.onTimeout, expiresAt: expiresAt.toISOString() } }
                : {}),
          }),
        )
        // A done summary waits for nobody: the session goes on as it was.
        if (attention?.reason === 'done') return { value: { asked: true, superseded }, events: tail }
        // Only the parked turn itself moves the session to waiting_input, from
        // running or from an approval it is also waiting on (the latest wait is
        // shown; each refreshStatus hands back to whichever is still pending).
        const moved = await tx`
          UPDATE ai_sessions SET status = 'waiting_input', updated_at = now()
          WHERE id = ${sessionId} AND turn_id = ${turnId} AND status <> 'waiting_input'`
        if (moved.count > 0) tail.push(event({ type: 'session.status', sessionId, status: 'waiting_input' }))
        return { value: { asked: true, superseded }, events: tail }
      })
      for (const r of asked.superseded) this.wake(r.id)
      await this.audited(
        asked.superseded.map((r) => ({
          kind: 'question',
          action: 'cancelled',
          surface: 'system',
          actor: SYSTEM_ACTOR,
          sessionId,
          turnId: r.turn_id,
          toolUseId: r.tool_use_id,
          tier: 'read',
          outcome: 'refused',
          detail: safeDetail(`${r.tool} attention request ${r.id}: replaced by a newer request for the same reason`),
          startedAt: r.created_at,
          finishedAt: new Date(),
        })),
      )
      if ('limited' in asked) {
        return {
          answered: false,
          message:
            `The user was not asked: at most ${ATTENTION_RATE_LIMIT} attention requests are sent in ` +
            `${ATTENTION_RATE_WINDOW_S / 60} minutes. Say what you need in your reply instead.`,
        }
      }
      if (!asked.asked) {
        return { answered: false, message: 'The question was not asked: the session is no longer the user’s, or its turn ended.' }
      }
      if (attention?.reason === 'done') return { answered: false, posted: true, message: 'posted' }
      // An abort (interrupt, shutdown) ends the wait and leaves the row
      // pending: the finishing turn cancels it (sessions/manager.ts finish).
      const signal = AbortSignal.any([context.signal, request.signal])
      const deadline = attention ? Date.now() + attention.timeoutS * 1000 : undefined
      const waited = await this.waitFor(id, signal, deadline)
      const resolved = waited === 'due' && attention ? await this.timeOut(sessionId, id, attention.onTimeout) : waited
      if (!resolved || resolved === 'due') {
        // The SDK dropped this one call while the turn goes on: its card must not stay
        // answerable for an answer nobody would read, nor the session say it waits.
        // (A turn that stopped cancels its questions as it finishes.)
        if (!context.signal.aborted) await this.cancelPending(sessionId, 'the call was withdrawn', { questionId: id })
        return { answered: false, message: 'The user did not answer: the turn stopped first.' }
      }
      if (resolved.outcome === 'timed_out' && attention) {
        if (attention.onTimeout === 'proceed') return { answered: false, timedOut: true, message: resolved.reason ?? 'timed out' }
        // `wait` waited to its ceiling and then does what `stop` does: end the
        // turn, as an interrupt would. Never an answer, never an approval.
        const why = `nobody replied to the attention request in ${attention.timeoutS} s (on_timeout: ${attention.onTimeout})`
        context.stopTurn?.(why)
        return { answered: false, message: `The user did not reply: ${why}. Your turn ends here.` }
      }
      if (resolved.outcome !== 'answered' || !resolved.answers) {
        return { answered: false, message: `The user did not answer: ${resolved.reason ?? 'the question was cancelled'}.` }
      }
      // Keyed by the question as the model asked it, which is how the tool reports answers.
      const answers: Record<string, string> = {}
      request.questions.forEach((q, i) => {
        answers[q.question] = resolved.answers?.[i] ?? ''
      })
      return { answered: true, answers }
    }
  }
}

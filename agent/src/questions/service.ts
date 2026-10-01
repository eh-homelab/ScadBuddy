import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import type { QuestionGate, QuestionRequest, QuestionVerdict, UserQuestion } from '../harness/questions.js'
import { isUuid } from '../harness/stateDirs.js'
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
// resume, unlike an approval. There is no expiry of its own yet: the attention
// work in #815 decides how long a question may wait, and a timeout there must
// cancel, never answer.
//
// WHO ANSWERS. Only the user in the ScadBuddy panel (the browser principal):
// the question is the agent's own call, and an answer is a human's. No tool
// result, no MCP client and no other agent can answer one; content the model
// read can at most make it ASK, which is the model's call like any other.

export const DEFAULT_QUESTION_POLL_MS = 1000

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
}

/** What `gate()` needs to know about the turn it parks. */
export type QuestionGateContext = {
  sessionId: string
  turnId: string
  /** Redacted from the stored questions and the events (the turn's credential). */
  secrets: () => readonly string[]
  /** The turn's own abort signal (interrupt, shutdown). */
  signal: AbortSignal
}

type Row = {
  id: string
  session_id: string
  outcome: 'answered' | 'cancelled' | null
  answers: string[] | null
  reason: string | null
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

  private async append(sessionId: string, events: ServerEvent[]): Promise<void> {
    if (events.length) await this.deps.events.append(sessionId, events)
  }

  private async row(id: string): Promise<Row | undefined> {
    const [row] = await this.deps.sql<Row[]>`
      SELECT id, session_id, outcome, answers, reason FROM ai_questions WHERE id = ${id}`
    return row
  }

  /** Whether the session has a question nobody answered yet. */
  async hasPending(sessionId: string): Promise<boolean> {
    const [row] = await this.deps.sql<{ n: number }[]>`
      SELECT count(*)::int AS n FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL`
    return (row?.n ?? 0) > 0
  }

  /**
   * Sets a session that no longer waits on a question back from
   * `waiting_input`: to `waiting_approval` while an approval is pending, to
   * `running` while its turn is live, else `idle`.
   */
  async refreshStatus(sessionId: string): Promise<void> {
    const [row] = await this.deps.sql<{ status: 'running' | 'idle' | 'waiting_approval' }[]>`
      UPDATE ai_sessions
      SET status = CASE
            WHEN EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = ${sessionId} AND decision IS NULL)
              THEN 'waiting_approval'
            WHEN turn_id IS NOT NULL AND lease_until > now() THEN 'running'
            ELSE 'idle'
          END,
          updated_at = now()
      WHERE id = ${sessionId} AND status = 'waiting_input'
        AND NOT EXISTS (SELECT 1 FROM ai_questions WHERE session_id = ${sessionId} AND outcome IS NULL)
      RETURNING status`
    if (row) await this.append(sessionId, [event({ type: 'session.status', sessionId, status: row.status })])
  }

  /** The panel's `question.answer`, from the user in the panel. */
  async answer(principal: Owner, message: QuestionAnswerMessage): Promise<void> {
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
    const [row] = await this.deps.sql<{ id: string }[]>`
      UPDATE ai_questions
      SET outcome = 'answered', answers = ${this.deps.sql.json(answers)}, resolved_at = now(),
          answered_by_kind = ${principal.kind}, answered_by_id = ${principal.id}, answered_by_label = ${principal.label}
      WHERE id = ${id} AND session_id = ${sessionId} AND outcome IS NULL
      RETURNING id`
    if (!row) throw new QuestionError('conflict', `question ${id} is no longer waiting for an answer`)
    await this.append(sessionId, [event({ type: 'question.resolved', sessionId, id, answered: true, answers, by: principal })])
    this.wake(id)
    await this.refreshStatus(sessionId)
  }

  /**
   * Cancels a session's pending questions (its turn ended); returns how many.
   * A parked call is refused with `reason`. `refresh: false`: the caller (a
   * finishing turn) sets the status itself.
   */
  async cancelPending(sessionId: string, reason: string, options: { refresh?: boolean } = {}): Promise<number> {
    const rows = await this.deps.sql<{ id: string }[]>`
      UPDATE ai_questions SET outcome = 'cancelled', reason = ${reason}, resolved_at = now()
      WHERE session_id = ${sessionId} AND outcome IS NULL
      RETURNING id`
    if (rows.length === 0) return 0
    await this.append(
      sessionId,
      rows.map((r) => event({ type: 'question.resolved', sessionId, id: r.id, answered: false, reason })),
    )
    for (const r of rows) this.wake(r.id)
    if (options.refresh !== false) await this.refreshStatus(sessionId)
    return rows.length
  }

  // -- waiting -----------------------------------------------------------------

  private wake(id: string): void {
    for (const wake of this.waiters.get(id) ?? []) wake()
  }

  private pause(id: string, signal: AbortSignal): Promise<void> {
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
      const timer = setTimeout(done, this.pollMs)
      set.add(done)
      signal.addEventListener('abort', done, { once: true })
    })
  }

  /** Waits until the question is resolved; undefined once `signal` aborts first. */
  private async waitFor(id: string, signal: AbortSignal): Promise<Row | undefined> {
    for (;;) {
      if (signal.aborted) return undefined
      const row = await this.row(id)
      if (!row) throw new Error(`question ${id} no longer exists`)
      if (row.outcome !== null) return row
      await this.pause(id, signal)
    }
  }

  /** The canUseTool question gate for one turn (harness/questions.ts QuestionGate). */
  gate(context: QuestionGateContext): QuestionGate {
    return async (request: QuestionRequest): Promise<QuestionVerdict> => {
      if (context.signal.aborted) return { answered: false, message: 'The turn is stopping; the question was not asked.' }
      const id = randomUUID()
      const questions = redactQuestions(request.questions, context.secrets())
      const { sessionId, turnId } = context
      const tail: ServerEvent[] = [event({ type: 'question.asked', sessionId, id, tool: request.toolUseId, questions })]
      await this.deps.sql.begin(async (tx) => {
        await tx`
          INSERT INTO ai_questions (id, session_id, turn_id, tool_use_id, questions)
          VALUES (${id}, ${sessionId}, ${turnId}, ${request.toolUseId}, ${tx.json(questions)})`
        // Only the parked turn itself moves the session to waiting_input.
        const moved = await tx`
          UPDATE ai_sessions SET status = 'waiting_input', updated_at = now()
          WHERE id = ${sessionId} AND turn_id = ${turnId} AND status = 'running'`
        if (moved.count > 0) tail.push(event({ type: 'session.status', sessionId, status: 'waiting_input' }))
      })
      await this.append(sessionId, tail)
      // An abort (interrupt, shutdown) ends the wait and leaves the row
      // pending: the finishing turn cancels it (sessions/manager.ts finish).
      const resolved = await this.waitFor(id, AbortSignal.any([context.signal, request.signal]))
      if (!resolved) return { answered: false, message: 'The user did not answer: the turn stopped first.' }
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

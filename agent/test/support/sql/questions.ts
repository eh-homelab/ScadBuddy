import type { Sql } from 'postgres'
import { type AuditEntry, safeDetail, SYSTEM_ACTOR } from '../../../src/audit/log.js'
import type { Logged } from '../../../src/gate/crudStore.js'
import {
  type AnswerCall,
  type DoneFacts,
  type DonePost,
  type GateAnswer,
  type PendingRow,
  type Posted,
  QuestionRefused,
  type QuestionRoutes,
  type QuestionRow,
} from '../../../src/gate/crudQuestions.js'
import { WAIT_FOR_USER } from '../../../src/gate/entry.js'
import { inputRequested, inputResolved, questionEntry } from '../../../src/gate/projection.js'
import { ATTENTION_RATE_WINDOW_S, DONE_POSTS_PER_TURN } from '../../../src/gate/questions.js'
import { ATTENTION_TOOL } from '../../../src/harness/questions.js'
import { event, type Owner, type ServerEvent } from '../../../src/sessions/protocol.js'
import { SqlAudit } from './audit.js'
import { loadDoneFacts } from './doneSummary.js'
import { appendIn } from './gate.js'

// Tests only: the `done` summaries and the attention limits over the test database, the SQL the
// agent ran before the API took area `questions` (#5299 step 12). src holds no SQL; the API's is
// tools/go/core/internal/questions. The lease `attention-rate` is the advisory lock below, and the
// audit rows are written after each transaction here, in it on the API.

/** The advisory lock that stands for the lease `attention-rate`. */
export const ATTENTION_RATE_LOCK = 'scadbuddy:attention-rate'
/** Why a newer summary ended an older one. */
const REPLACED = 'replaced by a newer request for the same reason'

type Resolved = { id: string; turn_id: string; tool: string; tool_use_id: string; created_at: Date }

/** A done summary not yet dismissed: `attention_reason` done with no timer. */
const IS_DONE = `(q.attention_reason IS NOT DISTINCT FROM 'done' AND q.expires_at IS NULL)`

export class SqlQuestions implements QuestionRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async #audited(entries: AuditEntry[]): Promise<void> {
    const audit = new SqlAudit(this.#sql)
    for (const entry of entries) await audit.record(entry)
  }

  async entry(id: string): Promise<QuestionRow | undefined> {
    const [row] = await this.#sql<QuestionRow[]>`
      SELECT session_id, kind, questions, outcome, reason, tool FROM ai_questions WHERE id = ${id}`
    return row
  }

  async pending(principal: Owner, sessionId?: string): Promise<PendingRow[]> {
    const rows = await this.#sql.unsafe<(Omit<PendingRow, 'created_at' | 'expires_at'> & { created_at: Date; expires_at: Date | null })[]>(
      `SELECT q.id, q.session_id, q.kind, q.tool, q.tool_use_id, q.questions, q.attention_reason, q.on_timeout, q.summary,
              q.created_at, q.expires_at
       FROM ai_questions q JOIN ai_sessions s ON s.id = q.session_id
       WHERE s.owner_kind = $1 AND (s.owner_id = $2 OR s.owner_kind = 'browser')
         AND ($3::uuid IS NULL OR q.session_id = $3::uuid) AND q.outcome IS NULL AND ${IS_DONE}
       ORDER BY q.created_at DESC, q.id DESC LIMIT 501`,
      [principal.kind, principal.id, sessionId ?? null],
    )
    return rows.map((r) => ({ ...r, created_at: r.created_at.toISOString(), expires_at: r.expires_at?.toISOString() ?? null }))
  }

  async gateAnswer(requestId: string): Promise<GateAnswer | undefined> {
    const [row] = await this.#sql<GateAnswer[]>`
      SELECT session_id, tool, requested_at, response FROM ai_input_responses
       WHERE request_id = ${requestId} AND kind = 'answer' AND outcome = 'answered'`
    return row
  }

  async answer(call: AnswerCall): Promise<Logged[]> {
    const { id, sessionId, answers, by } = call
    const done = await this.#sql.begin(async (tx) => {
      await tx`SELECT 1 FROM ai_sessions WHERE id = ${sessionId} FOR UPDATE`
      const [asked] = await tx<{ questions: unknown[]; outcome: string | null }[]>`
        SELECT questions, outcome FROM ai_questions WHERE id = ${id} AND session_id = ${sessionId}`
      if (!asked) throw new QuestionRefused('not_found', `no question ${id} in this session`)
      if (asked.outcome !== null) throw new QuestionRefused('conflict', `question ${id} is no longer waiting for an answer`)
      if (answers.length !== asked.questions.length || answers.some((a) => !a.trim())) {
        throw new QuestionRefused('invalid', `question ${id} needs one answer for each of its ${asked.questions.length} questions`)
      }
      const [row] = await tx<Resolved[]>`
        UPDATE ai_questions
        SET outcome = 'answered', answers = ${tx.json([...answers])}, resolved_at = now(),
            answered_by_kind = ${by.kind}, answered_by_id = ${by.id}, answered_by_label = ${by.label}
        WHERE id = ${id} AND session_id = ${sessionId} AND outcome IS NULL
        RETURNING id, turn_id, tool, tool_use_id, created_at`
      if (!row) throw new QuestionRefused('conflict', `question ${id} is no longer waiting for an answer`)
      const events: ServerEvent[] = [
        event({ type: 'question.resolved', sessionId, id, answered: true, answers: [...answers], by }),
        inputResolved(sessionId, `question:${id}`, 'answer', 'answered'),
      ]
      return { row, logged: [{ session_id: sessionId, events, seqs: await appendIn(tx, sessionId, events) }] }
    })
    await this.#audited([
      {
        kind: 'question',
        action: 'answered',
        surface: call.surface,
        actor: by,
        clientIp: call.clientIp,
        sessionId,
        turnId: done.row.turn_id,
        toolUseId: done.row.tool_use_id,
        tier: 'read',
        inputHash: call.inputHash,
        outcome: 'ok',
        detail: `${done.row.tool} question ${id}: ${answers.length} answer${answers.length === 1 ? '' : 's'}`,
        startedAt: done.row.created_at,
        finishedAt: new Date(),
      },
    ])
    return done.logged
  }

  async rateLeased<T>(work: (count: (own: boolean) => Promise<number>) => Promise<T>): Promise<{ value: T } | 'busy'> {
    return await this.#sql.begin(async (tx) => {
      const [lock] = await tx<{ held: boolean }[]>`SELECT pg_try_advisory_xact_lock(hashtextextended(${ATTENTION_RATE_LOCK}, 0)) AS held`
      if (!lock?.held) return 'busy' as const
      const tab = '%:tab:%'
      const count = async (own: boolean) => {
        const [row] = await tx<{ n: number }[]>`
          SELECT
            (SELECT count(*) FROM ai_pending_input
               WHERE tool = ${WAIT_FOR_USER} AND created_at > now() - make_interval(secs => ${ATTENTION_RATE_WINDOW_S})
                 AND (request_id LIKE ${tab}) <> ${own})
            + (SELECT count(*) FROM ai_input_responses
               WHERE tool = ${WAIT_FOR_USER} AND requested_at > now() - make_interval(secs => ${ATTENTION_RATE_WINDOW_S})
                 AND (request_id LIKE ${tab}) <> ${own}) AS n`
        return Number(row?.n ?? 0)
      }
      return { value: await work(count) }
    })
  }

  async doneFacts(sessionId: string, turnId: string, since: string): Promise<DoneFacts> {
    return await this.#sql.begin((tx) => loadDoneFacts(tx, sessionId, turnId, since))
  }

  async done(post: DonePost): Promise<Posted> {
    const { sessionId, turnId, id, questions } = post
    const startedAt = new Date()
    const made = await this.#sql.begin(async (tx) => {
      const none = { asked: false, superseded: [] as Resolved[], limited: null as string | null, logged: [] as Logged[] }
      await tx`SELECT 1 FROM ai_sessions WHERE id = ${sessionId} FOR UPDATE`
      // Still this turn's, and still the user's: after a handoff mid-turn the new owner is not posted to.
      const [owner] = await tx<{ owner_kind: string }[]>`
        SELECT owner_kind FROM ai_sessions WHERE id = ${sessionId} AND durable_turn_id = ${turnId} FOR UPDATE`
      if (owner?.owner_kind !== 'browser') return none
      const [count] = await tx<{ n: number }[]>`
        SELECT count(*)::int AS n FROM ai_questions
        WHERE session_id = ${sessionId} AND turn_id = ${turnId} AND kind = 'attention' AND attention_reason = 'done'`
      if ((count?.n ?? 0) >= DONE_POSTS_PER_TURN) {
        return { ...none, limited: `The summary was not posted: this turn already posted ${DONE_POSTS_PER_TURN}, the most a turn may.` }
      }
      const superseded = await tx<Resolved[]>`
        UPDATE ai_questions SET outcome = 'cancelled', reason = ${REPLACED}, resolved_at = now()
        WHERE session_id = ${sessionId} AND kind = 'attention' AND attention_reason = 'done'
          AND outcome IS NULL AND (NOT unattended OR turn_id = ${turnId}) AND (tool = ${ATTENTION_TOOL}) = ${post.tool === ATTENTION_TOOL}
        RETURNING id, turn_id, tool, tool_use_id, created_at`
      const events: ServerEvent[] = superseded.flatMap((r) => [
        event({ type: 'question.resolved', sessionId, id: r.id, answered: false, reason: REPLACED }),
        inputResolved(sessionId, `question:${r.id}`, 'answer', 'cancelled', REPLACED),
      ])
      const [inserted] = await tx<{ created_at: Date }[]>`
        INSERT INTO ai_questions (id, session_id, turn_id, tool, tool_use_id, questions, kind, attention_reason, summary, unattended)
        VALUES (${id}, ${sessionId}, ${turnId}, ${post.tool}, ${post.toolUseId}, ${tx.json(questions)},
                'attention', 'done', ${post.summary}, ${post.unattended})
        RETURNING created_at`
      const createdAt = inserted?.created_at ?? new Date()
      events.push(
        event({ type: 'question.asked', sessionId, id, tool: post.toolUseId, questions, attention: { reason: 'done' as const, summary: post.summary } }),
        inputRequested(
          sessionId,
          questionEntry({ id, sessionId, tool: post.tool, questions, attentionReason: 'done', onTimeout: null, summary: post.summary, createdAt: createdAt.toISOString(), expiresAt: null }),
        ),
      )
      return { ...none, asked: true, superseded, logged: [{ session_id: sessionId, events, seqs: await appendIn(tx, sessionId, events) }] }
    })
    await this.#audited(
      made.superseded.map((r) => ({
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
    if (made.limited !== null) {
      await this.#audited([
        {
          kind: 'question',
          action: 'rate_limited',
          surface: 'system',
          actor: SYSTEM_ACTOR,
          sessionId,
          turnId,
          toolUseId: post.toolUseId,
          tier: 'read',
          outcome: 'refused',
          detail: safeDetail(`${post.tool} done summary refused: ${made.limited}`),
          startedAt,
          finishedAt: new Date(),
        },
      ])
    }
    return { asked: made.asked, limited: made.limited, logged: made.logged }
  }
}

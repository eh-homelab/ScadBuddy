import { CrudReadError, wellFormed } from '../api/crudRead.js'
import { type CrudTarget, crudOf, CrudLeases, qs } from '../api/crudStores.js'
import type { AttentionReason, OnTimeout } from '../harness/attention.js'
import { isUuid } from '../harness/stateDirs.js'
import type { AuditSurface } from '../audit/log.js'
import type { Owner, QuestionView } from '../sessions/protocol.js'
import { type Logged, REQUEST_ID_MAX } from './crudStore.js'
import type { AwayWindow, DoneTouch } from './doneSummary.js'

// The `done` summaries and the attention limits through the API's CRUD routes (#5299 step 10;
// backend api/crud_agent_questions.py, agent_data/questions.py), area `questions`. QuestionService
// (gate/questions.ts) calls these once the API lists the area, and its own SQL until then (D9).
//
// The summary's text stays the agent's: `doneFacts` reads what the turn touched, the service
// redacts the turn's secrets and writes the text, and `done` posts it. The post lands only while
// the turn's placement (`attempt`, `turnId`) holds the session, checked in the post's transaction
// on the API, with its events and audit rows. The attention count is read only under the lease
// `attention-rate`, which stands for the SQL path's advisory lock: the API answers `lease-lost`,
// never a count, to a caller that does not hold it.
//
// Ids travel in the body or the query string, never the path. An id no row can hold is not sent.

/** The lease the attention count is read under (backend `ATTENTION_LEASE`). */
export const ATTENTION_LEASE = 'attention-rate'
/** How long the lease is taken for: past one holder's open, and freed by its release before that. */
const ATTENTION_LEASE_TTL_S = 60

/** A summary row by id, as the respond route checks a response against it. */
export type QuestionRow = {
  session_id: string
  kind: 'question' | 'attention'
  questions: QuestionView[]
  outcome: 'answered' | 'cancelled' | 'timed_out' | 'reconnected' | null
  reason: string | null
  tool: string
}

/** An undismissed `done` summary, as the API lists it. */
export type PendingRow = {
  id: string
  session_id: string
  kind: 'question' | 'attention'
  tool: string
  tool_use_id: string
  questions: QuestionView[]
  attention_reason: AttentionReason | null
  on_timeout: OnTimeout | null
  summary: string | null
  created_at: string
  expires_at: string | null
}

/** An AgentSession's answered entry, for its `question` audit row. */
export type GateAnswer = { session_id: string; tool: string | null; requested_at: Date | null; response: { answers?: unknown } | null }

/** A summary's dismissal. */
export type AnswerCall = {
  id: string
  sessionId: string
  answers: readonly string[]
  by: Owner
  inputHash: string | undefined
  surface: AuditSurface
  clientIp: string | undefined
}

/** A refused dismissal: the API's 409 `reason`. */
export class QuestionRefused extends Error {
  override name = 'QuestionRefused'
  readonly code: 'not_found' | 'conflict' | 'invalid'
  constructor(code: 'not_found' | 'conflict' | 'invalid', message: string) {
    super(message)
    this.code = code
  }
}

/** A turn's `done` summary, its card redacted and its text written here. */
export type DonePost = {
  id: string
  sessionId: string
  turnId: string
  attempt: string
  tool: string
  toolUseId: string
  questions: QuestionView[]
  summary: string
  unattended: boolean
}

/** What the post did: `limited` is the refusal the model reads when the turn posted its most. */
export type Posted = { asked: boolean; limited: string | null; logged: Logged[] }

/** The facts a `done` summary is written from (gate/doneSummary.ts `loadDoneSummary`). */
export type DoneFacts = { windows: AwayWindow[]; touches: DoneTouch[] }

/** QuestionService's routes. */
export interface QuestionRoutes {
  entry(id: string): Promise<QuestionRow | undefined>
  pending(principal: Owner): Promise<PendingRow[]>
  gateAnswer(requestId: string): Promise<GateAnswer | undefined>
  answer(call: AnswerCall): Promise<Logged[]>
  /**
   * Runs `work` holding the lease `attention-rate`, with the count it may read. `busy`: another
   * holder keeps it, and `work` did not run. The count throws once the lease is lost.
   */
  rateLeased<T>(work: (count: (own: boolean) => Promise<number>) => Promise<T>): Promise<{ value: T } | 'busy'>
  doneFacts(sessionId: string, turnId: string, since: string): Promise<DoneFacts>
  done(post: DonePost): Promise<Posted>
}

type WireTouch = {
  at: string
  in_turn: boolean
  in_away: boolean
  before_asked: boolean
  tool: string
  resource_type: string
  resource_id: string | null
  action: string
  model_slug: string | null
}

const dateOf = (at: string | null): Date | null => (at === null ? null : new Date(at))

/** QuestionRoutes over the API's CRUD routes, area `questions`. */
export class CrudQuestions implements QuestionRoutes {
  readonly #call: ReturnType<typeof crudOf>
  readonly #leases: CrudLeases

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
    this.#leases = new CrudLeases(target)
  }

  async entry(id: string): Promise<QuestionRow | undefined> {
    if (!isUuid(id)) return undefined
    const { entry } = await this.#call<{ entry: QuestionRow | null }>('a question', `/questions/entry${qs({ id })}`)
    return entry ?? undefined
  }

  async pending(principal: Owner): Promise<PendingRow[]> {
    const { questions } = await this.#call<{ questions: PendingRow[] }>('the pending summaries', '/questions/pending', {
      body: wellFormed({ kind: principal.kind, id: principal.id }),
    })
    return questions
  }

  async gateAnswer(requestId: string): Promise<GateAnswer | undefined> {
    if (requestId.length === 0 || requestId.length > REQUEST_ID_MAX) return undefined
    const { answer } = await this.#call<{ answer: (Omit<GateAnswer, 'requested_at'> & { requested_at: string | null }) | null }>(
      "an entry's answer",
      `/questions/gate-answer${qs({ request_id: requestId })}`,
    )
    return answer ? { ...answer, requested_at: dateOf(answer.requested_at) } : undefined
  }

  async answer(call: AnswerCall): Promise<Logged[]> {
    const body = {
      id: call.id,
      session_id: call.sessionId,
      answers: call.answers,
      by: { kind: call.by.kind, id: call.by.id, label: call.by.label },
      input_hash: call.inputHash ?? null,
      surface: call.surface,
      client_ip: call.clientIp ?? null,
    }
    try {
      const { logged } = await this.#call<{ logged: Logged[] }>('a summary dismissed', '/questions/answer', { body: wellFormed(body) })
      return logged
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 409) {
        const code = err.reason === 'conflict' || err.reason === 'invalid' ? err.reason : 'not_found'
        throw new QuestionRefused(code, err.detail || `question ${call.id} was not answered`)
      }
      throw err
    }
  }

  async rateLeased<T>(work: (count: (own: boolean) => Promise<number>) => Promise<T>): Promise<{ value: T } | 'busy'> {
    const lease = await this.#leases.acquire(ATTENTION_LEASE, { ttlS: ATTENTION_LEASE_TTL_S })
    if (lease === 'busy') return 'busy'
    const count = async (own: boolean): Promise<number> => {
      const { n } = await this.#call<{ n: number }>('the attention count', '/questions/attention-count', { body: { own, lease_token: lease.token } })
      return n
    }
    try {
      return { value: await work(count) }
    } finally {
      await this.#leases.release(lease).catch((err: unknown) => console.error(`lease ${ATTENTION_LEASE}: the release failed: ${err instanceof Error ? err.message : String(err)}`))
    }
  }

  async doneFacts(sessionId: string, turnId: string, since: string): Promise<DoneFacts> {
    if (!isUuid(sessionId) || !isUuid(turnId)) return { windows: [], touches: [] }
    const facts = await this.#call<{ windows: { id: string; away_from: string; away_until: string | null }[]; touches: WireTouch[] }>(
      "a turn's done summary facts",
      '/questions/done-facts',
      { body: wellFormed({ session_id: sessionId, turn_id: turnId, since }) },
    )
    // Before `since` the touches are the parked turns', and only their unattended ones carry over (loadDoneSummary).
    return {
      windows: facts.windows.map((w) => ({ requestId: w.id, from: new Date(w.away_from), until: dateOf(w.away_until) })),
      touches: facts.touches
        .filter((t) => t.in_turn || t.in_away)
        .map((t) => ({
          at: new Date(t.at),
          tool: t.tool,
          resourceType: t.resource_type,
          resourceId: t.resource_id,
          action: t.action,
          model: t.model_slug,
          unattended: t.in_away,
          beforeAsked: t.before_asked,
        })),
    }
  }

  async done(post: DonePost): Promise<Posted> {
    const none: Posted = { asked: false, limited: null, logged: [] }
    if (!isUuid(post.sessionId) || !isUuid(post.turnId) || !isUuid(post.id)) return none
    const body = {
      id: post.id,
      session_id: post.sessionId,
      turn_id: post.turnId,
      attempt: post.attempt,
      tool: post.tool,
      tool_use_id: post.toolUseId,
      questions: post.questions,
      summary: post.summary,
      unattended: post.unattended,
    }
    return await this.#call<Posted>("a turn's done summary", '/questions/done', { body: wellFormed(body) })
  }
}

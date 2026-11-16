import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { appendBatches } from './crudTranscripts.js'
import { type Committed, type EventRows, ForkParentGone, type LoggedEvent, type TurnEvents, type TurnFence } from './eventLog.js'
import type { ServerEvent } from './protocol.js'
import { sessionPath } from './rows.js'

// A session's event log over the API's CRUD routes (#5299 step 9, part b; backend
// api/crud_agent_events.py, area `session-events`): `PgEventRows` method for method, each one
// route and one transaction. An event travels as its JSON text, as it is stored; one read
// through the fork chain comes back as logged, and is addressed to the session here, as
// `chainRows` does. A session id goes into a path only as `sessionPath` makes it.

/** A log read's longest wait: the 1 s follower polls read here (D4). */
const READ_TIMEOUT_MS = 30_000
const APPEND_TIMEOUT_MS = 60_000

const gone = (error: unknown, reason: string): boolean =>
  error instanceof CrudReadError && error.fromRoute && error.status === 404 && error.reason === reason

type Logged = { seq: number; event: string; inherited: boolean }

export class CrudEventRows implements EventRows {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  #call<T>(step: string, path: string, init: { body?: unknown; timeoutMs?: number } = {}): Promise<T> {
    return crudRead<T>(this.#fetch, this.#base, step, path, {
      // Well-formed: the API refuses a lone surrogate, which postgres.js wrote as U+FFFD (crudRead.ts).
      ...(init.body === undefined ? {} : { method: 'POST' as const, body: wellFormed(init.body) }),
      timeoutMs: init.timeoutMs ?? READ_TIMEOUT_MS,
    })
  }

  /** The session's path, or a throw: a write names a session by its UUID. */
  #path(sessionId: string, tail: string): string {
    const path = sessionPath(sessionId)
    if (path === undefined) throw new Error(`session ${JSON.stringify(sessionId)} is not a session id`)
    return `${path}${tail}`
  }

  /**
   * One request per batch (`appendBatches`), each its own transaction under the fence: a batch past
   * the body's size is written as a prefix when the fence turns between two, as a placement cut off
   * between two SDK messages would leave it. Stops at the first refused (undefined).
   */
  async append(sessionId: string, events: readonly ServerEvent[], fence?: TurnFence, committed?: Committed): Promise<number[] | undefined> {
    const path = this.#path(sessionId, '/events')
    const seqs: number[] = []
    let at = 0
    for (const batch of appendBatches(events.map((e) => JSON.stringify(e)))) {
      let written: { seqs: number[] | null }
      try {
        written = await this.#call(`the event log of session ${sessionId}`, path, {
          body: { events: batch, ...(fence ? { fence: { attempt: fence.attempt, turn_id: fence.turnId } } : {}) },
          timeoutMs: APPEND_TIMEOUT_MS,
        })
      } catch (error) {
        // As appendEventsIn over SQL.
        if (gone(error, 'session_gone')) throw new Error(`session ${sessionId} does not exist`, { cause: error })
        throw error
      }
      if (written.seqs === null) return undefined
      seqs.push(...written.seqs)
      // This batch's transaction has committed, whatever a later one does.
      if (written.seqs.length > 0) committed?.(events.slice(at, at + batch.length), written.seqs)
      at += batch.length
    }
    return seqs
  }

  async read(sessionId: string, afterSeq: number, limit: number): Promise<LoggedEvent[]> {
    const path = sessionPath(sessionId)
    if (path === undefined) return []
    const query = new URLSearchParams({ after: String(afterSeq), limit: String(limit) })
    const { events } = await this.#call<{ events: Logged[] }>(`the event log of session ${sessionId}`, `${path}/events?${query.toString()}`)
    return events.map((r) => {
      const event = JSON.parse(r.event) as ServerEvent
      return { seq: r.seq, event: r.inherited ? { ...event, sessionId } : event }
    })
  }

  async userTurnText(sessionId: string, turnId: string): Promise<string | undefined> {
    const path = sessionPath(sessionId)
    if (path === undefined) return undefined
    const query = new URLSearchParams({ turn_id: turnId })
    const { text } = await this.#call<{ text: string | null }>(`turn ${turnId} of session ${sessionId}`, `${path}/events/user-turn?${query.toString()}`)
    return text ?? undefined
  }

  async lastSeq(sessionId: string): Promise<number> {
    const path = sessionPath(sessionId)
    if (path === undefined) return 0
    return (await this.#call<{ seq: number }>(`the event log of session ${sessionId}`, `${path}/events/last-seq`)).seq
  }

  async turnEvents(sessionId: string, turnId: string, after: string | undefined): Promise<TurnEvents> {
    const path = sessionPath(sessionId)
    if (path === undefined) return { here: false, after: undefined, events: [] }
    const query = new URLSearchParams({ turn_id: turnId, ...(after === undefined ? {} : { after }) })
    const answer = await this.#call<{ here: boolean; after: number | null; events: string[] }>(
      `turn ${turnId} of session ${sessionId}`,
      `${path}/events/turn?${query.toString()}`,
    )
    return {
      here: answer.here,
      after: answer.after === null ? undefined : String(answer.after),
      events: answer.events.map((text) => JSON.parse(text) as ServerEvent),
    }
  }

  async forkLog(childId: string, parentId: string, cut: number, started: readonly ServerEvent[], then: readonly ServerEvent[]): Promise<number[]> {
    const path = this.#path(childId, '/fork-log')
    try {
      const { seqs } = await this.#call<{ seqs: number[] }>(`the log of fork ${childId}`, path, {
        body: { parent_id: parentId.toLowerCase(), cut, started: started.map((e) => JSON.stringify(e)), then: then.map((e) => JSON.stringify(e)) },
        timeoutMs: APPEND_TIMEOUT_MS,
      })
      return seqs
    } catch (error) {
      if (gone(error, 'parent_gone')) throw new ForkParentGone(`session ${parentId} was forgotten while it was forked`, { cause: error })
      throw error
    }
  }
}

import { crudRead } from '../api/crudAreas.js'

// The backend's event log (`events`), as the LISTEN gap replay reads it (pgListener.ts): its
// last `seq` with the database clock, and the rows after a `seq`, newest first. The backend
// answers over its CRUD routes of area `events` (backend api/crud_agent.py, #5299 D7) with the
// queries the listener ran over SQL; the listener keeps that SQL for an API pod older than the
// routes, until `GET …/crud/agent/areas` lists `events` (api/crudAreas.ts).

/** max(seq) of the log and the database clock when it was read. */
export type EventLogHead = { seq: bigint; at: Date }

/** A logged event; `read_at` is the database clock when the rows were read. */
export type LoggedRow = { seq: string; event_id: string; payload: unknown; logged_at: Date; read_at: Date }

export interface EventLogReads {
  head(): Promise<EventLogHead>
  /** The rows with `seq` above `after`, newest first, `limit` at most. */
  gap(after: bigint, limit: number): Promise<LoggedRow[]>
}

export class CrudEventLogReads implements EventLogReads {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async head(): Promise<EventLogHead> {
    const head = await crudRead<{ seq: string; at: string }>(this.#fetch, this.#base, "the event log's head", '/events/head')
    return { seq: BigInt(head.seq), at: new Date(head.at) }
  }

  async gap(after: bigint, limit: number): Promise<LoggedRow[]> {
    const query = new URLSearchParams({ after: after.toString(), limit: String(limit) })
    const answer = await crudRead<{
      events: { seq: string; event_id: string; payload: unknown; logged_at: string }[]
      read_at: string
    }>(this.#fetch, this.#base, "the event log's gap", `/events?${query}`)
    const readAt = new Date(answer.read_at)
    return answer.events.map((e) => ({ seq: e.seq, event_id: e.event_id, payload: e.payload, logged_at: new Date(e.logged_at), read_at: readAt }))
  }
}

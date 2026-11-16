import type { Sql } from 'postgres'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { sessionPath } from './rows.js'

// Two reads of a session's own event log (never its fork chain) that no store owned (#5299,
// #6501): the calls with a `tool.call` and no `tool.result` (tools/activityProjection.ts, a
// restarted turn's finished calls and a forget's terminations) and the text a child's model
// wrote before a call (flows/queue.ts `childReasoning`). Over SQL until the API lists area
// `session-reads`, then `…/agent/sessions/{id}/open-calls` and `…/reasoning` (backend
// api/crud_agent_publish.py, agent_data/events.py: the same statements).

export type OpenCall = { id: string; name: string }
export type Delta = { message: string | null; delta: string | null }

/** The most characters of a tool_use id the API takes (backend `TOOL_USE_ID_MAX`): a longer one names no logged call. */
export const TOOL_USE_ID_MAX = 500
/** The longest turn id the API takes (backend api/crud_agent_events.py `TURN_ID_MAX`). */
export const TURN_ID_MAX = 500
/** The widest window one reasoning read scans (backend `REASONING_SCAN_MAX`). */
export const SCAN_MAX = 10_000

export interface SessionReads {
  /** The open calls, oldest first; with `turnId`, those after that turn's last `user.turn` (all of them when it is not logged). */
  openCalls(sessionId: string, turnId?: string): Promise<OpenCall[]>
  /**
   * The `assistant.text.delta` events since the last `user.turn` before the call's `tool.call`, up
   * to it, oldest first, within the log's last `scan` events; undefined when that `tool.call` is
   * not among them.
   */
  reasoningDeltas(sessionId: string, toolUseId: string, scan: number): Promise<Delta[] | undefined>
}

export class PgSessionReads implements SessionReads {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async openCalls(sessionId: string, turnId?: string): Promise<OpenCall[]> {
    let after = 0
    if (turnId !== undefined) {
      const [start] = await this.#sql<{ seq: string }[]>`
        SELECT seq FROM ai_session_events
         WHERE session_id = ${sessionId} AND event::jsonb->>'type' = 'user.turn' AND event::jsonb->>'turnId' = ${turnId}
         ORDER BY seq DESC LIMIT 1`
      after = start ? Number(start.seq) : 0
    }
    const rows = await this.#sql<{ type: string; id: string; name: string | null }[]>`
      SELECT event::jsonb->>'type' AS type, event::jsonb->>'id' AS id, event::jsonb->>'name' AS name
        FROM ai_session_events
       WHERE session_id = ${sessionId} AND seq > ${after} AND event::jsonb->>'type' IN ('tool.call', 'tool.result')
       ORDER BY seq`
    const open = new Map<string, string>()
    for (const row of rows) {
      if (row.type === 'tool.call') open.set(row.id, row.name ?? '')
      else open.delete(row.id)
    }
    return [...open].map(([id, name]) => ({ id, name }))
  }

  async reasoningDeltas(sessionId: string, toolUseId: string, scan: number): Promise<Delta[] | undefined> {
    const sql = this.#sql
    // The window's floor, from the log's last seq (a backward step on its primary key), and
    // the call's `tool.call` above it, in one query: a call not logged yet costs one read a
    // tick, not three (#2842).
    const [found] = await sql<{ floor: string; seq: string | null }[]>`
      WITH top AS (
        SELECT coalesce(max(seq), 0) - ${scan} AS floor FROM ai_session_events WHERE session_id = ${sessionId}::uuid)
      SELECT top.floor, (
        SELECT seq FROM ai_session_events
         WHERE session_id = ${sessionId}::uuid AND seq > top.floor AND strpos(event, ${toolUseId}) > 0
           AND event::jsonb ->> 'type' = 'tool.call' AND event::jsonb ->> 'id' = ${toolUseId}
         ORDER BY seq DESC LIMIT 1) AS seq
        FROM top`
    if (!found?.seq) return undefined
    const floor = found.floor
    const call = { seq: found.seq }
    const [start] = await sql<{ seq: string }[]>`
      SELECT seq FROM ai_session_events
       WHERE session_id = ${sessionId}::uuid AND seq > ${floor} AND seq < ${call.seq}
         AND strpos(event, '"user.turn"') > 0 AND event::jsonb ->> 'type' = 'user.turn'
       ORDER BY seq DESC LIMIT 1`
    return await sql<Delta[]>`
      SELECT event::jsonb ->> 'messageId' AS message, event::jsonb ->> 'delta' AS delta
        FROM ai_session_events
       WHERE session_id = ${sessionId}::uuid AND seq < ${call.seq} AND seq > ${start?.seq ?? floor}
         AND strpos(event, '"assistant.text.delta"') > 0 AND event::jsonb ->> 'type' = 'assistant.text.delta'
       ORDER BY seq`
  }
}

/** `PgSessionReads` over the API's CRUD routes (area `session-reads`). */
export class CrudSessionReads implements SessionReads {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  /** The path of session `id`'s read `tail`, or a throw, as the SQL's uuid cast threw for an id that is no UUID. */
  #path(id: string, tail: string): string {
    const path = sessionPath(id)
    if (path === undefined) throw new Error(`session ${JSON.stringify(id)} is not a session id`)
    return `${path}/${tail}`
  }

  async openCalls(sessionId: string, turnId?: string): Promise<OpenCall[]> {
    // A turn id past the API's bound (`TURN_ID_MAX`) is no logged turn: every open call, as the SQL read.
    const query = turnId === undefined || turnId.length > TURN_ID_MAX ? '' : `?${new URLSearchParams({ turn_id: turnId }).toString()}`
    const { calls } = await this.#call<{ calls: OpenCall[] }>(`session ${sessionId}'s open calls`, this.#path(sessionId, `open-calls${query}`))
    return calls
  }

  async reasoningDeltas(sessionId: string, toolUseId: string, scan: number): Promise<Delta[] | undefined> {
    const path = this.#path(sessionId, 'reasoning')
    if (toolUseId.length > TOOL_USE_ID_MAX) return undefined
    const query = new URLSearchParams({ tool_use_id: toolUseId, scan: String(Math.min(Math.max(Math.trunc(scan), 0), SCAN_MAX)) })
    const answer = await this.#call<{ logged: boolean; deltas: Delta[] }>(`session ${sessionId}'s reasoning`, `${path}?${query.toString()}`)
    return answer.logged ? answer.deltas : undefined
  }
}

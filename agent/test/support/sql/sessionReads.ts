import type { Sql } from 'postgres'
import type { Delta, OpenCall, SessionReads } from '../../../src/sessions/sessionReads.js'

// `SessionReads` straight on the test database, the statements the API's routes run (#5299).

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

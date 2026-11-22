import type { Sql } from 'postgres'
import { MCP_CHANNEL, type RelayRows } from '../../../src/mcp/relayRows.js'

// Test-only: the /mcp relay's rows over SQL, for the tests that run two replicas on one
// throwaway database. The agent itself goes through the API (src/mcp/relayRows.ts CrudRelayRows).

/** Relay rows older than this are nobody's any more. */
const STALE_MESSAGES = '10 minutes'

export class PgRelayRows implements RelayRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async register(hash: string, replica: string, callerKey: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_mcp_sessions (id_hash, replica, caller_key)
      VALUES (${hash}, ${replica}, ${callerKey})
      ON CONFLICT (id_hash) DO UPDATE SET replica = EXCLUDED.replica, caller_key = EXCLUDED.caller_key, last_seen = now()`
  }

  async touch(hash: string): Promise<void> {
    await this.#sql`UPDATE ai_mcp_sessions SET last_seen = now() WHERE id_hash = ${hash}`
  }

  async remove(hash: string, replica?: string): Promise<boolean> {
    const removed =
      replica === undefined
        ? await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} RETURNING id_hash`
        : await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} AND replica = ${replica} RETURNING id_hash`
    return removed.length > 0
  }

  async owner(hash: string): Promise<string | undefined> {
    const [found] = await this.#sql<{ replica: string }[]>`SELECT replica FROM ai_mcp_sessions WHERE id_hash = ${hash}`
    return found?.replica
  }

  async counts(callerKey: string, idleSeconds: number): Promise<{ total: number; caller: number }> {
    const [row] = await this.#sql<{ total: number; caller: number }[]>`
      SELECT count(*)::int AS total, (count(*) FILTER (WHERE caller_key = ${callerKey}))::int AS caller
      FROM ai_mcp_sessions WHERE last_seen > now() - make_interval(secs => ${idleSeconds})`
    return row ?? { total: 0, caller: 0 }
  }

  async send(header: object, body?: { row: string; value: unknown }): Promise<void> {
    await this.#sql.begin(async (tx) => {
      if (body) await tx`INSERT INTO ai_mcp_relay_messages (id, body) VALUES (${body.row}, ${tx.json(body.value as never)})`
      await tx`SELECT pg_notify(${MCP_CHANNEL}, ${JSON.stringify(header)})`
    })
  }

  async take(row: string): Promise<unknown> {
    const [taken] = await this.#sql<{ body: unknown }[]>`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING body`
    return taken?.body
  }

  async withdraw(row: string): Promise<boolean> {
    return (await this.#sql`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING id`).length > 0
  }

  async sweep(idleSeconds: number): Promise<void> {
    await Promise.all([
      this.#sql`DELETE FROM ai_mcp_relay_messages WHERE created_at < now() - ${STALE_MESSAGES}::interval`,
      this.#sql`DELETE FROM ai_mcp_sessions WHERE last_seen < now() - make_interval(secs => ${idleSeconds})`,
    ])
  }
}

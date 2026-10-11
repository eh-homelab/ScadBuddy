import type { Sql } from 'postgres'

// Which tab each chat session pairs with, shared by every agent replica (#2086,
// db/migrations/20261010T0610Z_session_tabs.sql). The hub keeps its own copy
// for the calls made where the pairing happened; this is for the ones that run
// elsewhere: a durable session's tool calls are activities on `agent-tools`,
// and any replica's worker may take one.

export interface SessionTabStore {
  set(sessionId: string, tabId: string): Promise<void>
  /** `signal` stops the read with the call it is for. */
  get(sessionId: string, signal?: AbortSignal): Promise<string | undefined>
}

export class PostgresSessionTabStore implements SessionTabStore {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async set(sessionId: string, tabId: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (${sessionId}, ${tabId})
      ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id, paired_at = now()`
  }

  async get(sessionId: string, signal?: AbortSignal): Promise<string | undefined> {
    const query = this.#sql<{ tab_id: string }[]>`SELECT tab_id FROM ai_session_tabs WHERE session_id = ${sessionId}`
    const onAbort = () => query.cancel()
    signal?.addEventListener('abort', onAbort, { once: true })
    try {
      const [row] = await query
      return row?.tab_id
    } finally {
      signal?.removeEventListener('abort', onAbort)
    }
  }
}

import type { Sql } from 'postgres'

// Which tab each chat session pairs with, shared by every agent replica (#2086,
// db/migrations/20261010T0610Z_session_tabs.sql). The hub keeps its own copy
// for the calls made where the pairing happened; this is for the ones that run
// elsewhere: a durable session's tool calls are activities on `agent-tools`,
// and any replica's worker may take one.

export interface SessionTabStore {
  set(sessionId: string, tabId: string): Promise<void>
  /**
   * `set`, only while the session's row still names `seen` or there is no row, in one
   * statement: a pairing made between a read and this write is never lost (#2271).
   * No row matches any `seen`: every stored pairing is a row, so there is none to
   * lose, and `seen` may be this replica's own map when the store's read failed
   * (#3478 review). Whether it wrote. Optional: without it the hub compares, then writes.
   */
  setIf?(sessionId: string, tabId: string, seen: string | undefined): Promise<boolean>
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

  async setIf(sessionId: string, tabId: string, seen: string | undefined): Promise<boolean> {
    // An insert, or an update of the row only while it names `seen`: on a row naming
    // another tab the conflict updates nothing and returns no row.
    const rows =
      seen === undefined
        ? await this.#sql`
            INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (${sessionId}, ${tabId})
            ON CONFLICT (session_id) DO NOTHING RETURNING 1`
        : await this.#sql`
            INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (${sessionId}, ${tabId})
            ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id, paired_at = now()
            WHERE ai_session_tabs.tab_id = ${seen} RETURNING 1`
    return rows.length > 0
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

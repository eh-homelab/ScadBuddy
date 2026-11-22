import type { Sql } from 'postgres'
import type { SessionTabStore } from '../../../src/bridge/sessionTabs.js'
import type { TabOwner, TabOwnerStore } from '../../../src/bridge/tabOwners.js'

// Test-only: the bridge's shared rows over SQL, for the tests that run whole replicas on a
// throwaway database. The agent itself goes through the API (CrudSessionTabStore,
// CrudTabOwnerStore).

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

export class PostgresTabOwnerStore implements TabOwnerStore {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async claim(tabId: string, queue: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_tab_owners (tab_id, queue) VALUES (${tabId}, ${queue})
      ON CONFLICT (tab_id) DO UPDATE SET queue = EXCLUDED.queue, claimed_at = now()`
  }

  async refresh(tabIds: string[], queue: string): Promise<void> {
    if (tabIds.length === 0) return
    await this.#sql`
      INSERT INTO ai_tab_owners (tab_id, queue)
      SELECT tab_id, ${queue} FROM unnest(${tabIds}::text[]) AS t (tab_id)
      ON CONFLICT (tab_id) DO UPDATE SET claimed_at = now() WHERE ai_tab_owners.queue = EXCLUDED.queue`
  }

  async release(tabId: string, queue: string, claimedBefore?: string): Promise<void> {
    await (claimedBefore
      ? this.#sql`DELETE FROM ai_tab_owners WHERE tab_id = ${tabId} AND queue = ${queue} AND claimed_at <= ${claimedBefore}::text::timestamptz`
      : this.#sql`DELETE FROM ai_tab_owners WHERE tab_id = ${tabId} AND queue = ${queue}`)
  }

  async releaseAll(queue: string): Promise<void> {
    await this.#sql`DELETE FROM ai_tab_owners WHERE queue = ${queue}`
  }

  async ownerOf(tabId: string, signal?: AbortSignal): Promise<TabOwner | undefined> {
    signal?.throwIfAborted()
    const query = this.#sql<{ queue: string; claimed_at: string }[]>`
      SELECT queue, claimed_at::text AS claimed_at FROM ai_tab_owners WHERE tab_id = ${tabId}`
    const onAbort = () => query.cancel()
    signal?.addEventListener('abort', onAbort, { once: true })
    try {
      const [row] = await query
      return row ? { queue: row.queue, claimedAt: row.claimed_at } : undefined
    } finally {
      signal?.removeEventListener('abort', onAbort)
    }
  }
}

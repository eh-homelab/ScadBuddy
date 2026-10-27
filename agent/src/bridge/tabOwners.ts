import type { Sql } from 'postgres'

// Which replica holds each tab's bridge socket (#4780,
// db/migrations/20261027T0103Z_tab_owners.sql), named by that replica's own queue
// (`agent-tools.<identity>`, main.ts HOST_QUEUE). A browser_* call made on another
// replica runs as an activity on that queue (bridge/relay.ts).

export interface TabOwnerStore {
  /** `queue` holds `tabId` now: the latest claim wins (a reconnect to another replica). */
  claim(tabId: string, queue: string): Promise<void>
  /** `queue` no longer holds `tabId`; nothing changes when another queue claimed it since. */
  release(tabId: string, queue: string): Promise<void>
  /** Every tab `queue` claimed: a replica's own rows at its start, left by the process before it. */
  releaseAll(queue: string): Promise<void>
  /** The queue of the replica holding `tabId`; `signal` stops the read with the call it is for. */
  ownerOf(tabId: string, signal?: AbortSignal): Promise<string | undefined>
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

  async release(tabId: string, queue: string): Promise<void> {
    await this.#sql`DELETE FROM ai_tab_owners WHERE tab_id = ${tabId} AND queue = ${queue}`
  }

  async releaseAll(queue: string): Promise<void> {
    await this.#sql`DELETE FROM ai_tab_owners WHERE queue = ${queue}`
  }

  async ownerOf(tabId: string, signal?: AbortSignal): Promise<string | undefined> {
    signal?.throwIfAborted()
    const query = this.#sql<{ queue: string }[]>`SELECT queue FROM ai_tab_owners WHERE tab_id = ${tabId}`
    const onAbort = () => query.cancel()
    signal?.addEventListener('abort', onAbort, { once: true })
    try {
      const [row] = await query
      return row?.queue
    } finally {
      signal?.removeEventListener('abort', onAbort)
    }
  }
}

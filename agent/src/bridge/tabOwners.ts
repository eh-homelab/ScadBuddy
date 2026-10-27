import type { Sql } from 'postgres'

// Which replica holds each tab's bridge socket (#4780,
// db/migrations/20261027T0103Z_tab_owners.sql), named by that replica's own queue
// (`agent-tools.<identity>`, main.ts HOST_QUEUE). A browser_* call made on another
// replica runs as an activity on that queue (bridge/relay.ts).

/**
 * A tab's owner as read: its queue, and when the row was last claimed or refreshed, as
 * Postgres's own text (microseconds, which a JS Date would cut to milliseconds).
 */
export type TabOwner = { queue: string; claimedAt: string }

export interface TabOwnerStore {
  /** `queue` holds `tabId` now: the latest claim wins (a reconnect to another replica). */
  claim(tabId: string, queue: string): Promise<void>
  /**
   * `queue` still holds `tabIds`: each row naming `queue` is refreshed, a missing one is made
   * again (a claim that was lost, or a row a missed call dropped). A row naming another queue
   * is left alone, so a reconnect there keeps its claim.
   */
  refresh(tabIds: string[], queue: string): Promise<void>
  /**
   * `queue` no longer holds `tabId`; nothing changes when another queue claimed it since. With
   * `claimedBefore`, only a row not claimed or refreshed after that moment goes.
   */
  release(tabId: string, queue: string, claimedBefore?: string): Promise<void>
  /** Every tab `queue` claimed: a replica's own rows at its start, left by the process before it. */
  releaseAll(queue: string): Promise<void>
  /** The replica holding `tabId`; `signal` stops the read with the call it is for. */
  ownerOf(tabId: string, signal?: AbortSignal): Promise<TabOwner | undefined>
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

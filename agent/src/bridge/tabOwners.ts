import { chunks, type CrudTarget, crudOf, qs, TABS_PER_CALL } from '../api/crudStores.js'

// Which replica holds each tab's bridge socket (#4780,
// backend/scadbuddy/migrations_agent/20261027T0103Z_tab_owners.sql), named by that replica's own queue
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

/** The owners through the API's CRUD routes, area `bridge-tabs` (backend api/crud_agent_stores.py). */
export class CrudTabOwnerStore implements TabOwnerStore {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async claim(tabId: string, queue: string): Promise<void> {
    await this.#call('a tab claimed', '/tab-owners/claim', { body: { tab_id: tabId, queue } })
  }

  async refresh(tabIds: string[], queue: string): Promise<void> {
    // In runs the route takes: a replica may hold more tabs than one call names.
    for (const run of chunks(tabIds, TABS_PER_CALL)) {
      await this.#call("a replica's tabs refreshed", '/tab-owners/refresh', { body: { tab_ids: run, queue } })
    }
  }

  async release(tabId: string, queue: string, claimedBefore?: string): Promise<void> {
    const body = { tab_id: tabId, queue, ...(claimedBefore ? { claimed_before: claimedBefore } : {}) }
    await this.#call('a tab released', '/tab-owners/release', { body })
  }

  async releaseAll(queue: string): Promise<void> {
    await this.#call("a replica's tabs released", '/tab-owners/release-all', { body: { queue } })
  }

  async ownerOf(tabId: string, signal?: AbortSignal): Promise<TabOwner | undefined> {
    const { owner } = await this.#call<{ owner: { queue: string; claimed_at: string } | null }>(
      "a tab's owner",
      `/tab-owners${qs({ tab_id: tabId })}`,
      { signal },
    )
    return owner ? { queue: owner.queue, claimedAt: owner.claimed_at } : undefined
  }
}

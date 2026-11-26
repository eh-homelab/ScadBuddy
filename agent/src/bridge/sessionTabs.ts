import { type CrudTarget, crudOf, qs } from '../api/crudStores.js'

// Which tab each chat session pairs with, shared by every agent replica (#2086,
// backend/scadbuddy/migrations_agent/20261010T0610Z_session_tabs.sql). The hub keeps its own copy
// for the calls made where the pairing happened; this is for the ones that run
// elsewhere: a durable session's tool calls are activities on `agent-tools`,
// and any replica's worker may take one.

export interface SessionTabStore {
  set(sessionId: string, tabId: string): Promise<void>
  /**
   * `set`, only while the session's row still names `seen` or there is no row, in one
   * statement: a pairing made between a read and this write is never lost (#2271).
   * With no row it inserts, whatever `seen` is: every stored pairing is a row, so
   * there is none to lose, and `seen` may be this replica's own map when the store's
   * read failed (#3478 review). Whether it wrote. Optional: without it the hub compares, then writes.
   */
  setIf?(sessionId: string, tabId: string, seen: string | undefined): Promise<boolean>
  /** `signal` stops the read with the call it is for. */
  get(sessionId: string, signal?: AbortSignal): Promise<string | undefined>
}

/** The pairings through the API's CRUD routes, area `bridge-tabs` (tools/go/core/internal/stores). */
export class CrudSessionTabStore implements SessionTabStore {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async set(sessionId: string, tabId: string): Promise<void> {
    await this.#call('a session paired with a tab', '/session-tabs', { body: { session_id: sessionId, tab_id: tabId } })
  }

  async setIf(sessionId: string, tabId: string, seen: string | undefined): Promise<boolean> {
    const body = { session_id: sessionId, tab_id: tabId, conditional: true, ...(seen === undefined ? {} : { seen }) }
    return (await this.#call<{ written: boolean }>('a session paired with a tab', '/session-tabs', { body })).written
  }

  async get(sessionId: string, signal?: AbortSignal): Promise<string | undefined> {
    const { tab_id } = await this.#call<{ tab_id: string | null }>("a session's tab", `/session-tabs${qs({ session_id: sessionId })}`, { signal })
    return tab_id ?? undefined
  }
}

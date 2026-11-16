import type { Sql } from 'postgres'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { PG_CHANNEL } from '../events/bus.js'
import { isUuid } from '../harness/stateDirs.js'
import type { SessionBusEvent } from './busEvents.js'
import { notifyWarmEvict } from './warmEvict.js'

// The agent's NOTIFYs outside a store's transaction (#5299, #6501): a `session.*` bus event
// (busEvents.ts `SessionEventPublisher`) and a warm process's eviction sent on its own
// (warmEvict.ts; a store's write sends its eviction in its own transaction, on the API once the
// store's area is listed). Over SQL until the API lists area `publish`, then its routes
// `…/agent/publish/session-event` and `…/publish/warm-evict` (backend
// api/crud_agent_publish.py), which NOTIFY on the same channels, so every listener hears the
// same payloads. Both are best-effort, as a NOTIFY on SQL was: a lost one costs latency only.

export interface Publisher {
  sessionEvent(event: SessionBusEvent): Promise<void>
  /** A session id or `*`; anything else is no session, and nothing is sent. */
  warmEvict(target: string): Promise<void>
}

export class PgPublisher implements Publisher {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async sessionEvent(event: SessionBusEvent): Promise<void> {
    await this.#sql`SELECT pg_notify(${PG_CHANNEL}, ${JSON.stringify(event)})`
  }

  async warmEvict(target: string): Promise<void> {
    await notifyWarmEvict(this.#sql, target)
  }
}

export class CrudPublisher implements Publisher {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async sessionEvent(event: SessionBusEvent): Promise<void> {
    // The API checks each field as its pattern says; a session id that is no UUID names no
    // session any listener follows, so it is not sent.
    if (!isUuid(event.session_id)) return
    await this.#call(`session ${event.session_id}'s bus event`, '/publish/session-event', { method: 'POST', body: event })
  }

  async warmEvict(target: string): Promise<void> {
    // The listener evicts only `*` or a session id (warmEvict.ts `evictOnNotify`).
    if (target !== '*' && !isUuid(target)) return
    await this.#call(`the warm eviction of ${target}`, '/publish/warm-evict', { method: 'POST', body: { target: target.toLowerCase() } })
  }
}

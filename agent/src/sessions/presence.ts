import type { Sql } from 'postgres'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { isUuid } from '../harness/uuid.js'

// Which sessions still have a row (#6501): what the pod's directory sweeps keep
// (temporal/turnServers.ts `sweepTurnDirs`, workspace/directory.ts `SessionDirectory`). Over SQL
// until the API lists area `session-sweeps`, then `POST …/crud/agent/sessions/live`. A failed
// read throws, so a sweep removes nothing it could not check.

/** The most ids one read asks the API about (backend api/crud_agent_sweeps.py `LIVE_IDS_MAX`). */
export const LIVE_IDS_MAX = 1000

export interface SessionPresence {
  /** Those of `ids` that have a session row, each as its lowercase UUID. */
  live(ids: readonly string[]): Promise<Set<string>>
}

export class PgSessionPresence implements SessionPresence {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async live(ids: readonly string[]): Promise<Set<string>> {
    if (ids.length === 0) return new Set()
    const rows = await this.#sql<{ id: string }[]>`SELECT id::text FROM ai_sessions WHERE id = ANY(${ids as string[]}::uuid[])`
    return new Set(rows.map((r) => r.id))
  }
}

/** `PgSessionPresence` over the API's CRUD route (area `session-sweeps`). */
export class CrudSessionPresence implements SessionPresence {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  async live(ids: readonly string[]): Promise<Set<string>> {
    // As the SQL's uuid cast threw for an id that is no UUID.
    const bad = ids.find((id) => !isUuid(id))
    if (bad !== undefined) throw new Error(`${JSON.stringify(bad)} is not a session id`)
    const asked = [...new Set(ids.map((id) => id.toLowerCase()))]
    const live = new Set<string>()
    for (let at = 0; at < asked.length; at += LIVE_IDS_MAX) {
      const page = asked.slice(at, at + LIVE_IDS_MAX)
      const answer = await this.#call<{ ids: string[] }>('the live sessions', '/sessions/live', { method: 'POST', body: { ids: page } })
      for (const id of answer.ids) live.add(id)
    }
    return live
  }
}

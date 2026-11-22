import type { Sql } from 'postgres'
import type { SessionPresence } from '../../../src/sessions/presence.js'

// `SessionPresence` straight on the test database, for tests with no API (#5299).

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

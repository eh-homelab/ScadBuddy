import type { Sql } from 'postgres'
import type { ResourceAction, ResourceRow, ResourceRows, ResourceType, TouchedRecord } from '../../../src/sessions/touched.js'

// `ResourceRows` straight on the test database, for tests with no API (#5299).

type Row = {
  resource_type: ResourceType
  resource_id: string | null
  action: ResourceAction
  model_slug: string | null
  before_id: string | null
  after_id: string | null
  tool: string
  at: Date
}

export class PgResourceRows implements ResourceRows {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async insert(sessionId: string, rows: ResourceRow[]): Promise<void> {
    await this.#sql`INSERT INTO ai_session_resources ${this.#sql(rows.map((r) => ({ session_id: sessionId, ...r })))}`
  }

  async list(sessionId: string): Promise<TouchedRecord[]> {
    const rows = await this.#sql<Row[]>`
      SELECT resource_type, resource_id, action, model_slug, before_id, after_id, tool, at
      FROM ai_session_resources WHERE session_id = ${sessionId}::uuid ORDER BY id`
    return rows.map((r) => ({
      type: r.resource_type,
      id: r.resource_id,
      action: r.action,
      model: r.model_slug,
      before: r.before_id,
      after: r.after_id,
      tool: r.tool,
      at: r.at.toISOString(),
    }))
  }
}

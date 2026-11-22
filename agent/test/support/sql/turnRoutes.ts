import type { Sql } from 'postgres'
import type { HeldResponse, TurnFence, TurnRoutes } from '../../../src/gate/crudPending.js'

// A turn placement's reads and marks of its held calls (sessions/turnHost.ts) straight on the
// test database, for tests with no API (#5299). The fence is the API's check; here it is not read.

export class SqlTurnRoutes implements TurnRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async response(requestId: string): Promise<HeldResponse | undefined> {
    const [row] = await this.#sql<HeldResponse[]>`
      SELECT outcome, response, reason, responder, decision_traceparent FROM ai_input_responses WHERE request_id = ${requestId}`
    return row
  }

  async markPark(requestId: string, traceparent: string, _fence: TurnFence): Promise<boolean> {
    const rows = await this.#sql`UPDATE ai_pending_input SET traceparent = ${traceparent} WHERE request_id = ${requestId}`
    return rows.count > 0
  }
}

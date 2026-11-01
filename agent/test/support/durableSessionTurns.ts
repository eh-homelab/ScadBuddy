import type { Sql } from 'postgres'
import { DurableTurns, type DurableTurn } from '../../src/sessions/durable.js'
import type { SessionRecord, Turn } from '../../src/sessions/manager.js'

/**
 * DurableTurns whose sessions are DurableSessions (agent-durable's Python workflow), as a
 * session an earlier release started is: since plan Step 6.3 no new one is made, and since
 * Step 7.2 the service moves one to AgentSession at its next send (sessions/migrateDurable.ts).
 * These keep going to it (`durableSends`), for the tests of the Python turn that Phase 8
 * retires with agent-durable's. Each send records the Python runtime
 * first, and a new session needs only the DurableSession worker, which is all a test of
 * that path runs.
 */
export class DurableSessionTurns extends DurableTurns {
  readonly #sql: Sql

  constructor(deps: ConstructorParameters<typeof DurableTurns>[0]) {
    super({ ...deps, durableSends: true })
    this.#sql = deps.sql
  }

  override unready(): Promise<string | undefined> {
    return super.unready('durable')
  }

  override async send(session: SessionRecord, turn: DurableTurn): Promise<Turn | undefined> {
    await this.#sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${session.id} AND runtime IS DISTINCT FROM 'durable'`
    return super.send(session, turn)
  }
}

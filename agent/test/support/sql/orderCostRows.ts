import type { Sql } from 'postgres'
import type { OrderCostRows, ReportStart, ReportWrites, Settled } from '../../../src/sessions/orderCosts.js'
import type { LockClient, LockLease } from '../database.js'

// `OrderCostRows` straight on the test database, the report lock an advisory lock on a
// lock-client connection, for tests with no API (#5299).

type Pending = ReportStart['pending'][number]

/** The advisory lock a session's report holds, by `hashtextextended` of this plus the session id. */
const LOCK_PREFIX = 'ai_session_order_costs:'

/** How long a row waits before the sweep reports it: past any turn that could still report it. */
export const SWEEP_AFTER = '15 minutes'

/**
 * How long an idle session has had no activity (`ai_sessions.updated_at`, which a turn's start
 * and end move, as a handoff and other writes of the row do) before its sub-unit share is
 * closed out.
 */
export const IDLE_OVER_AFTER = '1 hour'

const SETTLE: Record<Settled, string> = {
  reported: 'reported_at = now(), reported_usd = COALESCE(attempted_usd, 0), attempted_usd = NULL',
  unsent: 'attempted_usd = NULL',
  closed: 'reported_at = now(), reported_usd = 0, attempted_usd = NULL',
}

export class PgOrderCostRows implements OrderCostRows {
  readonly #sql: Sql
  readonly #locks: LockClient

  /** `sql` is the stores' pool, short queries only; `locks` the lock client's own connections, which hold a report's lock across its posts. */
  constructor(sql: Sql, locks: LockClient) {
    this.#sql = sql
    this.#locks = locks
  }

  async insert(sessionId: string, orderId: string, itemId: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_session_order_costs (session_id, order_id, order_item_id)
      SELECT ${sessionId}::uuid, ${orderId}, ${itemId}
      WHERE NOT EXISTS (SELECT 1 FROM ai_session_order_costs
                        WHERE session_id = ${sessionId}::uuid AND order_id = ${orderId} AND reported_at IS NOT NULL)
      ON CONFLICT DO NOTHING`
  }

  async sweepSessions(limit: number): Promise<string[]> {
    const sessions = await this.#sql<{ session_id: string }[]>`
      SELECT c.session_id FROM ai_session_order_costs c JOIN ai_sessions s ON s.id = c.session_id
      WHERE c.reported_at IS NULL AND c.at < now() - ${SWEEP_AFTER}::interval
        AND s.status NOT IN ('running', 'waiting_approval', 'waiting_input')
      GROUP BY c.session_id ORDER BY max(c.tried_at) NULLS FIRST, min(c.at) LIMIT ${limit}`
    return sessions.map((s) => s.session_id)
  }

  async locked(sessionId: string, fn: (writes: ReportWrites) => Promise<void>): Promise<void> {
    // Unhealthy unless shown otherwise: a connection whose lock state is unknown is never reused.
    let healthy = false
    const lease: LockLease | undefined = await this.#locks.take()
    if (!lease) return
    try {
      const key = `${LOCK_PREFIX}${sessionId}`
      const [lock] = await lease.sql<{ locked: boolean }[]>`SELECT pg_try_advisory_lock(hashtextextended(${key}, 0)) AS locked`
      // A report already running for the session (here or on another replica) sends what
      // this one would; what it leaves waits for the next turn's end or the sweep.
      if (!lock?.locked) {
        healthy = true
        return
      }
      try {
        await fn(this.#writes(sessionId))
      } finally {
        await lease.sql`SELECT pg_advisory_unlock(hashtextextended(${key}, 0))`
        healthy = true
      }
    } finally {
      lease.done(healthy)
    }
  }

  #writes(sessionId: string): ReportWrites {
    const sql = this.#sql
    return {
      begin: async () => {
        const pending = await sql<Pending[]>`
          SELECT order_id, order_item_id, attempted_usd FROM ai_session_order_costs
          WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL ORDER BY at, order_id, order_item_id`
        if (pending.length === 0) return undefined
        await sql`UPDATE ai_session_order_costs SET tried_at = now() WHERE session_id = ${sessionId}::uuid AND reported_at IS NULL`
        const [row] = await sql<{ cost: number; carried: number; principal: string; over: boolean }[]>`
          SELECT s.cost_usd AS cost, s.owner_id AS principal,
                 (s.status IN ('done', 'failed') OR (s.status = 'idle' AND s.updated_at < now() - ${IDLE_OVER_AFTER}::interval)) AS over,
                 (SELECT COALESCE(SUM(COALESCE(reported_usd, 0) + COALESCE(attempted_usd, 0)), 0)
                  FROM ai_session_order_costs WHERE session_id = s.id) AS carried
          FROM ai_sessions s WHERE s.id = ${sessionId}::uuid`
        if (!row) return undefined
        return { pending, cost: Number(row.cost), carried: Number(row.carried), principal: row.principal, over: row.over }
      },
      attempt: async (orderId, items, amountUsd) => {
        await sql`
          UPDATE ai_session_order_costs SET attempted_usd = ${amountUsd / items.length}
          WHERE session_id = ${sessionId}::uuid AND order_id = ${orderId} AND order_item_id = ANY(${items})
            AND reported_at IS NULL`
      },
      settle: async (orderId, outcome) => {
        await sql`
          UPDATE ai_session_order_costs SET ${sql.unsafe(SETTLE[outcome])}
          WHERE session_id = ${sessionId}::uuid AND order_id = ${orderId} AND reported_at IS NULL`
      },
    }
  }
}

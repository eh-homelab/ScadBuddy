import type { Sql } from 'postgres'
import { liveTokenTiers } from '../../../src/auth/tokens.js'
import { CLAIM_LEASE_S, type EscalationDeps } from '../../../src/flows/escalations.js'
import type { EscalationEntry, EscalationRoutes, RunEntryRow, SessionCalls, WaitingRow } from '../../../src/flows/crudEscalations.js'
import { PgSessionReads } from './sessionReads.js'
import { PgSessionRows } from './sessionRows.js'
import { PostgresTokenStore } from './tokens.js'

// Tests only: a flow run's calls at the starting session's hop over the test database, the SQL
// the agent ran before the API took area `escalations` (#5299 step 12). src holds no SQL; the
// API's is backend agent_data/escalations.py.

/** One step of an entry's way up (`hops`, spec §4.6). */
function step(action: string, actor: string, reason?: string): Record<string, string> {
  return { hop: 'parent', action, actor, at: new Date().toISOString(), ...(reason ? { reason } : {}) }
}

export class SqlEscalations implements EscalationRoutes {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async entry(requestId: string): Promise<EscalationEntry | undefined> {
    const [row] = await this.#sql<EscalationEntry[]>`
      SELECT session_id::text, hop, coalesce(hop_until <= now(), false) AS due, coalesce(hop_until = 'infinity', false) AS claimed,
             parent_session::text, parent_principal
      FROM ai_pending_input WHERE request_id = ${requestId} AND flow_run_id IS NOT NULL`
    return row
  }

  async passUp(requestId: string, session: string, reason: string | undefined): Promise<boolean> {
    const moved = await this.#sql`
      UPDATE ai_pending_input
      SET hop = 'person', hop_until = NULL, hops = hops || ${this.#sql.json([step('pass_up', `session:${session}`, reason)])}
      WHERE request_id = ${requestId} AND hop = 'parent' AND parent_session = ${session}::uuid
        AND hop_until > now() AND hop_until <> 'infinity'
      RETURNING request_id`
    return moved.length > 0
  }

  async claimDeny(requestId: string, session: string): Promise<Date | undefined> {
    const [claim] = await this.#sql<{ hop_until: Date }[]>`
      UPDATE ai_pending_input p SET hop_until = 'infinity', hop_claimed_at = now()
      FROM (SELECT request_id, hop_until FROM ai_pending_input WHERE request_id = ${requestId} FOR UPDATE) old
      WHERE p.request_id = old.request_id AND p.hop = 'parent' AND p.parent_session = ${session}::uuid
        AND p.hop_until > now() AND p.hop_until <> 'infinity'
      RETURNING old.hop_until`
    return claim?.hop_until
  }

  async releaseDeny(requestId: string, session: string, hopUntil: Date): Promise<void> {
    await this.#sql`
      UPDATE ai_pending_input SET hop_until = LEAST(${hopUntil}::timestamptz, expires_at), hop_claimed_at = NULL
      WHERE request_id = ${requestId} AND hop = 'parent' AND parent_session = ${session}::uuid AND hop_until = 'infinity'`
  }

  async moveDue(): Promise<number> {
    const moved = await this.#sql`
      UPDATE ai_pending_input
      SET hop = 'person', hop_until = NULL, hop_claimed_at = NULL,
          hops = hops || ${this.#sql.json([step('timeout', 'system')])}
      WHERE hop = 'parent'
        AND (hop_until <= now()
             OR (hop_until = 'infinity' AND hop_claimed_at <= now() - make_interval(secs => ${CLAIM_LEASE_S})))
      RETURNING request_id`
    return moved.length
  }

  async waiting(graceS: number): Promise<WaitingRow[]> {
    return await this.#sql<WaitingRow[]>`
      SELECT request_id, flow_run_id, tool, summary, outside_reason, hop_until,
             created_at < now() - make_interval(secs => ${graceS}) AS past_grace,
             session_id::text, parent_session::text, parent_principal
      FROM (
        SELECT *, row_number() OVER (PARTITION BY parent_session ORDER BY created_at) AS nth
        FROM ai_pending_input
        -- A claimed entry (a deny in flight) is not asked about again (#3517).
        WHERE hop = 'parent' AND hop_until > now() AND hop_until <> 'infinity' AND delivered_at IS NULL
      ) waiting
      ORDER BY nth, created_at LIMIT 200`
  }

  /** Marks the calls sent, once across callers; returns those this call claimed. Rows another caller holds are skipped. */
  async claim(calls: SessionCalls): Promise<string[]> {
    return await this.#sql.begin(async (tx) => {
      const won: string[] = []
      if (calls.child.length) {
        for (const r of await tx<{ request_id: string }[]>`
          UPDATE ai_pending_input SET delivered_at = now()
          WHERE request_id IN (
            SELECT request_id FROM ai_pending_input
            WHERE request_id = ANY(${calls.child}) AND delivered_at IS NULL AND hop = 'parent'
            FOR UPDATE SKIP LOCKED)
          RETURNING request_id`) won.push(r.request_id)
      }
      for (const id of calls.own) {
        const [r] = await tx<{ request_id: string }[]>`
          INSERT INTO ai_escalation_deliveries (request_id, session_id) VALUES (${id}, ${calls.session}::uuid)
          ON CONFLICT (request_id) DO NOTHING RETURNING request_id`
        if (r) won.push(r.request_id)
      }
      return won
    })
  }

  async unclaim(calls: SessionCalls): Promise<void> {
    await this.#sql.begin(async (tx) => {
      if (calls.child.length) {
        await tx`
          UPDATE ai_pending_input SET delivered_at = NULL
          WHERE request_id = ANY(${calls.child}) AND parent_session = ${calls.session}::uuid AND hop = 'parent'`
      }
      if (calls.own.length) {
        await tx`DELETE FROM ai_escalation_deliveries WHERE request_id = ANY(${calls.own}) AND session_id = ${calls.session}::uuid`
      }
    })
  }

  async skip(calls: SessionCalls): Promise<void> {
    if (calls.child.length) {
      await this.#sql`
        UPDATE ai_pending_input
        SET hop = 'person', hop_until = NULL,
            hops = hops || ${this.#sql.json([step('pass_up', 'system', 'the starting session cannot be asked')])}
        WHERE request_id = ANY(${calls.child}) AND hop = 'parent' AND hop_until <> 'infinity'`
    }
    for (const id of calls.own) {
      await this.#sql`
        INSERT INTO ai_escalation_deliveries (request_id, session_id) VALUES (${id}, ${calls.session}::uuid)
        ON CONFLICT (request_id) DO NOTHING`
    }
  }

  async deliveries(after: string, limit: number): Promise<string[]> {
    const rows = await this.#sql<{ request_id: string }[]>`
      SELECT request_id FROM ai_escalation_deliveries WHERE request_id > ${after}
       ORDER BY request_id LIMIT ${limit}`
    return rows.map((r) => r.request_id)
  }

  async sent(ids: readonly string[]): Promise<string[]> {
    const rows = await this.#sql<{ request_id: string }[]>`
      SELECT request_id FROM ai_escalation_deliveries WHERE request_id = ANY(${[...ids]})`
    return rows.map((r) => r.request_id)
  }

  async forget(ids: readonly string[]): Promise<void> {
    await this.#sql`DELETE FROM ai_escalation_deliveries WHERE request_id = ANY(${[...ids]})`
  }

  async runEntries(flowRunId: string): Promise<RunEntryRow[]> {
    const rows = await this.#sql<(Omit<RunEntryRow, 'hop_until'> & { hop_until: Date | null })[]>`
      SELECT request_id, kind, session_id::text, tool, summary, input_hash, hop, hop_until, outside_reason, hops,
             created_at, expires_at, (expires_at <= now()) AS expiring
        FROM ai_pending_input WHERE flow_run_id = ${flowRunId} ORDER BY created_at, request_id`
    // `infinity` (a deny's claim) reads as an Invalid Date; the API answers it as `infinity`.
    return rows.map((r) => ({ ...r, hop_until: r.hop_until === null ? null : Number.isNaN(r.hop_until.getTime()) ? 'infinity' : r.hop_until.toISOString() }))
  }
}

/** Escalations' stores over the test database: its routes, the sessions' rows and reads, and the tokens' live tiers. */
export function sqlEscalationDeps(sql: Sql): Pick<EscalationDeps, 'routes' | 'sessions' | 'sessionReads' | 'currentTiers'> {
  return {
    routes: new SqlEscalations(sql),
    sessions: new PgSessionRows(sql),
    sessionReads: new PgSessionReads(sql),
    currentTiers: liveTokenTiers(new PostgresTokenStore(sql)),
  }
}

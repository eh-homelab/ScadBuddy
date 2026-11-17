import type { Sql } from 'postgres'
import type { LedgerReader } from '../db/schema.js'
import type { CrudAreas } from './crudAreas.js'

// What health and the start read of the agent's data (#6501), apart from main.ts so a test can
// check the wiring: /healthz's `database` and the ledger `ready()` waits for.

/** One probe of the agent's data: `ok`, or which part failed. */
export type DataCheck = { ok: boolean; apiUnanswered: boolean }

/**
 * `database` with a ping that is the Postgres ping and, once the API lists every area, the
 * API's answer to them too: the agent still runs SQL (the LISTEN bus, a turn's edits, the MCP
 * relay) until #5299 step 12. `check` answers the same with which part failed, per call (no
 * state shared between concurrent probes), which aiStatus names; each change in the API's
 * answer is logged.
 */
export function dataHealthOf<D extends { ping(timeoutMs?: number): Promise<boolean> }>(
  database: D,
  areas: Pick<CrudAreas, 'complete' | 'answers'>,
  log: (message: string) => void = (message) => console.warn(message),
): D & { check(timeoutMs?: number): Promise<DataCheck> } {
  // Only for the log: the API's last answer, so each change is logged once. A probe that does
  // not ask the API (Postgres down, an area not listed) leaves it, so after an outage of both
  // "answers again" is logged when the API next answers, not when the outage ends.
  let answered = true
  const check = async (timeoutMs?: number): Promise<DataCheck> => {
    // The database first: when it fails, the API is not asked and not named.
    if (!(await database.ping(timeoutMs))) return { ok: false, apiUnanswered: false }
    if (!areas.complete) return { ok: true, apiUnanswered: false }
    const now = await areas.answers(timeoutMs)
    if (now !== answered) log(now ? "health: the API answers the agent's CRUD areas again" : "health: the API does not answer the agent's CRUD areas")
    answered = now
    return { ok: now, apiUnanswered: !now }
  }
  return { ...database, check, ping: async (timeoutMs?: number) => (await check(timeoutMs)).ok }
}

/** `database`'s probe for a health read: `check` when it has one, its ping otherwise. */
export async function checkData(
  database: { ping(timeoutMs?: number): Promise<boolean>; check?: (timeoutMs?: number) => Promise<DataCheck> } | undefined,
): Promise<DataCheck | undefined> {
  if (!database) return undefined
  if (database.check) return database.check()
  return { ok: await database.ping(), apiUnanswered: false }
}

/** The ledger through the API once it lists area `migrations`, over SQL until then. */
export function ledgerOf(areas: Pick<CrudAreas, 'has'>, crud: LedgerReader, sqlLedger: (sql: Sql) => LedgerReader): (sql: Sql) => LedgerReader {
  return (sql) => (expected) => (areas.has('migrations') ? crud : sqlLedger(sql))(expected)
}
